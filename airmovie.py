#!/usr/bin/env python3
"""AirMovie - AirPlay any movie file to your LG webOS TV.
Normally started by the AirMovie.app launcher; or run: python3 airmovie.py [movie file]
First start installs everything it needs (PyObjC + ffmpeg) into ~/.airmovie-venv by itself.
"""
import os, sys, subprocess, shutil, tempfile, atexit, threading, uuid, json, struct, plistlib
import urllib.request, urllib.parse, urllib.error, socket, secrets, http.server, mimetypes


def dialog(text, secs=0):
    """Small native macOS message box (non-blocking; returns the process)."""
    script = f'display dialog "{text}" with title "AirMovie" buttons {{"OK"}} default button "OK"'
    if secs:
        script += f" giving up after {secs}"
    return subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if sys.platform != "darwin":  # CrossOver / Wine / Windows / Linux
    msg = ("AirMovie only runs natively on macOS (it uses Apple AirPlay). You started it inside Windows (CrossOver/Wine). "
           "Please start it from the macOS Terminal or the AirMovie.app instead.")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "AirMovie", 0x10)
    except Exception:
        print(msg)
    sys.exit(1)

# ---------- self-bootstrap: no Terminal, no manual installs ----------
try:
    import AVFoundation, AVKit, AppKit, CoreMedia, imageio_ffmpeg  # noqa: F401
except ImportError:
    if os.environ.get("AIRMOVIE_BOOT"):
        dialog("Setup failed. Delete the folder .airmovie-venv in your home folder and open AirMovie again.").wait()
        sys.exit(1)
    import venv
    home = os.path.expanduser("~/.airmovie-venv")
    py = home + "/bin/python"
    note = dialog("First start: AirMovie is setting itself up. This takes 1-2 minutes, needs internet and happens only once. "
                  "The player opens by itself. You can click OK, setup keeps running.", 600)
    try:
        if not os.path.exists(py):
            venv.create(home, with_pip=True)
        subprocess.check_call([py, "-m", "pip", "install", "-q", "pyobjc-framework-Cocoa", "pyobjc-framework-AVFoundation",
                               "pyobjc-framework-AVKit", "pyobjc-framework-CoreMedia", "imageio-ffmpeg"])
    except Exception:
        note.terminate()
        dialog("Setup failed. Please check your internet connection and open AirMovie again.").wait()
        sys.exit(1)
    note.terminate()
    os.environ["AIRMOVIE_BOOT"] = "1"
    os.execv(py, [py] + sys.argv)

import re
import imageio_ffmpeg

import objc
from AppKit import *
from Foundation import *
from AVFoundation import AVPlayer, AVPlayerItem, AVURLAsset, AVMediaTypeVideo, AVMediaTypeAudio, AVMediaCharacteristicLegible
from AVKit import AVPlayerView, AVRoutePickerView
from CoreMedia import CMTimeMake, CMTimeMakeWithSeconds, CMTimeGetSeconds
from PyObjCTools import AppHelper

TMP = tempfile.mkdtemp(prefix="airmovie-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
CTL = None


# ---------- format handling ----------
def ffmpeg_path():
    return (shutil.which("ffmpeg")
            or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
            or imageio_ffmpeg.get_ffmpeg_exe())  # bundled copy, installed automatically


def native_playable(path):
    """True if macOS can decode the container and every audio/video track itself."""
    a = AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(path), None)
    if not a.isPlayable():
        return False
    return all(t.isPlayable() for t in a.tracks() if t.mediaType() in (AVMediaTypeVideo, AVMediaTypeAudio))


