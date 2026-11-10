#!/bin/sh
# Install IPTV Channels for the current user (Linux, macOS).
# Windows: no install step - run `pythonw iptv-channels.py`.
#
#   ./install.sh            install
#   ./install.sh --force    install even if the Python found has Tk < 8.6
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"
DATA="$HOME/.local/share/iptv-channels"

FORCE=no
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=yes ;;
        -h|--help)
            sed -n '1d;/^#/p;/^set -e/q' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "$0: unknown option: $arg (try --help)" >&2
            exit 2 ;;
    esac
done

if [ ! -d "$DIR/icons" ]; then
    echo "No $DIR/icons directory - run: python3 make-icons.py" >&2
    exit 1
fi

# macOS runs the app from a bundle, so the interpreter has to be picked here,
# where the real PATH is visible, and checked here: a Finder launch shows an
# empty window instead of a Python traceback. Refuse rather than install a
# bundle that cannot draw.
if [ "$(uname -s)" = "Darwin" ]; then
    PYTHON=${PYTHON:-$(command -v python3 || true)}
    if [ -z "$PYTHON" ]; then
        echo "No python3 on PATH. Re-run as: PYTHON=/path/to/python3 $0" >&2
        exit 1
    fi
    TK=$("$PYTHON" -c 'import tkinter; print(tkinter.Tcl().eval("info patchlevel"))' 2>/dev/null || true)
    if [ -z "$TK" ]; then
        PROBLEM="$PYTHON cannot import tkinter"
    else
        PROBLEM="$PYTHON has Tcl/Tk $TK, and the app needs 8.6 or newer"
    fi
    case "$TK" in
        8.[6-9]*|9.*|1[0-9].*) OK=yes ;;
        *) OK=no ;;
    esac
    if [ "$OK" = no ]; then
        if [ "$FORCE" = yes ]; then
            echo "Warning: $PROBLEM - installing anyway (--force)." >&2
            echo "         The app will refuse to open until it gets a newer Tk." >&2
        else
            echo "Refusing: $PROBLEM." >&2
            echo "Install the app against a working Python, for example:" >&2
            echo "  PYTHON=/path/to/python3 $0" >&2
            echo "Or install this interpreter anyway with: $0 --force" >&2
            exit 1
        fi
    fi
    echo "Bundle interpreter: $PYTHON (Tk ${TK:-no tkinter})"
fi

mkdir -p "$BIN" "$DATA/icons"
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
# Finder hands the app a minimal PATH, where python3 is Apple's
# CommandLineTools build - its Tk 8.5 paints an empty window. Exec the
# interpreter install.sh verified instead, and keep a log so a failure leaves
# evidence rather than a bouncing Dock icon.
PY="$PYTHON"
[ -x "\$PY" ] || PY=\$(command -v python3)
LOG="\$HOME/Library/Logs/IPTV Channels.log"
mkdir -p "\${LOG%/*}"
exec "\$PY" "$BIN/iptv-channels.py" "\$@" >>"\$LOG" 2>&1
LAUNCH
chmod 755 "$APP/Contents/MacOS/launch"
printf 'APPL????' > "$APP/Contents/PkgInfo"
touch "$APP"
mkdir -p "$HOME/Library/Logs"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP" 2>/dev/null || true

echo "Installed: $APP"
echo "Open it from Applications, or: open \"$APP\""
echo "Run it from a terminal with: $BIN/iptv-channels.py"
