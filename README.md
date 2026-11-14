# IPTV Channels

A small channel browser that **only lists playlists** — it does no decoding
itself. Selecting a channel hands the stream to **mpv**, which gives hardware
decoding, proper seeking and E-AC3/HEVC support that browsers lack.

Runs on **Linux, macOS and Windows** from a bare Python 3 install: the window
is Tkinter, which ships with Python, so there is no GTK, PyGObject or build
step. mpv is the only external program.

## Features

- Loads several playlists side by side:
  - **M3U / M3U8** by URL or local file
  - **Xtream Codes** accounts (server / username / password), browsed by
    category because providers often expose tens of thousands of channels
- Search box, category dropdown, sortable columns
- Health column showing the `[A:xx% y.yM]` tag when a playlist provides it
- **Play in mpv** / double-click to watch; one player at a time
- The status bar reports what mpv is actually decoding — `3840x2160 59.94fps ·
  hw · 0 dropped` — so a name like "8K UHD" is not the only evidence
- Caches each playlist so the window opens instantly and survives an outage
- Remembers your sources in `~/.config/iptv-browser/sources.json`

## Requirements

- Python 3 with Tkinter (included in the python.org and most distro builds)
- `mpv`

**The Tk must be 8.6 or newer.** Apple's `/usr/bin/python3` ships Tk 8.5,
which paints an empty window; the app now says so instead of opening a grey
rectangle. `install.sh` bakes the interpreter it was run with into the macOS
bundle, so a Finder launch uses the good one.

Linux also needs `python3-tk` if the distro splits it out. `pactl`
(`pulseaudio-utils`) is optional — it is only used when `audio_sink` is
configured.

On Debian/Ubuntu:

```sh
sudo apt install python3-tk mpv
```

## Install

```sh
./install.sh
```

On macOS the script refuses to install if the Python on `PATH` has
Tk older than 8.6, because that combination opens an empty window; pass
`--force` to install it anyway, or point it at a better interpreter with
`PYTHON=/path/to/python3 ./install.sh`.

That copies `iptv-channels.py` to `~/.local/bin`, the icon assets to
`~/.local/share/iptv-channels/icons` and the bundled shader to
`~/.local/share/iptv-channels/shaders`. On Linux it also registers the
`iptv-channels` icon in the hicolor theme and adds a menu entry; on macOS it
builds `~/Applications/IPTV Channels.app`, which is what gives the app its
Dock icon and name.

On Windows there is nothing to install: run `pythonw iptv-channels.py` (no
console window). The `.ico` is picked up from `icons/` for the taskbar, and
the shader from `shaders/` beside the script — keep the folder with it, or
the upscaler is silently skipped.

## Icon

`icons/` holds the app icon in every size the three platforms ask for
(`icon.png` at 1024 plus 512/256/128/64/32/16, and a multi-size `icon.ico`).
It is drawn in code, so it is reproducible and has no licence attached:

```sh
pip install pillow          # build-time only, the app does not import it
python make-icons.py                  # outline (default)
python make-icons.py --style solid
python make-icons.py --style disc
```

Each size is drawn at that size rather than downscaled from one master, and
below 32 px the outline and solid glyphs reduce to a bare play triangle
because the TV frame turns into noise at that scale.

## Usage

1. Press **Add Xtream...** (server, username, password) or **Add M3U...**
   (a URL or a local file)
2. Pick the playlist in the **Playlist** dropdown
3. Choose a category, search if needed
4. Double-click a channel (or **Play in mpv**)

**Remove** deletes the selected playlist, **Reload** refreshes it.

## Optional settings

`~/.config/iptv-browser/sources.json` accepts a few keys next to `sources`:

