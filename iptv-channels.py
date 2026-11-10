#!/usr/bin/env python3
"""IPTV Channel Browser - browse playlists and play the selected channel in
mpv. Nothing else: no built-in player, no decoding here.

Runs on Linux, macOS and Windows from a bare Python 3 install: the interface
is Tkinter (stdlib), so there is no GTK/PyGObject to install and nothing to
build. mpv is the only external program needed.

Sources (in ~/.config/iptv-browser/sources.json):
  {"name": "...", "type": "m3u",    "url": "http://... or /path/list.m3u"}
  {"name": "...", "type": "xtream", "server": "http://host:port",
   "user": "...", "pass": "..."}

Optional keys in the same file:
  "proxy": "direct" | "env"   - default "direct"; "env" honours HTTP(S)_PROXY
  "audio_sink": "<pactl sink>" - Linux only, ignored where pactl is absent
  "libva_driver": "i965"       - Linux only, sets LIBVA_DRIVER_NAME for mpv

Xtream providers can list tens of thousands of channels, so those are loaded
per category on demand; plain M3U sources are loaded whole. Each source keeps
a cached copy so the list opens instantly and survives an outage.
"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk
from urllib.request import ProxyHandler, Request, build_opener

CONF_DIR = os.path.expanduser("~/.config/iptv-browser")
SOURCES_FILE = os.path.join(CONF_DIR, "sources.json")
CACHE_DIR = os.path.expanduser("~/.cache/iptv-browser")

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

USER_AGENT = "Mozilla/5.0"
GROUP_ORDER = {"Live Events": 0, "PPV": 1, "TV": 2}
ALL_GROUPS = "All groups"
MAX_ROWS = 3000
MIN_TK = (8, 6)
DEFAULT_SOURCES = [
    {"name": "Ace (local proxy)", "type": "m3u",
     "url": "http://127.0.0.1:8080/playlist-direct.m3u8"},
]


# ------------------------------------------------------------------ platform
def tk_too_old(root):
    """Tk 8.5 paints an empty window on current macOS, and Apple's
    CommandLineTools Python - the python3 a Finder launch resolves - ships
    8.5.9. Reporting that beats opening a grey rectangle."""
    try:
        parts = str(root.tk.call("info", "patchlevel")).split(".")
        return tuple(int(p) for p in parts[:2]) < MIN_TK
    except (tk.TclError, ValueError):
        return False


def icon_file(name):
    """Icon assets live next to the script (repo checkout) or in the data
    directory install.sh uses; either is fine, neither is required."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "icons", name), os.path.join(here, name),
                 os.path.expanduser("~/.local/share/iptv-channels/icons/"
                                    + name)):
        if os.path.exists(path):
            return path
    return None


def direct_opener():
    return build_opener(ProxyHandler({}))


def make_opener(cfg):
    """Providers are usually local or on an edge host; a shell proxy breaks
    them. "env" restores the old behaviour for networks that require one."""
    if (cfg or {}).get("proxy") == "env":
        return urllib.request.build_opener()
    return direct_opener()


def mpv_binary():
    found = shutil.which("mpv")
    if found:
        return found
    if IS_WIN:
        candidates = [r"C:\Program Files\mpv\mpv.exe",
                      r"C:\Program Files (x86)\mpv\mpv.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\mpv\mpv.exe")]
    elif IS_MAC:
        candidates = ["/opt/homebrew/bin/mpv", "/usr/local/bin/mpv",
                      "/Applications/mpv.app/Contents/MacOS/mpv"]
    else:
        candidates = ["/usr/bin/mpv", "/usr/local/bin/mpv", "/snap/bin/mpv"]
    for path in candidates:
        if os.path.exists(path):
            return path
    return "mpv"          # Popen reports the miss with a readable error


def player_env(cfg):
    env = dict(os.environ)
    if (cfg or {}).get("proxy") != "env":
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            env.pop(name, None)
    driver = (cfg or {}).get("libva_driver")
    if driver and IS_LINUX:
        env["LIBVA_DRIVER_NAME"] = driver
    return env


