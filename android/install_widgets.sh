#!/data/data/com.termux/files/usr/bin/bash
set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET_DIR="${DALA_WIDGET_DIR:-$HOME/.shortcuts/dala}"

required_files="dala_start.sh dala_stop.sh dala_status.sh"

missing=0
for file in $required_files; do
    if [ ! -f "$SCRIPT_DIR/$file" ]; then
        echo "Missing $SCRIPT_DIR/$file"
        missing=1
    fi
done

if [ "$missing" -ne 0 ]; then
    echo ""
    echo "Run this script from the extracted release bundle's android/ folder."
    exit 1
fi

mkdir -p "$TARGET_DIR"

cp "$SCRIPT_DIR/dala_start.sh" "$TARGET_DIR/dala_start.sh"
cp "$SCRIPT_DIR/dala_stop.sh" "$TARGET_DIR/dala_stop.sh"
cp "$SCRIPT_DIR/dala_status.sh" "$TARGET_DIR/dala_status.sh"

cat > "$TARGET_DIR/start.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/dala_start.sh"
EOF

cat > "$TARGET_DIR/stop.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/dala_stop.sh"
EOF

cat > "$TARGET_DIR/status.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/dala_status.sh"
EOF

chmod +x \
    "$TARGET_DIR/dala_start.sh" \
    "$TARGET_DIR/dala_stop.sh" \
    "$TARGET_DIR/dala_status.sh" \
    "$TARGET_DIR/start.sh" \
    "$TARGET_DIR/stop.sh" \
    "$TARGET_DIR/status.sh"

echo "Installed Dala Termux:Widget shortcuts:"
echo "  $TARGET_DIR/start.sh"
echo "  $TARGET_DIR/stop.sh"
echo "  $TARGET_DIR/status.sh"
echo ""
echo "Install the Termux:Widget app if needed, then add the Dala shortcut folder to your Android home screen."
echo "If Dala is not installed yet, run scripts/install-dala.sh from the release bundle first."
