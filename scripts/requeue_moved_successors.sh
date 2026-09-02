#!/usr/bin/env bash
# Cancel same-name PENDING jobs of ours that the site moved off the partition
# we submitted them to. The tick chain keeps two successors queued under
# `--dependency=singleton`; a successor shifted to a lower-tier catch-all
# partition starves there AND blocks its twin (singleton waits for same-name
# jobs to END, and a pending job never ends) — the whole chain stalls
# (Torch, 2026-09-02). Cancelling the moved job frees the twin, and the
# chain's top-up queues a fresh successor on the right partition. Prints each
# cancelled job id. Never fails the caller.
#
# usage: requeue_moved_successors.sh <job-name> <partition>
set -u
name="${1:?usage: requeue_moved_successors.sh <job-name> <partition>}"
wanted="${2:-}"
[ -n "$wanted" ] || exit 0
squeue -u "$USER" --name="$name" -h -t PENDING -o "%i %P" 2>/dev/null | while read -r jid part; do
    [ -n "$jid" ] || continue
    if [ "$part" != "$wanted" ]; then
        scancel "$jid" 2>/dev/null && echo "chain: cancelled successor $jid moved to $part (asked for $wanted)"
    fi
done
exit 0