def prepare_audio(cfg):
    """Point PulseAudio/PipeWire at the configured sink, where that exists.
    Silent no-op on Windows, macOS and any box without pactl."""
    sink = (cfg or {}).get("audio_sink")
    if not sink or not IS_LINUX:
        return
    pactl = shutil.which("pactl")
    if not pactl:
        return
    try:
        subprocess.run([pactl, "set-default-sink", sink],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


# -------------------------------------------------------------------- config
def load_config():
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {"sources": data}   # old GTK format: a bare list
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        return {}


def load_sources():
    cfg = load_config()
    raw = cfg.get("sources")
    if not isinstance(raw, list):
        return [dict(s) for s in DEFAULT_SOURCES]
    out = [normalise(s) for s in raw]
    out = [s for s in out if s]
    return out or [dict(s) for s in DEFAULT_SOURCES]


def save_sources(sources):
    cfg = load_config()
    cfg["sources"] = sources
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = SOURCES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SOURCES_FILE)
    except OSError as exc:
        sys.stderr.write("could not save sources: %s\n" % exc)


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


# --------------------------------------------------------------------- cache
def cache_file(key):
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".cache")


def cache_read(key):
    try:
        with open(cache_file(key), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def cache_write(key, text):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = cache_file(key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


# ------------------------------------------------------------------ fetching
def http_text(url, timeout=60):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with make_opener(load_config()).open(req, timeout=timeout) as r:
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
    return "%s/player_api.php?%s" % (src["server"], q)


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
    extra = "category_id=%s" % cat_id if cat_id else ""
    items, fresh = xtream_json(src, "get_live_streams", extra, timeout=180)
    user = urllib.parse.quote(str(src["user"]), safe="")
    password = urllib.parse.quote(str(src["pass"]), safe="")
    out = []
    for s in items:
        try:
            sid = s["stream_id"]
            name = (s.get("name") or str(sid)).strip()
        except (KeyError, TypeError):
            continue
        url = "%s/live/%s/%s/%s.ts" % (src["server"], user, password, sid)
        out.append(("", name, "", url))
    return out, fresh


def resolve_url(url, base):
    """Resolve a stream URL found in an M3U against the playlist location."""
    if not base or "://" in url:
        return url
    if "://" not in base:
        # local file base: absolute paths pass through, relatives join
        if url.startswith("/") or url.startswith("\\") or (
                len(url) > 1 and url[1] == ":"):
            return url
        return os.path.join(os.path.dirname(os.path.expanduser(base)), url)
    return urllib.parse.urljoin(base, url)


def parse_m3u(text, base=""):
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
                out.append((grp or "-", name, health, resolve_url(url, base)))
            i = j
        i += 1
    return out


def read_m3u_source(src):
    url = src.get("url", "")
    if "://" not in url:
        with open(os.path.expanduser(url), "r", encoding="utf-8",
                  errors="replace") as f:
            text = f.read()
        cache_write(url, text)
        return text, True
    return fetch_text(url)


# ----------------------------------------------------------------- playback
def play(url, cfg):
    """One stream at a time: replace whatever we started last time."""
    prepare_audio(cfg)
    cmd = [mpv_binary(), "--no-terminal", "--force-window=yes", "--ontop",
           "--ytdl=no", "--network-timeout=15", "--loop-file=inf", url]
    kwargs = {}
    if IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, env=player_env(cfg),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, **kwargs)


def stop_player(player):
    if player is None or player.poll() is not None:
        return
    try:
        player.terminate()
    except OSError:
        pass


# ----------------------------------------------------------------------- UI
class FormDialog(tk.Toplevel):
    """Small modal form: fields = [(key, label, hint, secret)]."""

    def __init__(self, parent, title, fields, ok_label="Add"):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self.entries = {}
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        for row, (key, label, hint, secret) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row * 2, column=0,
                                             sticky="w", pady=(6, 0))
            entry = ttk.Entry(body, width=46,
                              show="*" if secret else "")
            entry.grid(row=row * 2 + 1, column=0, sticky="we")
            if hint:
                ttk.Label(body, text=hint, foreground="#777").grid(
                    row=row * 2 + 2, column=0, sticky="w")
            self.entries[key] = entry
        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields) * 3, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(
            side="right", padx=(6, 0))
        ttk.Button(buttons, text=ok_label, command=self._ok).pack(side="right")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        self.result = {k: e.get().strip() for k, e in self.entries.items()}
        self.destroy()


