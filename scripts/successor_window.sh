#!/usr/bin/env bash
# The scheduling window of one tick successor: when it may start (--begin) and
# when Slurm should give up on it (--deadline). A successor must START within
# one cadence of its slot; Slurm removes a pending job once it can no longer
# finish its walltime before the deadline (start > deadline - time), so the
# deadline is slot + cadence + walltime — never inside the walltime itself
# (a 6-minute cadence with a 15-minute tick would otherwise remove every
# successor at once, terra #235 r1).
#
# usage: successor_window.sh <slot-epoch> <cadence-seconds> <walltime-minutes>
# prints: <begin ISO> <deadline ISO> <deadline-epoch>
set -u
slot="${1:?usage: successor_window.sh <slot-epoch> <cadence-seconds> <walltime-minutes>}"
cadence_s="${2:?cadence seconds}"
walltime_min="${3:?walltime minutes}"
deadline_epoch=$((slot + cadence_s + walltime_min * 60))
iso() { date -d "@$1" +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -r "$1" +%Y-%m-%dT%H:%M:%S; }
printf '%s %s %s\n' "$(iso "$slot")" "$(iso "$deadline_epoch")" "$deadline_epoch"
