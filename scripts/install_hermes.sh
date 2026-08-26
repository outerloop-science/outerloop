#!/bin/bash
# Idempotent install of the pinned hermes-agent clone (the hermes panel lens's
# host prerequisite, like codex's binary). The clone itself is read-only at
# session time — the harness binds it :ro and builds the venv in the per-run
# home — so this only materializes source at a pinned tag. Safe to run
# repeatedly: the fast path checks the tag already checked out.
#
# Usage: install_hermes.sh [target_dir]
#   target_dir  where the clone lives (default: $REVIEW_HERMES_REPO,
#               else ~/hermes-agent)
set -euo pipefail

# Pinned: the tag the GH review workflows run (bump deliberately, everywhere
# at once — the workflows' HERMES_REF and this pin must move together).
WANT="v2026.8.13"
TARGET="${1:-${REVIEW_HERMES_REPO:-$HOME/hermes-agent}}"

if [ -d "$TARGET/.git" ]; then
    have=$(git -C "$TARGET" describe --tags --exact-match 2>/dev/null || echo none)
    if [ "$have" = "$WANT" ]; then
        echo "hermes-agent $WANT already at $TARGET"
        exit 0
    fi
    echo "hermes-agent at $TARGET is $have; re-pinning to $WANT"
    git -C "$TARGET" fetch --depth 1 origin "refs/tags/$WANT:refs/tags/$WANT"
    git -C "$TARGET" checkout -q "tags/$WANT"
else
    git clone --depth 1 --branch "$WANT" \
        https://github.com/NousResearch/hermes-agent "$TARGET"
fi
echo "hermes-agent $WANT ready at $TARGET"