class Browser(tk.Tk):
    def __init__(self):
        super().__init__()
        if tk_too_old(self):
            self.fail_on_old_tk()
        self.title("IPTV Channels")
        self.geometry("900x660")
        self.minsize(680, 440)
        self.sources = load_sources()
        self.cfg = load_config()
        self.entries = []
        self.categories = []
        self.mode = "m3u"
        self.busy = False
        self.suppress = False
        self.player = None
        self.status_text = ""
        self.jobs = queue.Queue()
        self._sort = (None, False)
        self._icon = None
        self.set_window_icon()
        self._build()
        self.after(60, self._drain)
        self.refresh_sources()
        self.load_current()

    # ---- layout ---------------------------------------------------------
    def fail_on_old_tk(self):
        version = self.tk.call("info", "patchlevel")
        message = ("Tk %s cannot draw this window.\n\nStart the app with a "
                   "Python that has Tk 8.6 or newer: on macOS the system "
                   "Python ships Tk 8.5." % version)
        sys.stderr.write("iptv-channels: %s\n"
                         % message.replace("\n\n", " ").replace("\n", " "))
        self.destroy()
        # No Tk dialog: a Tk that cannot paint a frame cannot paint an alert
        # either, and messagebox blocks forever on the click that never comes.
        if IS_MAC and shutil.which("osascript"):
            alert = message.replace("\\", "\\\\").replace('"', '\\"')
            try:
                subprocess.run(["osascript", "-e",
                                'display alert "IPTV Channels" message "%s" '
                                'as critical' % alert.replace("\n\n", " ")],
                               timeout=15, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
        raise SystemExit(1)

    def set_window_icon(self):
        """Window and taskbar icon. Tk 8.6 reads PNG directly; the .ico file is
        what Windows Explorer and the taskbar pick up. On macOS the Dock icon
        comes from the .app bundle install.sh builds, not from here."""
        png = icon_file("icon-256.png") or icon_file("icon.png")
        if png:
            try:
                self._icon = tk.PhotoImage(file=png)     # must stay referenced
                self.iconphoto(True, self._icon)
            except tk.TclError:
                self._icon = None
        if IS_WIN:
            ico = icon_file("icon.ico")
            if ico:
                try:
                    self.iconbitmap(default=ico)
                except tk.TclError:
                    pass

    def _build(self):
        style = ttk.Style(self)
        if IS_MAC:
            style.theme_use("clam")
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Label(bar, text="Playlist:").pack(side="left")
        self.source_var = tk.StringVar()
        self.source = ttk.Combobox(bar, textvariable=self.source_var,
                                   state="readonly", width=34)
        self.source.pack(side="left", padx=(6, 6))
        self.source.bind("<<ComboboxSelected>>",
                         lambda _e: self.load_current())
        ttk.Button(bar, text="Add M3U...", command=self.on_add_m3u).pack(
            side="left")
        ttk.Button(bar, text="Add Xtream...", command=self.on_add_xtream).pack(
            side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self.on_remove).pack(side="left")
        ttk.Button(bar, text="Reload", command=self.load_current).pack(
            side="left", padx=4)

        filt = ttk.Frame(outer)
        filt.pack(fill="x", pady=(8, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self.refresh_view())
        search = ttk.Entry(filt, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True)
        ttk.Label(filt, text="  Group:").pack(side="left")
        self.group_var = tk.StringVar(value=ALL_GROUPS)
        self.group = ttk.Combobox(filt, textvariable=self.group_var,
                                  state="readonly", width=26,
                                  values=[ALL_GROUPS])
        self.group.pack(side="left", padx=(6, 0))
        self.group.bind("<<ComboboxSelected>>", self.on_group_changed)

        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("group", "channel", "health"),
                                 show="headings", selectmode="browse")
        for col, title, width in (("group", "Group", 170),
                                  ("channel", "Channel", 520),
                                  ("health", "Health", 130)):
            self.tree.heading(col, text=title,
                              command=lambda c=col: self.sort_by(c))
            self.tree.column(col, width=width, anchor="w",
                             stretch=(col == "channel"))
        self.tree.bind("<Double-1>", lambda _e: self.start_playback())
        self.tree.bind("<Return>", lambda _e: self.start_playback())
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        scroll = ttk.Scrollbar(table, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="Play in mpv",
                   command=self.start_playback).pack(side="left")
        self.status = ttk.Label(bottom, text="Loading playlist...",
                                anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=8)

    def set_status(self, text):
        self.status_text = text
        self.status.configure(text=text)

    # ---- thread marshalling --------------------------------------------
    def post(self, fn, *args):
        self.jobs.put((fn, args))

    def _drain(self):
        try:
            while True:
                fn, args = self.jobs.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(60, self._drain)

    # ---- sources --------------------------------------------------------
    def current_index(self):
        idx = self.source.current()
        if 0 <= idx < len(self.sources):
            return idx
        return None

    def current_source(self):
        idx = self.current_index()
        return self.sources[idx] if idx is not None else None

    def refresh_sources(self, select=None):
        names = [s["name"] for s in self.sources]
        self.source.configure(values=names)
        if names:
            idx = 0 if select is None else select
            self.source.current(max(0, min(idx, len(names) - 1)))
        else:
            self.source_var.set("")

    def load_current(self):
        idx = self.current_index()
        if idx is None or self.busy:
            return
        src = self.sources[idx]
        self.cfg = load_config()
        self.busy = True
        self.entries = []
        self.fill_store()
        name = src["name"]
        self.refresh_sources(idx)

        if src["type"] == "xtream":
            self.mode = "xtream"
            self.set_status("Loading categories from %s..." % name)

            def work():
                try:
                    cats, fresh = xtream_categories(src)
                except Exception as exc:
                    self.post(self.load_failed, name, exc)
                    return
                self.post(self.apply_categories, cats, fresh, name)

            threading.Thread(target=work, daemon=True).start()
        else:
            self.mode = "m3u"
            self.set_status("Loading %s..." % name)

            def work():
                try:
                    text, fresh = read_m3u_source(src)
                    entries = parse_m3u(text, src.get("url", ""))
                except Exception as exc:
                    self.post(self.load_failed, name, exc)
                    return
                self.post(self.apply_m3u, entries, fresh, name)

            threading.Thread(target=work, daemon=True).start()

    def load_failed(self, name, exc):
        self.busy = False
        self.set_status("Could not load %s: %s" % (name, exc))

    def apply_m3u(self, entries, fresh, name):
        self.busy = False
        self.entries = entries
        groups = []
        for g, _n, _h, _u in entries:
            if g and g not in groups:
                groups.append(g)
        groups.sort(key=lambda g: (GROUP_ORDER.get(g, 99), g))
        self.suppress = True
        self.group.configure(values=[ALL_GROUPS] + groups)
        self.group_var.set(ALL_GROUPS)
        self.suppress = False
        self.fill_store()
        self.set_status("%s: %d channels - %s" % (
            name, len(entries), "live" if fresh else "cached"))

    def apply_categories(self, cats, fresh, name):
        self.busy = False
        self.categories = cats
        if not cats:
            self.set_status("%s: no categories (check the account)" % name)
            return
        self.suppress = True
        self.group.configure(values=[ALL_GROUPS] + [c[1] for c in cats])
        self.group.current(1)          # first category: keeps it light
        self.suppress = False
        self.set_status("%s: %d categories - %s" % (
            name, len(cats), "live" if fresh else "cached"))
        self.load_category()

    def on_group_changed(self, _event=None):
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
        idx = self.group.current() - 1     # 0 is "All groups"
        if 0 <= idx < len(self.categories):
            cat_id, cat_name = self.categories[idx]
        else:
            cat_id, cat_name = None, ALL_GROUPS
        self.busy = True
        self.set_status("Loading %s..." % cat_name)

        def work():
            try:
                entries, fresh = xtream_streams(src, cat_id)
            except Exception as exc:
                self.post(self.load_failed, cat_name, exc)
                return
            self.post(self.apply_category, entries, fresh, cat_name)

        threading.Thread(target=work, daemon=True).start()

    def apply_category(self, entries, fresh, cat_name):
        self.busy = False
        self.entries = [(cat_name, n, h, u) for _g, n, h, u in entries]
        self.fill_store()
        self.set_status("%s: %d channels - %s" % (
            cat_name, len(self.entries), "live" if fresh else "cached"))

    # ---- table ----------------------------------------------------------
    def fill_store(self):
        self.refresh_view()
        self.set_status(self.status_text)

    def visible_entries(self):
        query = self.search_var.get().strip().lower()
        group = self.group_var.get() if self.mode == "m3u" else None
        rows = []
        for row in self.entries:
            if group and group != ALL_GROUPS and row[0] != group:
                continue
            if query and query not in ("%s %s" % (row[0], row[1])).lower():
                continue
            rows.append(row)
        return rows

    def refresh_view(self):
        rows = self.visible_entries()
        if self._sort[0]:
            idx = {"group": 0, "channel": 1, "health": 2}[self._sort[0]]
            rows = sorted(rows, key=lambda r: r[idx].lower(),
                          reverse=self._sort[1])
        self.tree.delete(*self.tree.get_children())
        for row in rows[:MAX_ROWS]:
            self.tree.insert("", "end", values=row)
        shown = min(len(rows), MAX_ROWS)
        if len(rows) > MAX_ROWS:
            self.set_status("%d matches - showing the first %d, narrow the "
                            "search" % (len(rows), MAX_ROWS))
        elif rows:
            self.set_status("%d channel%s shown" % (
                shown, "" if shown == 1 else "s"))
        else:
            self.set_status("No channels match")

    def sort_by(self, column):
        current, desc = self._sort
        self._sort = (column, not desc if current == column else False)
        self.refresh_view()

    def selected(self):
        item = self.tree.focus()
        if not item:
            return None
        return self.tree.item(item, "values") or None

    def on_select(self, _event=None):
        row = self.selected()
        if row:
            self.set_status("%s: %s" % (row[0], row[1]))

    # ---- dialogs --------------------------------------------------------
    def on_add_m3u(self):
        dlg = FormDialog(self, "Add M3U playlist", [
            ("name", "Name", "My IPTV", False),
            ("url", "M3U URL or file path",
             "http://host/get.php?... or /path/list.m3u", False),
        ])
        if not dlg.result:
            return
        url = dlg.result.get("url", "")
        if not url:
            return
        name = dlg.result.get("name") or url.rsplit("/", 1)[-1][:40]
        self.add_source({"name": name, "type": "m3u", "url": url})

    def on_add_xtream(self):
        dlg = FormDialog(self, "Add Xtream Codes", [
            ("name", "Name", "My provider", False),
            ("server", "Server address", "http://host:8080", False),
            ("user", "Username", "", False),
            ("pass", "Password", "", True),
        ])
        if not dlg.result:
            return
        vals = dlg.result
        if not (vals.get("server") and vals.get("user") and vals.get("pass")):
            messagebox.showwarning("Xtream Codes",
                                   "Server, username and password are required.")
            return
        self.add_source({"name": vals.get("name") or "Xtream",
                         "type": "xtream",
                         "server": vals["server"].rstrip("/"),
                         "user": vals["user"], "pass": vals["pass"]})

    def add_source(self, src):
        self.sources.append(src)
        save_sources(self.sources)
        self.refresh_sources(len(self.sources) - 1)
        self.load_current()

    def on_remove(self):
        if len(self.sources) <= 1:
            self.set_status("Keep at least one playlist")
            return
        idx = self.current_index()
        if idx is None:
            return
        src = self.sources.pop(idx)
        save_sources(self.sources)
        self.refresh_sources()
        self.entries = []
        self.fill_store()
        self.load_current()
        self.set_status("Removed %s" % src["name"])

    # ---- playback -------------------------------------------------------
    def start_playback(self):
        row = self.selected()
        if not row:
            self.set_status("Select a channel first")
            return
        grp, name, _health, url = row
        self.cfg = load_config()
        try:
            stop_player(self.player)
            self.player = play(url, self.cfg)
        except OSError as exc:
            self.set_status("Could not start mpv: %s" % exc)
            return
        self.set_status("Playing: %s  (%s)" % (name, grp))

    def shutdown(self):
        stop_player(self.player)
        self.destroy()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if IS_LINUX and not (os.environ.get("DISPLAY")
                         or os.environ.get("WAYLAND_DISPLAY")):
        print("This needs a graphical session.", file=sys.stderr)
        return 1
    app = Browser()
    if "--smoke" in argv:
        captured = {}

        def snap():
            captured["status"] = app.status_text
            captured["rows"] = len(app.tree.get_children())
            captured["groups"] = len(app.group.cget("values"))
            app.destroy()

        app.after(6000, snap)
        app.mainloop()
        print("smoke status: %s" % captured.get("status"))
        print("smoke rows: %d" % captured.get("rows", 0))
        print("smoke group choices: %d" % captured.get("groups", 0))
        return 0
    app.protocol("WM_DELETE_WINDOW", app.shutdown)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
