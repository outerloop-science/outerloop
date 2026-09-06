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
#                (default: $OUTERLOOP_CODEX_BIN, else ~/.local/bin/codex)
set -euo pipefail

# Pinned: 0.130.0 is harness-verified — it needs neither the code-mode-host helper
# that 0.149.x requires nor bubblewrap on the (danger-full-access) author path.
WANT="0.130.0"
# SHA256 of codex-x86_64-unknown-linux-musl.tar.gz for rust-v0.130.0. The download
# is verified against this BEFORE anything in it is extracted or run, so a swapped
# release asset can never execute on the host (integrity, not self-reported
# version). To bump WANT: fetch the new asset and `sha256sum` it, then update both.
WANT_SHA256="16779e7b7857508a768a36d7d4e084eec336ec23946ed70a9b09489b8f861190"
TARGET="${1:-${OUTERLOOP_CODEX_BIN:-$HOME/.local/bin/codex}}"

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
staged="$TARGET.tmp.$$"
# clean BOTH the work dir and any staged binary on every exit, so a failed
# download/mv never leaves a stale temp next to the target
trap 'rm -rf "$tmp" "$staged"' EXIT
# bounded so a stalled download can't eat the tick's own walltime: this runs
# INSIDE the ~15-min tick job, so cap the fetch well under it (single attempt —
# a transient failure is retried by the next tick, since the install is idempotent)
curl -fsSL --connect-timeout 15 --max-time 120 --retry 0 "$url" -o "$tmp/codex.tar.gz"
# INTEGRITY GATE: verify the archive's sha256 against the pin BEFORE extracting or
# executing anything from it — never run an unverified download on the host (a
# self-reported --version proves nothing; a swapped asset could print anything).
got_sha=$(sha256sum "$tmp/codex.tar.gz" | cut -d' ' -f1)
if [ "$got_sha" != "$WANT_SHA256" ]; then
    echo "install_codex: sha256 mismatch (got $got_sha, want $WANT_SHA256) — refusing" >&2
    exit 1
fi
tar -xzf "$tmp/codex.tar.gz" -C "$tmp"
# the tarball holds one binary, named `codex` or `codex-<target-triple>`; don't
# assume its depth in the archive
bin="$(find "$tmp" -type f \( -name codex -o -name 'codex-*' \) | head -1)"
[ -n "$bin" ] || { echo "install_codex: no codex binary in the tarball" >&2; exit 1; }
# the target dir must exist BEFORE staging into it (the staged temp lives beside
# TARGET so the final mv is atomic on the same filesystem)
mkdir -p "$(dirname "$TARGET")"
install -m 0755 "$bin" "$staged"
# secondary sanity (integrity is already the sha256 gate above, so this runs
# VERIFIED bytes): the staged binary reports the pinned version before the mv
got="$("$staged" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ "$got" != "$WANT" ]; then
    echo "install_codex: downloaded codex reports '$got', wanted '$WANT' — not installing" >&2
    exit 1
fi
# atomic replace on the same filesystem: never leave a half-written binary
mv -f "$staged" "$TARGET"
echo "install_codex: installed codex $got at $TARGET"
