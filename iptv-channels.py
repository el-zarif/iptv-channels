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
  "hwdec": "auto-safe"         - mpv --hwdec mode; "no" forces software
                                 decoding
  "mpv_args": ["--scale=..."] - extra mpv options, one per list item; a
                                 missing key uses the defaults, [] turns them off
  "fsr": true                  - appends the bundled FSR shader (off by default)
  "theme": "auto"              - "auto" follows the desktop where Tk can tell
                                 (macOS aqua, Windows registry, GNOME
                                 gsettings); "dark" and "light" force it

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
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk
from urllib.request import ProxyHandler, Request, build_opener

CONF_DIR = os.path.expanduser("~/.config/iptv-browser")
SOURCES_FILE = os.path.join(CONF_DIR, "sources.json")
CACHE_DIR = os.path.expanduser("~/.cache/iptv-browser")
DATA_DIR = os.path.expanduser("~/.local/share/iptv-channels")

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

USER_AGENT = "Mozilla/5.0"
GROUP_ORDER = {"Live Events": 0, "PPV": 1, "TV": 2}
ALL_GROUPS = "All groups"
SCOPE_ONE = "This playlist"
SCOPE_ALL = "All playlists"
MAX_ROWS = 3000
MIN_TK = (8, 6)
# What mpv gets on top of the flags play() always passes. ewa_lanczossharp
# resamples 1080p better than mpv's default lanczos, which is the case that
# matters here: providers label upscaled 1080p feeds "4K" and a few thousand
# channels are honestly 1080p. deband removes the stair-stepping that
# low-bitrate H.265 leaves in gradients. Measured at 4K 50 fps on an M4 Max:
# 50.0 fps sustained once the renderer spins up; only the first few frames
# drop.
DEFAULT_MPV_ARGS = ("--scale=ewa_lanczossharp", "--deband=yes")
# A deblock is added to the above when the mpv binary has the filter (see
# default_mpv_args): IPTV channels carry a grid of encoder block edges, and a
# scaler turns it into visible squares. Measured on a live 1080p channel that
# is itself a provider upscale, the filter cut block-edge energy at the 16
# pixel grid from 1.134 to 0.850 with edge detail unchanged, while a light
# denoise changed nothing at all on the same frames.
# Shipped with the app and used only when the config sets "fsr": true. AMD's
# FSR 1.0.2, which engages only when the window is bigger than the video (its
# own WHEN condition), so a 2160p feed is left alone. RCAS is at its maximum
# in this copy, which helps a clean source and hurts a compressed one. A local
# copy in the config directory wins, so it can be swapped without touching the
# install.
# Shader chains, by the name the config asks for. "anime4k" is the default
# pair: Restore removes the encoder's block edges, Upscale does the 2x with a
# network instead of a kernel. Measured on a live 1080p channel that was
# itself a provider upscale, the pair held the block grid neutral while
# roughly tripling edge energy, at 25 fps with no dropped frames on an M4 Max.
# Both files are MIT with their headers intact. "fsr" is the single AMD
# upscaler, which sharpens rather than reconstructs; "none" leaves mpv's
# scaler alone.
SHADER_CHAINS = {
    "anime4k": ("Anime4K_Restore_CNN_M.glsl",
                "Anime4K_Upscale_CNN_x2_M.glsl"),
    "fsr": ("FSR.glsl",),
    "none": (),
}
SHADER_NAME = "FSR.glsl"
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


# Result of mpv_has_vf per binary: probing costs a cold mpv start (~1 s), so
# run it once per process and keep the set of names the help lists.
_MPV_VF_CACHE = {}
_MPV_VF_HELP_TIMEOUT = 10


def mpv_has_vf(name):
    """True when the mpv binary lists the video filter `name` in --vf=help.
    Used for filters that are missing from some builds: an unknown --vf value
    is a fatal parse error in mpv (exits at once, nothing plays), so a
    feature-detected default is the difference between a working app and one
    that refuses to start on a filter-less mpv. Fails closed on anything but a
    successful probe, including a binary that does not run at all."""
    if not name:
        return False
    binary = mpv_binary()
    names = _MPV_VF_CACHE.get(binary)
    if names is None:
        try:
            out = subprocess.run([binary, "--vf=help"], capture_output=True,
                                 timeout=_MPV_VF_HELP_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            names = frozenset()
        else:
            if out.returncode != 0:
                names = frozenset()
            else:
                text = out.stdout.decode("utf-8", "replace")
                names = frozenset(line.split()[0] for line in text.splitlines()
                                  if line[:1] in (" ", "\t") and
                                  line.split())
        _MPV_VF_CACHE[binary] = names
    return name in names


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


def save_config(cfg):
    merged = load_config()
    merged.update(cfg)
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = SOURCES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SOURCES_FILE)
    except OSError as exc:
        sys.stderr.write("could not save config: %s\n" % exc)


