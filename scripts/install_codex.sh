#!/bin/bash
# Idempotent install of the harness-verified codex author binary.
#
# codex is a HOST prerequisite for the codex author backend (like uv, apptainer,
# and the .sif image) — deliberately NOT baked into the image, so it updates by
# swapping one host binary. Safe to run repeatedly: the fast path is a local
# `codex --version`, so it only touches the network on a version mismatch or a
# missing binary. Run at host setup, or best-effort from the tick chain.
#
# Usage: install_codex.sh [target_path]
#   target_path  where to place the binary
#                (default: $AUTORESEARCH_CODEX_BIN, else ~/.local/bin/codex)
set -euo pipefail

# Pinned: 0.130.0 is harness-verified — it needs neither the code-mode-host helper
# that 0.149.x requires nor bubblewrap on the (danger-full-access) author path.
WANT="0.130.0"
TARGET="${1:-${AUTORESEARCH_CODEX_BIN:-$HOME/.local/bin/codex}}"

have="$("$TARGET" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ "$have" = "$WANT" ]; then
    echo "install_codex: codex $WANT already at $TARGET"
    exit 0
fi

arch="$(uname -m)"
if [ "$arch" != "x86_64" ]; then
    echo "install_codex: unsupported arch $arch (only x86_64 is pinned)" >&2
    exit 1
fi
echo "install_codex: installing codex $WANT -> $TARGET (have '${have:-none}')"

asset="codex-x86_64-unknown-linux-musl.tar.gz"
url="https://github.com/openai/codex/releases/download/rust-v${WANT}/${asset}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$url" -o "$tmp/codex.tar.gz"
tar -xzf "$tmp/codex.tar.gz" -C "$tmp"
# the tarball holds one binary, named `codex` or `codex-<target-triple>`
bin="$(find "$tmp" -maxdepth 1 -type f \( -name codex -o -name 'codex-*' \) | head -1)"
[ -n "$bin" ] || { echo "install_codex: no codex binary in the tarball" >&2; exit 1; }
mkdir -p "$(dirname "$TARGET")"
# atomic replace on the same filesystem: never leave a half-written binary
install -m 0755 "$bin" "$TARGET.tmp.$$"
mv -f "$TARGET.tmp.$$" "$TARGET"
echo "install_codex: installed $("$TARGET" --version 2>&1 | head -1)"
