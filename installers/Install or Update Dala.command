#!/bin/sh
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname "$DIR")
INSTALL_SCRIPT="$ROOT/scripts/install-dala.sh"

if [ ! -f "$INSTALL_SCRIPT" ]; then
  echo "Could not find installer helper: $INSTALL_SCRIPT" >&2
  exit 1
fi

INSTALL_ARGS="--upgrade"

echo
echo "Install optional headless browser support?"
echo "This lets the Dala server control Chrome/Chromium in the background."
echo "It is needed for PDF output and some JavaScript-heavy pages."
echo "It is separate from the normal Dala browser extension."
printf "Install headless browser support now? [y/N] "
read answer || answer=""
case "$answer" in
  y|Y|yes|YES|Yes) INSTALL_ARGS="$INSTALL_ARGS --headless-browser" ;;
esac

sh "$INSTALL_SCRIPT" $INSTALL_ARGS

DESKTOP="$HOME/Desktop"
if [ -d "$DESKTOP" ]; then
  LAUNCHER="$DESKTOP/Start Dala Server.command"
  cat > "$LAUNCHER" <<'EOF'
#!/bin/sh
dala-server --open
EOF
  chmod +x "$LAUNCHER"
  echo "Created launcher: $LAUNCHER"
fi

echo
echo "Dala is installed or updated."
echo "Start it with the Desktop launcher or run: dala-server --open"