def server_url(server):
    """Xtream providers hand out "host:port" as often as a full URL, and
    "host:8080" parses as a URL whose scheme is the host, so every request
    fails. Give it a scheme and drop the trailing slash, which otherwise
    turns into a doubled path."""
    server = (server or "").strip().rstrip("/")
    if server and "://" not in server:
        server = "http://" + server
    return server


def normalise(s):
    if not isinstance(s, dict):
        return None
    if s.get("type") == "xtream":
        if s.get("server") and s.get("user") and s.get("pass"):
            return {"name": s.get("name") or "Xtream", "type": "xtream",
                    "server": server_url(s["server"]), "user": s["user"],
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
def hwdec_mode(cfg):
    """Hardware decode where the platform has it: "auto-safe" lets mpv pick
    VideoToolbox, VAAPI or D3D11VA and fall back to software when the driver
    cannot do it. mpv's own default is "no", which is why a 4K channel used to
    run a whole CPU core at 120%."""
    return (cfg or {}).get("hwdec") or "auto-safe"


def shader_dirs():
    """Where a shipped shader can live. The config directory comes first so a
    hand-installed copy is never shadowed; then next to the script, which is
    how a checkout runs on Windows where there is no install step; then the
    data directory install.sh fills."""
    here = os.path.dirname(os.path.abspath(__file__))
    return (os.path.join(CONF_DIR, "shaders"),
            os.path.join(here, "shaders"),
            os.path.join(DATA_DIR, "shaders"))


def find_shader(name=SHADER_NAME):
    for directory in shader_dirs():
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return ""


def default_mpv_args(cfg=None):
    """DEFAULT_MPV_ARGS plus a deblock when the mpv binary has the filter, and
    the bundled shader when the config asks for it. The filter is probed first
    because an mpv that lacks it treats --vf=deblock as a fatal option error
    and exits before playing anything, so the argument is added only when the
    binary itself lists it. It costs one cold mpv start, cached for the rest of
    the process.

    mpv is not told where shaders live: a missing file only prints to stdout
    and stderr that play() discards, and playback carries on, so the argument
    is added only for a file that exists.

    The shader chain is chosen by the "shaders" key and defaults to anime4k:
    a learned upscaler reconstructs the edges a kernel smears, and the pair
    measured better than anything else tried on the kind of channel this app
    plays. "fsr" is the single AMD upscaler, "none" leaves mpv's scaler, and a
    shader that is missing is skipped rather than passed to mpv."""
    args = list(DEFAULT_MPV_ARGS)
    if mpv_has_vf("deblock"):
        args.append("--vf=deblock=filter=strong")
    mode = (cfg or {}).get("shaders")
    if mode is None:
        # "fsr": true in an older config still means what it says.
        mode = "fsr" if (cfg or {}).get("fsr") is True else "anime4k"
    for name in SHADER_CHAINS.get(mode, ()):
        shader = find_shader(name)
        if shader:
            # append, not replace: --glsl-shaders= would drop any shader the
            # user listed in mpv.conf, and the app is a guest in that file.
            args.append("--glsl-shaders-append=" + shader)
    return args


def mpv_args(cfg):
    """"mpv_args" from the config: extra options handed to mpv verbatim, so a
    better scaler or a tone mapping curve is a config change and not a patch.
    A missing key means the built-in defaults, an empty list means none — the
    distinction lets someone who dislikes the defaults switch them off."""
    raw = (cfg or {}).get("mpv_args")
    if raw is None:
        return default_mpv_args(cfg)
    if not isinstance(raw, (list, tuple)):
        return default_mpv_args(cfg)
    return [str(a) for a in raw if str(a).strip()]


def ipc_path(seq):
    """Where mpv should listen for JSON commands. POSIX gets a unix socket in
    the temp directory, Windows a named pipe; the pid keeps two running copies
    of the app apart."""
    name = "iptv-channels-%d-%d" % (os.getpid(), seq)
    if IS_WIN:
        return "\\\\.\\pipe\\" + name
    path = os.path.join(tempfile.gettempdir(), name + ".sock")
    if len(os.fsencode(path)) > 100:      # sun_path is 104 bytes on macOS
        path = os.path.join("/tmp", name + ".sock")
    return path


class IpcStream:
    """A line-oriented handle on mpv's JSON IPC: a unix socket on POSIX, a
    named pipe on Windows."""

    def __init__(self, path, timeout=10.0):
        self.sock = None
        self.request_id = 0
        if IS_WIN:
            # Windows has no AF_UNIX; Python can still open a pipe by name.
            self.file = open(path, "r+b", buffering=0)
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            try:
                self.sock.connect(path)
            except OSError:
                self.sock.close()
                raise
            self.file = self.sock.makefile("rwb")

    def command(self, *args):
        self.request_id += 1
        mine = self.request_id
        payload = json.dumps({"command": list(args),
                              "request_id": mine}).encode("utf-8") + b"\n"
        self.file.write(payload)
        self.file.flush()
        while True:
            line = self.file.readline()
            if not line:
                raise OSError("mpv closed the IPC socket")
            reply = json.loads(line.decode("utf-8", "replace"))
            # mpv pushes events (file-loaded, video-reconfig, ...) to every
            # client, so the next line is not necessarily our reply.
            if reply.get("request_id") == mine:
                return reply

    def close(self):
        try:
            self.file.close()
        except OSError:
            pass
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def format_stream_info(width, height, dropped, fps, scaler="",
                       out_width=0, out_height=0):
    """One line for the status bar: the picture that arrived, what it was
    turned into, and by what. The arrow is the scale, so it appears only when
    the two sizes differ; "scaler" names the pass the renderer ran (see
    scaler_from_passes) so an upscale is visible rather than assumed."""
    if not width or not height:
        return None
    line = "%dx%d" % (width, height)
    if fps:
        line += " %sfps" % ("%.2f" % fps).rstrip("0").rstrip(".")
    if out_width and out_height and (out_width, out_height) != (width, height):
        line += " → %dx%d" % (out_width, out_height)
    if scaler:
        line += " · " + scaler
    line += " · %d dropped" % (dropped or 0)
    return line


def scaler_from_passes(passes, width, height, out_width, out_height):
    """Which upscaler the renderer actually ran, read from "vo-passes" instead
    of guessed: mpv describes every pass, FSR as its own Super Resolution
    entry, the kernel as "polar upscaling (<name>)". The kernel pass is listed
    even when nothing is scaled, so the scale factor has to decide first
    whether there was anything to upscale."""
    if not (width and height and out_width and out_height):
        return ""
    # out_* is the OSD/window size, not the picture rect: a 2.39:1 film in a
    # 16:9 window fills the width and leaves bars, and the smaller of the two
    # axis ratios is the fit scale mpv itself used.
    if min(float(out_width) / width, float(out_height) / height) <= 1.0:
        return ""                    # native size or shrunk: no upscaler ran
    if not isinstance(passes, dict):
        return ""
    descriptions = []
    for group in passes.values():
        for item in (group if isinstance(group, list) else []):
            if isinstance(item, dict) and item.get("desc"):
                descriptions.append(str(item["desc"]))
    for desc in descriptions:
        if "Super Resolution" in desc:
            return "fsr"
    # A learned upscaler names its passes after itself, not after a kernel, so
    # it would otherwise be invisible here and the bar would credit the chroma
    # scaler for work the network did.
    for desc in descriptions:
        if "Anime4K" in desc:
            return "anime4k"
    for desc in descriptions:
        if "feature map" in desc or "sub-pixel convolution" in desc:
            return "fsrcnnx"
    for desc in descriptions:
        if "upscaling (" in desc:
            return desc.split("upscaling (", 1)[1].split(")", 1)[0].strip()
    return ""


class StreamWatcher:
    """Polls mpv for resolution, decoder and dropped frames, and hands one
    summary line to the window. Optional by design: if the socket never
    appears, or mpv says something unexpected, the thread ends quietly and
    playback is untouched."""

    POLL = 2.0
    # The first samples land while the renderer is still warming up, and those
    # frames are gone for good; counting from the sample where playback has
    # settled keeps the number about stalls instead of about startup.
    SETTLE = 3
    # "vo-passes" carries per-pass timings and descriptions: a few kB, so it is
    # sampled on a resize and every ten seconds otherwise, which catches a
    # renderer change that did not come with one.
    PASS_EVERY = 5
    PROPS = ("video-params/w", "video-params/h", "frame-drop-count",
             "container-fps")

    def __init__(self, path, post, show):
        self.path = path
        self.post = post
        self.show = show
        self.stopped = threading.Event()
        self.scaler = ""
        self.window = (0, 0)
        self.settled = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stopped.set()
        if not IS_WIN:
            try:
                os.unlink(self.path)     # a killed mpv leaves the socket
            except OSError:
                pass

    def _run(self):
        stream = None
        tick = 0
        try:
            stream = self._connect()
            while stream is not None and not self.stopped.is_set():
                values = []
                for prop in self.PROPS:
                    values.append(stream.command("get_property",
                                                 prop).get("data"))
                out_w = stream.command("get_property",
                                       "osd-dimensions/w").get("data")
                out_h = stream.command("get_property",
                                       "osd-dimensions/h").get("data")
                # Refetch when the window changed size too: going fullscreen is
                # how a channel starts being upscaled, and waiting for the next
                # periodic sample would leave the old answer on the bar while
                # the picture is already bigger.
                if (out_w, out_h) != self.window or tick % self.PASS_EVERY == 0:
                    self.window = (out_w, out_h)
                    self.scaler = scaler_from_passes(
                        stream.command("get_property",
                                       "vo-passes").get("data"),
                        values[0], values[1], out_w, out_h)
                if self.settled is None and tick >= self.SETTLE and values[0]:
                    self.settled = values[2] or 0
                dropped = values[2]
                if self.settled is not None:
                    if dropped is not None:
                        dropped = max(0, dropped - self.settled)
                else:
                    dropped = 0         # still warming up: nothing to report yet
                tick += 1
                text = format_stream_info(values[0], values[1], dropped,
                                          values[3], self.scaler,
                                          out_w, out_h)
                if text and not self.stopped.is_set():
                    self.post(self.show, text)
                self.stopped.wait(self.POLL)
        except Exception:
            pass             # mpv exited, or the socket is not mpv's
        finally:
            if stream is not None:
                stream.close()
            if not self.stopped.is_set():
                self.post(self.show, "")   # mpv is gone: drop the readout

    def _connect(self):
        deadline = time.time() + 10.0
        while not self.stopped.is_set() and time.time() < deadline:
            try:
                return IpcStream(self.path)
            except OSError:
                self.stopped.wait(0.25)
        return None


def play(url, cfg, ipc=""):
    """One stream at a time: replace whatever we started last time."""
    prepare_audio(cfg)
    cmd = [mpv_binary(), "--no-terminal", "--force-window=yes", "--ontop",
           "--ytdl=no", "--network-timeout=15", "--loop-file=inf",
           "--hwdec=" + hwdec_mode(cfg)]
    cmd.extend(mpv_args(cfg))
    if ipc:
        cmd.append("--input-ipc-server=" + ipc)
    cmd.append(url)
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


# --------------------------------------------------------------------- theme
def appearance(cfg):
    """The "theme" key, normalised: "auto", "dark" or "light"."""
    choice = str((cfg or {}).get("theme") or "auto").lower()
    return choice if choice in ("dark", "light") else "auto"


def _gsettings(key):
    exe = shutil.which("gsettings")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "get", "org.gnome.desktop.interface", key],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.decode("utf-8", "replace").strip().strip("'")


