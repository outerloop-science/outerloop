#!/bin/bash
# Install the pinned Codex binary and its code-mode host together.
# Usage: install_codex.sh [target_path]
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
WANT="$(pin codex version)"
WANT_SHA256="$(pin codex sha256)"
HOST_SHA256="$(pin codex code_mode_host_sha256)"
TARGET="${1:-${OUTERLOOP_CODEX_BIN:-$HOME/.local/bin/codex}}"

HOST="$(dirname "$TARGET")/codex-code-mode-host"

have="$("$TARGET" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][[:alnum:].-]+)?' | head -1 || true)"

if [ "$have" = "$WANT" ]; then
    actual=$(sha256sum "$TARGET" | cut -d' ' -f1)
    expected="$WANT_SHA256 $actual"
    host_actual=$(sha256sum "$HOST" 2>/dev/null | cut -d' ' -f1 || true)
    if [ -x "$HOST" ] && [ "$(cat "$HOST.verified-sha256" 2>/dev/null || true)" = "$HOST_SHA256 $host_actual" ] &&
        [ "$(cat "$TARGET.verified-sha256" 2>/dev/null || true)" = "$expected" ]; then
        echo "install_codex: codex $WANT already at $TARGET"
        exit 0
    fi
    echo "install_codex: installed sha256 marker missing/mismatch — reinstalling" >&2
fi

arch="$(uname -m)"
if [ "$arch" != "x86_64" ]; then
    echo "install_codex: unsupported arch $arch (only x86_64 is pinned)" >&2
    exit 1
fi
echo "install_codex: installing codex $WANT -> $TARGET (have '${have:-none}')"

tmp="$(mktemp -d)"
staged="$TARGET.tmp.$$"
host_staged="$HOST.tmp.$$"
trap 'rm -rf "$tmp" "$staged" "$host_staged"' EXIT
host_asset="codex-code-mode-host-x86_64-unknown-linux-musl"
curl -fsSL --connect-timeout 15 --max-time 120 --retry 0 \
    "https://github.com/openai/codex/releases/download/rust-v${WANT}/${host_asset}.tar.gz" -o "$tmp/host.tar.gz"
host_sha=$(sha256sum "$tmp/host.tar.gz" | cut -d' ' -f1)
if [ "$host_sha" != "$HOST_SHA256" ]; then
    echo "install_codex: code-mode host sha256 mismatch — refusing" >&2
    exit 1
fi
mkdir "$tmp/host"
tar -xzf "$tmp/host.tar.gz" -C "$tmp/host"
mkdir -p "$(dirname "$TARGET")"
install -m 0755 "$tmp/host/$host_asset" "$host_staged"

# npm verifies registry integrity and supports operator trial releases.
if [ -n "${OUTERLOOP_CODEX_VERSION:-}" ]; then
    export npm_config_cache="${npm_config_cache:-${OUTERLOOP_CACHE_ROOT:-${OUTERLOOP_ROOT:-$HOME/.outerloop}/cache}/npm}"
    mkdir -p "$(dirname "$TARGET")"
    npm install --prefix "$TARGET.package" "@openai/codex@$WANT"
    bin="$(find "$TARGET.package/node_modules" -type f -path '*/codex/codex' | head -1)"
    [ -n "$bin" ] || { echo "codex native binary missing" >&2; exit 1; }
    actual=$(sha256sum "$bin" | cut -d' ' -f1)
    [ "$actual" = "$WANT_SHA256" ] || { echo "codex sha256 mismatch — refusing" >&2; exit 1; }
    got="$("$bin" --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][[:alnum:].-]+)?' | head -1)"
    [ "$got" = "$WANT" ] || { echo "codex version mismatch: $got" >&2; exit 1; }
else
    asset="codex-x86_64-unknown-linux-musl.tar.gz"
    url="https://github.com/openai/codex/releases/download/rust-v${WANT}/${asset}"
    curl -fsSL --connect-timeout 15 --max-time 120 --retry 0 "$url" -o "$tmp/codex.tar.gz"
    # Verify the archive before extracting or executing it.
    got_sha=$(sha256sum "$tmp/codex.tar.gz" | cut -d' ' -f1)
    if [ "$got_sha" != "$WANT_SHA256" ]; then
        echo "install_codex: sha256 mismatch (got $got_sha, want $WANT_SHA256) — refusing" >&2
        exit 1
    fi
    mkdir "$tmp/codex"
    tar -xzf "$tmp/codex.tar.gz" -C "$tmp/codex"
    # the tarball holds one binary, named `codex` or `codex-<target-triple>`; don't
    # assume its depth in the archive
    bin="$(find "$tmp/codex" -type f \( -name codex -o -name 'codex-*' \) | head -1)"
    [ -n "$bin" ] || { echo "install_codex: no codex binary in the tarball" >&2; exit 1; }
fi
# Stage beside the targets for atomic replacement.
mkdir -p "$(dirname "$TARGET")"
install -m 0755 "$bin" "$staged"
# Check the verified binary before replacing either executable.
got="$("$staged" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+([-+][[:alnum:].-]+)?' | head -1 || true)"
if [ "$got" != "$WANT" ]; then
    echo "install_codex: downloaded codex reports '$got', wanted '$WANT' — not installing" >&2
    exit 1
fi
# atomic replace on the same filesystem: never leave a half-written binary
mv -f "$host_staged" "$HOST"
printf '%s %s\n' "$HOST_SHA256" "$(sha256sum "$HOST" | cut -d' ' -f1)" > "$HOST.verified-sha256"
mv -f "$staged" "$TARGET"
printf '%s %s\n' "$WANT_SHA256" "$(sha256sum "$TARGET" | cut -d' ' -f1)" > "$TARGET.verified-sha256"
echo "install_codex: installed codex $got at $TARGET"
