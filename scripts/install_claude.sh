#!/bin/bash
# Idempotent install of the harness-verified claude author binary.
#
# claude is a HOST prerequisite for the claude author backend (like uv, apptainer,
# and the .sif image) — deliberately NOT baked into the image, so it updates by
# swapping one host binary. Safe to run repeatedly: the fast path is a local
# `claude --version`, so it only touches the network on a version mismatch or a
# missing binary. Run at host setup, or best-effort from the tick chain.
#
# Usage: install_claude.sh [target_path]
#   target_path  where to place the binary
#                (default: $OUTERLOOP_CLAUDE_BIN, else ~/.local/bin/claude)
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
WANT="$(pin claude version)"
TARGET="${1:-${OUTERLOOP_CLAUDE_BIN:-$HOME/.local/bin/claude}}"

have="$("$TARGET" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][[:alnum:].-]+)?' | head -1 || true)"


platform="$(uname -s)-$(uname -m)"
case "$platform" in
    Linux-x86_64)
        platform="linux-x64"
        WANT_SHA256="$(pin claude linux-x64)"
        if compgen -G '/lib/libc.musl-*' > /dev/null; then
            platform="linux-x64-musl"
            WANT_SHA256="$(pin claude linux-x64-musl)"
        fi
        ;;
    Linux-aarch64|Linux-arm64)
        platform="linux-arm64"
        WANT_SHA256="$(pin claude linux-arm64)"
        ;;
    *)
        echo "install_claude: unsupported platform $platform (no sha256 pin)" >&2
        exit 1
        ;;
esac
if [ "$have" = "$WANT" ]; then
    actual=$(sha256sum "$TARGET" | cut -d' ' -f1)
    if [ "$actual" = "$WANT_SHA256" ]; then
        echo "install_claude: claude $WANT already at $TARGET"
        exit 0
    fi
    echo "install_claude: installed sha256 mismatch — reinstalling" >&2
fi

for tool in curl sha256sum; do
    command -v "$tool" >/dev/null || {
        echo "install_claude: required tool $tool is missing" >&2
        exit 1
    }
done
echo "install_claude: installing claude $WANT -> $TARGET (have '${have:-none}')"
url="https://downloads.claude.ai/claude-code-releases/$WANT/$platform/claude"
tmp="$(mktemp -d)"
staged="$TARGET.tmp.$$"
# clean BOTH the work dir and any staged binary on every exit, so a failed
# download/mv never leaves a stale temp next to the target
trap 'rm -rf "$tmp" "$staged"' EXIT
# bounded so a stalled download can't eat the tick's own walltime: this runs
# INSIDE the ~15-min tick job, so cap the fetch well under it (single attempt —
# a transient failure is retried by the next tick, since the install is idempotent)
curl -fsSL --connect-timeout 15 --max-time 120 --retry 0 "$url" -o "$tmp/claude"
# INTEGRITY GATE: verify the binary against the pin BEFORE installing or
# executing anything from it.
got_sha=$(sha256sum "$tmp/claude" | cut -d' ' -f1)
if [ "$got_sha" != "$WANT_SHA256" ]; then
    echo "install_claude: sha256 mismatch (got $got_sha, want $WANT_SHA256) — refusing" >&2
    exit 1
fi
# the target dir must exist BEFORE staging into it (the staged temp lives beside
# TARGET so the final mv is atomic on the same filesystem)
mkdir -p "$(dirname "$TARGET")"
install -m 0755 "$tmp/claude" "$staged"
# secondary sanity (integrity is already the sha256 gate above, so this runs
# VERIFIED bytes): the staged binary reports the pinned version before the mv
got="$("$staged" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][[:alnum:].-]+)?' | head -1 || true)"
if [ "$got" != "$WANT" ]; then
    echo "install_claude: downloaded claude reports '$got', wanted '$WANT' — not installing" >&2
    exit 1
fi
# atomic replace on the same filesystem: never leave a half-written binary
mv -f "$staged" "$TARGET"
echo "install_claude: installed claude $got at $TARGET"