def system_wants_dark():
    """Tk has no appearance API, so "auto" is answered by hand: the Windows
    registry and GNOME's gsettings both publish a preference. Anything unknown
    counts as light, which is what Tk falls back to everywhere."""
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion"
                    r"\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False
    if IS_LINUX:
        if "dark" in os.environ.get("GTK_THEME", "").lower():
            return True
        return (_gsettings("color-scheme") == "prefer-dark"
                or "dark" in _gsettings("gtk-theme").lower())
    return False


def paint_dark(root, style):
    """clam is drawn by Tk rather than by the desktop, so these colours are
    what every platform shows. ttk themes ignore global colour settings by
    design, hence one configure per widget class."""
    bg, alt, fg = "#1e2023", "#2a2d31", "#e6e6e6"
    field, edge, sel, accent = "#151618", "#3a3d42", "#3a4150", "#4a90d9"
    root.configure(background=bg)
    for name in ("TFrame", "TLabelframe", "TLabelframe.Label", "TLabel"):
        style.configure(name, background=bg, foreground=fg)
    style.configure("TButton", background=alt, foreground=fg,
                    bordercolor=edge, lightcolor=alt, darkcolor=alt,
                    focuscolor=accent, relief="flat")
    style.map("TButton",
              background=[("pressed", "#3a3f45"), ("active", "#33373c")],
              foreground=[("disabled", "#6b6f75")])
    style.configure("TEntry", fieldbackground=field, foreground=fg,
                    insertcolor=fg, bordercolor=edge, lightcolor=edge,
                    darkcolor=edge)
    style.map("TEntry", fieldbackground=[("disabled", bg)])
    style.configure("TCombobox", fieldbackground=field, background=alt,
                    foreground=fg, arrowcolor=fg, bordercolor=edge,
                    lightcolor=edge, darkcolor=edge)
    style.map("TCombobox",
              fieldbackground=[("readonly", field)],
              foreground=[("readonly", fg)],
              selectbackground=[("readonly", field)],
              selectforeground=[("readonly", fg)])
    style.configure("Treeview", background=bg, fieldbackground=bg,
                    foreground=fg, bordercolor=alt, lightcolor=bg,
                    darkcolor=bg)
    style.map("Treeview", background=[("selected", sel)],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=alt, foreground=fg,
                    bordercolor=edge, relief="flat")
    style.map("Treeview.Heading", background=[("active", "#33373c")])
    # clam defines the oriented names, which win over the base class.
    for name in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=alt, troughcolor=bg, bordercolor=bg,
                        lightcolor=alt, darkcolor=alt, arrowcolor=fg,
                        relief="flat")
        style.map(name, background=[("active", "#3a3f45")])
    # The combobox popdown is a plain Tk listbox, not a ttk widget.
    for pattern in ("*TCombobox*Listbox", "*Listbox"):
        root.option_add(pattern + ".background", bg)
        root.option_add(pattern + ".foreground", fg)
        root.option_add(pattern + ".selectBackground", accent)
        root.option_add(pattern + ".selectForeground", "#ffffff")


