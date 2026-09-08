#!/usr/bin/env bash
# The chain's deploy step, SOURCED by tick_chain.sbatch (it exports the
# operator's config knobs into the tick's environment): sweep stale git locks,
# move the checkout per the operator's update policy, sync deps, read the
# allowlisted .env knobs, install the non-claude backends. Every step is
# best-effort — a bad merge crashes the tick, never the chain. Shared by the
# per-cadence chain (once per job) and the resident loop (once per iteration).
#
# Expects: OUTERLOOP_HOME, OUTERLOOP_ROOT; optional OUTERLOOP_PAT_FILE.

# --- the operator's .env: ~/.config/outerloop (new) or ~/.config/autoresearch
# (pre-rename). We do NOT source it: sourcing would execute it and let it set
# ANY variable (the chain's own HOME/ROOT/PATH/cadence, arbitrary code).
# Instead single allowlisted keys are read from it, and only when the file is
# ours and not group/world-writable (a writable one could still inject a
# malicious VALUE, e.g. a bad codex binary path).
ENV_FILE="$HOME/.config/outerloop/.env"
[ -r "$ENV_FILE" ] || ENV_FILE="$HOME/.config/autoresearch/.env"
ENV_TRUSTED=""
if [ -r "$ENV_FILE" ]; then
    # GNU stat first, BSD stat second (a developer's Mac runs this too)
    perms=$(stat -c "%a" "$ENV_FILE" 2>/dev/null || stat -f "%Lp" "$ENV_FILE" 2>/dev/null || echo 777)
    owner=$(stat -c "%u" "$ENV_FILE" 2>/dev/null || stat -f "%u" "$ENV_FILE" 2>/dev/null || echo -1)
    if [ "$owner" = "$(id -u)" ] && [ $((8#$perms & 8#022)) -eq 0 ]; then
        ENV_TRUSTED=1
    else
        echo "deploy: refusing to read $ENV_FILE (owner=$owner perms=$perms;" \
             "needs to be yours and not group/world-writable)"
    fi
fi
# env_line: the last assignment of key $_k in the trusted .env, into $_line.
# Either spelling, the canonical key first: a pre-rename AUTORESEARCH_ twin
# counts only when the OUTERLOOP_ key is absent, whatever the order. Empty
# when the key is absent or the file is not trusted.
env_line() {
    _line=""
    [ -n "$ENV_TRUSTED" ] || return 0
    _line=$(grep -E "^${_k}=" "$ENV_FILE" 2>/dev/null | tail -1)
    [ -n "$_line" ] || _line=$(grep -E "^AUTORESEARCH_${_k#OUTERLOOP_}=" "$ENV_FILE" 2>/dev/null | tail -1)
}
# env_value: the value of $_line into $_v — the CR of a CRLF-edited file and
# one pair of surrounding quotes stripped
env_value() {
    _v=${_line#*=}
    _v=${_v%$'\r'}
    _v=${_v#[\"\']}; _v=${_v%[\"\']}
}

# --- 2. deploy: move the checkout per the update policy, sync deps ---
# OUTERLOOP_AUTO_UPDATE, the .env line first, then the environment:
#   off      (default) run the checkout as it is; the operator upgrades by hand
#   release  move to the newest release tag (vX.Y.Z; dev/rc pre-releases count)
#   main     follow every merge — for the kernel's own developers
_k=OUTERLOOP_AUTO_UPDATE; env_line
if [ -n "$_line" ]; then
    env_value; POLICY="${_v:-off}"
else
    POLICY="${OUTERLOOP_AUTO_UPDATE:-${AUTORESEARCH_AUTO_UPDATE:-off}}"
fi
case "$POLICY" in
    off|release|main) ;;
    *) echo "deploy: OUTERLOOP_AUTO_UPDATE=$POLICY is not one of off, release, main; not updating"
       POLICY=off ;;
esac
# newest_release: of the tags on stdin, the newest release in version order —
# at one X.Y.Z a final release beats rc beats beta beats alpha beats dev;
# anything not shaped like a release tag is ignored
newest_release() {
    sed -E -n \
        -e 's/^v([0-9]+)\.([0-9]+)\.([0-9]+)$/\1 \2 \3 4 0 &/p' \
        -e 's/^v([0-9]+)\.([0-9]+)\.([0-9]+)rc([0-9]+)$/\1 \2 \3 3 \4 &/p' \
        -e 's/^v([0-9]+)\.([0-9]+)\.([0-9]+)b([0-9]+)$/\1 \2 \3 2 \4 &/p' \
        -e 's/^v([0-9]+)\.([0-9]+)\.([0-9]+)a([0-9]+)$/\1 \2 \3 1 \4 &/p' \
        -e 's/^v([0-9]+)\.([0-9]+)\.([0-9]+)\.dev([0-9]+)$/\1 \2 \3 0 \4 &/p' \
        | sort -k1,1n -k2,2n -k3,3n -k4,4n -k5,5n | tail -1 | awk '{print $6}'
}
# A tick killed mid-fetch leaves .git/*.lock files that make every later
# deploy fail and the chain run stale code; sweep locks older than a few
# minutes (a live git op holds one for seconds) before touching the checkout.
bash "$OUTERLOOP_HOME/scripts/sweep_git_locks.sh" "$OUTERLOOP_HOME" 10 || true
# DEPLOY_PREV is the commit whose environment is installed: a successful sync
# records it beside the environment, so a checkout moved by hand (the `off`
# policy) still rolls back to it when its sync fails. Before the first record
# it is the checkout as found, which is right when this step is what moves it.
VENV="${UV_PROJECT_ENVIRONMENT:-$OUTERLOOP_HOME/.venv}"
record_synced() { [ -d "$VENV" ] && printf '%s\n' "$1" > "$VENV/.synced-head" 2>/dev/null || true; }
HEAD_BEFORE=$(git -C "$OUTERLOOP_HOME" rev-parse HEAD 2>/dev/null || echo "")
DEPLOY_PREV=$(cat "$VENV/.synced-head" 2>/dev/null || echo "")
[ -n "$DEPLOY_PREV" ] || DEPLOY_PREV="$HEAD_BEFORE"
if [ "$POLICY" != off ]; then
    # The repo is public, so the fetch needs no credential; with a bot PAT the
    # fetch authenticates, and the PAT never appears in argv (argv is
    # world-readable via /proc on shared nodes): git asks through GIT_ASKPASS.
    KERNEL="https://github.com/outerloop-science/outerloop.git"
    ASKPASS=""
    if [ -n "${OUTERLOOP_PAT_FILE:-}" ] && [ -r "$OUTERLOOP_PAT_FILE" ]; then
        ASKPASS=$(mktemp 2>/dev/null || echo "")
        if [ -n "$ASKPASS" ]; then
            chmod 700 "$ASKPASS"
            printf '#!/bin/sh\ncat "%s"\n' "$OUTERLOOP_PAT_FILE" > "$ASKPASS"
            KERNEL="https://x-access-token@github.com/outerloop-science/outerloop.git"
        fi
    fi
    kgit() {
        env ${ASKPASS:+GIT_ASKPASS="$ASKPASS"} GIT_TERMINAL_PROMPT=0 git -C "$OUTERLOOP_HOME" "$@"
    }
    if [ "$POLICY" = main ]; then
        if kgit fetch --quiet "$KERNEL" main; then
            git -C "$OUTERLOOP_HOME" reset --hard --quiet FETCH_HEAD || echo "deploy: reset failed"
        else
            echo "deploy: fetch failed; running previous code"
        fi
    # the newest release among the tags the public repo has NOW; a stale or
    # local v-tag in this checkout is never chosen, and the chosen tag is
    # force-updated to the public one
    elif TAGS=$(kgit ls-remote --tags --refs "$KERNEL" 'v*'); then
        TAG=$(printf '%s\n' "$TAGS" | sed -n 's|.*refs/tags/||p' | newest_release)
        if [ -z "$TAG" ]; then
            echo "deploy: no release tag found; running previous code"
        elif ! kgit fetch --quiet "$KERNEL" "+refs/tags/$TAG:refs/tags/$TAG"; then
            echo "deploy: fetch failed; running previous code"
        elif [ "$(git -C "$OUTERLOOP_HOME" rev-parse "refs/tags/$TAG^{commit}" 2>/dev/null)" != "$HEAD_BEFORE" ]; then
            if git -C "$OUTERLOOP_HOME" reset --hard --quiet "refs/tags/$TAG"; then
                echo "deploy: at release $TAG"
            else
                echo "deploy: reset to $TAG failed"
            fi
        fi
    else
        echo "deploy: tag lookup failed; running previous code"
    fi
    [ -n "$ASKPASS" ] && rm -f "$ASKPASS"
fi
# Host-side caches must never land in $HOME: home quotas are tiny on many
# clusters and invisible until EDQUOT (verified on Torch — a full home took
# down a live run). Default them under the state root (scratch-class
# storage); explicit env wins. Submitted jobs inherit these via sbatch.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$OUTERLOOP_ROOT/cache/uv}"
export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$OUTERLOOP_ROOT/cache/apptainer}"
mkdir -p "$UV_CACHE_DIR" "$APPTAINER_CACHEDIR" || true

# Code and environment move together or not at all. The tick runs with
# `uv run --no-sync` afterwards, so when the sync fails (quota, network) the
# checkout goes BACK to the commit whose environment is installed: new code
# never runs against an old venv (a merge that adds a dependency would fail
# at import), and the tick keeps running on the previous, consistent pair.
# OUTERLOOP_DEPLOY_BROKEN=1 tells the caller NOT to run the tick this
# iteration: the checkout and the installed environment do not match and
# could not be made to (the rollback itself failed). Cleared on every deploy.
OUTERLOOP_DEPLOY_BROKEN=""
if (cd "$OUTERLOOP_HOME" && uv sync --locked --quiet); then
    record_synced "$(git -C "$OUTERLOOP_HOME" rev-parse HEAD 2>/dev/null || echo "")"
else
    NEW_HEAD=$(git -C "$OUTERLOOP_HOME" rev-parse HEAD 2>/dev/null || echo "")
    if [ -z "${DEPLOY_PREV:-}" ] || [ "$NEW_HEAD" = "$DEPLOY_PREV" ]; then
        # the checkout is at the commit whose environment is installed (or
        # neither is known): nothing to go back to, keep running it
        echo "deploy: uv sync failed; environment unchanged"
    elif [ -n "$NEW_HEAD" ] \
        && [ "$(git -C "$OUTERLOOP_HOME" rev-parse "$DEPLOY_PREV":uv.lock 2>/dev/null)" \
           = "$(git -C "$OUTERLOOP_HOME" rev-parse HEAD:uv.lock 2>/dev/null)" ]; then
        # the lockfile did not change between the old and new commit, so the
        # installed environment already satisfies the new code: run it. This
        # is the common case under quota exhaustion — an ordinary merge does
        # not touch uv.lock — and it keeps the tick alive (2026-09-03: the
        # scratch quota filled and every tick died here for two hours).
        echo "deploy: uv sync failed but uv.lock is unchanged; running new code on the current environment"
        record_synced "$NEW_HEAD"
    else
        # dependencies changed and could not be installed; the partial sync
        # may have altered the environment, so go back to the previous commit
        # and reinstall ITS environment. If that also fails (the quota is
        # still full), the pair cannot be made consistent — skip the tick
        # until a deploy succeeds rather than run on a half-synced venv.
        if git -C "$OUTERLOOP_HOME" reset --hard --quiet "$DEPLOY_PREV" \
           && (cd "$OUTERLOOP_HOME" && uv sync --locked --quiet); then
            echo "deploy: uv sync failed; back on $DEPLOY_PREV with its environment"
            record_synced "$DEPLOY_PREV"
        else
            echo "deploy: uv sync failed and the environment could not be made consistent; tick skipped until a deploy succeeds"
            OUTERLOOP_DEPLOY_BROKEN=1
        fi
    fi
fi
export OUTERLOOP_DEPLOY_BROKEN

# --- config knobs: the config-driven AUTHOR knobs from the operator .env, so
# live config changes need no chain restart. These are where
# OUTERLOOP_AUTHOR_BACKEND/_MODEL and the per-backend key files live; the
# config-driven climb/followup default from them and the tick preflights them.
# Only this ALLOWLIST is read, so .env is structurally per-tick author config
# and can never hijack the chain's identity or scheduling.
if [ -n "$ENV_TRUSTED" ]; then
    for _k in OUTERLOOP_AUTHOR_BACKEND OUTERLOOP_AUTHOR_MODEL \
                  OUTERLOOP_CLAUDE_BIN OUTERLOOP_CODEX_BIN OUTERLOOP_CODEX_KEY_FILE \
                  OUTERLOOP_CLAUDE_KEY_FILE OUTERLOOP_HARNESS_KEY_FILE \
                  OUTERLOOP_VERTEX_PROJECT OUTERLOOP_VERTEX_REGION \
                  OUTERLOOP_VERTEX_ADC \
                  OUTERLOOP_TARGET \
                  OUTERLOOP_GITHUB_APP_FILE OUTERLOOP_BOT_LOGIN OUTERLOOP_BOT_ALIASES \
                  OUTERLOOP_GPU_PARTITION OUTERLOOP_GPU_ACCOUNT \
                  OUTERLOOP_IMAGE \
                  OUTERLOOP_PANEL OUTERLOOP_PANEL_KEY_FILE \
                  OUTERLOOP_PANEL_CODEX_KEY_FILE \
                  OUTERLOOP_PANEL_HERMES_KEY_FILE \
                  REVIEW_HERMES_REPO REVIEW_HERMES_PROVIDER; do
        env_line
        # PRESENCE-based, not value-based: a key set to "" in .env is a
        # live OFF-SWITCH (OUTERLOOP_PANEL="" disables the panel,
        # VERTEX_PROJECT="" reverts to API-key billing) and must override
        # an inherited chain value; an ABSENT key changes nothing.
        if [ -n "$_line" ]; then
            env_value
            export "$_k=$_v"
        fi
    done
fi

# The codex author binary is a host prerequisite; install it (idempotent, fast
# path is a local version check) when ANY codex role is deployed — the fleet
# author, or a codex panel lens (a claude-author/codex-panel rollout still
# bind-mounts the binary into every judge container). Best-effort: a failure
# here must never break the chain — the climb will report a missing codex
# clearly if it comes to that.
case "${OUTERLOOP_AUTHOR_BACKEND:-}:${OUTERLOOP_PANEL:-}" in
    codex:*|*:*codex*)
        bash "$OUTERLOOP_HOME/scripts/install_codex.sh" || echo "deploy: codex install failed"
        ;;
esac
case "${OUTERLOOP_PANEL:-}" in
    *hermes*)
        # the default install location IS the default config: exporting it
        # here connects the provisioned clone to the preflight/climb without
        # requiring the operator to name a path they didn't choose
        export REVIEW_HERMES_REPO="${REVIEW_HERMES_REPO:-$HOME/hermes-agent}"
        bash "$OUTERLOOP_HOME/scripts/install_hermes.sh" "$REVIEW_HERMES_REPO" \
            || echo "deploy: hermes install failed"
        ;;
esac
