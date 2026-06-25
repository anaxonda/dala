#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$ROOT_DIR/dist"
CHROME_DIR="$ROOT_DIR/extension_chrome"
FIREFOX_DIR="$ROOT_DIR/firefox_extension"

VERSION="$(sed -n 's/.*"version": "\(.*\)",/\1/p' "$CHROME_DIR/manifest.json" | head -n 1)"
PY_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -n 1)"

if [[ -z "$VERSION" ]]; then
  echo "Could not determine extension version from manifest."
  exit 1
fi

if [[ -z "$PY_VERSION" ]]; then
  echo "Could not determine Python package version from pyproject.toml."
  exit 1
fi

mkdir -p "$DIST_DIR"

CHROME_ZIP="$DIST_DIR/dala-chrome-v${VERSION}.zip"
FIREFOX_XPI="$DIST_DIR/dala-firefox-v${VERSION}.xpi"
INSTALLERS_ZIP="$DIST_DIR/dala-installers-v${PY_VERSION}.zip"

rm -f "$CHROME_ZIP" "$FIREFOX_XPI" "$INSTALLERS_ZIP"

echo "Packaging Chrome extension..."
(
  cd "$CHROME_DIR"
  zip -qr "$CHROME_ZIP" . -x "web-ext-artifacts/*" -x ".*"
)
echo "Created $CHROME_ZIP"

echo "Packaging Firefox extension..."
(
  cd "$FIREFOX_DIR"
  zip -qr "$FIREFOX_XPI" . -x "web-ext-artifacts/*" -x ".*"
)
echo "Created $FIREFOX_XPI"

echo "Packaging installer bundle..."
(
  cd "$ROOT_DIR"
  zip -qr "$INSTALLERS_ZIP" \
    installers \
    scripts \
    launchers \
    android \
    readme.md \
    LICENSE \
    -x "*/__pycache__/*" \
    -x "*/.DS_Store"
)
echo "Created $INSTALLERS_ZIP"

cat <<EOF

Release assets to attach:
  $CHROME_ZIP
  $FIREFOX_XPI (unsigned; replace with signed XPI when available)
  $INSTALLERS_ZIP

Python package:
  Publish with: uv build && twine upload dist/dala-${PY_VERSION}*
EOF
