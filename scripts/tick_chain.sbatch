#!/bin/bash
# The self-resubmitting tick chain (docs/design/architecture.md, Scheduling).
#
# Order matters and is the fail-safety:
#   1. top up successors FIRST — a crash anywhere below never breaks the chain
#   2. pull main + sync (deploy-at-tick-start; failures logged, tick continues
#      on the previous code — a bad merge crashes ticks, never the chain)
#   3. exec the tick
#
# Deploy/config knobs come from the environment (set once via
# `sbatch --export=...` when starting the chain; successors inherit):
#   AUTORESEARCH_HOME  checkout to run from        (required)
#   AUTORESEARCH_ROOT  state root on the shared FS (required)
#   AUTORESEARCH_ACCOUNT / AUTORESEARCH_PARTITION  slurm placement (required)
#   AUTORESEARCH_CADENCE_MIN  tick cadence, default 30
#   AUTORESEARCH_PAT_FILE     bot PAT for the deploy pull (optional; no file
#                             means run whatever code is already deployed)
# Further config also loads from ~/.config/autoresearch/.env (0600) each tick, so
# a live config change (e.g. AUTORESEARCH_AUTHOR_BACKEND=codex + its key file)
# needs no chain restart — just an edit to that file.
#
#SBATCH --job-name=autoresearch-tick
#SBATCH --time=15
#SBATCH --mem=4G
#SBATCH --cpus-per-task=2
#SBATCH --output=/dev/null

# NO set -u before the top-up: a config mistake must fail loudly AFTER the
# chain has secured its successors, not break the chain silently. Required
# vars are checked by hand instead.
CADENCE_MIN="${AUTORESEARCH_CADENCE_MIN:-30}"
JOB_NAME="autoresearch-tick"
missing=""
for var in AUTORESEARCH_HOME AUTORESEARCH_ROOT AUTORESEARCH_ACCOUNT AUTORESEARCH_PARTITION; do
    eval "val=\${$var:-}"
    [ -z "$val" ] && missing="$missing $var"
done

# --- 1. keep two successors queued (singleton serializes same-name jobs) ---
# Successors land on slots AFTER the ones already-queued jobs occupy —
# otherwise every top-up doubles the tick rate on the next slot.
if squeue_out=$(squeue -u "$USER" --name="$JOB_NAME" -h -t PENDING 2>/dev/null); then
    pending=$(printf '%s' "$squeue_out" | grep -c '[0-9]')
    need=$((2 - pending))
else
    # A failed squeue must not read as "zero pending" — that would stack
    # extra successors onto occupied slots every outage. Skip the top-up;
    # the (very likely still-queued) successors keep the chain alive and
    # the next tick retries.
    pending=0
    need=0
fi
epoch_now=$(date +%s)
cadence_s=$((CADENCE_MIN * 60))
next_slot=$(( (epoch_now / cadence_s + 1) * cadence_s ))
for i in $(seq 1 "$need"); do
    begin_epoch=$((next_slot + (pending + i - 1) * cadence_s))
    begin=$(date -d "@$begin_epoch" +%Y-%m-%dT%H:%M:%S 2>/dev/null \
            || date -r "$begin_epoch" +%Y-%m-%dT%H:%M:%S)
    for attempt in 1 2 3; do
        if sbatch --dependency=singleton --begin="$begin" \
                  --account="${AUTORESEARCH_ACCOUNT:-}" --partition="${AUTORESEARCH_PARTITION:-}" \
                  "${AUTORESEARCH_HOME:-}/scripts/tick_chain.sbatch"; then
            break
        fi
        echo "sbatch retry $attempt failed; backing off"
        sleep $((attempt * 20))
    done
done

# Only NOW may configuration problems kill this tick — successors are queued.
set -u
if [ -n "$missing" ]; then
    echo "tick misconfigured; missing:$missing" >&2
    exit 1
fi
LOG_DIR="$AUTORESEARCH_ROOT/logs"
mkdir -p "$LOG_DIR" || true
if [ -w "$LOG_DIR" ]; then
    exec >>"$LOG_DIR/tick-$(date +%Y%m%d).log" 2>&1
fi
echo "=== tick $(date -Is) on $(hostname -s) job=${SLURM_JOB_ID:-none}"

# --- 2. deploy: pull main with the bot PAT, sync deps (best-effort) ---
# The PAT never appears in argv (argv is world-readable via /proc on shared
# nodes): git asks for it through GIT_ASKPASS instead.
if [ -n "${AUTORESEARCH_PAT_FILE:-}" ] && [ -r "$AUTORESEARCH_PAT_FILE" ]; then
    ASKPASS=$(mktemp 2>/dev/null || echo "") && [ -n "$ASKPASS" ] && chmod 700 "$ASKPASS"
    printf '#!/bin/sh\ncat "%s"\n' "$AUTORESEARCH_PAT_FILE" > "$ASKPASS"
    if GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 git -C "$AUTORESEARCH_HOME" fetch --quiet \
        "https://x-access-token@github.com/agentic-learning-ai-lab/autoresearch.git" main; then
        git -C "$AUTORESEARCH_HOME" reset --hard --quiet FETCH_HEAD || echo "deploy: reset failed"
    else
        echo "deploy: fetch failed; running previous code"
    fi
    rm -f "$ASKPASS"
