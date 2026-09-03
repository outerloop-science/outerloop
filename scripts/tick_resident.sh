#!/usr/bin/env bash
# The resident tick (docs/design/resident-tick.md), SOURCED by tick_chain.sbatch
# when AUTORESEARCH_RESIDENT=1: one long-lived job that loops
#   deploy -> one tick as a child under a hard timeout -> sleep to the next slot
# and keeps exactly ONE successor queued, dependent on its own end
# (afterany:self + singleton), so the chain needs a handful of scheduling
# events a day instead of one per cadence. The pause sentinel cancels the
# successor and exits without resubmitting; a deploy that changed the shim
# resubmits the successor so handover runs the current script (Slurm spools
# batch scripts at submission). The tick itself is unchanged: records,
# leases, markers and the coalescing guard make a late or repeated tick safe.
#
# Knobs (chain environment): AUTORESEARCH_RESIDENT_MINUTES (walltime the job
# was started with; default 360 = cpu_short's maximum), AUTORESEARCH_CADENCE_MIN,
# AUTORESEARCH_TICK_TIMEOUT (default 15m), AUTORESEARCH_RESIDENT_MARGIN_S
# (stop this many seconds before walltime; default 1200),
# AUTORESEARCH_RESIDENT_CADENCE_S (tests: override the cadence in seconds).

resident_minutes="${AUTORESEARCH_RESIDENT_MINUTES:-360}"
cadence_s="${AUTORESEARCH_RESIDENT_CADENCE_S:-$((${AUTORESEARCH_CADENCE_MIN:-30} * 60))}"
tick_timeout="${AUTORESEARCH_TICK_TIMEOUT:-15m}"
margin_s="${AUTORESEARCH_RESIDENT_MARGIN_S:-1200}"
self="${SLURM_JOB_ID:-}"
shim="$AUTORESEARCH_HOME/scripts/tick_chain.sbatch"
sentinel="$AUTORESEARCH_ROOT/PAUSE"

epoch_of() { date -d "$1" +%s 2>/dev/null || date -j -f %Y-%m-%dT%H:%M:%S "$1" +%s 2>/dev/null || echo ""; }

# the job's real end (Slurm's EndTime), else start + the configured minutes
started=$(date +%s)
end_epoch=$((started + resident_minutes * 60))
if [ -n "$self" ]; then
    et=$(scontrol show job "$self" -o 2>/dev/null | tr ' ' '\n' | sed -n 's/^EndTime=//p' | head -1)
    case "$et" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]) e=$(epoch_of "$et"); [ -n "$e" ] && end_epoch=$e ;; esac
fi

submit_successor() {
    # one successor, ineligible until this job ends: afterany:self keeps it
    # off the scheduler's eligible list (never a candidate to starve), and
    # singleton keeps two residents from ever running together
    local dep="singleton"
    [ -n "$self" ] && dep="afterany:${self},singleton"
    local out="" attempt
    for attempt in 1 2 3; do
        if out=$(sbatch --parsable --dependency="$dep" --time="$resident_minutes" \
                    --job-name="$RESIDENT_JOB_NAME" --export=ALL \
                    --account="${AUTORESEARCH_ACCOUNT:-}" --partition="${AUTORESEARCH_PARTITION:-}" \
                    "$shim" 2>/dev/null); then
            printf '%s' "${out%%;*}"
            return 0
        fi
        sleep $((attempt * 20))
    done
    return 1
}

drain_per_cadence_chain() {
    # the per-cadence chain's queued successors are superseded; cancel the
    # PENDING ones so the two modes never tick side by side for long (a
    # running one finishes its tick and, seeing us, queues no more)
    squeue -u "$USER" --name="$JOB_NAME" -h -t PENDING -o "%i" 2>/dev/null | while read -r jid; do
        [ -n "$jid" ] && scancel "$jid" 2>/dev/null && echo "resident: cancelled per-cadence successor $jid"
    done
}

successor=""
shim_sum=""
LOG_DIR="$AUTORESEARCH_ROOT/logs"
mkdir -p "$LOG_DIR" || true
while :; do
    # the log file rolls daily: reopen it every iteration
    if [ -w "$LOG_DIR" ]; then exec >>"$LOG_DIR/tick-$(date +%Y%m%d).log" 2>&1; fi
    now=$(date +%s)
    if [ $((end_epoch - now)) -le "$margin_s" ]; then
        echo "resident: walltime margin reached; handing over to successor ${successor:-none}"
        exit 0
    fi
    if [ -e "$sentinel" ]; then
        echo "resident: pause sentinel present; cancelling successor ${successor:-none} and exiting"
        [ -n "$successor" ] && scancel "$successor" 2>/dev/null
        exit 0
    fi
    if [ -z "$successor" ]; then
        if successor=$(submit_successor); then
            echo "resident: successor $successor queued (afterany:${self:-none})"
            drain_per_cadence_chain
        else
            successor=""
            echo "resident: successor submit failed; retrying next iteration"
        fi
    fi
    # deploy + operator knobs, fresh every iteration (exports reach the tick)
    . "$AUTORESEARCH_HOME/scripts/tick_deploy.sh"
    new_sum=$(cksum "$shim" 2>/dev/null | cut -d' ' -f1)
    if [ -n "$shim_sum" ] && [ "$new_sum" != "$shim_sum" ] && [ -n "$successor" ]; then
        # Slurm spooled the successor's script at submission: resubmit so
        # handover runs the shim the deploy just installed
        scancel "$successor" 2>/dev/null
        if successor=$(submit_successor); then
            echo "resident: shim changed; successor resubmitted as $successor"
        else
            successor=""
            echo "resident: shim changed but resubmit failed; retrying next iteration"
        fi
    fi
    shim_sum="$new_sum"
    echo "=== tick $(date -Is) on $(hostname -s) job=${self:-none} (resident)"
    (cd "$AUTORESEARCH_HOME" && timeout --kill-after=60s "$tick_timeout" \
        uv run python -m autoresearch.tick --root "$AUTORESEARCH_ROOT")
    rc=$?
    [ "$rc" -ne 0 ] && echo "resident: tick exited $rc; the loop continues"
    now=$(date +%s)
    next=$(( (now / cadence_s + 1) * cadence_s ))
    sleep $((next - now))
done
