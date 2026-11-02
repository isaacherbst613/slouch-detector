"""Slouch Detector: webcam + MediaPipe pose, sits in the system tray and nudges you when you slouch.

First time:   run install.bat (installs packages, adds Start menu/desktop shortcuts, starts with Windows)
Run:          pythonw posture.py      (opens the settings page at http://127.0.0.1:8765; running it again just reopens it)
Debug:        python posture.py       (prints each reading)
"""
import collections
import json
import math
import os
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave
import webbrowser
import winreg
import winsound
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
# Everything we write goes to the user's AppData, so the code folder can be read-only (e.g. Program Files)
DATA = Path(os.environ["LOCALAPPDATA"]) / "SlouchDetector"
DATA.mkdir(parents=True, exist_ok=True)
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task"
MODEL = DATA / "pose_landmarker_full.task"
SETTINGS = DATA / "settings.json"
HISTORY = DATA / "history.json"
PAGE = HERE / "settings.html"
ICON = DATA / "slouch.ico"
PING = DATA / "ping.wav"
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"  # not "localhost": Windows tries IPv6 first and each request stalls ~200 ms
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
DEFAULTS = {"view": "front", "seconds": 30, "tolerance": 12, "interval": 3, "camera": 0, "alert": "popup", "baseline": None}
ALERTS = ("popup", "sound", "both")
CALIBRATION_SAMPLES = 15
LIVE_FOR = 5  # keep the camera streaming this many seconds after the settings page last asked for anything
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SlouchDetector"
COLORS = {"good": "#B5CC2E", "bad": "#E8A33D", "": "#9AA6B2"}  # tray bubble colour per state


def measure(lm, view, aspect):
    """Posture score from pose landmarks, higher = straighter. None if body parts aren't visible."""
    if view == "front":
        # nose height above the shoulder line, in shoulder widths: drops when you slump or crane forward
        nose, l, r = lm[0], lm[11], lm[12]
        if min(nose.visibility, l.visibility, r.visibility) < 0.5:
            return None
        width = math.hypot((l.x - r.x) * aspect, l.y - r.y)
        return ((l.y + r.y) / 2 - nose.y) / width
    # side: how far the ear leans forward of the shoulder, in degrees (negated so higher = straighter)
    ear, sh = max([(lm[7], lm[11]), (lm[8], lm[12])], key=lambda p: min(p[0].visibility, p[1].visibility))
    if min(ear.visibility, sh.visibility) < 0.5:
        return None
    return -math.degrees(math.atan2(abs(ear.x - sh.x) * aspect, sh.y - ear.y))


def slouch_ratio(score, baseline, view, tolerance):
    """0 = calibrated posture, 1 = at the slouch threshold, >1 = slouching.
    front: tolerance is % below baseline; side: tolerance is degrees of extra lean."""
    drop = (1 - score / baseline) * 100 if view == "front" else baseline - score
    return drop / tolerance


def load_settings():
    try:
        return DEFAULTS | json.loads(SETTINGS.read_text())
    except (FileNotFoundError, ValueError):
        return dict(DEFAULTS)


def save_settings(cfg):
    SETTINGS.write_text(json.dumps(cfg, indent=2))


def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_history(history):
    HISTORY.write_text(json.dumps(history))


def record(history, when, kind, seconds=0):
    """Add to the day's totals. kind: "straight" / "slouch" (seconds) or "reminder" (a count)."""
    t = time.localtime(when)
    day = history.setdefault(time.strftime("%Y-%m-%d", t),
                             {"straight": 0, "slouch": 0, "reminders": 0, "slouch_by_hour": [0] * 24})
    if kind == "reminder":
        day["reminders"] += 1
    else:
        day[kind] += seconds
        if kind == "slouch":
            day["slouch_by_hour"][t.tm_hour] += seconds


def clean(new, cfg):
    """Apply settings posted from the page to a copy of cfg. Raises ValueError/TypeError on junk."""
    out = dict(cfg)
    if new.get("view") in ("front", "side"):
        out["view"] = new["view"]
    if new.get("alert") in ALERTS:
        out["alert"] = new["alert"]
    for key, lo, hi in [("seconds", 1, 3600), ("tolerance", 1, 90), ("interval", 1, 60), ("camera", 0, 9)]:
        if key in new:
            out[key] = min(max(float(new[key]), lo), hi)
    out["camera"] = int(out["camera"])
    if (out["view"], out["camera"]) != (cfg["view"], cfg["camera"]):
        out["baseline"] = None  # old calibration means nothing from a different angle
    return out


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except FileNotFoundError:
        return False


