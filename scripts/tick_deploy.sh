#!/usr/bin/env bash
# The chain's deploy step, SOURCED by tick_chain.sbatch (it exports the
# operator's config knobs into the tick's environment): sweep stale git locks,
# pull main with the bot PAT, sync deps, read the allowlisted .env knobs,
# install the non-claude backends. Every step is best-effort — a bad merge
# crashes the tick, never the chain. Shared by the per-cadence chain (once per
# job) and the resident loop (once per iteration).
#
# Expects: AUTORESEARCH_HOME, AUTORESEARCH_ROOT; optional AUTORESEARCH_PAT_FILE.

# --- 2. deploy: pull main with the bot PAT, sync deps (best-effort) ---
# A tick killed mid-fetch leaves .git/*.lock files that make every later
# deploy fail and the chain run stale code; sweep locks older than a few
# minutes (a live git op holds one for seconds) before touching the checkout.
bash "$AUTORESEARCH_HOME/scripts/sweep_git_locks.sh" "$AUTORESEARCH_HOME" 10 || true
# The PAT never appears in argv (argv is world-readable via /proc on shared
# nodes): git asks for it through GIT_ASKPASS instead.
if [ -n "${AUTORESEARCH_PAT_FILE:-}" ] && [ -r "$AUTORESEARCH_PAT_FILE" ]; then
    ASKPASS=$(mktemp 2>/dev/null || echo "") && [ -n "$ASKPASS" ] && chmod 700 "$ASKPASS"
    printf '#!/bin/sh\ncat "%s"\n' "$AUTORESEARCH_PAT_FILE" > "$ASKPASS"
    DEPLOY_PREV=$(git -C "$AUTORESEARCH_HOME" rev-parse HEAD 2>/dev/null || echo "")
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

# Code and environment move together or not at all. The tick runs with
# `uv run --no-sync` afterwards, so when the sync fails (quota, network) the
# checkout goes BACK to the commit whose environment is installed: new code
# never runs against an old venv (a merge that adds a dependency would fail
# at import), and the tick keeps running on the previous, consistent pair.
# AUTORESEARCH_DEPLOY_BROKEN=1 tells the caller NOT to run the tick this
# iteration: the checkout and the installed environment do not match and
# could not be made to (the rollback itself failed). Cleared on every deploy.
AUTORESEARCH_DEPLOY_BROKEN=""
if ! (cd "$AUTORESEARCH_HOME" && uv sync --locked --quiet); then
    NEW_HEAD=$(git -C "$AUTORESEARCH_HOME" rev-parse HEAD 2>/dev/null || echo "")
    if [ -z "${DEPLOY_PREV:-}" ] || [ "$NEW_HEAD" = "$DEPLOY_PREV" ]; then
        # nothing was fetched: the environment is whatever the last good
        # deploy installed, and the checkout still matches it
        echo "deploy: uv sync failed; environment unchanged"
    elif [ -n "$NEW_HEAD" ] \
        && [ "$(git -C "$AUTORESEARCH_HOME" rev-parse "$DEPLOY_PREV":uv.lock 2>/dev/null)" \
           = "$(git -C "$AUTORESEARCH_HOME" rev-parse HEAD:uv.lock 2>/dev/null)" ]; then
        # the lockfile did not change between the old and new commit, so the
        # installed environment already satisfies the new code: run it. This
        # is the common case under quota exhaustion — an ordinary merge does
        # not touch uv.lock — and it keeps the tick alive (2026-09-03: the
        # scratch quota filled and every tick died here for two hours).
        echo "deploy: uv sync failed but uv.lock is unchanged; running new code on the current environment"
    else
        # dependencies changed and could not be installed; the partial sync
        # may have altered the environment, so go back to the previous commit
        # and reinstall ITS environment. If that also fails (the quota is
        # still full), the pair cannot be made consistent — skip the tick
        # until a deploy succeeds rather than run on a half-synced venv.
        if git -C "$AUTORESEARCH_HOME" reset --hard --quiet "$DEPLOY_PREV" \
           && (cd "$AUTORESEARCH_HOME" && uv sync --locked --quiet); then
            echo "deploy: uv sync failed; back on $DEPLOY_PREV with its environment"
        else
            echo "deploy: uv sync failed and the environment could not be made consistent; tick skipped until a deploy succeeds"
            AUTORESEARCH_DEPLOY_BROKEN=1
        fi
    fi
fi
export AUTORESEARCH_DEPLOY_BROKEN

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
                  AUTORESEARCH_GITHUB_APP_FILE AUTORESEARCH_BOT_LOGIN AUTORESEARCH_BOT_ALIASES \
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