| Key | Default | Meaning |
|---|---|---|
| `proxy` | `direct` | `env` honours `HTTP(S)_PROXY`; `direct` ignores it. Providers are usually local or on an edge host, where a shell proxy breaks them |
| `audio_sink` | unset | Linux only: passed to `pactl set-default-sink` before playback |
| `libva_driver` | unset | Linux only: sets `LIBVA_DRIVER_NAME` for mpv |
| `hwdec` | `auto-safe` | mpv `--hwdec` mode. `auto-safe` uses VideoToolbox / VAAPI / D3D11VA and falls back to software when the driver cannot do it; `no` forces software decoding |
| `theme` | `auto` | `auto` follows the desktop: macOS through the `aqua` theme, Windows through the registry, Linux through GNOME's `gsettings` (and `GTK_THEME`). `dark` and `light` force one look — on macOS that means leaving `aqua` for a hand-painted palette, which is darker than it is native |
| `mpv_args` | `["--scale=ewa_lanczossharp", "--deband=yes"]`, plus FSR when the bundled shader is found | Extra mpv options, one per list item, handed over verbatim. An empty list turns the defaults off |

### Upscaling and sharpness

mpv runs a luma upscaler only when the video is smaller than the window
(bigger video is downscaled instead, and chroma is always resampled). A
channel that really is 1080p gets resampled to your display, and
`ewa_lanczossharp` reconstructs those edges better than mpv's default
`lanczos`. A channel the provider already upscaled to 2160p is drawn 1:1 on a
2160p display — the upscaler never runs, so a sharper scaler cannot help it;
only sharpening or a different tone mapping curve changes how it looks, and
neither recovers detail that was not there.

The app ships AMD's FSR 1.0.2 (`shaders/FSR.glsl`, MIT) and adds it to the
default mpv options whenever the file is found: first in
`~/.config/iptv-browser/shaders/`, then next to the script, then in the data
directory `install.sh` fills. A `"mpv_args"` list in the config replaces those
defaults verbatim, shader included, so a channel wanting its own scaler
options is not stuck with FSR. FSR has its own trigger — it engages only when
the output is larger than the source — so it is silent on a 2160p feed and
does its work on a 1080p one. An mpv version that cannot run a shader simply
ignores it.

The bundled shader differs from upstream in one constant: RCAS sharpening is
at its maximum. The scale runs the other way, so `0.0` is the strongest
setting and every step above it halves the sharpening. What that buys is edge
contrast, not detail — EASU's upscaling measures about the same as
`ewa_lanczossharp` against a true 4K reference, so the visible change is the
sharpening. On a soft 1080p feed the maximum raises edge energy by roughly a
third for a small loss of fidelity and a little more noise in flat areas;
turning the shader's RCAS denoise off doubles both, which means sharpening
compression artifacts rather than picture. Set the sharpening constant back to
`0.2` for upstream's gentler default.

The status bar names the upscaler the renderer actually ran (`· fsr` or
`· ewa_lanczossharp`) and says nothing when the picture is drawn at its own
size or shrunk, so whether a channel is being upscaled is read off the line
instead of assumed. It comes from mpv's per-pass timings, which report what
happened rather than what was configured, and it refreshes when the window
changes size, so going fullscreen shows the new answer within a couple of
seconds.

```json
{ "mpv_args": ["--scale=ewa_lanczossharp", "--deband=yes"] }
```

Useful additions: `--sharpen=0.5` for perceived acutance on soft feeds,
`--tone-mapping=bt.2446a` (or `spline`) when an HDR channel looks flat on an
SDR screen, and `[]` to compare against mpv's defaults.

## Notes

- Playlists are cached in `~/.cache/iptv-browser/`.
- Credentials are stored locally in `~/.config/iptv-browser/sources.json`
  (mode 600).
- Channels are handed to mpv as `.../live/<user>/<pass>/<id>.ts`; some
  providers only serve `.m3u8`, which mpv also plays.
- The status bar readout comes from mpv's JSON IPC socket, which is created per
  playback in the temp directory. If it never appears, playback carries on and
  the readout simply stays blank.
- Channel names are marketing: providers label 1080p feeds "4K" and "8K UHD".
  The readout is the part that cannot lie.
- Some channels are genuinely dead (the swarm/edge serves nothing); the player
  simply reports the failure. Try another channel.

## License

MIT

### Text size

The interface scales with **Ctrl + / Ctrl -** (or Ctrl + mouse wheel);
**Ctrl+0** resets. The choice is saved as `font_size` in
`~/.config/iptv-browser/sources.json`, so you can also set it directly:

```json
{ "font_size": 20, "sources": [ ... ] }
```