def apply_theme(root, cfg):
    """macOS is the one platform where Tk follows the desktop on its own: the
    aqua theme resolves system colours, so switching to clam there is exactly
    what turned dark mode off. Windows' vista theme has no dark variant and a
    Linux Tk does not read the GTK theme, so those get clam plus a palette."""
    style = ttk.Style(root)
    mode = appearance(cfg)
    if IS_MAC and mode == "auto":
        style.theme_use("aqua")
        style.configure("Hint.TLabel", foreground="#777")
        return
    style.theme_use("clam")
    if mode == "dark" or (mode == "auto" and system_wants_dark()):
        paint_dark(root, style)
        style.configure("Hint.TLabel", foreground="#9aa0a6")
    else:
        style.configure("Hint.TLabel", foreground="#777")


# ----------------------------------------------------------------------- UI
class FormDialog(tk.Toplevel):
    """Small modal form: fields = [(key, label, hint, secret)]. `initial`
    pre-fills the entries so the same dialog adds and edits, and
    `delete_label` puts a destructive button next to Cancel that only sets
    `deleted` — the caller decides what deleting means."""

    def __init__(self, parent, title, fields, ok_label="Add", initial=None,
                 delete_label=None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self.deleted = False
        self.entries = {}
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        for row, (key, label, hint, secret) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row * 2, column=0,
                                             sticky="w", pady=(6, 0))
            entry = ttk.Entry(body, width=46,
                              show="*" if secret else "")
            entry.grid(row=row * 2 + 1, column=0, sticky="we")
            value = (initial or {}).get(key, "")
            if value:
                entry.insert(0, str(value))
            if hint:
                ttk.Label(body, text=hint, style="Hint.TLabel").grid(
                    row=row * 2 + 2, column=0, sticky="w")
            self.entries[key] = entry
        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields) * 3, column=0, sticky="we", pady=(12, 0))
        if delete_label:
            ttk.Button(buttons, text=delete_label,
                       command=self._delete).pack(side="left")
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

    def _delete(self):
        self.deleted = True
        self.destroy()


