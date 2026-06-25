#!/bin/sh
set -eu

INSTALL_BROWSER=0
UPGRADE_DALA=0
UPGRADE_UV=0
PACKAGE_SPEC="${DALA_PACKAGE_SPEC:-dala}"
DALA_USER_BIN="${DALA_USER_BIN:-$HOME/.local/bin}"
DALA_TERMUX_PIP_CONSTRAINTS="${DALA_TERMUX_PIP_CONSTRAINTS:-fastapi<0.100 pydantic<2}"

for arg in "$@"; do
  case "$arg" in
    --headless-browser|--browser) INSTALL_BROWSER=1 ;;
    --upgrade) UPGRADE_DALA=1 ;;
    --upgrade-uv) UPGRADE_UV=1 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--headless-browser] [--upgrade] [--upgrade-uv]" >&2
      exit 2
      ;;
  esac
done

is_termux() {
  [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux/files/usr" ]
}

write_termux_wrapper() {
  name="$1"
  module="$2"
  mkdir -p "$DALA_USER_BIN"
  {
    echo "#!/bin/sh"
    echo "exec python -m $module \"\$@\""
  } > "$DALA_USER_BIN/$name"
  chmod +x "$DALA_USER_BIN/$name"
}

termux_dala_installed() {
  python -c 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("dala") else 1)' >/dev/null 2>&1
}

write_termux_constraints() {
  constraints_dir="${TMPDIR:-$HOME/.cache/dala}"
  mkdir -p "$constraints_dir"
  constraints_file="$constraints_dir/termux-constraints.txt"
  cat > "$constraints_file" <<'EOF'
aiohttp==3.14.1
anyio==4.14.1
dateparser==1.4.1
fastapi==0.99.1
htmldate==1.10.0
pydantic==1.10.26
regex==2026.5.9
starlette==0.27.0
trafilatura==2.1.0
uvicorn==0.49.0
EOF
  echo "$constraints_file"
}

ensure_termux_pip() {
  if command -v pip >/dev/null 2>&1 && pip --version >/dev/null 2>&1; then
    return 0
  fi

  if command -v pkg >/dev/null 2>&1; then
    echo "Installing or repairing Termux python-pip..."
    pkg install -y python python-pip
  fi

  if command -v pip >/dev/null 2>&1 && pip --version >/dev/null 2>&1; then
    return 0
  fi

  if command -v pkg >/dev/null 2>&1; then
    echo "python-pip is present but not importable; reinstalling it..."
    pkg reinstall -y python-pip
  fi

  if ! command -v pip >/dev/null 2>&1 || ! pip --version >/dev/null 2>&1; then
    echo "Could not find a working Termux pip." >&2
    echo "Run: pkg update && pkg install -y python python-pip" >&2
    echo "If pip still fails, run: pkg reinstall -y python-pip" >&2
    exit 1
  fi
}

install_termux() {
  if [ "$INSTALL_BROWSER" -eq 1 ]; then
    echo "Headless browser/PDF support is not installed by the Termux installer."
    echo "The base EPUB server will be installed. Use extension capture for browser-rendered pages."
  fi

  if ! command -v python >/dev/null 2>&1; then
    if command -v pkg >/dev/null 2>&1; then
      pkg install -y python
    else
      echo "Python is required. Install Python, then rerun this script." >&2
      exit 1
    fi
  fi

  if [ "$UPGRADE_DALA" -eq 0 ] && termux_dala_installed; then
    write_termux_wrapper "dala" "dala.cli"
    write_termux_wrapper "dala-server" "dala.server"
    echo "Dala is already installed for Termux."
    echo "Run '$0 --upgrade' to update it."
    echo
    echo "Start the server with:"
    echo "  dala-server --no-open"
    return 0
  fi

  ensure_termux_pip

  if command -v pkg >/dev/null 2>&1; then
    echo "Installing Termux native packages for image and HTML dependencies..."
    pkg install -y python-lxml python-pillow
  fi

  echo "Installing Dala for Termux with pip --user..."
  termux_constraints_file="$(write_termux_constraints)"
  # FastAPI 0.100+ pulls Pydantic 2/pydantic-core, which currently falls back
  # to a Rust source build on Android. FastAPI 0.99 + Pydantic 1 are pure
  # Python wheels and are sufficient for Dala's server API usage.
  pip install --user --upgrade -c "$termux_constraints_file" $DALA_TERMUX_PIP_CONSTRAINTS "$PACKAGE_SPEC"

  write_termux_wrapper "dala" "dala.cli"
  write_termux_wrapper "dala-server" "dala.server"

  echo
  echo "Dala installed for Termux. Start the server with:"
  echo "  dala-server --no-open"
  echo "If dala-server is not found, add this to your shell profile:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
}

if is_termux; then
  install_termux
  exit 0
fi

if command -v uv >/dev/null 2>&1; then
  echo "Found uv: $(uv --version)"
  if [ "$UPGRADE_UV" -eq 1 ]; then
    uv self update || echo "uv self update is not available for this install; continuing."
  fi
else
  echo "uv not found; installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

INSTALL_ARGS=""
if [ "$UPGRADE_DALA" -eq 1 ] || [ "$INSTALL_BROWSER" -eq 1 ]; then
  INSTALL_ARGS="--force"
fi

if [ "$INSTALL_BROWSER" -eq 1 ]; then
  echo "Installing Dala with optional headless browser support..."
  echo "This lets the Dala server control Chrome/Chromium in the background."
  echo "It is needed for PDF output and some JavaScript-heavy pages."
  echo "It is separate from the normal Dala browser extension."
  uv tool install $INSTALL_ARGS --with playwright "$PACKAGE_SPEC"
  if command -v dala-setup-browser >/dev/null 2>&1; then
    dala-setup-browser
  else
    uv tool run --with playwright --from "$PACKAGE_SPEC" dala-setup-browser
  fi
else
  echo "Installing Dala server..."
  uv tool install $INSTALL_ARGS "$PACKAGE_SPEC"
fi

echo
echo "Dala installed. Start the server with:"
echo "  dala-server"
echo "If dala-server is not found, restart your terminal or run: uv tool update-shell"
