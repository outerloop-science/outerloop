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

# Pinned tag AND its commit sha: the tag names the version for humans, the
# sha is the integrity pin (tags are mutable; a moved tag must fail loudly,
# never run with the panel key). Bump both together, in lockstep with the
# GH review workflows' HERMES_REF.
WANT="v2026.8.13"
WANT_SHA="4e693dc685b5716e7da22656eccc6ece37c5db72"
TARGET="${1:-${REVIEW_HERMES_REPO:-$HOME/hermes-agent}}"

if [ ! -d "$TARGET/.git" ]; then
    git clone --depth 1 --branch "$WANT" \
        https://github.com/NousResearch/hermes-agent "$TARGET"
fi
head=$(git -C "$TARGET" rev-parse HEAD)
dirty=$(git -C "$TARGET" status --porcelain)
if [ "$head" != "$WANT_SHA" ] || [ -n "$dirty" ]; then
    # a wrong or DIRTY checkout must never run with the panel key: re-pin
    # hard (this clone is a provisioned artifact, not a dev tree)
    echo "hermes-agent at $TARGET is $head (dirty=$([ -n "$dirty" ] && echo yes || echo no)); re-pinning to $WANT"
    git -C "$TARGET" fetch --depth 1 origin "refs/tags/$WANT:refs/tags/$WANT"
    git -C "$TARGET" checkout -q --detach "tags/$WANT"
    git -C "$TARGET" reset --hard -q "tags/$WANT"
    git -C "$TARGET" clean -fdxq
fi
head=$(git -C "$TARGET" rev-parse HEAD)
if [ "$head" != "$WANT_SHA" ]; then
    echo "hermes-agent: tag $WANT resolves to $head, expected $WANT_SHA — refusing" >&2
    exit 1
fi
echo "hermes-agent $WANT ($WANT_SHA) ready at $TARGET"
