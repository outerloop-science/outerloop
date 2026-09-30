#!/bin/bash
# Install verified source at <repo>, and its runtime at <repo>.runtime/<sha>/.
# The sibling runtime contains python/ (uv-managed Python) and venv/; neither
# can dirty the checkout or be removed by its git clean. Both source and the
# whole runtime are bound read-only at their host paths during contained runs.
# .complete is written last; reruns reuse a completed runtime for this pin.
# The venv links to its interpreter by absolute path: build a runtime in place,
# never copy one to another location.
#
# Usage: install_hermes.sh [target_dir]
#   target_dir  where the clone lives (default: $REVIEW_HERMES_REPO,
#               else ~/hermes-agent)
set -euo pipefail

# A checkout needs no installed kernel; wheels use the same packaged reader.
pin() {
    local reader
    reader="$(dirname "${BASH_SOURCE[0]}")/../src/outerloop/harness_pins.py"
    if [ -f "$reader" ]; then
        python3 "$reader" "$@"
    else
        python3 -m outerloop.harness_pins "$@"
    fi
}
WANT="$(pin hermes ref)"
WANT_SHA="$(pin hermes sha)"
TARGET="${1:-${REVIEW_HERMES_REPO:-$HOME/hermes-agent}}"

# Reject conflicting pins supplied by legacy callers.
if [ "${HERMES_REF:-$WANT}" != "$WANT" ] || [ "${HERMES_SHA:-$WANT_SHA}" != "$WANT_SHA" ]; then
    echo "hermes-agent: workflow and installer pins disagree — refusing" >&2
    exit 1
fi
# Canonicalize before locking, including when the source does not exist yet.
mkdir -p "$(dirname "$TARGET")"
if [ -d "$TARGET" ]; then
    TARGET=$(cd "$TARGET" && pwd -P)
else
    TARGET="$(cd "$(dirname "$TARGET")" && pwd -P)/$(basename "$TARGET")"
fi
LOCK="${TARGET}.installing"
# One source-specific lock covers clone/re-pin AND runtime creation/reuse.
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "hermes-agent: installation in progress; if interrupted, remove $LOCK and retry" >&2
    exit 1
fi
trap 'rmdir "$LOCK"' EXIT

if [ ! -d "$TARGET/.git" ]; then
    # Public foreign repository: clone anonymously, never with a job's
    # repo-scoped GitHub token. Retry cold fan-out/rate-limit failures.
    for i in 1 2 3 4 5 6; do
        if git clone --depth 1 --branch "$WANT" \
            https://github.com/NousResearch/hermes-agent "$TARGET"; then
            break
        fi
        if [ "$i" = 6 ]; then
            echo "hermes clone failed after 6 attempts" >&2
            exit 1
        fi
        sleep $(( i * 10 + RANDOM % 10 ))
        rm -rf "$TARGET"
    done
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
RUNTIME="${TARGET}.runtime/$WANT_SHA"
if [ -f "$RUNTIME/.complete" ] && [ "$(cat "$RUNTIME/.complete")" = "$WANT_SHA" ] && \
   [ -x "$RUNTIME/venv/bin/python" ] && \
   [ "$(cat "$RUNTIME/venv/bin/python.verified-sha256" 2>/dev/null || true)" = \
     "$(sha256sum "$RUNTIME/venv/bin/python" | cut -d' ' -f1)" ]; then
    echo "hermes-agent $WANT ($WANT_SHA) ready at $TARGET"
    exit 0
fi
mkdir -p "$RUNTIME"
rm -f "$RUNTIME/.complete"
# Rebuild both the interpreter and venv after an interrupted or changed install.
rm -rf "$RUNTIME/python" "$RUNTIME/venv"
export UV_PYTHON_INSTALL_DIR="$RUNTIME/python"
export UV_PROJECT_ENVIRONMENT="$RUNTIME/venv"
export UV_CACHE_DIR="$RUNTIME/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_LINK_MODE=copy
uv python install --no-bin 3.12
python=$(uv python find --system 3.12)  # UV_PYTHON_PREFERENCE=only-managed restricts the search
case "$python" in
    "$RUNTIME/python/"*) ;;
    *) echo "hermes-agent: Python must live under $RUNTIME/python" >&2; exit 1 ;;
esac
uv sync --project "$TARGET" --frozen --no-install-project --python "$python"
"$RUNTIME/venv/bin/python" -B -c 'import sys; assert sys.version_info >= (3, 12)'
rm -rf "$UV_CACHE_DIR"  # the venv is complete; sessions never need the download cache
sha256sum "$RUNTIME/venv/bin/python" | cut -d' ' -f1 > "$RUNTIME/venv/bin/python.verified-sha256"
printf '%s\n' "$WANT_SHA" > "$RUNTIME/.complete"
echo "hermes-agent $WANT ($WANT_SHA) ready at $TARGET"
