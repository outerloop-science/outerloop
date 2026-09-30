#!/bin/bash
# Frozen runtime with an in-place managed interpreter; never moved after installation.
set -euo pipefail
pin() {
    local reader
    reader="$(dirname "${BASH_SOURCE[0]}")/../src/outerloop/harness_pins.py"
    if [ -f "$reader" ]; then python3 "$reader" "$@"; else python3 -m outerloop.harness_pins "$@"; fi
}
WANT="$(pin bridge version)"
SOURCE="$(dirname "${BASH_SOURCE[0]}")/../src/outerloop/bridge_runtime"
if [ ! -d "$SOURCE" ]; then
    SOURCE=$(python3 -c 'from pathlib import Path; import outerloop; print(Path(outerloop.__file__).parent / "bridge_runtime")')
fi
TARGET="${1:-${OUTERLOOP_BRIDGE_RUNTIME:-${OUTERLOOP_CACHE_ROOT:-${OUTERLOOP_ROOT:-$HOME/.outerloop}/cache}/bridge}}"
mkdir -p "$TARGET"
TARGET=$(cd "$TARGET" && pwd -P)
mkdir "$TARGET/.installing"
trap 'rmdir "$TARGET/.installing"' EXIT
rm -f "$TARGET/.complete"
export UV_PYTHON_INSTALL_DIR="$TARGET/python"
export UV_PROJECT_ENVIRONMENT="$TARGET/venv"
export UV_CACHE_DIR="$TARGET/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_LINK_MODE=copy
uv python install --no-bin 3.12
python=$(uv python find --system 3.12)
case "$python" in "$TARGET/python/"*) ;; *) exit 1 ;; esac
uv sync --project "$SOURCE" --frozen --compile-bytecode --no-install-project --python "$python"
"$TARGET/venv/bin/python" -c 'import importlib.metadata, sys; assert importlib.metadata.version("litellm") == sys.argv[1]' "$WANT"
DIGEST=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$SOURCE/uv.lock")
python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$TARGET/venv/bin/python" > "$TARGET/python.sha256"
printf '%s %s\n' "$WANT" "$DIGEST" > "$TARGET/.complete"
rm -rf "$UV_CACHE_DIR"
