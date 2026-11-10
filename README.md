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
- Caches each playlist so the window opens instantly and survives an outage
- Remembers your sources in `~/.config/iptv-browser/sources.json`

## Requirements

- Python 3 with Tkinter (included in the python.org and most distro builds)
- `mpv`

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

That copies `iptv-channels.py` to `~/.local/bin` and, on Linux, adds a menu
entry (“IPTV Channels”). Start it from the menu or run `iptv-channels.py`.

On Windows and macOS there is nothing to install: run
`python iptv-channels.py` (or `pythonw iptv-channels.py` on Windows to skip
the console window).

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

## Notes

- Playlists are cached in `~/.cache/iptv-browser/`.
- Credentials are stored locally in `~/.config/iptv-browser/sources.json`
  (mode 600).
- Channels are handed to mpv as `.../live/<user>/<pass>/<id>.ts`; some
  providers only serve `.m3u8`, which mpv also plays.
- Some channels are genuinely dead (the swarm/edge serves nothing); the player
  simply reports the failure. Try another channel.

## License

MIT
