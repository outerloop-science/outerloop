#!/usr/bin/env bash
# The resident tick (docs/design/resident-tick.md), SOURCED by tick_chain.sbatch
# when OUTERLOOP_RESIDENT=1: one long-lived job that loops
#   deploy -> one tick as a child under a hard timeout -> sleep to the next slot
# and keeps exactly ONE successor queued, dependent on its own end
# (afterany:self + singleton), so the chain needs a handful of scheduling
# events a day instead of one per cadence. The pause sentinel cancels the
# successor and exits without resubmitting; a deploy that changed the shim
# resubmits the successor so handover runs the current script (Slurm spools
# batch scripts at submission). The tick itself is unchanged: records,
# leases, markers and the coalescing guard make a late or repeated tick safe.
#
# Knobs (chain environment): OUTERLOOP_RESIDENT_MINUTES (walltime the job
# was started with; default 360 = cpu_short's maximum), OUTERLOOP_CADENCE_MIN,
# OUTERLOOP_TICK_TIMEOUT (default 15m), OUTERLOOP_RESIDENT_MARGIN_S
# (stop this many seconds before walltime; default 1200),
# OUTERLOOP_RESIDENT_CADENCE_S (tests: override the cadence in seconds).

resident_minutes="${OUTERLOOP_RESIDENT_MINUTES:-360}"
cadence_s="${OUTERLOOP_RESIDENT_CADENCE_S:-$((${OUTERLOOP_CADENCE_MIN:-30} * 60))}"
tick_timeout="${OUTERLOOP_TICK_TIMEOUT:-15m}"
margin_s="${OUTERLOOP_RESIDENT_MARGIN_S:-1200}"
retry_s="${OUTERLOOP_RESIDENT_RETRY_S:-20}"  # backoff unit for submit retries (tests: 0)
self="${SLURM_JOB_ID:-}"
shim="$OUTERLOOP_HOME/scripts/tick_chain.sbatch"
sentinel="$OUTERLOOP_ROOT/PAUSE"

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
    # Account and partition are optional (unset -> Slurm's defaults); pass
    # them only when set.
    local acct_arg=""
    [ -n "${OUTERLOOP_ACCOUNT:-}" ] && acct_arg="--account=${OUTERLOOP_ACCOUNT}"
    local qos_arg=""
    [ -n "${OUTERLOOP_QOS:-}" ] && qos_arg="--qos=${OUTERLOOP_QOS}"
    local part_arg=""
    [ -n "${OUTERLOOP_PARTITION:-}" ] && part_arg="--partition=${OUTERLOOP_PARTITION}"
    local out="" attempt
    for attempt in 1 2 3; do
        if out=$(sbatch --parsable --dependency="$dep" --time="$resident_minutes" \
                    --job-name="$RESIDENT_JOB_NAME" --export=ALL \
                    ${acct_arg:+"$acct_arg"} ${part_arg:+"$part_arg"} ${qos_arg:+"$qos_arg"} \
                    "$shim" 2>/dev/null); then
            printf '%s' "${out%%;*}"
            return 0
        fi
        sleep $((attempt * retry_s))
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

shim_checksum() { cksum "$shim" 2>/dev/null | cut -d' ' -f1; }

ensure_successor() {
    local state="" old="$successor" replacement=""
    if [ -n "$successor" ]; then
        if ! state=$(squeue -h -j "$successor" -o %T 2>&1); then
            case "$state" in
                *"Invalid job id specified"*) state="" ;;
                *)
                    # A query outage is not evidence that the job vanished.
                    # Keep its id to avoid duplicates, but do not hand over.
                    echo "resident: ERROR: cannot check successor $successor: $state; retrying next iteration"
                    return 1 ;;
            esac
        fi
        state=$(printf '%s' "$state" | awk 'NF {print $1; exit}')
        case "$state" in
            PENDING|RUNNING) return 0 ;;
            "") state="GONE" ;;
            CANCELLED|FAILED|TIMEOUT|NODE_FAIL|COMPLETED|OUT_OF_MEMORY|BOOT_FAIL|DEADLINE|PREEMPTED|REVOKED) ;;
            *)
                echo "resident: successor $successor is $state; waiting before handover"
                return 1 ;;
        esac
    fi
    if replacement=$(submit_successor); then
        successor="$replacement"
        successor_sum=$(shim_checksum)
        if [ -n "$old" ]; then
            echo "resident: successor $old vanished ($state); requeued as $successor"
        elif [ $((end_epoch - $(date +%s))) -le "$margin_s" ]; then
            echo "resident: successor $successor queued at handover"
        else
            echo "resident: successor $successor queued (afterany:${self:-none})"
        fi
        drain_per_cadence_chain
        return 0
    fi
    echo "resident: ERROR: successor ${old:-none} submit failed; retrying next iteration"
    return 1
}

