"""Tray daemon: holds recording state, controls audio, talks to clients via TCP socket.

Modes:
  python -m recorder.daemon             # long-lived service: tray + IPC server
  python -m recorder.daemon --start     # IPC client: tells the daemon to start
  python -m recorder.daemon --stop      # IPC client: tells the daemon to stop
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform != "win32":
    # AppIndicator is the only tray backend that works on GNOME/Wayland; without this
    # pystray auto-falls-back to Xorg/Xlib, which then fails on Wayland sessions.
    os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")

from PIL import Image, ImageDraw  # noqa: E402
from pystray import Icon, Menu, MenuItem  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

if sys.platform == "win32":
    RECORDINGS_DIR = Path(os.environ.get("TEMP", "C:/Temp")) / "meeting-recorder"
else:
    RECORDINGS_DIR = Path("/tmp/meeting-recorder")

# IPC: TCP localhost so it works on both Linux and Windows
IPC_HOST = "127.0.0.1"
IPC_PORT = 57321

# Linux-only
NULL_SINK = "meetingmix"
if sys.platform != "win32":
    RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/meeting-recorder-{os.getuid()}")
    SOCKET_PATH = RUNTIME_DIR / "meeting-recorder.sock"

LANGUAGES = [
    ("Auto-detect", ""),
    ("Portuguese (PT)", "pt"),
    ("English (EN)", "en"),
    ("Spanish (ES)", "es"),
]


def notify(title: str, body: str = "") -> None:
    if sys.platform == "win32":
        try:
            from plyer import notification
            notification.notify(app_name="Meeting Recorder", title=title, message=body, timeout=5)
        except Exception:
            pass
    else:
        try:
            subprocess.run(["notify-send", "-a", "Meeting Recorder", title, body], check=False)
        except FileNotFoundError:
            pass


def make_icon_image(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([6, 6, 58, 58], fill=color)
    return img


class Recorder:
    """Owns the recording state. All mutating methods take the lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None  # Linux only
        self._module_ids: list[int] = []            # Linux only
        self._wav_path: Path | None = None
        self._started_at: dt.datetime | None = None
        self._next_language: str = ""
        self._recording_language: str = ""
        # Windows audio backend
        if sys.platform == "win32":
            from recorder.audio_win import AudioRecorderWin
            self._win_recorder = AudioRecorderWin()
        else:
            self._win_recorder = None

    def is_recording(self) -> bool:
        with self._lock:
            return self._wav_path is not None

    def started_at(self) -> dt.datetime | None:
        with self._lock:
            return self._started_at

    def get_language(self) -> str:
        with self._lock:
            return self._next_language

    def set_language(self, lang: str) -> None:
        with self._lock:
            self._next_language = lang

    def language_label(self) -> str:
        lang = self.get_language()
        return next((label for label, code in LANGUAGES if code == lang), "Auto-detect")

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._wav_path is not None:
                return False, "already recording"

            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            wav_path = RECORDINGS_DIR / f"recording-{ts}.wav"
            self._recording_language = self._next_language

            if sys.platform == "win32":
                try:
                    self._win_recorder.start(wav_path)
                except Exception as e:
                    return False, f"audio start failed: {e}"
            else:
                try:
                    module_ids = self._setup_pipewire()
                except RuntimeError as e:
                    return False, f"pipewire setup failed: {e}"

                try:
                    proc = subprocess.Popen(
                        [
                            "parecord",
                            f"--device={NULL_SINK}.monitor",
                            "--file-format=wav",
                            "--format=s16le",
                            "--rate=16000",
                            "--channels=1",
                            str(wav_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                except FileNotFoundError:
                    self._unload_modules(module_ids)
                    return False, "parecord not found (install pulseaudio-utils)"

                self._proc = proc
                self._module_ids = module_ids

            self._wav_path = wav_path
            self._started_at = dt.datetime.now()
            return True, str(wav_path)

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if self._wav_path is None:
                return False, "not recording"
            proc = self._proc
            module_ids = self._module_ids
            wav_path = self._wav_path
            recording_language = self._recording_language
            self._proc = None
            self._module_ids = []
            self._wav_path = None
            self._started_at = None
            self._recording_language = ""

        if sys.platform == "win32":
            self._win_recorder.stop()
        else:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self._unload_modules(module_ids)

        if wav_path is None or not wav_path.exists() or wav_path.stat().st_size < 1024:
            return False, f"recording too short or empty: {wav_path}"

        env = os.environ.copy()
        env["WHISPER_LANGUAGE"] = recording_language

        subprocess.Popen(
            [sys.executable, "-m", "recorder.pipeline", str(wav_path)],
            cwd=str(REPO_ROOT),
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, str(wav_path)

    def shutdown(self) -> None:
        if self.is_recording():
            self.stop()

    def _setup_pipewire(self) -> list[int]:
        ids: list[int] = []
        try:
            ids.append(self._load_module(
                "module-null-sink",
                f"sink_name={NULL_SINK}",
                "sink_properties=device.description=MeetingMix",
            ))
            # Loop from every output sink monitor so we capture audio regardless of
            # which device the meeting app chose (headset, built-in speakers, USB, etc).
            for sink_name in self._list_sink_names():
                if sink_name == NULL_SINK:
                    continue
                ids.append(self._load_module(
                    "module-loopback",
                    f"source={sink_name}.monitor",
                    f"sink={NULL_SINK}",
                    "latency_msec=20",
                ))
            # Microphone
            ids.append(self._load_module(
                "module-loopback",
                "source=@DEFAULT_SOURCE@",
                f"sink={NULL_SINK}",
                "latency_msec=20",
            ))
        except RuntimeError:
            self._unload_modules(ids)
            raise
        return ids

    @staticmethod
    def _list_sink_names() -> list[str]:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True,
        )
        names = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                names.append(parts[1])
        return names

    @staticmethod
    def _load_module(name: str, *args: str) -> int:
        result = subprocess.run(
            ["pactl", "load-module", name, *args],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"pactl load-module {name} failed")
        return int(result.stdout.strip())

    @staticmethod
    def _unload_modules(ids: list[int]) -> None:
        for mid in reversed(ids):
            subprocess.run(["pactl", "unload-module", str(mid)], check=False)


class AmbientScheduler:
    """Always-on ambient recorder: chunks audio every N minutes during a time window.

    Pauses automatically when the manual Recorder is active so both don't compete
    for the same audio device. Windows-only (requires AudioRecorderWin).
    """

    WINDOW_START = (0, 0)    # inclusive
    WINDOW_END   = (0, 0)    # exclusive — midnight treated as 24:00 → always-on
    CHUNK_MINUTES = 15
    CHECK_INTERVAL = 20      # seconds between scheduler ticks

    AUDIO_POLL_INTERVAL = 60   # seconds between audio-source snapshots
    AUDIO_PEAK_THRESHOLD = 0.002

    END_OF_DAY_HOUR = 22  # trigger /process-inbox once per day after this hour

    def __init__(self, manual_recorder: Recorder) -> None:
        self._manual = manual_recorder
        self._wav_path: Path | None = None
        self._chunk_started: dt.datetime | None = None
        self._paused = False
        self._lock = threading.Lock()
        self._audio_log: list[dict] = []   # [{time, apps}] accumulated per chunk
        self._log_lock = threading.Lock()
        self._last_process_inbox_date: dt.date | None = None
        if sys.platform == "win32":
            from recorder.audio_win import AudioRecorderWin
            self._win_recorder = AudioRecorderWin()
        else:
            self._win_recorder = None

    def start_thread(self) -> None:
        if sys.platform != "win32":
            return
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        self._flush_chunk()

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def shutdown(self) -> None:
        self._flush_chunk()

    def _in_window(self) -> bool:
        now = dt.datetime.now()
        cur = now.hour * 60 + now.minute
        s = self.WINDOW_START[0] * 60 + self.WINDOW_START[1]
        e = self.WINDOW_END[0] * 60 + self.WINDOW_END[1]
        if e == 0:
            e = 24 * 60
        return s <= cur < e

    def _loop(self) -> None:
        while True:
            time.sleep(self.CHECK_INTERVAL)
            with self._lock:
                paused = self._paused

            if paused or self._manual.is_recording():
                if self._wav_path is not None:
                    self._flush_chunk()
                continue

            if self._in_window():
                if self._wav_path is None:
                    self._start_chunk()
                else:
                    elapsed = (dt.datetime.now() - self._chunk_started).total_seconds()
                    if elapsed >= self.CHUNK_MINUTES * 60:
                        self._flush_chunk()
                        self._start_chunk()
            else:
                if self._wav_path is not None:
                    self._flush_chunk()

            self._maybe_trigger_process_inbox()

    def _poll_audio_sources(self) -> list[str]:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            sessions = AudioUtilities.GetAllSessions()
            active: list[str] = []
            for s in sessions:
                if s.Process is None:
                    continue
                try:
                    meter = s._ctl.QueryInterface(IAudioMeterInformation)
                    if meter.GetPeakValue() > self.AUDIO_PEAK_THRESHOLD:
                        name = s.Process.name()
                        if name not in active:
                            active.append(name)
                except Exception:
                    pass
            return active
        except Exception:
            return []

    def _poll_loop(self) -> None:
        while True:
            time.sleep(self.AUDIO_POLL_INTERVAL)
            if self._wav_path is not None:
                apps = self._poll_audio_sources()
                if apps:
                    entry = {"time": dt.datetime.now().strftime("%H:%M"), "apps": apps}
                    with self._log_lock:
                        self._audio_log.append(entry)

    def _start_chunk(self) -> None:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        wav_path = RECORDINGS_DIR / f"ambient-{ts}.wav"
        try:
            self._win_recorder.start(wav_path)
            self._wav_path = wav_path
            self._chunk_started = dt.datetime.now()
            with self._log_lock:
                self._audio_log = []
        except Exception:
            pass

    def _maybe_trigger_process_inbox(self) -> None:
        now = dt.datetime.now()
        if now.hour < self.END_OF_DAY_HOUR:
            return
        today = now.date()
        if self._last_process_inbox_date == today:
            return
        self._last_process_inbox_date = today
        self._dispatch_process_inbox()

    def _dispatch_process_inbox(self) -> None:
        vault_path = os.environ.get("VAULT_PATH", "")
        if sys.platform == "win32":
            wsl_path = vault_path.replace("\\\\wsl.localhost\\Ubuntu", "").replace("\\", "/")
            if not wsl_path:
                # Neutral fallback; set VAULT_PATH (UNC form) in .env for your machine.
                wsl_path = os.environ.get("WSL_VAULT_PATH") or "/home/user/Documents/vault"
            cmd = [
                "wsl.exe", "-d", "Ubuntu", "--", "bash", "-c",
                f"cd '{wsl_path}' && claude --dangerously-skip-permissions -p '/process-inbox'",
            ]
        else:
            vault_dir = vault_path or str(Path.home() / "Documents/Vault")
            cmd = ["claude", "--dangerously-skip-permissions", "-p", "/process-inbox"]
        subprocess.Popen(
            cmd,
            cwd=None if sys.platform == "win32" else vault_dir,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _flush_chunk(self) -> None:
        if self._wav_path is None:
            return
        wav_path = self._wav_path
        self._wav_path = None
        self._chunk_started = None
        with self._log_lock:
            audio_log = list(self._audio_log)
            self._audio_log = []
        try:
            self._win_recorder.stop()
        except Exception:
            pass
        if not wav_path.exists() or wav_path.stat().st_size < 4096:
            try:
                wav_path.unlink()
            except OSError:
                pass
            return
        if audio_log:
            import json as _json
            sidecar = wav_path.with_suffix(".sources.json")
            sidecar.write_text(_json.dumps(audio_log, ensure_ascii=False), encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-m", "recorder.pipeline", "--ambient", str(wav_path)],
            cwd=str(REPO_ROOT),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def serve(recorder: Recorder, icon: Icon) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((IPC_HOST, IPC_PORT))
    srv.listen(4)

    def handle(conn: socket.socket) -> None:
        try:
            data = conn.recv(64).decode("utf-8", errors="replace").strip()
            if data == "start":
                ok, msg = recorder.start()
                if ok:
                    lang = recorder.language_label()
                    icon.icon = make_icon_image("red")
                    icon.title = "Meeting Recorder - recording"
                    icon.update_menu()
                    notify("Recording meeting", f"Language: {lang} - Super+Shift+S to stop")
                else:
                    notify("Start ignored", msg)
                conn.sendall(f"{'OK' if ok else 'ERR'} {msg}\n".encode())
            elif data == "stop":
                ok, msg = recorder.stop()
                if ok:
                    icon.icon = make_icon_image("white")
                    icon.title = "Meeting Recorder - idle"
                    icon.update_menu()
                    notify("Pipeline started", "Whisper + Claude running in background")
                else:
                    notify("Stop ignored", msg)
                conn.sendall(f"{'OK' if ok else 'ERR'} {msg}\n".encode())
            else:
                conn.sendall(b"ERR unknown command\n")
        finally:
            conn.close()

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def tooltip_ticker(recorder: Recorder, icon: Icon) -> None:
    while True:
        time.sleep(2)
        started = recorder.started_at()
        if started is None:
            continue
        elapsed = int((dt.datetime.now() - started).total_seconds())
        m, s = divmod(elapsed, 60)
        icon.title = f"Meeting Recorder - recording {m:02d}:{s:02d}"


def run_daemon() -> int:
    recorder = Recorder()
    ambient = AmbientScheduler(recorder)

    def menu_status(_):
        if recorder.is_recording():
            return f"Recording ({recorder.language_label()})"
        return f"Idle - Language: {recorder.language_label()}"

    def make_language_submenu():
        def set_lang(code):
            def _action(icon_, _item):
                recorder.set_language(code)
                icon_.update_menu()
            return _action

        def is_selected(code):
            return lambda _item: recorder.get_language() == code

        return Menu(*[
            MenuItem(label, set_lang(code), checked=is_selected(code), radio=True)
            for label, code in LANGUAGES
        ])

    icon = Icon(
        "meeting-recorder",
        make_icon_image("white"),
        "Meeting Recorder - idle",
    )

    def on_start(icon_, _item):
        threading.Thread(target=lambda: _client_local("start", recorder, icon_, ambient), daemon=True).start()

    def on_stop(icon_, _item):
        threading.Thread(target=lambda: _client_local("stop", recorder, icon_, ambient), daemon=True).start()

    def on_quit(icon_, _item):
        ambient.shutdown()
        recorder.shutdown()
        icon_.stop()

    icon.menu = Menu(
        MenuItem(menu_status, None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("Language", make_language_submenu()),
        Menu.SEPARATOR,
        MenuItem("Start recording", on_start),
        MenuItem("Stop & process", on_stop),
        Menu.SEPARATOR,
        MenuItem("Quit", on_quit),
    )

    def on_signal(*_):
        ambient.shutdown()
        recorder.shutdown()
        icon.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    threading.Thread(target=serve, args=(recorder, icon), daemon=True).start()
    threading.Thread(target=tooltip_ticker, args=(recorder, icon), daemon=True).start()
    ambient.start_thread()

    if sys.platform == "win32":
        import keyboard

        def _hotkey_start():
            threading.Thread(target=lambda: _client_local("start", recorder, icon, ambient), daemon=True).start()

        def _hotkey_stop():
            threading.Thread(target=lambda: _client_local("stop", recorder, icon, ambient), daemon=True).start()

        keyboard.add_hotkey("ctrl+shift+r", _hotkey_start)
        keyboard.add_hotkey("ctrl+shift+s", _hotkey_stop)

    icon.run()
    return 0


def _client_local(cmd: str, recorder: Recorder, icon: Icon, ambient: AmbientScheduler) -> None:
    """Tray menu shortcut for start/stop — bypasses the socket since we're in-process."""
    if cmd == "start":
        ambient.pause()
        ok, msg = recorder.start()
        if ok:
            lang = recorder.language_label()
            icon.icon = make_icon_image("red")
            icon.title = "Meeting Recorder - recording"
            icon.update_menu()
            notify("Recording meeting", f"Language: {lang} - Ctrl+Shift+S to stop")
        else:
            ambient.resume()
            notify("Start ignored", msg)
    elif cmd == "stop":
        ok, msg = recorder.stop()
        if ok:
            icon.icon = make_icon_image("white")
            icon.title = "Meeting Recorder - idle"
            icon.update_menu()
            notify("Pipeline started", "Whisper + Ollama running in background")
            ambient.resume()
        else:
            notify("Stop ignored", msg)


def send_command(cmd: str) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect((IPC_HOST, IPC_PORT))
            s.sendall(f"{cmd}\n".encode())
            reply = s.recv(256).decode("utf-8", errors="replace").strip()
    except OSError as e:
        notify("Failed to reach the daemon", f"Is the daemon not running? {e}")
        return 1
    return 0 if reply.startswith("OK") else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true", help="Send start command and exit")
    parser.add_argument("--stop", action="store_true", help="Send stop command and exit")
    args = parser.parse_args()

    if args.start:
        return send_command("start")
    if args.stop:
        return send_command("stop")
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
