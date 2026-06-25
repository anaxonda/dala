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
if [ "${1:-}" = "--headless-browser" ] || [ "${1:-}" = "--browser" ]; then
  INSTALL_ARGS="$INSTALL_ARGS --headless-browser"
fi

sh "$INSTALL_SCRIPT" $INSTALL_ARGS

echo
echo "Dala is installed or updated."
echo "Start it with: dala-server"
