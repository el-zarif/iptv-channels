#!/bin/sh
# Install IPTV Channels for the current user.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

mkdir -p "$BIN" "$APPS"
install -m 755 "$DIR/iptv-channels.py" "$BIN/iptv-channels.py"
sed "s|@EXEC@|$BIN/iptv-channels.py|" "$DIR/iptv-channels.desktop.in" \
    > "$APPS/iptv-channels.desktop"
chmod 644 "$APPS/iptv-channels.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

echo "Installed:"
echo "  $BIN/iptv-channels.py"
echo "  $APPS/iptv-channels.desktop"
echo "Launch 'IPTV Channels' from your menu."
