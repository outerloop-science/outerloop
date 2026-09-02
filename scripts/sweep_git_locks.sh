#!/usr/bin/env bash
# Remove STALE git lock files from a checkout before the tick deploys into it.
#
# A tick job killed mid-fetch (walltime, node death) leaves .git/*.lock files
# behind, and every later deploy then fails with "Unable to create ...lock:
# File exists" — the chain keeps running OLD code until someone clears them by
# hand (twice on Torch, 2026-09-01). A lock is stale when it is older than
# MIN_AGE_MINUTES (default 10 — a live git op holds one for seconds) AND no
# git process is running against the checkout. Only *.lock files are touched;
# git recreates them as needed. Every removed path is printed.
#
# usage: sweep_git_locks.sh <checkout> [min-age-minutes]
set -u
checkout="${1:?usage: sweep_git_locks.sh <checkout> [min-age-minutes]}"
min_age="${2:-10}"
gitdir="$checkout/.git"
[ -d "$gitdir" ] || exit 0
# a live git process on this checkout owns its locks — never race it
if pgrep -f "git -C $checkout" >/dev/null 2>&1 || pgrep -f "git.*--git-dir=$gitdir" >/dev/null 2>&1; then
    exit 0
fi
find "$gitdir" -maxdepth 4 -type f -name '*.lock' -mmin "+$min_age" 2>/dev/null | while IFS= read -r lock; do
    rm -f -- "$lock" && echo "deploy: swept stale git lock $lock"
done
exit 0
