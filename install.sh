#!/bin/sh
# Install IPTV Channels for the current user (Linux, macOS).
# Windows: no install step - run `python iptv-channels.py`.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"

mkdir -p "$BIN"
install -m 755 "$DIR/iptv-channels.py" "$BIN/iptv-channels.py"
echo "Installed: $BIN/iptv-channels.py"

# Menu entry only makes sense on Linux desktops.
if [ "$(uname -s)" = "Linux" ]; then
    APPS="$HOME/.local/share/applications"
    mkdir -p "$APPS"
    python3 - "$DIR/iptv-channels.desktop.in" "$APPS/iptv-channels.desktop" "$BIN/iptv-channels.py" <<'PY'
import sys
src, dst, exe = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, encoding="utf-8") as f:
    text = f.read().replace("@EXEC@", exe)
with open(dst, "w", encoding="utf-8") as f:
    f.write(text)
PY
    chmod 644 "$APPS/iptv-channels.desktop"
    update-desktop-database "$APPS" 2>/dev/null || true
    echo "Installed: $APPS/iptv-channels.desktop"
    echo "Launch 'IPTV Channels' from your menu."
else
    echo "Run it with: $BIN/iptv-channels.py"
fi