class ChoiceDialog(tk.Toplevel):
    """Modal question with one button per choice; `result` is the chosen key
    or None. For forks in the flow, where a form field would be a lie."""

    def __init__(self, parent, title, choices):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        for key, label in choices:
            ttk.Button(body, text=label, width=32,
                       command=lambda k=key: self._pick(k)).pack(pady=3)
        ttk.Button(body, text="Cancel", command=self.destroy).pack(
            pady=(10, 0))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _pick(self, key):
        self.result = key
        self.destroy()


class Browser(tk.Tk):
    def __init__(self):
        super().__init__()
        if tk_too_old(self):
            self.fail_on_old_tk()
        self.title("IPTV Channels")
        self.geometry("1020x720")
        self.minsize(760, 500)
        self.sources = load_sources()
        self.cfg = load_config()
        try:
            self.font_size = max(8, min(40, int(self.cfg.get("font_size", 14))))
        except (TypeError, ValueError):
            self.font_size = 14
        self.entries = []
        self.categories = []
        self.mode = "m3u"
        self.all_mode = False
        self.pending = False
        self.busy = False
        self.suppress = False
        self.player = None
        self.watcher = None
        self.play_seq = 0
        self.status_text = ""
        self.jobs = queue.Queue()
        self._sort = (None, False)
        self._icon = None
        self.set_window_icon()
        self._build()
        self._apply_fonts()
        self._bind_zoom()
        self.after(60, self._drain)
        self.refresh_sources()
        self.load_current()

    # ---- fonts ----------------------------------------------------------
    def _apply_fonts(self):
        """Scale all widgets through Tk's named fonts, plus the Treeview row
        height (which does not follow the font)."""
        size = self.font_size
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont",
                     "TkMenuFont", "TkFixedFont", "TkSmallCaptionFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=int(size * 2.0))
        style.configure("Treeview.Heading", font=("TkDefaultFont", size, "bold"))
        try:
            self.tree.column("group", width=int(size * 20))
            self.tree.column("channel", width=int(size * 14))
            self.tree.column("health", width=int(size * 7))
        except Exception:
            pass

    def _set_font(self, size):
        size = max(8, min(40, int(size)))
        if size == self.font_size:
            return
        self.font_size = size
        self._apply_fonts()
        cfg = load_config()
        if not cfg:                        # file lost or corrupt mid-session
            cfg = dict(self.cfg)
            cfg["sources"] = self.sources
        cfg["font_size"] = size
        save_config(cfg)
        self.set_status("Text size: %d  (Ctrl +/- to change, Ctrl+0 to reset)"
                        % size)

    def _zoom(self, delta):
        self._set_font(self.font_size + delta)

    def _zoom_wheel(self, event):
        self._zoom(1 if getattr(event, "delta", 0) > 0 else -1)

    def _bind_zoom(self):
        self.bind_all("<Control-plus>", lambda _e: self._zoom(1))
        self.bind_all("<Control-equal>", lambda _e: self._zoom(1))
        self.bind_all("<Control-minus>", lambda _e: self._zoom(-1))
        self.bind_all("<Control-Key-0>", lambda _e: self._set_font(14))
        self.bind_all("<Control-Button-4>", lambda _e: self._zoom(1))
        self.bind_all("<Control-Button-5>", lambda _e: self._zoom(-1))
        self.bind_all("<Control-MouseWheel>", self._zoom_wheel)
        # Widget-level binding runs before the Treeview's built-in wheel
        # scroll (a class binding that also matches ctrl-wheel), and the
        # "break" stops both that scroll and the bind_all zoom above, so
        # ctrl-wheel over the tree zooms once and never scrolls.
        self.tree.bind("<Control-MouseWheel>",
                       lambda e: self._zoom_wheel(e) or "break")

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
        apply_theme(self, self.cfg)
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Label(bar, text="Playlist:").pack(side="left")
        self.source_var = tk.StringVar()
        self.source = ttk.Combobox(bar, textvariable=self.source_var,
                                   state="readonly", width=34)
        self.source.pack(side="left", padx=(6, 6))
        self.source.bind("<<ComboboxSelected>>", self.on_playlist_changed)
        ttk.Button(bar, text="Add...", command=self.on_add_source).pack(
            side="left")
        ttk.Button(bar, text="Edit...", command=self.on_edit_source).pack(
            side="left", padx=4)
        ttk.Button(bar, text="Reload", command=self.on_reload).pack(
            side="left", padx=4)

        filt = ttk.Frame(outer)
        filt.pack(fill="x", pady=(8, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self.refresh_view())
        search = ttk.Entry(filt, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True)
        # Searching every playlist is a different fetch from searching the one
        # on screen, so it is a scope rather than a second search box.
        ttk.Label(filt, text="  In:").pack(side="left")
        self.scope_var = tk.StringVar(value=SCOPE_ONE)
        self.scope = ttk.Combobox(filt, textvariable=self.scope_var,
                                  state="readonly", width=14,
                                  values=[SCOPE_ONE, SCOPE_ALL])
        self.scope.pack(side="left", padx=(6, 0))
        self.scope.bind("<<ComboboxSelected>>", self.on_scope_changed)
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
        # Packed before the status label, which expands and would starve it.
        self.stream_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.stream_var,
                  anchor="e").pack(side="right")
        self.status = ttk.Label(bottom, text="Loading playlist...",
                                anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=8)

    def report_callback_exception(self, exc, val, tb):
        """Tk sends callback errors to stderr, which a .app does not show, so
        a broken button looks like a button that does nothing. Put the error
        where the user is already looking."""
        self.set_status("Error: %s" % val)

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

    def on_playlist_changed(self, _event=None):
        """Picking a playlist means looking at that playlist, so the scope
        follows the choice instead of leaving a stale all-playlists result on
        screen."""
        if self.scope_var.get() == SCOPE_ALL:
            self.scope_var.set(SCOPE_ONE)
        self.load_current()

    def on_reload(self):
        """Reload what is on screen: the all-playlists fetch, or the one
        playlist."""
        if self.scope_var.get() == SCOPE_ALL:
            self.load_all_sources()
        else:
            self.load_current()

    def on_scope_changed(self, _event=None):
        if self.scope_var.get() == SCOPE_ALL:
            self.load_all_sources()
        else:
            self.group.configure(state="readonly")
            self.load_current()

    def load_all_sources(self):
        """Search every playlist at once: one fetch per playlist, off the UI
        thread, and a playlist that fails to load does not hide the others.
        Categories stop mattering here, so the group box is disabled and the
        playlist name takes its column."""
        if self.busy:
            return
        self.busy = True
        self.all_mode = True
        self.group.configure(state="disabled")
        self.entries = []
        self.fill_store()
        self.set_status("Loading all playlists...")
        sources = [dict(s) for s in self.sources]

        def work():
            rows = []
            for src in sources:
                label = src["name"]
                try:
                    if src["type"] == "xtream":
                        entries, _fresh = xtream_streams(src, None)
                        for _g, name, health, url in entries:
                            rows.append((label, name, health, url))
                    else:
                        text, _fresh = read_m3u_source(src)
                        for group, name, health, url in parse_m3u(
                                text, src.get("url", "")):
                            rows.append(("%s · %s" % (label, group) if group
                                         else label, name, health, url))
                except Exception:
                    continue
            self.post(self.apply_all_sources, rows)

        threading.Thread(target=work, daemon=True).start()

    def apply_all_sources(self, rows):
        self.busy = False
        if self._resume_pending():
            return
        if not self.all_mode:
            # The user left the scope while this was loading. If they are
            # back on All playlists, the all-fetch is owed again; otherwise
            # the single load they chose is still owed.
            if self.scope_var.get() == SCOPE_ALL:
                self.load_all_sources()
                return
            self.load_current()
            return
        self.entries = rows
        self.fill_store()
        self.set_status("%d channels from %d playlists" % (
            len(rows), len(self.sources)))

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
        if idx is None:
            return
        # Loading one playlist means looking at one playlist: a Reload,
        # Edit, Delete or Add while the all-playlists scope is showing must
        # not leave the scope box pointing at it.
        if self.scope_var.get() == SCOPE_ALL:
            self.scope_var.set(SCOPE_ONE)
        # Leaving the all-playlists scope has to restore the group box even if
        # a fetch is still running; apply_all_sources picks the load back up.
        self.all_mode = False
        self.group.configure(state="readonly")
        if self.busy:
            # Remember the request instead of dropping it: dropping leaves the
            # table showing the previous playlist while the box already says
            # this one, and the fetch that is running would repaint over it.
            self.pending = True
            self.set_status("Loading %s..." % self.sources[idx]["name"])
            return
        self.pending = False
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

    def _resume_pending(self):
        """A load just finished. If the user asked for another playlist while
        it ran, that request is still owed, so start it now rather than paint
        the result they are no longer looking at."""
        if not self.pending:
            return False
        self.pending = False
        self.load_current()
        return True

    def load_failed(self, name, exc):
        self.busy = False
        if self._resume_pending():
            return
        if self.scope_var.get() == SCOPE_ALL:
            self.load_all_sources()
            return
        self.set_status("Could not load %s: %s" % (name, exc))

    def apply_m3u(self, entries, fresh, name):
        self.busy = False
        if self._resume_pending():
            return
        if self.scope_var.get() == SCOPE_ALL:
            self.load_all_sources()
            return
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
        if self._resume_pending():
            return
        if self.scope_var.get() == SCOPE_ALL:
            self.load_all_sources()
            return
        self.categories = cats
        if not cats:
            self.set_status("%s: no categories (check the account)" % name)
            return
        self.suppress = True
        self.group.configure(values=[ALL_GROUPS] + [c[1] for c in cats])
        self.group.current(0)          # every category, not just the first
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
        if self._resume_pending():
            return
        if self.scope_var.get() == SCOPE_ALL:
            self.load_all_sources()
            return
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
        group = None
        if self.mode == "m3u" and not self.all_mode:
            group = self.group_var.get()
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
    def on_add_source(self):
        """One Add button: the two playlist types share a purpose but not a
        form, so the choice comes before the fields."""
        choice = ChoiceDialog(self, "Add playlist", [
            ("m3u", "M3U playlist (URL or file)"),
            ("xtream", "Xtream Codes (server account)"),
        ]).result
        if choice == "m3u":
            self.on_add_m3u()
        elif choice == "xtream":
            self.on_add_xtream()

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
            ("server", "Server address", "", False),
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
                         "server": server_url(vals["server"]),
                         "user": vals["user"], "pass": vals["pass"]})

    def add_source(self, src):
        self.sources.append(src)
        save_sources(self.sources)
        self.refresh_sources(len(self.sources) - 1)
        self.load_current()

    def remove_source(self, idx):
        """Delete by index. The guard stays: an empty list leaves the window
        with nothing to load and no way back."""
        if idx is None or idx >= len(self.sources) or len(self.sources) <= 1:
            self.set_status("Keep at least one playlist")
            return
        src = self.sources.pop(idx)
        save_sources(self.sources)
        self.refresh_sources()
        self.entries = []
        self.fill_store()
        self.load_current()
        self.set_status("Removed %s" % src["name"])

    def on_edit_source(self):
        """One dialog for renaming, re-pointing and deleting: the playlist is
        already selected in the bar, so Add/Edit/Delete stay in one place."""
        idx = self.current_index()
        if idx is None:
            return
        src = dict(self.sources[idx])
        xtream = src["type"] == "xtream"
        if xtream:
            fields = [("name", "Name", "", False),
                      ("server", "Server address", "", False),
                      ("user", "Username", "", False),
                      ("pass", "Password", "", True)]
        else:
            fields = [("name", "Name", "My IPTV", False),
                      ("url", "M3U URL or file path",
                       "http://host/get.php?... or /path/list.m3u", False)]
        dlg = FormDialog(self, "Edit playlist", fields, ok_label="Save",
                         initial=src, delete_label="Delete")
        if dlg.deleted:
            self.remove_source(idx)
            return
        if not dlg.result:
            return
        vals = dlg.result
        if xtream:
            if not (vals.get("server") and vals.get("user")
                    and vals.get("pass")):
                messagebox.showwarning(
                    "Xtream Codes",
                    "Server, username and password are required.")
                return
            src.update({"name": vals.get("name") or "Xtream",
                        "server": server_url(vals["server"]),
                        "user": vals["user"], "pass": vals["pass"]})
        else:
            if not vals.get("url"):
                return
            src.update({"name": vals.get("name")
                        or vals["url"].rsplit("/", 1)[-1][:40],
                        "url": vals["url"]})
        self.sources[idx] = src
        save_sources(self.sources)
        self.refresh_sources(idx)
        self.load_current()

    # ---- playback -------------------------------------------------------
    def start_playback(self):
        row = self.selected()
        if not row:
            self.set_status("Select a channel first")
            return
        grp, name, _health, url = row
        self.cfg = load_config()
        self.stop_watcher()
        self.play_seq += 1
        ipc = ipc_path(self.play_seq)
        if not IS_WIN:
            try:
                os.unlink(ipc)       # left behind by an mpv that was killed
            except OSError:
                pass
        try:
            stop_player(self.player)
            self.player = play(url, self.cfg, ipc)
        except OSError as exc:
            self.set_status("Could not start mpv: %s" % exc)
            return
        self.watcher = StreamWatcher(ipc, self.post, self.show_stream_info)
        self.watcher.start()
        self.set_status("Playing: %s  (%s)" % (name, grp))
        self.after(1200, lambda: self._check_player(self.play_seq))

    def _check_player(self, seq):
        """mpv exits at once on an option it does not know or a URL the
        provider refuses, and the status bar used to go on claiming playback.
        One check 1200 ms after the start says so instead, since mpv's own
        stderr goes to /dev/null and its window never appears."""
        if seq != self.play_seq or self.player is None:
            return
        if self.player.poll() is not None:
            self.set_status("mpv exited at once (code %s): check mpv_args "
                            "and the channel URL" % self.player.returncode)

    def show_stream_info(self, text):
        self.stream_var.set(text)

    def stop_watcher(self):
        watcher, self.watcher = self.watcher, None
        if watcher is not None:
            watcher.stop()
        self.stream_var.set("")

    def shutdown(self):
        self.stop_watcher()
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
