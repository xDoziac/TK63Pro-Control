"""TK-63 Pro Control - a small replacement for the Tezarre Driver.

Run:  pythonw app.py   (or double-click "Start TK63 Pro Control.bat")
Opens a window (Edge app mode if available, otherwise your browser) on http://127.0.0.1:47563.
Closing the window shuts the app down ~30 s later. Settings live on the keyboard itself.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen

import tk63pro as kbp
from capture import ScreenSampler, AudioLoopback, Analyser


class LiveSource:
    """Produces frames for the streaming effects on its own thread; the worker just picks up the latest."""

    def __init__(self):
        self.kind = None
        self.lock = threading.Lock()
        self.color = (0, 0, 0)
        self.levels = []
        self.audio = AudioLoopback()
        self.analyser = Analyser()
        self.screen = None
        self.thread = None
        self.error = None
        self.peak = 0.0
        self.smooth = 2          # 0 = raw (official), 4 = smoothest
        self.smoothed = None

    def frame_interval(self):
        """Seconds between music frames: 33 ms raw (official) up to ~80 ms at max smoothness."""
        return 0.033 + 0.012 * self.smooth

    def _smooth(self, lv, kind):
        if self.smooth == 0 or self.smoothed is None or len(self.smoothed) != len(lv):
            self.smoothed = [float(v) for v in lv]
            return list(lv)
        k = self.smooth / 4.0
        attack = 1.0 - 0.6 * k            # how fast bars rise
        decay = 1.0 - 0.9 * k             # how fast they fall
        if kind == "music_soothe":
            attack *= 0.6
            decay *= 0.5
        out = []
        for i, v in enumerate(lv):
            cur = self.smoothed[i]
            cur += (v - cur) * (attack if v > cur else decay)
            self.smoothed[i] = cur
            out.append(int(round(cur)))
        return out

    def set_kind(self, kind):
        if kind == self.kind:
            return
        self.kind = kind
        if kind in ("music_edm", "music_soothe"):
            self.audio.start()
        else:
            self.audio.stop()
        if kind and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        while self.kind:
            kind = self.kind
            try:
                if kind == "screen":
                    if self.screen is None:
                        self.screen = ScreenSampler()
                    c = self.screen.sample()
                    with self.lock:
                        self.color = c
                    time.sleep(0.01)
                else:
                    s = self.audio.samples()
                    self.analyser.update(s)
                    lv = self.analyser.edm_levels() if kind == "music_edm" else self.analyser.soothe_levels()
                    lv = self._smooth(lv, kind)
                    with self.lock:
                        self.levels, self.peak = lv, float(abs(s).max())
                    self.error = self.audio.error
                    time.sleep(0.03)
            except Exception as e:
                self.error = str(e)
                time.sleep(0.5)

    def latest(self):
        with self.lock:
            return self.color, list(self.levels), self.peak

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # Packaged .exe: bundled files live in the unpack folder; presets/log go to %APPDATA% so they persist.
    HERE = getattr(sys, "_MEIPASS", HERE)
    DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TK63Pro-Control")
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    DATA_DIR = HERE
PORT = 47563
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
LOG_FILE = os.path.join(DATA_DIR, "watchdog.log")
IDLE_EXIT_SECONDS = 30
_log_lock = threading.Lock()


def wlog(msg):
    """Watchdog log: every command sent, read-back checks and user notes, timestamped."""
    line = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d}  {msg}"
    with _log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def describe_lighting(s):
    e = kbp.EFFECT_BY_MODE.get(s.get("mode"))
    name = e[1] if e else f"mode {s.get('mode')}"
    opt = ""
    if e and e[5]:
        opt = " option=" + next((l for l, v in e[5] if v == s.get("option")), str(s.get("option")))
    return (f"{name} brightness={s.get('brightness')} speed={s.get('speed')} "
            f"{'rainbow' if s.get('rainbow') else 'colour=' + str(s.get('color'))}{opt}")


class Worker(threading.Thread):
    """Owns the keyboard. All USB traffic happens on this thread, paced by the quiet-time rules."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.kb = None
        self.info = None
        self.last_write = 0.0
        self.next_connect = 0.0
        self.next_presence = 0.0
        self.next_battery = 0.0
        self.retry_after = 0.0
        self.pending = {}  # setting name -> value waiting to be written (latest wins)
        self.need_read = True
        self.verify = {}   # setting name -> value we wrote and want to read back once it's safe
        self.verify_enabled = False  # read-backs cause extra dongle traffic (and blinks) - test mode only
        self.live = LiveSource()
        self.next_frame = 0.0
        self.last_frame_sent = None
        self.last_frame_time = 0.0
        self.stream_ok = self.stream_fail = self.stream_fail_run = 0
        self.next_stream_log = 0.0
        self.state = dict(status="searching", message="Looking for your TK-63 Pro...",
                          connection=None, model=None, firmware=None, report_rate=None,
                          battery=None, lighting=None, lighting_known=False, pending=False,
                          debounce=None, sleep=None)

    # ---- called from HTTP threads ----
    def snapshot(self):
        with self.lock:
            s = dict(self.state)
            s["pending"] = bool(self.pending)
            s["verify_enabled"] = self.verify_enabled
            color, levels, peak = self.live.latest()
            s["live"] = dict(kind=self.live.kind, color="#%02X%02X%02X" % color, levels=levels[:43],
                             audio=peak > 0.0005, error=self.live.error, smooth=self.live.smooth,
                             fps_ok=self.stream_ok, fps_fail=self.stream_fail)
            for key, state_key in (("lighting", "lighting"), ("rate", "report_rate"),
                                   ("debounce", "debounce"), ("sleep", "sleep")):
                if key in self.pending:
                    s[state_key] = self.pending[key]
            return s

    def _queue(self, key, value):
        with self.lock:
            self.pending[key] = value

    def request_lighting(self, settings):
        kbp.lighting_packet(**settings)  # validate now so the UI gets an error immediately
        with self.lock:
            same = (self.state["lighting_known"] and self.state["lighting"] is not None and
                    kbp.lighting_packet(**self.state["lighting"]) == kbp.lighting_packet(**settings))
            if same:  # keyboard already has exactly this - don't resend (each write makes it blink)
                self.pending.pop("lighting", None)
                return
        self._queue("lighting", settings)

    def request_rate(self, hz):
        if int(hz) not in (125, 250, 500, 1000):
            raise ValueError("bad rate")
        self._queue("rate", int(hz))

    def request_debounce(self, value):
        v = int(value)
        if not kbp.DEBOUNCE_RANGE[0] <= v <= kbp.DEBOUNCE_RANGE[1]:
            raise ValueError("bad sensitivity")
        self._queue("debounce", v)

    def request_sleep(self, s):
        out = {}
        for k, rng in (("light_off_bt", kbp.LIGHT_OFF_MIN), ("light_off_24", kbp.LIGHT_OFF_MIN),
                       ("sleep_bt", kbp.SLEEP_MIN), ("sleep_24", kbp.SLEEP_MIN)):
            v = int(s[k])
            if not rng[0] <= v <= rng[1]:
                raise ValueError(f"bad {k}")
            out[k] = v
        self._queue("sleep", out)

    def request_refresh(self):
        with self.lock:
            self.need_read = True

    # ---- internals ----
    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _disconnect(self, status="searching", message="Looking for your TK-63 Pro..."):
        if self.kb:
            try:
                self.kb.close()
            except Exception:
                pass
        self.kb, self.info = None, None
        self.need_read = True
        self._set(status=status, message=message, connection=None, battery=None)

    def _quiet(self):
        return time.time() - self.last_write

    def _connect(self):
        info, how = kbp.find_keyboard()
        if not info:
            self._set(status="searching", message="Keyboard not found. Plug in the USB cable or the 2.4G dongle.")
            return
        try:
            self.kb = kbp.TK63Pro(info)
        except OSError as e:
            self._set(status="error", message=f"Could not open the keyboard: {e}")
            return
        self.info = info
        if how == "2.4G dongle":
            self.kb.check24()
        self._set(status="ready", connection=how, message="Connected")

    def _jammed(self):
        self.retry_after = time.time() + 10
        self._set(status="jammed",
                  message="The keyboard stopped answering. Leave it a minute, or unplug and replug it.")

    def _read_all(self):
        if self.kb.is24 and not self.kb.online():
            self._set(status="asleep", message="Keyboard is asleep - press any key on it.")
            self.retry_after = time.time() + 3
            return
        dev_id = self.kb.who_am_i()
        if dev_id is None:
            self._jammed()
            return
        fw = self.kb.firmware()
        rate = self.kb.get_report_rate()
        light = self.kb.get_lighting()
        debounce = self.kb.get_debounce()
        sleep = self.kb.get_sleep()
        model = "TK-63 Pro" if dev_id == kbp.TK63PRO_ID else f"Unknown model #{dev_id}"
        upd = dict(model=model, firmware=fw, report_rate=rate, status="ready",
                   debounce=debounce, sleep=sleep,
                   message="Connected" if dev_id == kbp.TK63PRO_ID else
                   "This doesn't identify as a TK-63 Pro - be careful.")
        if light:
            upd.update(lighting=light, lighting_known=True)
        self._set(**upd)
        self.need_read = False

    def _verify(self):
        """Read back what we last wrote and log whether the keyboard kept it."""
        if self.kb.is24 and not self.kb.online():
            self.retry_after = time.time() + 3
            return
        checks = self.verify
        self.verify = {}
        for key, sent in checks.items():
            if key == "lighting":
                got = self.kb.get_lighting()
                fields = ("mode", "brightness", "speed", "rainbow", "option") + (() if sent.get("rainbow") else ("color",))
                same = got is not None and all(
                    str(got.get(f)).upper() == str(sent.get(f)).upper() for f in fields
                    if f in sent and not (f == "speed" and not kbp.EFFECT_BY_MODE[sent["mode"]][4]))
                shown = describe_lighting(got) if got else None
            elif key == "rate":
                got = self.kb.get_report_rate(); same, shown = got == sent, got
            elif key == "debounce":
                got = self.kb.get_debounce(); same, shown = got == sent, got
            else:
                got = self.kb.get_sleep(); same, shown = got == sent, got
            if got is None:
                wlog(f"CHECK ?? {key}: keyboard didn't answer the read-back")
            else:
                wlog(f"CHECK {'ok  ' if same else 'DIFF'} {key}: keyboard reports {shown}")
                if key == "lighting":
                    self._set(lighting=got)
                elif key == "debounce":
                    self._set(debounce=got)
                elif key == "sleep":
                    self._set(sleep=got)

    def _live_kind(self):
        light = self.state.get("lighting") if self.state.get("lighting_known") else None
        return kbp.LIVE_MODES.get(light["mode"]) if light else None

    def _stream(self, kind, now):
        """Send one live frame if it's time. Returns True if it did USB traffic."""
        if now < self.next_frame:
            return False
        color, levels, _ = self.live.latest()
        if kind == "screen":
            frame = color
            changed = frame != self.last_frame_sent
            interval = 0.04 if changed else 0.5        # official: every 40 ms; we skip repeats
        else:
            if not levels:
                self.next_frame = now + 0.05
                return False
            frame = tuple(levels)
            silent = not any(frame)
            interval = 0.5 if silent and self.last_frame_sent == frame else self.live.frame_interval()
        if frame == self.last_frame_sent and now - self.last_frame_time < interval:
            self.next_frame = now + 0.02
            return False
        if kind == "screen":
            ok = self.kb.stream_screen(*frame)
        else:
            ok = self.kb.stream_audio(list(frame), 0 if kind == "music_edm" else 14)
        self.last_frame_sent, self.last_frame_time = frame, now
        self.next_frame = now + (0.04 if kind == "screen" else self.live.frame_interval())
        if ok:
            self.stream_ok += 1
            self.stream_fail_run = 0
        else:
            self.stream_fail += 1
            self.stream_fail_run += 1
            if self.stream_fail_run >= 3:
                wlog(f"STREAM paused: {self.stream_fail_run} frames in a row refused by the keyboard")
                self.retry_after = now + 3
                self.stream_fail_run = 0
        if now >= self.next_stream_log:
            if self.stream_ok or self.stream_fail:
                wlog(f"STREAM {kind}: {self.stream_ok} frames sent, {self.stream_fail} refused (last 10 s)")
            self.stream_ok = self.stream_fail = 0
            self.next_stream_log = now + 10
        return True

    def run(self):
        while True:
            time.sleep(0.01)
            now = time.time()
            kind = self._live_kind() if self.kb else None
            if kind != self.live.kind:
                wlog(f"STREAM {'start ' + kind if kind else 'stop'}")
            self.live.set_kind(kind)
            if kind is None:
                self.last_frame_sent = None
            try:
                # presence check (enumeration only - doesn't touch the keyboard's settings channel)
                if self.kb and now >= self.next_presence:
                    self.next_presence = now + 3
                    info, _ = kbp.find_keyboard()
                    if not info or info["path"] != self.info["path"]:
                        self._disconnect()
                        continue
                if not self.kb:
                    if now >= self.next_connect:
                        self.next_connect = now + 3
                        self._connect()
                    continue
                if now < self.retry_after or self._quiet() < kbp.QUIET_AFTER_WRITE:
                    continue

                with self.lock:
                    job = next(iter(self.pending.items()), None)  # oldest queued setting first
                if job is not None:
                    if self.kb.is24 and not self.kb.online():
                        self._set(status="asleep", message="Keyboard is asleep - press any key on it to apply.")
                        self.retry_after = now + 2
                        continue
                    self._set(status="applying", message="Applying...")
                    key, value = job
                    if key == "lighting":
                        ok, state_upd = self.kb.set_lighting(**value), dict(lighting=value, lighting_known=True)
                    elif key == "rate":
                        ok, state_upd = self.kb.set_report_rate(value), dict(report_rate=value)
                    elif key == "debounce":
                        ok, state_upd = self.kb.set_debounce(value), dict(debounce=value)
                    else:
                        ok, state_upd = self.kb.set_sleep(**value), dict(sleep=value)
                    self.last_write = time.time()
                    desc = describe_lighting(value) if key == "lighting" else f"{key} = {value}"
                    raw = getattr(self.kb, "last_packet", b"").hex(" ")
                    wlog(f"SEND {'ok  ' if ok else 'FAIL'} {desc}   [bytes: {raw}]")
                    if ok:
                        with self.lock:
                            if self.pending.get(key) == value:
                                del self.pending[key]
                            self.state.update(state_upd)
                        if self.verify_enabled:
                            self.verify[key] = value
                    if ok:
                        self._set(status="ready", message="Saved to keyboard")
                    else:
                        self._jammed()
                    continue

                if self.need_read and self._quiet() >= kbp.QUIET_BEFORE_READ:
                    self._read_all()
                    continue

                if self.verify and self._quiet() >= kbp.QUIET_BEFORE_READ:
                    self._verify()
                    continue

                if kind is not None and not (self.kb.is24 and self.state["status"] == "asleep"):
                    self._stream(kind, now)
                    if not self.kb.is24 or now < self.next_battery:
                        continue  # battery/online poll below still runs every 20 s on 2.4G

                if self.kb.is24 and now >= self.next_battery:
                    self.next_battery = now + 20
                    s = self.kb.status24()
                    if s:
                        upd = dict(battery=s["battery"])
                        if not s["online"]:
                            upd.update(status="asleep", message="Keyboard is asleep - press any key on it.")
                        elif self.state["status"] == "asleep":
                            upd.update(status="ready", message="Connected")
                        self._set(**upd)
            except Exception as e:  # never let the worker die
                self._set(status="error", message=f"Error: {e}")
                self.retry_after = time.time() + 5


