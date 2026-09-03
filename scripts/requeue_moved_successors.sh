#!/usr/bin/env bash
# Cancel same-name PENDING jobs of ours that the site moved off the partition
# we submitted them to AND that are starving there: eligible (past their
# --begin, no dependency left) and not started for MIN_STARVE_MINUTES. The
# tick chain keeps two successors queued under `--dependency=singleton`; a
# successor stuck on a lower-tier partition blocks its twin (singleton waits
# for same-name jobs to END) and the chain stalls (Torch, 2026-09-02).
#
# Relocation ALONE is not a fault: the site moves pending jobs routinely
# (cpu_short -> cs, 2026-09-02 evening), and cancelling a relocated job that
# is not yet eligible only resets its queue age. Prints each cancelled job
# id. Never fails the caller.
#
# usage: requeue_moved_successors.sh <job-name> <partition> [min-starve-minutes]
set -u
name="${1:?usage: requeue_moved_successors.sh <job-name> <partition> [min-starve-minutes]}"
wanted="${2:-}"
starve_min="${3:-20}"
[ -n "$wanted" ] || exit 0
now=$(date +%s)
epoch_of() { date -d "$1" +%s 2>/dev/null || date -j -f %Y-%m-%dT%H:%M:%S "$1" +%s 2>/dev/null || echo ""; }
# %i id, %P partition(s), %r reason, %S = scheduled/estimated start (N/A when unknown)
squeue -u "$USER" --name="$name" -h -t PENDING -o "%i|%P|%r" 2>/dev/null | while IFS='|' read -r jid part reason; do
    [ -n "$jid" ] && [ -n "$part" ] || continue
    # both sides may be comma-separated lists: moved only when the job holds
    # NONE of the partitions we asked for
    moved=1
    for have in $(printf '%s' "$part" | tr ',' ' '); do
        for want in $(printf '%s' "$wanted" | tr ',' ' '); do
            [ "$have" = "$want" ] && moved=0
        done
    done
    [ "$moved" -eq 1 ] || continue
    # not yet eligible (waiting for its slot or a dependency): leave it alone
    case "$reason" in BeginTime|Dependency|DependencyNeverSatisfied) continue ;; esac
    # eligible: starving only if it has been eligible for a while
    eligible=$(scontrol show job "$jid" -o 2>/dev/null | tr ' ' '\n' | sed -n 's/^EligibleTime=//p' | head -1)
    el_epoch=$(epoch_of "$eligible")
    [ -n "$el_epoch" ] || continue  # unknown = never cancel on doubt
    if [ $(( now - el_epoch )) -ge $(( starve_min * 60 )) ]; then
        scancel "$jid" 2>/dev/null && echo "chain: cancelled successor $jid starving on $part since $eligible (asked for $wanted)"
    fi
done
exit 0
