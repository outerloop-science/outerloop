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

# --- config knobs: source the operator .env (0600) so live config changes need
# no chain restart. This is where AUTORESEARCH_AUTHOR_BACKEND/_MODEL and the
# per-backend key files live; the config-driven climb/followup default from them
# and the tick preflights them. The submitted work jobs inherit this env.
ENV_FILE="$HOME/.config/autoresearch/.env"
if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE" || echo "deploy: .env source failed"
    set +a
fi

# The codex author binary is a host prerequisite; install it (idempotent, fast
# path is a local version check) only when the fleet author is codex. Best-effort:
# a failure here must never break the chain — the climb will report a missing
# codex clearly if it comes to that.
if [ "${AUTORESEARCH_AUTHOR_BACKEND:-}" = "codex" ]; then
    bash "$AUTORESEARCH_HOME/scripts/install_codex.sh" || echo "deploy: codex install failed"
fi

# --- 3. the tick itself (scrubbed of the PAT path; sessions are scrubbed
#        further down in the harness) ---
cd "$AUTORESEARCH_HOME"
# The tick keeps AUTORESEARCH_PAT_FILE: it reads GitHub (PR states, review
# comments) and submits follow-up jobs. Agent sessions remain scrubbed by the
# harness env allowlist regardless.
exec uv run python -m autoresearch.tick --root "$AUTORESEARCH_ROOT"