def convert(src, progress, sub=None, burn=False):
    """Remux to MP4: copy streams when possible, otherwise re-encode. Optional subtitle file: soft track or burned in."""
    ff = ffmpeg_path()
    info = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True, text=True).stderr

    def codec(kind):
        m = re.search(rf"Stream #\S+: {kind}: (\w+)", info)
        return m.group(1) if m else ""

    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    total = max(int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]), 1) if d else 1
    v, a = codec("Video"), codec("Audio")
    enc = [["-c:v", "h264_videotoolbox", "-b:v", "10M", "-pix_fmt", "yuv420p"],
           ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]]
    burn = bool(sub and burn)
    video_options = enc if burn else [["-c:v", "copy"]] if v == "h264" else [["-c:v", "copy", "-tag:v", "hvc1"]] if v == "hevc" else enc
    vf = ["-vf", "subtitles=" + os.path.basename(sub)] if burn else []
    soft = bool(sub and not burn)
    aargs = ["-c:a", "copy"] if a in ("aac", "ac3", "eac3", "mp3") else ["-c:a", "aac", "-b:a", "384k"]
    for vargs in video_options:
        out = os.path.join(TMP, uuid.uuid4().hex + ".mp4")
        cmd = [ff, "-v", "error", "-y", "-i", src] + (["-sub_charenc", "UTF-8", "-i", sub] if soft else []) + \
              ["-map", "0:v:0", "-map", "0:a:0?"] + (["-map", "1:0", "-c:s", "mov_text", "-metadata:s:s:0", "title=Subtitles"] if soft else []) + \
              vf + vargs + aargs + ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=TMP)
        for line in p.stdout:
            if line.startswith("out_time_us="):
                try:
                    progress(min(int(line[12:]) / 1e6 / total, 1))
                except ValueError:
                    pass
        if p.wait() == 0:
            return out
    raise RuntimeError("Conversion failed" + (" - try Soft subtitle mode in Settings" if burn else ""))


def fmt(s):
    s = int(max(s, 0))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---------- settings, OpenSubtitles, click-to-run launcher ----------
D = NSUserDefaults.alloc().initWithSuiteName_("com.airmovie.app")
DEFAULTS = {"apiKey": "", "user": "", "lang": "en", "auto": False, "mode": 0, "skip": 10, "resume": True}
LANGS = {"English": "en", "German": "de", "Turkish": "tr", "Croatian": "hr", "Serbian": "sr", "Bosnian": "bs",
         "French": "fr", "Spanish": "es", "Italian": "it", "Polish": "pl", "Russian": "ru", "Arabic": "ar",
         "Portuguese": "pt-PT", "Dutch": "nl"}
API = "https://api.opensubtitles.com/api/v1"


def cfg(k):
    v, d = D.objectForKey_(k), DEFAULTS[k]
    return d if v is None else type(d)(v)


def put(k, v):
    D.setObject_forKey_(v, k)


