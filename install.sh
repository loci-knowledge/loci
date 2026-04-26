#!/usr/bin/env sh
# Install loci as a CLI tool.
# Works on macOS and Linux. Requires Python 3.12+.
set -e

PACKAGE="loci"
MIN_PYTHON="3.12"

err() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[32m=>\033[0m %s\n' "$*"; }

# Check Python version
check_python() {
    local py="$1"
    if ! command -v "$py" >/dev/null 2>&1; then return 1; fi
    local ver
    ver=$("$py" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null) || return 1
    # Compare major.minor
    local major minor req_major req_minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    req_major=$(echo "$MIN_PYTHON" | cut -d. -f1)
    req_minor=$(echo "$MIN_PYTHON" | cut -d. -f2)
    [ "$major" -gt "$req_major" ] || { [ "$major" -eq "$req_major" ] && [ "$minor" -ge "$req_minor" ]; }
}

PYTHON=""
for py in python3.13 python3.12 python3 python; do
    if check_python "$py"; then PYTHON="$py"; break; fi
done

[ -n "$PYTHON" ] || err "Python $MIN_PYTHON+ not found. Install from https://python.org/downloads/ and re-run."
info "Using $($PYTHON --version)"

# Try uv tool install (fastest, isolated)
if command -v uv >/dev/null 2>&1; then
    info "Installing via uv tool install..."
    uv tool install "$PACKAGE"
    info "Done! Run: loci --help"
    exit 0
fi

# Try pipx (also isolated)
if command -v pipx >/dev/null 2>&1; then
    info "Installing via pipx..."
    pipx install "$PACKAGE"
    info "Done! Run: loci --help"
    exit 0
fi

# Fall back to pip with --user
info "Installing via pip (no uv or pipx found)..."
"$PYTHON" -m pip install --user --upgrade "$PACKAGE"

# Remind about PATH
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) printf '\n\033[33mwarning:\033[0m Add ~/.local/bin to your PATH:\n  export PATH="$HOME/.local/bin:$PATH"\n' ;;
esac

info "Done! Run: loci --help"
