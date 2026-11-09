#!/usr/bin/env python3
"""IPTV Channel Browser - browse playlists and play the selected channel in
mpv. Nothing else: no built-in player, no decoding here.

Sources (in ~/.config/iptv-browser/sources.json):
  {"name": "...", "type": "m3u",    "url": "http://... or /path/list.m3u"}
  {"name": "...", "type": "xtream", "server": "http://host:port",
   "user": "...", "pass": "..."}

Xtream providers can list tens of thousands of channels, so those are loaded
per category on demand; plain M3U sources are loaded whole. Each source keeps
a cached copy so the list opens instantly and survives an outage.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

CONF_DIR = os.path.expanduser("~/.config/iptv-browser")
SOURCES_FILE = os.path.join(CONF_DIR, "sources.json")
CACHE_DIR = os.path.expanduser("~/.cache/iptv-browser")
SPEAKER = "alsa_output.platform-cht-bsw-rt5645.HiFi__Speaker__sink"
ENV = dict(os.environ, LIBVA_DRIVER_NAME="i965")
GROUP_ORDER = {"Live Events": 0, "PPV": 1, "TV": 2}
ALL_GROUPS = "All groups"
DEFAULT_SOURCES = [
    {"name": "Ace (local proxy)", "type": "m3u",
     "url": "http://127.0.0.1:8080/playlist-direct.m3u8"},
]


# ---------------------------------------------------------------- config
def normalise(s):
    if not isinstance(s, dict):
        return None
    if s.get("type") == "xtream":
        if s.get("server") and s.get("user") and s.get("pass"):
            return {"name": s.get("name") or "Xtream", "type": "xtream",
                    "server": s["server"].rstrip("/"), "user": s["user"],
                    "pass": s["pass"]}
        return None
    if s.get("url"):
        return {"name": s.get("name") or s["url"][:40], "type": "m3u",
                "url": s["url"]}
    return None


def load_sources():
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = [normalise(s) for s in data if normalise(s)]
        if out:
            return out
    except Exception:
        pass
    save_sources(DEFAULT_SOURCES)
    return list(DEFAULT_SOURCES)


def save_sources(sources):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(SOURCES_FILE, "w", encoding="utf-8") as f:
            json.dump(sources, f, indent=2)
    except OSError:
        pass


def cache_file(key):
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{h}.cache")


def cache_read(key):
    try:
        with open(cache_file(key), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def cache_write(key, text):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file(key), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


# ---------------------------------------------------------------- fetching
def http_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_text(url, timeout=60):
    """Return (text, fresh) with cache fallback."""
    try:
        text = http_text(url, timeout)
        cache_write(url, text)
        return text, True
    except Exception:
        cached = cache_read(url)
        return (cached, False) if cached is not None else ("", False)


def xtream_url(src, action="", extra=""):
    q = urllib.parse.urlencode({"username": src["user"],
                                "password": src["pass"]})
    if action:
        q += "&action=" + action
    if extra:
        q += "&" + extra
    return f"{src['server']}/player_api.php?{q}"


def xtream_json(src, action="", extra="", timeout=90):
    url = xtream_url(src, action, extra)
    key = "xtream:" + url
    try:
        text = http_text(url, timeout)
        cache_write(key, text)
        fresh = True
    except Exception:
        text = cache_read(key)
        fresh = False
        if text is None:
            return [], False
    try:
        data = json.loads(text)
    except ValueError:
        return [], False
    return (data if isinstance(data, list) else []), fresh


def xtream_categories(src):
    cats, fresh = xtream_json(src, "get_live_categories")
    out = []
    for c in cats:
        try:
            out.append((str(c["category_id"]), c.get("category_name") or "?"))
        except (KeyError, TypeError):
            continue
    return out, fresh


def xtream_streams(src, cat_id=None):
    extra = f"category_id={cat_id}" if cat_id else ""
    items, fresh = xtream_json(src, "get_live_streams", extra, timeout=180)
    out = []
    for s in items:
        try:
            sid = s["stream_id"]
            name = (s.get("name") or str(sid)).strip()
        except (KeyError, TypeError):
            continue
        url = f"{src['server']}/live/{src['user']}/{src['pass']}/{sid}.ts"
        out.append((cat_id and "" or "", name, "", url))
    return out, fresh


def parse_m3u(text):
    """Return [(group, name, health, url)] preserving playlist order."""
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            grp = ""
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                grp = m.group(1)
            name = line.rsplit(",", 1)[-1].strip()
            health = ""
            hm = re.search(r"\[A:([^\]]*)\]", name)
            if hm:
                health = hm.group(1).strip()
                name = re.sub(r"\s*\[A:[^\]]*\]", "", name).strip()
            url = ""
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    url = nxt
                    break
                j += 1
            if url:
                out.append((grp or "-", name, health, url))
            i = j
        i += 1
    return out


# ---------------------------------------------------------------- playback
def stop_mpv():
    subprocess.run(["pkill", "-x", "mpv"], capture_output=True)


def play(url):
    """One stream at a time: replace whatever mpv is playing."""
    stop_mpv()
    subprocess.run(["pactl", "set-default-sink", SPEAKER], capture_output=True)
    cmd = ["mpv", "--no-terminal", "--ontop", "--ytdl=no",
           "--network-timeout=15", "--loop-file=inf", url]
    subprocess.Popen(cmd, env=ENV, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     start_new_session=True)


# ---------------------------------------------------------------- UI
class Browser(Gtk.Window):
    def __init__(self):
        super().__init__(title="IPTV Channels")
        self.set_default_size(800, 660)
        self.set_icon_name("video-display")
        self.entries = []
        self.sources = load_sources()
        self.mode = "m3u"
        self.categories = []
        self.busy = False
        self.suppress = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        self.add(box)

        # source row
        src_bar = Gtk.Box(spacing=6)
        box.pack_start(src_bar, False, False, 0)
        src_bar.pack_start(Gtk.Label(label="Playlist:"), False, False, 0)
        self.source = Gtk.ComboBoxText()
        for s in self.sources:
            self.source.append_text(s["name"])
        self.source.set_active(0)
        self.source.connect("changed", self.on_source_changed)
        src_bar.pack_start(self.source, True, True, 0)

        add_btn = Gtk.Button(label="Add M3U...")
        add_btn.connect("clicked", self.on_add_m3u)
        src_bar.pack_start(add_btn, False, False, 0)

        add_x = Gtk.Button(label="Add Xtream...")
        add_x.connect("clicked", self.on_add_xtream)
        src_bar.pack_start(add_x, False, False, 0)

        del_btn = Gtk.Button(label="Remove")
        del_btn.connect("clicked", self.on_remove)
        src_bar.pack_start(del_btn, False, False, 0)

        reload_btn = Gtk.Button(label="Reload")
        reload_btn.connect("clicked", self.on_reload)
        src_bar.pack_start(reload_btn, False, False, 0)

        # filter row
        bar = Gtk.Box(spacing=6)
        box.pack_start(bar, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search channel...")
        self.search.connect("search-changed", lambda *_: self.refresh_view())
        bar.pack_start(self.search, True, True, 0)
        self.group = Gtk.ComboBoxText()
        self.group.append_text(ALL_GROUPS)
        self.group.set_active(0)
        self.group.connect("changed", self.on_group_changed)
        bar.pack_start(self.group, False, False, 0)

        # list
        self.store = Gtk.ListStore(str, str, str, str)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self.visible)
        view = Gtk.TreeView(model=self.filter)
        for idx, title in enumerate(("Group", "Channel", "Health")):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=idx)
            col.set_sort_column_id(idx)
            col.set_resizable(True)
            if idx == 0:
                col.set_fixed_width(150)
            elif idx == 2:
                col.set_fixed_width(150)
            view.append_column(col)
        view.connect("row-activated", self.on_activate)
        view.connect("cursor-changed", self.on_cursor)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)

        # bottom
        bottom = Gtk.Box(spacing=6)
        box.pack_start(bottom, False, False, 0)
        self.play_btn = Gtk.Button(label="Play in mpv")
        self.play_btn.connect("clicked", self.on_play_clicked)
        bottom.pack_start(self.play_btn, False, False, 0)
        self.status = Gtk.Label(label="Loading playlist...", xalign=0)
        self.status.set_ellipsize(3)
        bottom.pack_start(self.status, True, True, 0)

        self.view = view
        self.connect("destroy", Gtk.main_quit)
        self.load_current()

    # ---- sources --------------------------------------------------------
    def current_source(self):
        i = self.source.get_active()
        if 0 <= i < len(self.sources):
            return self.sources[i]
        return None

    def on_source_changed(self, _cb):
        self.load_current()

    def load_current(self):
        src = self.current_source()
        if not src or self.busy:
            return
        self.busy = True
        self.entries = []
        self.fill_store()
        name = src["name"]

        if src["type"] == "xtream":
            self.mode = "xtream"
            self.status.set_text(f"Loading categories from {name}...")

            def worker():
                cats, fresh = xtream_categories(src)
                GLib.idle_add(self.apply_categories, cats, fresh, name)

            threading.Thread(target=worker, daemon=True).start()
        else:
            self.mode = "m3u"
            self.status.set_text(f"Loading {name}...")

            def worker():
                text, fresh = fetch_text(src["url"])
                entries = parse_m3u(text)
                GLib.idle_add(self.apply_m3u, entries, fresh, name)

            threading.Thread(target=worker, daemon=True).start()

    def apply_m3u(self, entries, fresh, name):
        self.busy = False
        self.entries = entries
        groups = []
        for g, _n, _h, _u in entries:
            if g and g not in groups:
                groups.append(g)
        groups.sort(key=lambda g: (GROUP_ORDER.get(g, 99), g))
        self.suppress = True
        self.group.remove_all()
        self.group.append_text(ALL_GROUPS)
        for g in groups:
            self.group.append_text(g)
        self.group.set_active(0)
        self.suppress = False
        self.fill_store()
        src = "live" if fresh else "cached"
        self.status.set_text(f"{name}: {len(entries)} channels - {src}")
        return False

    def apply_categories(self, cats, fresh, name):
        self.busy = False
        self.categories = cats
        if not cats:
            self.status.set_text(f"{name}: no categories (check the account)")
            return False
        self.suppress = True
        self.group.remove_all()
        self.group.append_text(ALL_GROUPS)
        for _cid, cname in cats:
            self.group.append_text(cname)
        self.group.set_active(1)          # first category: keeps it light
        self.suppress = False
        src = "live" if fresh else "cached"
        self.status.set_text(f"{name}: {len(cats)} categories - {src}")
        self.load_category()
        return False

    def on_group_changed(self, _cb):
        if self.suppress:
            return
        if self.mode == "xtream":
            self.load_category()
        else:
            self.refresh_view()

    def load_category(self):
        src = self.current_source()
        if not src or src["type"] != "xtream" or self.busy:
            return
        idx = self.group.get_active() - 1     # 0 is "All groups"
        if idx < 0:
            cat_id, cat_name = None, ALL_GROUPS
        else:
            cat_id, cat_name = self.categories[idx]
        self.busy = True
        self.status.set_text(f"Loading {cat_name}...")

        def worker():
            entries, fresh = xtream_streams(src, cat_id)
            GLib.idle_add(self.apply_category, entries, fresh, cat_name, cat_id)

        threading.Thread(target=worker, daemon=True).start()

    def apply_category(self, entries, fresh, cat_name, cat_id):
        self.busy = False
        named = []
        for _g, name, health, url in entries:
            named.append((cat_name, name, health, url))
        self.entries = named
        self.fill_store()
        src = "live" if fresh else "cached"
        self.status.set_text(f"{cat_name}: {len(named)} channels - {src}")
        return False

    def fill_store(self):
        self.store.clear()
        for grp, name, health, url in self.entries:
            self.store.append([grp, name, health, url])
        self.refresh_view()

    # ---- add / remove ---------------------------------------------------
    def on_add_m3u(self, _btn):
        dlg = Gtk.Dialog(title="Add M3U playlist", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Add", Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        area.add(Gtk.Label(label="Name", xalign=0))
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("My IPTV")
        area.add(name_entry)
        area.add(Gtk.Label(label="M3U URL or file path", xalign=0))
        url_entry = Gtk.Entry()
        url_entry.set_placeholder_text("http://host/get.php?... or /path/list.m3u")
        area.add(url_entry)
        dlg.show_all()
        resp = dlg.run()
        name = name_entry.get_text().strip()
        url = url_entry.get_text().strip()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not url:
            return
        self.add_source({"name": name or url.rsplit("/", 1)[-1][:40],
                         "type": "m3u", "url": url})

    def on_add_xtream(self, _btn):
        dlg = Gtk.Dialog(title="Add Xtream Codes", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Add", Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        fields = {}
        for key, label, hint in (
                ("name", "Name", "My provider"),
                ("server", "Server address", "http://host:8080"),
                ("user", "Username", ""),
                ("pass", "Password", "")):
            area.add(Gtk.Label(label=label, xalign=0))
            e = Gtk.Entry()
            e.set_placeholder_text(hint)
            if key == "pass":
                e.set_visibility(False)
            area.add(e)
            fields[key] = e
        dlg.show_all()
        resp = dlg.run()
        vals = {k: e.get_text().strip() for k, e in fields.items()}
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not (vals["server"] and vals["user"] and vals["pass"]):
            return
        self.add_source({"name": vals["name"] or "Xtream",
                         "type": "xtream",
                         "server": vals["server"].rstrip("/"),
                         "user": vals["user"], "pass": vals["pass"]})

    def add_source(self, src):
        self.sources.append(src)
        save_sources(self.sources)
        self.source.append_text(src["name"])
        self.source.set_active(len(self.sources) - 1)

    def on_remove(self, _btn):
        i = self.source.get_active()
        if i < 0 or len(self.sources) <= 1:
            self.status.set_text("Keep at least one playlist")
            return
        name = self.sources[i]["name"]
        del self.sources[i]
        save_sources(self.sources)
        self.suppress = True
        self.source.remove(i)
        self.suppress = False
        self.source.set_active(0)
        self.status.set_text(f"Removed {name}")

    def on_reload(self, _btn):
        self.load_current()

    # ---- filtering ------------------------------------------------------
    def refresh_view(self):
        self.filter.refilter()

    def visible(self, model, it, _data):
        q = self.search.get_text().strip().lower()
        if q:
            hay = f"{model[it][0]} {model[it][1]}".lower()
            if q not in hay:
                return False
        return True

    # ---- actions --------------------------------------------------------
    def selected(self):
        model, it = self.view.get_selection().get_selected()
        if it is None:
            return None
        return (model[it][0], model[it][1], model[it][2], model[it][3])

    def on_cursor(self, _view):
        row = self.selected()
        if row:
            self.status.set_text(f"{row[0]}: {row[1]}")

    def on_activate(self, _view, _path, _col):
        self.start_playback()

    def on_play_clicked(self, _btn):
        self.start_playback()

    def start_playback(self):
        row = self.selected()
        if not row:
            self.status.set_text("Select a channel first")
            return
        grp, name, _health, url = row
        try:
            play(url)
            self.status.set_text(f"Playing: {name}  ({grp})")
        except Exception as e:
            self.status.set_text(f"Could not start mpv: {e}")


def main():
    if not os.environ.get("DISPLAY"):
        print("This needs a graphical session.", file=sys.stderr)
        return 1
    win = Browser()
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
