#!/bin/sh
set -eu

INSTALL_BROWSER=0
UPGRADE_DALA=0
UPGRADE_UV=0
PACKAGE_SPEC="${DALA_PACKAGE_SPEC:-dala}"

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
