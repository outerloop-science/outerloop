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
#
#SBATCH --job-name=autoresearch-tick
#SBATCH --time=15
#SBATCH --mem=4G
#SBATCH --cpus-per-task=2
#SBATCH --output=/dev/null

set -u
CADENCE_MIN="${AUTORESEARCH_CADENCE_MIN:-30}"
JOB_NAME="autoresearch-tick"
LOG_DIR="$AUTORESEARCH_ROOT/logs"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/tick-$(date +%Y%m%d).log" 2>&1
echo "=== tick $(date -Is) on $(hostname -s) job=${SLURM_JOB_ID:-none}"

# --- 1. keep two successors queued (singleton serializes same-name jobs) ---
pending=$(squeue -u "$USER" --name="$JOB_NAME" -h -t PENDING 2>/dev/null | wc -l)
need=$((2 - pending))
epoch_now=$(date +%s)
cadence_s=$((CADENCE_MIN * 60))
next_slot=$(( (epoch_now / cadence_s + 1) * cadence_s ))
for i in $(seq 1 "$need"); do
    begin_epoch=$((next_slot + (i - 1) * cadence_s))
    begin=$(date -d "@$begin_epoch" +%Y-%m-%dT%H:%M:%S 2>/dev/null \
            || date -r "$begin_epoch" +%Y-%m-%dT%H:%M:%S)
    for attempt in 1 2 3; do
        if sbatch --dependency=singleton --begin="$begin" \
                  --account="$AUTORESEARCH_ACCOUNT" --partition="$AUTORESEARCH_PARTITION" \
                  "$AUTORESEARCH_HOME/scripts/tick_chain.sbatch"; then
            break
        fi
        echo "sbatch retry $attempt failed; backing off"
        sleep $((attempt * 20))
    done
done

# --- 2. deploy: pull main with the bot PAT, sync deps (best-effort) ---
if [ -n "${AUTORESEARCH_PAT_FILE:-}" ] && [ -r "$AUTORESEARCH_PAT_FILE" ]; then
    PAT=$(cat "$AUTORESEARCH_PAT_FILE")
    if git -C "$AUTORESEARCH_HOME" fetch --quiet \
        "https://x-access-token:${PAT}@github.com/agentic-learning-ai-lab/autoresearch.git" main; then
        git -C "$AUTORESEARCH_HOME" reset --hard --quiet FETCH_HEAD || echo "deploy: reset failed"
    else
        echo "deploy: fetch failed; running previous code"
    fi
    unset PAT
fi
(cd "$AUTORESEARCH_HOME" && uv sync --locked --quiet) || echo "deploy: uv sync failed"

# --- 3. the tick itself (scrubbed of the PAT path; sessions are scrubbed
#        further down in the harness) ---
cd "$AUTORESEARCH_HOME"
exec env -u AUTORESEARCH_PAT_FILE uv run python -m autoresearch.tick --root "$AUTORESEARCH_ROOT"