def kc_get():  # password lives in the macOS Keychain, not in the prefs file
    r = subprocess.run(["security", "find-generic-password", "-s", "AirMovie", "-a", "opensubtitles", "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def kc_set(pw):
    subprocess.run(["security", "add-generic-password", "-U", "-s", "AirMovie", "-a", "opensubtitles", "-w", pw])


def creds_ok():
    return bool(cfg("apiKey") and cfg("user") and kc_get())


def os_hash(path):
    """OpenSubtitles movie hash: size + first/last 64 KB. Finds the exact release."""
    size = os.path.getsize(path)
    if size < 131072:
        return None
    h = size
    with open(path, "rb") as f:
        for off in (0, size - 65536):
            f.seek(off)
            for (x,) in struct.iter_unpack("<Q", f.read(65536)):
                h = (h + x) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


def os_api(method, endpoint, body=None, token=None):
    req = urllib.request.Request(API + endpoint, method=method, data=None if body is None else json.dumps(body).encode())
    for k, v in (("Api-Key", cfg("apiKey")), ("User-Agent", "AirMovie v1.0"), ("Content-Type", "application/json"), ("Accept", "application/json")):
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError("OpenSubtitles error %d: %s" % (e.code, e.read()[:120].decode(errors="ignore")))


def os_search(path, lang):
    q = {"languages": lang, "query": os.path.splitext(os.path.basename(path))[0].replace(".", " ")}
    h = os_hash(path)
    if h:
        q["moviehash"] = h
    out = []
    for d in os_api("GET", "/subtitles?" + urllib.parse.urlencode(sorted(q.items()))).get("data", []):
        a = d["attributes"]
        f = (a.get("files") or [{}])[0]
        if f.get("file_id"):
            out.append({"name": a.get("release") or f.get("file_name", "?"), "dl": a.get("download_count", 0),
                        "hash": bool(a.get("moviehash_match")), "id": f["file_id"]})
    return sorted(out, key=lambda r: (not r["hash"], -r["dl"]))


def os_download(file_id):
    tok = os_api("POST", "/login", {"username": cfg("user"), "password": kc_get()})["token"]
    link = os_api("POST", "/download", {"file_id": file_id, "sub_format": "srt"}, tok)["link"]
    out = os.path.join(TMP, uuid.uuid4().hex + ".srt")
    with urllib.request.urlopen(link, timeout=30) as r, open(out, "wb") as f:
        f.write(r.read())
    return out


def lang_popup(code):
    p = NSPopUpButton.alloc().initWithFrame_pullsDown_(((0, 0), (150, 26)), False)
    p.addItemsWithTitles_(list(LANGS))
    p.selectItemWithTitle_(next((n for n, c in LANGS.items() if c == code), "English"))
    return p


def make_app():
    """Creates ~/Applications/AirMovie.app: a double-clickable launcher (no Terminal window)."""
    app = os.path.expanduser("~/Applications/AirMovie.app")
    os.makedirs(app + "/Contents/Resources", exist_ok=True)
    os.makedirs(app + "/Contents/MacOS", exist_ok=True)
    try:
        shutil.copy(os.path.abspath(__file__), app + "/Contents/Resources/airmovie.py")
    except shutil.SameFileError:
        pass
    exe = app + "/Contents/MacOS/AirMovie"
    with open(exe, "w") as f:
        f.write('#!/bin/bash\nD="$(cd "$(dirname "$0")/../Resources" && pwd)"\n'
                'PY="$HOME/.airmovie-venv/bin/python"\n[ -x "$PY" ] || PY=/usr/bin/python3\n'
                'exec "$PY" "$D/airmovie.py" "$@" >> "$HOME/airmovie.log" 2>&1\n')
    os.chmod(exe, 0o755)
    with open(app + "/Contents/Info.plist", "wb") as f:
        plistlib.dump({"CFBundleName": "AirMovie", "CFBundleDisplayName": "AirMovie", "CFBundleIdentifier": "com.airmovie.app",
                       "CFBundleExecutable": "AirMovie", "CFBundlePackageType": "APPL", "CFBundleVersion": "1.0",
                       "NSHighResolutionCapable": True, "NSLocalNetworkUsageDescription": "AirMovie needs the local network to find AirPlay TVs.",
                       "NSBonjourServices": ["_airplay._tcp", "_raop._tcp"]}, f)
    return app


# ---------- media server: the TV downloads the movie straight from this Mac ----------
SERVER = None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    files = {}  # secret token -> file path (only these files are ever served)

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.serve(False)

    def do_GET(self):
        self.serve(True)

    def serve(self, body):
        parts = self.path.split("?")[0].split("/")
        path = Handler.files.get(parts[1]) if len(parts) > 2 else None
        if not path or not os.path.exists(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        try:
            if rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].partition("-")
                if a == "":
                    start = max(size - int(b), 0)
                else:
                    start = int(a)
                    end = min(int(b), size - 1) if b else end
                code = 206
        except ValueError:
            return self.send_error(400)
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if not body:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # players cancel requests all the time


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def serve_url(path):
    global SERVER
    if SERVER is None:
        SERVER = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    tok = secrets.token_urlsafe(12)
    Handler.files[tok] = path
    return "http://%s:%d/%s/%s" % (lan_ip(), SERVER.server_address[1], tok, urllib.parse.quote(os.path.basename(path)))


def faststart(path):
    """True if the MP4/MOV index (moov) sits before the video data, so a TV can start streaming at once."""
    try:
        with open(path, "rb") as f:
            for _ in range(12):
                h = f.read(8)
                if len(h) < 8:
                    return False
                size, kind = int.from_bytes(h[:4], "big"), h[4:8]
                if kind == b"moov":
                    return True
                if kind == b"mdat" or size == 0:
                    return False
                if size == 1:
                    size = int.from_bytes(f.read(8), "big")
                    f.seek(size - 16, 1)
                else:
                    f.seek(size - 8, 1)
    except OSError:
        pass
    return False


# ---------- UI ----------
class DropView(NSView):
    def draggingEntered_(self, info):
        return NSDragOperationCopy

    def performDragOperation_(self, info):
        urls = info.draggingPasteboard().readObjectsForClasses_options_([NSURL], None)
        if urls and CTL:
            CTL.open_path(urls[0].path())
            return True
        return False


class Controller(NSObject):
    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.player = AVPlayer.alloc().init()
        self.player.setAllowsExternalPlayback_(True)
        self.dur, self.scrubbing, self.hold, self.was_playing, self.last_tmp = 0.0, False, 0, None, None
        self.cur, self.todo, self.n, self.results, self.prefwin = None, None, 0, [], None

        W, H, BAR = 960, 600, 48
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((0, 0), (W, H)), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("AirMovie")
        self.window.setMinSize_((720, 300))
        self.window.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
        root = DropView.alloc().initWithFrame_(((0, 0), (W, H)))
        root.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        self.window.setContentView_(root)
        grow = NSViewWidthSizable | NSViewHeightSizable

        video = AVPlayerView.alloc().initWithFrame_(((0, BAR), (W, H - BAR)))
        video.setPlayer_(self.player)
        video.setControlsStyle_(0)  # none - we draw our own controls
        video.setAutoresizingMask_(grow)
        root.addSubview_(video)

        self.hint = NSTextField.labelWithString_("Drop a movie here or press ⌘O")
        self.hint.setFrame_(((0, H // 2), (W, 24)))
        self.hint.setAlignment_(NSTextAlignmentCenter)
        self.hint.setTextColor_(NSColor.secondaryLabelColor())
        self.hint.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin | NSViewMaxYMargin)
        root.addSubview_(self.hint)

        mono = NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0)

        def button(symbol, x, action):
            b = NSButton.alloc().initWithFrame_(((x, 10), (28, 28)))
            b.setBordered_(False)
            b.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None))
            b.setTarget_(self)
            b.setAction_(action)
            root.addSubview_(b)
            return b

        button("folder", 12, "openFile:")
        button("gobackward.10", 48, "back:")
        self.play_btn = button("play.fill", 84, "toggle:")
        button("goforward.10", 120, "fwd:")

        self.elapsed = NSTextField.labelWithString_("0:00")
        self.elapsed.setFrame_(((160, 14), (56, 20)))
        self.elapsed.setFont_(mono)
        self.remaining = NSTextField.labelWithString_("-0:00")
        self.remaining.setFrame_(((W - 114, 14), (64, 20)))
        self.remaining.setFont_(mono)
        self.remaining.setAlignment_(NSTextAlignmentRight)
        self.remaining.setAutoresizingMask_(NSViewMinXMargin)
        self.slider = NSSlider.alloc().initWithFrame_(((220, 12), (W - 340, 24)))
        self.slider.setMinValue_(0)
        self.slider.setMaxValue_(1)
        self.slider.setContinuous_(True)
        self.slider.setTarget_(self)
        self.slider.setAction_("scrub:")
        self.slider.setAutoresizingMask_(NSViewWidthSizable)

        # Bound to the player => picks an AirPlay *video* route (not just audio output)
        picker = AVRoutePickerView.alloc().initWithFrame_(((W - 44, 10), (32, 28)))
        picker.setPlayer_(self.player)
        picker.setRoutePickerButtonBordered_(False)
        picker.setPrioritizesVideoDevices_(True)
        picker.setAutoresizingMask_(NSViewMinXMargin)
        for v in (self.elapsed, self.slider, self.remaining, picker):
            root.addSubview_(v)

        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.4, self, "tick:", None, True)
        self.keys = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, self.on_key)
        return self

    # --- helpers (pure Python, hidden from Objective-C) ---
    @objc.python_method
    def on_key(self, event):
        k = event.keyCode()
        if k == 49:
            self.toggle_(None)
        elif k == 123:
            self.skip(-self.step())
        elif k == 124:
            self.skip(self.step())
        else:
            return event
        return None

    @objc.python_method
    def say(self, text):
        self.hint.setStringValue_(text or "")
        self.hint.setHidden_(not text)

    @objc.python_method
    def open_path(self, path):
        self.window.setTitle_(os.path.basename(path))
        self.say("Opening...")
        self.cur = path
        if self.last_tmp:
            try:
                os.remove(self.last_tmp)
            except OSError:
                pass
            self.last_tmp = None
        threading.Thread(target=self.prepare, args=(path,), daemon=True).start()

    @objc.python_method
    def prepare(self, path, hit=None, start=None):  # background thread
        try:
            if start is None:
                start = self.saved_pos(path)
            if hit is None and cfg("auto") and creds_ok():
                try:
                    hits = os_search(path, cfg("lang"))
                    hit = hits[0] if hits else None
                except Exception:
                    hit = None  # subtitles are best-effort
            sub = None
            if hit:
                AppHelper.callAfter(self.say, "Downloading subtitle...")
                sub = os_download(hit["id"])
            src = path
            if sub or not native_playable(path) or not faststart(path):
                AppHelper.callAfter(self.say, "Preparing... 0%")
                src = convert(path, lambda p: AppHelper.callAfter(self.say, "Preparing... %d%%" % int(p * 100)), sub, cfg("mode") == 1)
                self.last_tmp = src
            AppHelper.callAfter(self.play, src, start, bool(sub))
        except Exception as e:
            AppHelper.callAfter(self.say, "⚠️ %s" % e)

    @objc.python_method
    def play(self, path, start=0, subs=False):
        self.player.replaceCurrentItemWithPlayerItem_(AVPlayerItem.playerItemWithURL_(NSURL.URLWithString_(serve_url(path))))
        self.todo, self.n = (start, subs), 0
        self.say(None)
        self.slider.setDoubleValue_(start)
        self.player.play()

    @objc.python_method
    def step(self):
        return int(cfg("skip"))

    @objc.python_method
    def saved_pos(self, path):
        v = D.doubleForKey_("pos:" + path)
        return v if cfg("resume") and v > 30 else 0

    @objc.python_method
    def on_ready(self, item):
        if item.status() == 2:  # failed: show the real reason instead of a silent black screen
            err = item.error()
            self.say("⚠️ %s" % (err.localizedDescription() if err else "Playback failed"))
            return
        if item.status() != 1:  # not ready to play yet
            return
        if self.todo:
            (start, subs), self.todo = self.todo, None
            if start > 1:
                self.seek(start)
            if subs:
                g = item.asset().mediaSelectionGroupForMediaCharacteristic_(AVMediaCharacteristicLegible)
                if g and g.options():
                    item.selectMediaOption_inMediaSelectionGroup_(g.options()[0], g)
        self.n += 1
        if self.cur and self.n % 25 == 0:  # remember position every ~10 s
            t = CMTimeGetSeconds(self.player.currentTime())
            D.setDouble_forKey_(0 if self.dur - t < 60 else t, "pos:" + self.cur)

    @objc.python_method
    def make_window(self, title, w, h):
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (w, h)), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable, NSBackingStoreBuffered, False)
        win.setTitle_(title)
        win.setReleasedWhenClosed_(False)
        win.center()
        win.setDelegate_(self)
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return win

    @objc.python_method
    def save_prefs(self):
        put("apiKey", str(self.f_key.stringValue()).strip())
        put("user", str(self.f_user.stringValue()).strip())
        if str(self.f_pass.stringValue()):
            kc_set(str(self.f_pass.stringValue()))
        put("lang", LANGS[self.f_lang.titleOfSelectedItem()])
        put("mode", int(self.f_mode.indexOfSelectedItem()))
        put("skip", int(self.f_skip.titleOfSelectedItem()))
        put("auto", self.f_auto.state() == 1)
        put("resume", self.f_res.state() == 1)

    @objc.python_method
    def search_bg(self, lang):
        try:
            res = os_search(self.cur, lang)
            AppHelper.callAfter(self.show_results, res)
        except Exception as e:
            AppHelper.callAfter(self.st.setStringValue_, "⚠️ %s" % e)

    @objc.python_method
    def show_results(self, res):
        self.results = res
        self.p_res.removeAllItems()
        self.p_res.addItemsWithTitles_(["%d. %s (%s downloads)%s" % (i + 1, r["name"], r["dl"], "  [exact match]" if r["hash"] else "")
                                        for i, r in enumerate(res)])
        self.st.setStringValue_("%d subtitles found" % len(res) if res else "Nothing found - try another language")

    # --- windows & actions ---
    def settings_(self, sender):
        if self.prefwin is not None and self.prefwin.isVisible():
            return self.prefwin.makeKeyAndOrderFront_(None)
        win = self.prefwin = self.make_window("Settings", 470, 340)
        v, y = win.contentView(), [296]

        def row(label, ctl, w=270):
            t = NSTextField.labelWithString_(label)
            t.setFrame_(((8, y[0] + 2), (160, 20)))
            t.setAlignment_(NSTextAlignmentRight)
            ctl.setFrame_(((178, y[0]), (w, 24)))
            v.addSubview_(t)
            v.addSubview_(ctl)
            y[0] -= 36
            return ctl

        def popup(items, sel):
            p = NSPopUpButton.alloc().initWithFrame_pullsDown_(((0, 0), (10, 10)), False)
            p.addItemsWithTitles_(items)
            p.selectItemAtIndex_(sel)
            return p

        def check(title, on):
            b = NSButton.checkboxWithTitle_target_action_(title, None, None)
            b.setState_(1 if on else 0)
            return b

        self.f_key = row("OpenSubtitles API key", NSTextField.alloc().init())
        self.f_key.setStringValue_(cfg("apiKey"))
        self.f_user = row("OpenSubtitles username", NSTextField.alloc().init())
        self.f_user.setStringValue_(cfg("user"))
        self.f_pass = row("OpenSubtitles password", NSSecureTextField.alloc().init())
        self.f_pass.setStringValue_(kc_get())
        self.f_lang = row("Subtitle language", lang_popup(cfg("lang")))
        self.f_mode = row("Subtitle mode", popup(["Soft (fast, pick on TV)", "Burned in (any TV, slower)"], cfg("mode")))
        self.f_skip = row("Skip button (seconds)", popup(["5", "10", "15", "30"], ["5", "10", "15", "30"].index(str(cfg("skip")))))
        self.f_auto = row("", check("Auto-download subtitle on open", cfg("auto")))
        self.f_res = row("", check("Resume where I left off", cfg("resume")))
        note = NSTextField.labelWithString_("Free API key + account: opensubtitles.com (Consumers). Saved when you close this window.")
        note.setFont_(NSFont.systemFontOfSize_(10))
        note.setFrame_(((16, 8), (440, 16)))
        v.addSubview_(note)

    def windowWillClose_(self, note):
        if self.prefwin is not None and note.object() == self.prefwin:
            self.save_prefs()

    def findSubs_(self, sender):
        if not self.cur:
            return NSBeep()
        win = self.subwin = self.make_window("Find Subtitles", 480, 150)
        v = win.contentView()
        self.p_lang = lang_popup(cfg("lang"))
        self.p_lang.setFrame_(((16, 108), (170, 26)))
        go = NSButton.buttonWithTitle_target_action_("Search", self, "doSearch:")
        go.setFrame_(((196, 108), (90, 26)))
        self.p_res = NSPopUpButton.alloc().initWithFrame_pullsDown_(((16, 66), (448, 26)), False)
        use = NSButton.buttonWithTitle_target_action_("Use this subtitle", self, "useSub:")
        use.setFrame_(((16, 24), (160, 28)))
        self.st = NSTextField.labelWithString_("Searches by file fingerprint + name.")
        self.st.setFrame_(((186, 28), (280, 20)))
        for c in (self.p_lang, go, self.p_res, use, self.st):
            v.addSubview_(c)

    def doSearch_(self, sender):
        if not cfg("apiKey"):
            return self.st.setStringValue_("Add your API key in Settings first")
        self.st.setStringValue_("Searching...")
        threading.Thread(target=self.search_bg, args=(LANGS[self.p_lang.titleOfSelectedItem()],), daemon=True).start()

    def useSub_(self, sender):
        i = self.p_res.indexOfSelectedItem()
        if not self.results or i < 0:
            return
        if not creds_ok():
            return self.st.setStringValue_("Downloading needs username + password in Settings")
        self.subwin.close()
        threading.Thread(target=self.prepare, args=(self.cur, self.results[i], self.slider.doubleValue()), daemon=True).start()

    def makeApp_(self, sender):
        app = make_app()
        subprocess.run(["open", "-R", app])
        dialog("Done! AirMovie.app is in your Applications folder (inside your Home folder). Double-click it, or drag it to the Dock.")

    @objc.python_method
    def seek(self, s):
        self.player.seekToTime_toleranceBefore_toleranceAfter_(CMTimeMakeWithSeconds(s, 600), CMTimeMake(0, 600), CMTimeMake(0, 600))
        self.slider.setDoubleValue_(s)
        self.hold = 2  # ignore the next ticks so the knob doesn't jump back

    @objc.python_method
    def skip(self, delta):
        self.seek(min(max(self.slider.doubleValue() + delta, 0), self.dur))

    @objc.python_method
    def show_time(self, t):
        self.elapsed.setStringValue_(fmt(t))
        self.remaining.setStringValue_("-" + fmt(self.dur - t))

    # --- actions ---
    def openFile_(self, sender):
        panel = NSOpenPanel.openPanel()
        if panel.runModal() == NSModalResponseOK:
            self.open_path(panel.URL().path())

    def toggle_(self, sender):
        if self.player.currentItem() is None:
            return
        self.player.pause() if self.player.timeControlStatus() != 0 else self.player.play()

    def back_(self, sender):
        self.skip(-self.step())

    def fwd_(self, sender):
        self.skip(self.step())

    def scrub_(self, sender):
        t = sender.doubleValue()
        self.show_time(t)
        ev = NSApplication.sharedApplication().currentEvent()
        if ev is not None and ev.type() in (NSEventTypeLeftMouseDown, NSEventTypeLeftMouseDragged):
            self.scrubbing = True  # seek once, on release (gentler on the TV)
        else:
            self.scrubbing = False
            self.seek(t)

    def tick_(self, timer):
        item = self.player.currentItem()
        if item is None:
            return
        self.on_ready(item)
        d = CMTimeGetSeconds(item.duration())
        if d == d and 0 < d < 1e9 and d != self.dur:
            self.dur = d
            self.slider.setMaxValue_(d)
        if self.hold:
            self.hold -= 1
        elif not self.scrubbing:
            t = CMTimeGetSeconds(self.player.currentTime())
            if t == t:
                self.slider.setDoubleValue_(t)
                self.show_time(t)
        playing = self.player.timeControlStatus() != 0
        if playing != self.was_playing:
            self.was_playing = playing
            self.play_btn.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_("pause.fill" if playing else "play.fill", None))
        ext, st = self.player.isExternalPlaybackActive(), self.player.timeControlStatus()
        self.window.setSubtitle_("" if not ext else "AirPlay: waiting for the TV to load the video..." if st == 1 else "Playing on AirPlay device")

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True


