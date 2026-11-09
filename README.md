# IPTV Channels

A small GTK3 channel browser that **only lists playlists** — it does no
decoding itself. Selecting a channel hands the stream to **mpv**, which gives
hardware decoding, proper seeking and E-AC3/HEVC support that browsers lack.

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

- Python 3 with GTK3 (`python3-gi`, `gir1.2-gtk-3.0`)
- `mpv`
- `pulseaudio-utils` (`pactl`) — used to make sure the default audio sink is a
  real output before playback

On Debian/Ubuntu:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 mpv pulseaudio-utils
```

## Install

```sh
./install.sh
```

That copies `iptv-channels.py` to `~/.local/bin` and adds a menu entry
(“IPTV Channels”). Start it from the menu or run `iptv-channels.py`.

## Usage

1. Press **Add...** → *Xtream Codes* (server, username, password) or
   *M3U / M3U8 URL* (a URL or a local file)
2. Pick the playlist in the **Playlist** dropdown
3. Choose a category, search if needed
4. Double-click a channel (or **Play in mpv**)

**Edit...** changes an existing entry in place, **Remove** deletes it,
**Reload** refreshes the current playlist.

## Notes

- Playlists are cached in `~/.cache/iptv-browser/`.
- Credentials are stored locally in `~/.config/iptv-browser/sources.json`.
- If a provider requires a `Referer` or blocks non-browser user agents, add it
  to the playlist URL or use the provider's M3U export.
- Some channels are genuinely dead (the swarm/edge serves nothing); the player
  simply reports the failure. Try another channel.

## License

MIT
