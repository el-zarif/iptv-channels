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
| `mpv_args` | `["--scale=ewa_lanczossharp", "--deband=yes"]` plus a `--vf=deblock` when the installed mpv has that filter | Extra mpv options, one per list item, handed over verbatim. An empty list turns the defaults off |
| `fsr` | `false` | `true` appends the bundled FSR shader to the mpv options. It sharpens, which helps a clean source and makes a heavily compressed one look more blocky; see below |

### Upscaling and sharpness

mpv runs a luma upscaler only when the video is smaller than the window
(bigger video is downscaled instead, and chroma is always resampled). A
channel that really is 1080p gets resampled to your display, and
`ewa_lanczossharp` reconstructs those edges better than mpv's default
`lanczos`. A channel the provider already upscaled to 2160p is drawn 1:1 on a
2160p display — the upscaler never runs, so a sharper scaler cannot help it;
only sharpening or a different tone mapping curve changes how it looks, and
neither recovers detail that was not there.

What makes an HD channel look pixelated on a 4K screen is the stream, not the
scaler. Measured on a live 1080p channel, the detail stopped at roughly 650
lines — the provider had already upscaled a smaller source — and the encoder's
block grid was stronger than in a 1.2 Mbit/s test encode, at 1.27 versus 1.09
on the 16-pixel grid. Magnifying that 2.1x magnifies the grid, and sharpening
afterwards raises its contrast further, so a sharper setting can look worse
than a plain one.

The default options therefore include a deblock before the upscale, added when
the installed mpv has the `deblock` filter — the app probes `--vf=help` once
and skips it where that fails, because mpv treats a `--vf` name it does not
know as a fatal option error. On that channel it cut the block-edge energy
from 1.13 to 0.85 with edge detail unchanged. A light denoise was measured on
the same frames first and changed nothing, which is why it is not the default.
For a noisy feed, `--vf=hqdn3d=2:2:6:6` in `mpv_args` is the knob.

None of this recovers the missing detail. A channel that carries 650 lines
will look like 650 lines however it is scaled, and the honest fixes are a
higher-quality variant of the channel or sitting further from the panel.

The app ships AMD's FSR 1.0.2 (`shaders/FSR.glsl`, MIT) but leaves it off
unless the config sets `"fsr": true`. Its EASU upscale measures about the same
as `ewa_lanczossharp` against a true 4K reference, so what it adds is RCAS
edge contrast — and on the same blocky feed that raised the visible block
edges by a tenth rather than lowering them. On a clean source it is a matter
of taste, which is what the option is for. When enabled it is appended rather
than substituted, so a shader list in `mpv.conf` survives, and it is looked up
in `~/.config/iptv-browser/shaders/`, then next to the script, then in the
data directory `install.sh` fills. An mpv version that cannot run a shader
simply ignores the option.

The bundled copy differs from upstream in one constant: RCAS sharpening is at
its maximum. The scale runs the other way, so `0.0` is the strongest setting
and every step above it halves the sharpening; upstream ships `0.2`, which is
a gentler starting point if the maximum is too much on a clean source.

The status bar names the upscaler the renderer actually ran (`· anime4k`,
`· fsrcnnx`, `· fsr` or `· ewa_lanczossharp`) and says nothing when the
picture is drawn at its own size or shrunk, so whether a channel is being
upscaled is read off the line instead of assumed. It comes from mpv's
per-pass report, which describes what happened rather than what was
configured, and it refreshes when the window changes size, so going
fullscreen shows the new answer within a couple of seconds. The dropped-frame
count starts once playback has settled, so the frames the renderer loses
while warming up are not reported as a stall.

```json
{ "mpv_args": ["--scale=ewa_lanczossharp", "--deband=yes", "--vf=deblock=filter=strong"] }
{ "fsr": true }
```

Useful additions: `--sharpen=0.5` for perceived acutance on soft feeds,
`--tone-mapping=bt.2446a` (or `spline`) when an HDR channel looks flat on an
SDR screen, `--vf=hqdn3d=2:2:6:6` for a noisy one, and `[]`
to compare against mpv's defaults.

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