fi
# Host-side caches must never land in $HOME: home quotas are tiny on many
# clusters and invisible until EDQUOT (verified on Torch — a full home took
# down a live run). Default them under the state root (scratch-class
# storage); explicit env wins. Submitted jobs inherit these via sbatch.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$AUTORESEARCH_ROOT/cache/uv}"
export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$AUTORESEARCH_ROOT/cache/apptainer}"
mkdir -p "$UV_CACHE_DIR" "$APPTAINER_CACHEDIR" || true

(cd "$AUTORESEARCH_HOME" && uv sync --locked --quiet) || echo "deploy: uv sync failed"

# --- config knobs: read the config-driven AUTHOR knobs from the operator .env so
# live config changes need no chain restart. These are where
# AUTORESEARCH_AUTHOR_BACKEND/_MODEL and the per-backend key files live; the
# config-driven climb/followup default from them and the tick preflights them.
#
# We do NOT source .env: sourcing would execute it and let it set ANY variable
# (the chain's own HOME/ROOT/PATH/cadence, arbitrary code). Instead we extract
# only an ALLOWLIST of author knobs — so .env is structurally per-tick author
# config and can never hijack the chain's identity or scheduling. And only from a
# file that is ours and not group/world-writable (a writable one could still
# inject a malicious VALUE, e.g. a bad codex binary path).
ENV_FILE="$HOME/.config/autoresearch/.env"
if [ -r "$ENV_FILE" ]; then
    perms=$(stat -c "%a" "$ENV_FILE" 2>/dev/null || echo 777)
    owner=$(stat -c "%u" "$ENV_FILE" 2>/dev/null || echo -1)
    if [ "$owner" = "$(id -u)" ] && [ $((8#$perms & 8#022)) -eq 0 ]; then
        for _k in AUTORESEARCH_AUTHOR_BACKEND AUTORESEARCH_AUTHOR_MODEL \
                  AUTORESEARCH_CODEX_BIN AUTORESEARCH_CODEX_KEY_FILE \
                  AUTORESEARCH_HARNESS_KEY_FILE \
                  AUTORESEARCH_VERTEX_PROJECT AUTORESEARCH_VERTEX_REGION \
                  AUTORESEARCH_VERTEX_ADC \
                  AUTORESEARCH_TARGET \
                  AUTORESEARCH_GITHUB_APP_FILE AUTORESEARCH_BOT_LOGIN \
                  AUTORESEARCH_GPU_PARTITION AUTORESEARCH_GPU_ACCOUNT \
                  AUTORESEARCH_PANEL AUTORESEARCH_PANEL_KEY_FILE \
                  AUTORESEARCH_PANEL_CODEX_KEY_FILE \
                  AUTORESEARCH_PANEL_HERMES_KEY_FILE \
                  REVIEW_HERMES_REPO REVIEW_HERMES_PROVIDER; do
            _line=$(grep -E "^${_k}=" "$ENV_FILE" 2>/dev/null | tail -1)
            # PRESENCE-based, not value-based: a key set to "" in .env is a
            # live OFF-SWITCH (AUTORESEARCH_PANEL="" disables the panel,
            # VERTEX_PROJECT="" reverts to API-key billing) and must override
            # an inherited chain value; an ABSENT key changes nothing.
            if [ -n "$_line" ]; then
                _v=${_line#*=}
                _v=${_v%$'\r'}                     # CRLF-edited file
                _v=${_v#[\"\']}; _v=${_v%[\"\']}   # optional surrounding quote
                export "$_k=$_v"
            fi
        done
    else
        echo "deploy: refusing to read $ENV_FILE (owner=$owner perms=$perms;" \
             "needs to be yours and not group/world-writable)"
    fi
fi

# The codex author binary is a host prerequisite; install it (idempotent, fast
# path is a local version check) when ANY codex role is deployed — the fleet
# author, or a codex panel lens (a claude-author/codex-panel rollout still
# bind-mounts the binary into every judge container). Best-effort: a failure
# here must never break the chain — the climb will report a missing codex
# clearly if it comes to that.
case "${AUTORESEARCH_AUTHOR_BACKEND:-}:${AUTORESEARCH_PANEL:-}" in
    codex:*|*:*codex*)
        bash "$AUTORESEARCH_HOME/scripts/install_codex.sh" || echo "deploy: codex install failed"
        ;;
esac
case "${AUTORESEARCH_PANEL:-}" in
    *hermes*)
        # the default install location IS the default config: exporting it
        # here connects the provisioned clone to the preflight/climb without
        # requiring the operator to name a path they didn't choose
        export REVIEW_HERMES_REPO="${REVIEW_HERMES_REPO:-$HOME/hermes-agent}"
        bash "$AUTORESEARCH_HOME/scripts/install_hermes.sh" "$REVIEW_HERMES_REPO" \
            || echo "deploy: hermes install failed"
        ;;
esac

# --- 3. the tick itself (scrubbed of the PAT path; sessions are scrubbed
#        further down in the harness) ---
cd "$AUTORESEARCH_HOME"
# The tick keeps AUTORESEARCH_PAT_FILE: it reads GitHub (PR states, review
# comments) and submits follow-up jobs. Agent sessions remain scrubbed by the
# harness env allowlist regardless.
exec uv run python -m autoresearch.tick --root "$AUTORESEARCH_ROOT"
