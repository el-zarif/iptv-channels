#!/bin/sh
# Install IPTV Channels for the current user (Linux, macOS).
# Windows: no install step - run `pythonw iptv-channels.py`.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"
DATA="$HOME/.local/share/iptv-channels"

mkdir -p "$BIN" "$DATA/icons"
if [ ! -d "$DIR/icons" ]; then
    echo "No $DIR/icons directory - run: python3 make-icons.py" >&2
    exit 1
fi
install -m 755 "$DIR/iptv-channels.py" "$BIN/iptv-channels.py"
install -m 644 "$DIR"/icons/*.png "$DIR"/icons/*.ico "$DATA/icons/"
echo "Installed: $BIN/iptv-channels.py"
echo "Installed: $DATA/icons"

# Menu entry and icon theme only make sense on Linux desktops.
if [ "$(uname -s)" = "Linux" ]; then
    for size in 16 32 64 128 256; do
        target="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
        mkdir -p "$target"
        install -m 644 "$DIR/icons/icon-$size.png" "$target/iptv-channels.png"
    done
    gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

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
    exit 0
fi

# macOS: the Dock gets its icon from a bundle, never from Tk, so wrap the
# script in a minimal .app. Iconutil ships with macOS, nothing to download.
APP="$HOME/Applications/IPTV Channels.app"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ICONSET="$TMP/icon.iconset"
mkdir -p "$ICONSET"
for pair in "icon-16.png icon_16x16" "icon-32.png icon_16x16@2x" \
            "icon-32.png icon_32x32" "icon-64.png icon_32x32@2x" \
            "icon-128.png icon_128x128" "icon-256.png icon_128x128@2x" \
            "icon-256.png icon_256x256" "icon-512.png icon_256x256@2x" \
            "icon-512.png icon_512x512" "icon.png icon_512x512@2x"; do
    set -- $pair
    cp "$DIR/icons/$1" "$ICONSET/$2.png"
done
iconutil -c icns "$ICONSET" -o "$TMP/icon.icns"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/icon.icns" "$APP/Contents/Resources/icon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>IPTV Channels</string>
  <key>CFBundleDisplayName</key><string>IPTV Channels</string>
  <key>CFBundleIdentifier</key><string>local.iptv-channels</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/sh
exec "$BIN/iptv-channels.py" "\$@"
LAUNCH
chmod 755 "$APP/Contents/MacOS/launch"
printf 'APPL????' > "$APP/Contents/PkgInfo"
touch "$APP"

echo "Installed: $APP"
echo "Open it from Applications, or: open \"$APP\""
echo "Run it from a terminal with: $BIN/iptv-channels.py"
