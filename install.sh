#!/usr/bin/env sh
# Relay one-command installer (macOS / Linux / bash)
#
# Usage:
#   ./install.sh                        # install from the local checkout
#   ./install.sh <pip-source>           # e.g. a GitHub URL or PyPI name
#   RELAY_PREFIX=<dir> ./install.sh     # where the venv is created
#
# Once Relay is published to PyPI, `pip install relay` is the primary
# path; this script is a convenience wrapper around that same pip flow.

set -e

SOURCE="${1:-}"
PREFIX="${RELAY_PREFIX:-$HOME/.relay}"

if [ -z "$SOURCE" ]; then
    if [ -f "$(dirname "$0")/pyproject.toml" ]; then
        SOURCE="$(cd "$(dirname "$0")" && pwd)"
    fi
fi

if [ -z "$SOURCE" ]; then
    echo "[relay] No install source. Pass a pip source (e.g. a GitHub URL)." >&2
    exit 1
fi

PY="$(command -v python3 || command -v python)"

"$PY" -m venv "$PREFIX"
"$PREFIX/bin/python" -m pip install --upgrade pip
"$PREFIX/bin/python" -m pip install "$SOURCE"

BINDIR="$PREFIX/bin"
if [ -d "$HOME/.local/bin" ] && echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
    ln -sf "$BINDIR/relay" "$HOME/.local/bin/relay"
    echo "[relay] Linked 'relay' into $HOME/.local/bin."
else
    echo "[relay] Add this to your shell profile to run 'relay' from anywhere:"
    echo "  export PATH=\"$BINDIR:\$PATH\""
fi

echo ""
echo "Installation complete."
echo ""
echo "Relay was added to your PATH, but this current shell will not pick"
echo "it up. Open a NEW terminal and type:"
echo ""
echo "    relay"
echo ""
echo "If a new terminal still cannot find 'relay', start the shell with"
echo "the PATH above (or source your profile) before running it."