successor=""
successor_sum=""  # the shim checksum the queued successor was submitted under
LOG_DIR="$OUTERLOOP_ROOT/logs"
mkdir -p "$LOG_DIR" || true
while :; do
    # the log file rolls daily: reopen it every iteration
    if [ -w "$LOG_DIR" ]; then exec >>"$LOG_DIR/tick-$(date +%Y%m%d).log" 2>&1; fi
    now=$(date +%s)
    # the sentinel is read FIRST on every iteration, the handover included: a
    # paused chain ends with nothing queued, whatever brought the loop here
    if [ -e "$sentinel" ]; then
        echo "resident: pause sentinel present; cancelling successor ${successor:-none} and exiting"
        [ -n "$successor" ] && scancel "$successor" 2>/dev/null
        exit 0
    fi
    if [ "$now" -ge "$end_epoch" ]; then
        echo "resident: ERROR: walltime ended without a verified live successor — the chain needs a restart"
        exit 1
    fi
    successor_ready=0
    ensure_successor && successor_ready=1
    if [ $((end_epoch - $(date +%s))) -le "$margin_s" ]; then
        # Recheck immediately before exiting, including freshly submitted jobs.
        if [ "$successor_ready" -eq 1 ] && ensure_successor; then
            if handover_state=$(squeue -h -j "$successor" -o %T 2>/dev/null); then
                case "$handover_state" in
                    PENDING|RUNNING)
                        echo "resident: walltime margin reached; handing over to successor $successor"
                        exit 0 ;;
                esac
            fi
        fi
        echo "resident: ERROR: walltime margin reached without a verified live successor; keeping ticking until walltime ends"
    fi
    # deploy + operator knobs, fresh every iteration (exports reach the tick)
    . "$OUTERLOOP_HOME/scripts/tick_deploy.sh"
    new_sum=$(shim_checksum)
    if [ -n "$successor" ] && [ "$new_sum" != "$successor_sum" ]; then
        # Slurm spooled the successor's script at submission: resubmit so
        # handover runs the shim the deploy just installed. The REPLACEMENT is
        # queued first (a moment with two singleton successors is harmless —
        # they serialize), and the stale one is cancelled only then; a
        # cancellation Slurm refuses keeps the stale one and drops the
        # replacement, so the chain can never fork or go successor-less here
        if replacement=$(submit_successor); then
            if scancel "$successor" 2>/dev/null; then
                echo "resident: shim changed; successor $successor replaced by $replacement"
                successor="$replacement"
                successor_sum="$new_sum"
            else
                scancel "$replacement" 2>/dev/null || true
                echo "resident: could not cancel stale successor $successor; keeping it (retry next iteration)"
            fi
        else
            echo "resident: shim changed but resubmit failed; retrying next iteration"
        fi
    fi
    echo "=== tick $(date -Is) on $(hostname -s) job=${self:-none} (resident)"
    if [ "${OUTERLOOP_DEPLOY_BROKEN:-}" = "1" ]; then
        echo "$(date -u +%FT%TZ) tick skipped: checkout and environment inconsistent after a failed deploy (resident)"
    else
    (cd "$OUTERLOOP_HOME" && timeout --kill-after=60s "$tick_timeout" \
        uv run --no-sync python -m outerloop.tick --root "$OUTERLOOP_ROOT")
    fi
    rc=$?
    [ "$rc" -ne 0 ] && echo "resident: tick exited $rc; the loop continues"
    # sleep to the next slot, but never past the walltime margin: the loop
    # must wake to hand over, not be killed asleep
    now=$(date +%s)
    next=$(( (now / cadence_s + 1) * cadence_s ))
    wait=$((next - now))
    limit=$((end_epoch - margin_s - now))
    # After a failed handover keep the normal cadence, capped by actual end.
    [ "$limit" -le 0 ] && limit=$((end_epoch - now))
    [ "$limit" -lt "$wait" ] && wait="$limit"
    [ "$wait" -gt 0 ] && sleep "$wait"
done