def set_autostart(on):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{Path(__file__).resolve()}" --background')
        elif autostart_enabled():
            winreg.DeleteValue(k, RUN_NAME)


def icon_image(level):
    """A little spirit-level vial: the bubble colour shows the state."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 16, 61, 47), radius=15, fill=COLORS[level], outline="#1F2A36", width=4)
    d.ellipse((22, 23, 41, 40), fill="white")
    return img


def make_ping():
    """A soft two-tone ping, generated once so it sounds the same on every PC (Windows sound schemes vary)."""
    rate = 44100
    samples = bytearray()
    for i in range(int(rate * 0.7)):
        t = i / rate
        envelope = min(1, t * 300) * math.exp(-t * 6)  # quick fade-in avoids a click, then a bell-like decay
        s = 0.55 * math.sin(2 * math.pi * 988 * t) + 0.2 * math.sin(2 * math.pi * 1976 * t)
        samples += struct.pack("<h", int(s * envelope * 32767))
    with wave.open(str(PING), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))


def install():
    """Start with Windows + Start menu and desktop shortcuts. Run by install.bat."""
    set_autostart(True)
    icon_image("good").save(ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # PowerShell single-quoted string
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = '"' + str(Path(__file__).resolve()) + '"'
    ps = "$w = New-Object -ComObject WScript.Shell\n" + "".join(
        f"$s = $w.CreateShortcut((Join-Path ([Environment]::GetFolderPath('{folder}')) 'Slouch Detector.lnk'))\n"
        f"$s.TargetPath = {q(pythonw)}; $s.Arguments = {q(script)}\n"
        f"$s.WorkingDirectory = {q(HERE)}; $s.IconLocation = {q(ICON)}; $s.Save()\n"
        for folder in ("Programs", "Desktop"))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def open_camera(index):
    import cv2
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if cap.isOpened():
        return cap
    cap.release()
    return None


def grab(index):
    # Background mode: open/close the camera per check so Teams/Zoom can still use it.
    cap = open_camera(index)
    if cap is None:
        return None
    try:
        frame = None
        for _ in range(5):  # first frames are dark while auto-exposure settles
            ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def make_handler(cfg, state, wake, on_change, alert, history, lock):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # pythonw has no stderr

        def send(self, body, ctype="text/plain", code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def from_us(self):
            # Host check blocks DNS-rebinding pages from reading the camera preview
            return self.headers.get("Host") in ALLOWED_HOSTS

        def do_GET(self):
            if not self.from_us():
                return self.send(b"", code=403)
            path = self.path.split("?")[0]
            if path in ("/state", "/frame.jpg"):
                state["page_seen"] = time.time()
                wake.set()  # switch to live camera straight away
            if path == "/":
                self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/state":
                public = {k: v for k, v in state.items() if k not in ("jpeg", "page_seen")}
                self.send(json.dumps(cfg | public | {"autostart": autostart_enabled()}).encode(), "application/json")
            elif path == "/history":
                with lock:
                    recent_days = {d: history[d] for d in sorted(history)[-14:]}
                self.send(json.dumps({"today": time.strftime("%Y-%m-%d"), "days": recent_days}).encode(), "application/json")
            elif path == "/frame.jpg" and state["jpeg"]:
                self.send(state["jpeg"], "image/jpeg")
            else:
                self.send(b"not found", code=404)

        def do_POST(self):
            # JSON content type forces a CORS preflight from other sites, which we never answer
            if not self.from_us() or self.headers.get("Content-Type") != "application/json":
                return self.send(b"", code=403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/settings":
                    cfg.update(clean(body, cfg))
                    save_settings(cfg)
                elif self.path == "/calibrate":
                    state["calibrating"] = True
                elif self.path == "/pause":
                    state["paused"] = bool(body["paused"])
                elif self.path == "/autostart":
                    set_autostart(bool(body["on"]))
                elif self.path == "/test-alert":
                    alert()
                else:
                    return self.send(b"not found", code=404)
            except (ValueError, TypeError, AttributeError, KeyError):
                return self.send(b"bad request", code=400)
            wake.set()
            on_change()
            self.send(b"{}", "application/json")

    return Handler


def detect_loop(cfg, state, wake, tray, history, lock):
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    if not MODEL.exists():
        state["status"] = "Downloading the pose model (one time, ~9 MB)…"
        urllib.request.urlretrieve(MODEL_URL, MODEL)
    landmarker = vision.PoseLandmarker.create_from_options(
        vision.PoseLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(MODEL))))

    def analyse(frame):
        h, w = frame.shape[:2]
        res = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        lm = res.pose_landmarks[0] if res.pose_landmarks else None
        if lm:  # draw the head/shoulder points the score uses, in level green
            pts = {i: (int(lm[i].x * w), int(lm[i].y * h)) for i in (0, 7, 8, 11, 12) if lm[i].visibility > 0.5}
            for a, b in [(11, 12), (7, 11), (8, 12), (0, 11), (0, 12)]:
                if a in pts and b in pts:
                    cv2.line(frame, pts[a], pts[b], (46, 204, 181), 3, cv2.LINE_AA)
            for p in pts.values():
                cv2.circle(frame, p, 7, (255, 255, 255), -1, cv2.LINE_AA)
        state["jpeg"] = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])[1].tobytes()
        return measure(lm, cfg["view"], w / h) if lm else None

    def show(status, level="", ratio=None):
        state.update(status=status, level=level, ratio=ratio)
        tray(level, status)

    cap = cap_index = None
    calibration = []
    slouch_since = None
    last_tick, last_save = None, time.time()
    recent = collections.deque(maxlen=3)  # median of last few readings smooths out single-frame glitches
    while True:
        if state["paused"]:
            if cap:
                cap.release()
                cap = None
            state["jpeg"], slouch_since, last_tick = None, None, None
            show("Paused")
            wake.wait()
            wake.clear()
            continue

        # Live mode (page open or calibrating): keep the camera open for a smooth preview.
        live = state["calibrating"] or time.time() - state["page_seen"] < LIVE_FOR
        if cap and (not live or cap_index != cfg["camera"]):
            cap.release()
            cap = None
        if live and cap is None:
            cap, cap_index = open_camera(cfg["camera"]), cfg["camera"]
        if cap:
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap, frame = None, None
        else:
            frame = grab(cfg["camera"])
        raw = analyse(frame) if frame is not None else None

        if state["calibrating"]:
            if raw is not None:
                calibration.append(raw)
            state["progress"] = len(calibration) / CALIBRATION_SAMPLES
            show("Hold still…")
            if len(calibration) == CALIBRATION_SAMPLES:
                cfg["baseline"] = statistics.median(calibration)
                save_settings(cfg)
                calibration.clear()
                recent.clear()
                state.update(calibrating=False, progress=0)
            wake.wait(0.05)
            wake.clear()
            continue

        if raw is None:
            recent.clear()
        else:
            recent.append(raw)
        score = statistics.median(recent) if recent else None
        now = time.time()
        kind = None  # what this check counts towards in the progress stats
        if frame is None:
            slouch_since = None
            show("Camera unavailable")
        elif score is None:
            slouch_since = None
            show("Can't see you")
        elif cfg["baseline"] is None:
            show("Not calibrated")
        else:
            ratio = slouch_ratio(score, cfg["baseline"], cfg["view"], cfg["tolerance"])
            if ratio <= 1:
                slouch_since, kind = None, "straight"
                show("Sitting straight", "good", ratio)
            else:
                slouch_since, kind = slouch_since or now, "slouch"
                show("Slouching", "bad", ratio)
                if now - slouch_since >= cfg["seconds"]:
                    tray.alert()
                    with lock:
                        record(history, now, "reminder")
                    slouch_since = now  # nag again only after another full stretch of slouching
        state["slouching_for"] = 0 if slouch_since is None else now - slouch_since

        # Progress: time since the last counted check goes to this check's state. Gaps (away, camera busy,
        # PC asleep) aren't counted, and a single step is capped so a long stall can't inflate the totals.
        if kind and last_tick:
            with lock:
                record(history, now, kind, min(now - last_tick, cfg["interval"] * 2 + 2))
        last_tick = now if kind else None
        if now - last_save > 60:
            with lock:
                save_history(history)
            last_save = now
        print(f"{time.strftime('%X')} raw={raw} score={score} {state['status']}")
        wake.wait(0.05 if live else cfg["interval"])  # page activity / settings changes wake us early
        wake.clear()


def main():
    background = "--background" in sys.argv  # set by the Windows startup entry
    cfg = load_settings()
    state = {"status": "Starting…", "level": "", "ratio": None, "slouching_for": 0, "paused": False,
             "calibrating": False, "progress": 0, "jpeg": None, "page_seen": 0}
    wake = threading.Event()
    history, lock = load_history(), threading.Lock()
    open_page = lambda *_: webbrowser.open(URL)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), None)
    except OSError:  # already running: just show its settings page
        open_page()
        return

    import pystray

    def toggle_pause(*_):
        state["paused"] = not state["paused"]
        wake.set()

    def quit_app(*_):
        with lock:
            save_history(history)
        icon.stop()

    icon = pystray.Icon("SlouchDetector", icon_image(""), "Slouch Detector", pystray.Menu(
        pystray.MenuItem("Open Slouch Detector", open_page, default=True),
        pystray.MenuItem("Pause", toggle_pause, checked=lambda _: state["paused"]),
        pystray.MenuItem("Quit", quit_app),
    ))
    last_level = [""]

    def tray(level, status):
        icon.title = f"Slouch Detector: {status}"
        if level != last_level[0]:
            icon.icon, last_level[0] = icon_image(level), level

    if not PING.exists():
        make_ping()

    def alert():
        if cfg["alert"] in ("sound", "both"):
            winsound.PlaySound(str(PING), winsound.SND_FILENAME | winsound.SND_ASYNC)
        if cfg["alert"] in ("popup", "both"):
            icon.notify("You've been slouching for a while.", "Time to sit up straight")
    tray.alert = alert

    server.RequestHandlerClass = make_handler(cfg, state, wake, icon.update_menu, alert, history, lock)
    server.daemon_threads = True

    def setup(icon):
        icon.visible = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=detect_loop, args=(cfg, state, wake, tray, history, lock), daemon=True).start()
        if not background or cfg["baseline"] is None:
            open_page()

    icon.run(setup)  # blocks until Quit


def selftest():
    pt = lambda x, y: SimpleNamespace(x=x, y=y, visibility=1.0)
    lm = [pt(0.5, 0.5)] * 33
    # front: nose 0.3 above shoulders that are 0.2 apart -> 1.5 shoulder widths
    front = lm.copy(); front[0], front[11], front[12] = pt(0.5, 0.2), pt(0.6, 0.5), pt(0.4, 0.5)
    assert math.isclose(measure(front, "front", 1.0), 1.5)
    # side: ear straight above shoulder = 0 deg, ear forward by its height = 45 deg
    side = lm.copy(); side[7], side[11] = pt(0.5, 0.3), pt(0.5, 0.5)
    assert math.isclose(measure(side, "side", 1.0), 0, abs_tol=1e-9)
    side[7] = pt(0.7, 0.3)
    assert math.isclose(measure(side, "side", 1.0), -45)
    assert slouch_ratio(1.2, 1.5, "front", 12) > 1 > slouch_ratio(1.4, 1.5, "front", 12)
    assert math.isclose(slouch_ratio(1.5, 1.5, "front", 12), 0)
    # progress: seconds land on the right day and hour, reminders are counted
    h, t = {}, time.mktime((2026, 11, 2, 15, 30, 0, 0, 0, -1))
    record(h, t, "straight", 3); record(h, t, "slouch", 5); record(h, t, "slouch", 2); record(h, t, "reminder")
    assert h["2026-11-02"]["straight"] == 3 and h["2026-11-02"]["slouch"] == 7 and h["2026-11-02"]["reminders"] == 1
    assert h["2026-11-02"]["slouch_by_hour"][15] == 7 and sum(h["2026-11-02"]["slouch_by_hour"]) == 7
    assert slouch_ratio(-20, -5, "side", 10) > 1 > slouch_ratio(-12, -5, "side", 10)
    front[0].visibility = 0.1
    assert measure(front, "front", 1.0) is None
    # settings: clamped, typed, and switching view drops the old calibration
    cfg = DEFAULTS | {"baseline": 1.2}
    assert clean({"seconds": "45", "tolerance": 500}, cfg) == cfg | {"seconds": 45, "tolerance": 90}
    assert clean({"view": "side"}, cfg)["baseline"] is None
    assert clean({"view": "nonsense"}, cfg)["view"] == "front"
    assert clean({"alert": "sound"}, cfg)["alert"] == "sound" and clean({"alert": "x"}, cfg)["alert"] == "popup"
    try:
        clean({"seconds": "abc"}, cfg)
        assert False
    except ValueError:
        pass
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--install" in sys.argv:
        install()
    else:
        main()
