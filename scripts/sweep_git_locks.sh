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
# every worktree sharing $common: a git working in a SIBLING worktree holds
# locks in the shared dir too, and must count as live for this sweep
worktrees=$(git -C "$checkout" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')
[ -n "$worktrees" ] || worktrees="$checkout"
base=$(basename "$checkout")
# ages are compared exactly, in seconds: find's -mmin truncates on GNU and
# rounds UP on BSD, so neither says "at least N minutes old" the same way
[ "$min_age" -ge 0 ] 2>/dev/null || min_age=10
min_age_s=$((min_age * 60))
now=$(date +%s)

mtime_of() {
    stat -c %Y -- "$1" 2>/dev/null || stat -f %m -- "$1" 2>/dev/null || echo "$now"
}

live_git() {
    # only REAL git processes of ours (comm == git — never this bash script,
    # whose own argv names the checkout): live when one names the checkout
    # or its git dir on its command line, or works inside it (cwd via /proc
    # on Linux, lsof elsewhere); with no cwd source, any git of ours is
    # reason enough to wait a cadence
    for pid in $(pgrep -u "$uid" -x git 2>/dev/null); do
        args=$(ps -o args= -p "$pid" 2>/dev/null || echo "")
        case "$args" in *"$gitdir"*|*"$common"*) return 0 ;; esac
        for wt in $worktrees; do
            case "$args" in *"$wt"*) return 0 ;; esac
        done
        # a RELATIVE -C / --git-dir / --work-tree naming the checkout by its
        # basename (started from the parent directory): matched by name,
        # conservatively — over-matching only delays the sweep a cadence
        case " $args " in
            *" -C $base "*|*" -C ./$base "*|*" -C $base/"*|*" -C ./$base/"*) return 0 ;;
            *"--git-dir=$base/"*|*"--git-dir=./$base/"*) return 0 ;;
            *"--work-tree=$base"*|*"--work-tree=./$base"*) return 0 ;;
        esac
        if [ -d /proc ]; then
            cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
        elif command -v lsof >/dev/null 2>&1; then
            cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
            [ -n "$cwd" ] || return 0
        else
            return 0
        fi
        case "$cwd" in "$common"|"$common"/*) return 0 ;; esac
        for wt in $worktrees; do
            case "$cwd" in "$wt"|"$wt"/*) return 0 ;; esac
        done
    done
    return 1
}

if live_git; then
    exit 0
fi
for dir in "$gitdir" "$common"; do
    [ -d "$dir" ] || continue
    find "$dir" -type f -name '*.lock' 2>/dev/null
done | sort -u | while IFS= read -r lock; do
    age=$(( now - $(mtime_of "$lock") ))
    [ "$age" -ge "$min_age_s" ] || continue
    rm -f -- "$lock" && echo "deploy: swept stale git lock $lock"
done
exit 0
