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
#   AUTORESEARCH_RESIDENT=1   run as the RESIDENT tick (design/resident-tick.md):
#                             one long-lived job that loops deploy -> tick ->
#                             sleep, with a single afterany:self successor.
#                             `outerloop start` submits it (walltime,
#                             name, and exports filled in); by hand:
#                             `sbatch --time=360 --job-name=autoresearch-resident
#                             --export=ALL,AUTORESEARCH_RESIDENT=1,...`. The
#                             per-cadence chain drains itself once a resident
#                             job exists. Unset = the per-cadence chain below.
# Further config also loads from ~/.config/outerloop/.env (0600) each tick, so
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
# Accept the new OUTERLOOP_* names at the boundary: copy each into its
# AUTORESEARCH_* twin (when the twin is unset) so the chain's internals, which
# still read AUTORESEARCH_* during the rename, see one name. The Python side does
# the same on import. Removed with the final internal flip.
for _ov in $(env | sed -n 's/^OUTERLOOP_\([A-Z_][A-Z0-9_]*\)=.*/\1/p'); do
    eval "[ -n \"\${AUTORESEARCH_${_ov}:-}\" ] || export AUTORESEARCH_${_ov}=\"\${OUTERLOOP_${_ov}}\""
done
CADENCE_MIN="${AUTORESEARCH_CADENCE_MIN:-30}"
JOB_NAME="autoresearch-tick"
RESIDENT_JOB_NAME="autoresearch-resident"
missing=""
for var in AUTORESEARCH_HOME AUTORESEARCH_ROOT; do
    eval "val=\${$var:-}"
    [ -z "$val" ] && missing="$missing $var"
done
# Account and partition are optional: unset, Slurm bills the default
# association and places the job on its default partition. When set (a
# partition may be a comma-list like "a,b"), pass them through.
acct_arg=""
[ -n "${AUTORESEARCH_ACCOUNT:-}" ] && acct_arg="--account=${AUTORESEARCH_ACCOUNT}"
part_arg=""
[ -n "${AUTORESEARCH_PARTITION:-}" ] && part_arg="--partition=${AUTORESEARCH_PARTITION}"

# --- 0. the resident tick: hand the whole job to the loop ---
# A resident job has no queued successor to protect yet, so a misconfigured
# start fails LOUDLY here (the per-cadence chain below checks after its
# top-up on purpose: its successors are already queued and must stay so).
if [ "${AUTORESEARCH_RESIDENT:-}" = "1" ]; then
    if [ -n "$missing" ]; then
        echo "resident tick misconfigured; missing:$missing" >&2
        exit 1
    fi
    . "${AUTORESEARCH_HOME:-.}/scripts/tick_resident.sh"
    exit $?
fi

# --- 1. keep two successors queued (singleton serializes same-name jobs) ---
# A resident tick (design/resident-tick.md) supersedes this chain: while one
# is queued or running, stop topping up so the per-cadence chain drains.
if squeue -u "$USER" --name="$RESIDENT_JOB_NAME" -h 2>/dev/null | grep -q '[0-9]'; then
    echo "chain: a resident tick exists; not queuing successors (draining)"
    need=0
    pending=0
else
# A successor the SITE moved off our partition starves there and blocks its
# twin through singleton; cancel it first so the top-up below requeues it.
bash "${AUTORESEARCH_HOME:-.}/scripts/requeue_moved_successors.sh" "$JOB_NAME" "${AUTORESEARCH_PARTITION:-}" || true
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
fi
epoch_now=$(date +%s)
cadence_s=$((CADENCE_MIN * 60))
next_slot=$(( (epoch_now / cadence_s + 1) * cadence_s ))
# a while loop, not `seq 1 $need`: BSD seq counts DOWN for `seq 1 0`
i=1
while [ "$i" -le "$need" ]; do
    begin_epoch=$((next_slot + (pending + i - 1) * cadence_s))
    begin=$(date -d "@$begin_epoch" +%Y-%m-%dT%H:%M:%S 2>/dev/null \
            || date -r "$begin_epoch" +%Y-%m-%dT%H:%M:%S)
    for attempt in 1 2 3; do
        if sbatch --dependency=singleton --begin="$begin" \
                  ${acct_arg:+"$acct_arg"} ${part_arg:+"$part_arg"} \
                  "${AUTORESEARCH_HOME:-}/scripts/tick_chain.sbatch"; then
            break
        fi
        echo "sbatch retry $attempt failed; backing off"
        sleep $((attempt * 20))
    done
    i=$((i + 1))
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

# --- 2. deploy: pull main, sync deps, read the operator's knobs (best-effort;
#        scripts/tick_deploy.sh, sourced so its exports reach the tick) ---
. "$AUTORESEARCH_HOME/scripts/tick_deploy.sh"

# --- 3. the tick itself (scrubbed of the PAT path; sessions are scrubbed
#        further down in the harness) ---
cd "$AUTORESEARCH_HOME"
# The tick keeps AUTORESEARCH_PAT_FILE: it reads GitHub (PR states, review
# comments) and submits follow-up jobs. Agent sessions remain scrubbed by the
# harness env allowlist regardless.
# --no-sync: the deploy step above already synced (or logged its failure);
# a bare `uv run` would sync AGAIN and, when that fails — a full quota,
# 2026-09-03: every tick from 13:30Z died here for two hours — the tick never
# starts. Running the venv as it stands is the "continue on the previous
# code" the deploy promises.
if [ "${AUTORESEARCH_DEPLOY_BROKEN:-}" = "1" ]; then
    echo "tick skipped: checkout and environment inconsistent after a failed deploy"
    exit 0  # successors are already queued; the next deploy retries
fi
exec uv run --no-sync python -m outerloop.tick --root "$AUTORESEARCH_ROOT"