worker = Worker()
last_poll = [time.time()]
seen_poll = [False]


def load_presets():
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_presets(p):
    tmp = PRESETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, PRESETS_FILE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "ui", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            last_poll[0], seen_poll[0] = time.time(), True
            s = worker.snapshot()
            s["effects"] = [dict(id=e[0], name=e[1], mode=e[2], color=e[3], speed=e[4],
                                 live=e[2] in kbp.LIVE_MODES, brightness=e[2] not in (0, 21),
                                 desc=kbp.DESCRIPTIONS.get(e[2], ""),
                                 experimental=e[2] in kbp.EXPERIMENTAL_OPTIONS,
                                 options=[dict(label=l, value=v) for l, v in (e[5] or [])]) for e in kbp.EFFECTS]
            return self._send(200, s)
        if self.path == "/api/presets":
            return self._send(200, load_presets())
        if self.path == "/api/ping":
            return self._send(200, {"app": "tk63pro-control"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("Content-Type") != "application/json":
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            if self.path == "/api/lighting":
                keys = ("mode", "brightness", "speed", "color", "rainbow", "option")
                worker.request_lighting({k: body[k] for k in keys if k in body})
            elif self.path == "/api/report-rate":
                worker.request_rate(body["hz"])
            elif self.path == "/api/debounce":
                worker.request_debounce(body["value"])
            elif self.path == "/api/sleep":
                worker.request_sleep(body)
            elif self.path == "/api/smooth":
                worker.live.smooth = max(0, min(4, int(body["value"])))
                worker.live.smoothed = None
                wlog(f"MUSIC smoothness = {worker.live.smooth}")
            elif self.path == "/api/verify":
                worker.verify_enabled = bool(body.get("on"))
                wlog(f"VERIFY read-backs {'ON (test mode)' if worker.verify_enabled else 'OFF'}")
            elif self.path == "/api/note":
                wlog(f"NOTE  {str(body.get('text', ''))[:300]}")
            elif self.path == "/api/refresh":
                worker.request_refresh()
            elif self.path == "/api/presets":
                save_presets(body if isinstance(body, list) else [])
            elif self.path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except (ValueError, KeyError, TypeError) as e:
            self._send(400, {"error": str(e)})


def open_window(url):
    for exe in (shutil.which("msedge"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1100,780"])
            return
    webbrowser.open(url)


def already_running():
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1) as r:
            return b"tk63pro-control" in r.read()
    except OSError:
        return False


def idle_watchdog():
    while True:
        time.sleep(5)
        if seen_poll[0] and time.time() - last_poll[0] > IDLE_EXIT_SECONDS:
            os._exit(0)


def main():
    url = f"http://127.0.0.1:{PORT}/"
    if already_running():
        if "--no-window" not in sys.argv:
            open_window(url)
        return
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    wlog("---- app started ----")
    worker.start()
    threading.Thread(target=idle_watchdog, daemon=True).start()
    if "--no-window" not in sys.argv:
        open_window(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
