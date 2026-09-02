#!/usr/bin/env bash
# Remove STALE git lock files from a checkout before the tick deploys into it.
#
# A tick job killed mid-fetch (walltime, node death) leaves .git/*.lock files
# behind, and every later deploy then fails with "Unable to create ...lock:
# File exists" — the chain keeps running OLD code until someone clears them by
# hand (twice on Torch, 2026-09-01). A lock is stale when it is older than
# MIN_AGE_MINUTES (default 10 — a live git op holds one for seconds) AND no
# git process of ours is working in the checkout. Only *.lock files are
# touched, at any depth (slash-named branches nest their ref locks); git
# recreates them as needed. Linked worktrees are followed to their real git
# dir. Every removed path is printed. Never fails the caller.
#
# usage: sweep_git_locks.sh <checkout> [min-age-minutes]
set -u
checkout="${1:?usage: sweep_git_locks.sh <checkout> [min-age-minutes]}"
min_age="${2:-10}"
# resolve the git dir through git itself (a linked worktree's .git is a FILE
# naming it); rev-parse takes no locks, so stale ones cannot block this
gitdir=$(git -C "$checkout" rev-parse --absolute-git-dir 2>/dev/null) || exit 0
common=$(git -C "$checkout" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$gitdir")
[ -d "$gitdir" ] || exit 0
uid=$(id -u)

live_git() {
    # a git of ours that NAMES this checkout or its git dir on its command line
    if pgrep -u "$uid" -f -- "git.*($checkout|$gitdir|$common)" >/dev/null 2>&1; then
        return 0
    fi
    # a git of ours started AFTER cd into the checkout names nothing: read its
    # cwd (/proc on Linux, lsof elsewhere); with neither, any git of ours is
    # reason enough to wait a cadence
    for pid in $(pgrep -u "$uid" -x git 2>/dev/null); do
        if [ -d /proc ]; then
            cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
        elif command -v lsof >/dev/null 2>&1; then
            cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
            [ -n "$cwd" ] || return 0
        else
            return 0
        fi
        case "$cwd" in "$checkout"|"$checkout"/*|"$common"|"$common"/*) return 0 ;; esac
    done
    return 1
}

if live_git; then
    exit 0
fi
for dir in "$gitdir" "$common"; do
    [ -d "$dir" ] || continue
    find "$dir" -type f -name '*.lock' -mmin "+$min_age" 2>/dev/null
done | sort -u | while IFS= read -r lock; do
    rm -f -- "$lock" && echo "deploy: swept stale git lock $lock"
done
exit 0