def main():
    global CTL
    if '--make-app' in sys.argv:
        print(make_app())
        return
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    try:
        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "AirMovie"
    except Exception:
        pass
    CTL = Controller.alloc().init()
    app.setDelegate_(CTL)

    menubar, app_item, file_item = NSMenu.alloc().init(), NSMenuItem.alloc().init(), NSMenuItem.alloc().init()
    app_menu, file_menu = NSMenu.alloc().init(), NSMenu.alloc().initWithTitle_("File")
    app_menu.addItemWithTitle_action_keyEquivalent_("Settings...", "settings:", ",").setTarget_(CTL)
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit AirMovie", "terminate:", "q")
    item = file_menu.addItemWithTitle_action_keyEquivalent_("Open...", "openFile:", "o")
    item.setTarget_(CTL)
    for title, sel, key in (("Find Subtitles...", "findSubs:", "f"), ("Create Click-to-Run App", "makeApp:", "")):
        file_menu.addItemWithTitle_action_keyEquivalent_(title, sel, key).setTarget_(CTL)
    app_item.setSubmenu_(app_menu)
    file_item.setSubmenu_(file_menu)
    menubar.addItem_(app_item)
    menubar.addItem_(file_item)
    app.setMainMenu_(menubar)

    app.activateIgnoringOtherApps_(True)
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        CTL.open_path(os.path.abspath(sys.argv[1]))
    app.run()


if __name__ == "__main__":
    main()
