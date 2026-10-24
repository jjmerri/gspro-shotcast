"""The Windows app: a tray icon that runs Shotcast in the background.

`shotcast tray` (what the installer's shortcut and "Start with Windows" run) keeps the booth running
as a child process (restarting it if it crashes), serves the stats page on this PC, and puts the
rest in the tray menu: the stats page, health warnings, settings, the screen check, a backup, updates
and starting with Windows. Your files live in the home folder (%LOCALAPPDATA%\\Shotcast by default):
config.yaml, .env, the league file and logs\\ (the season database, backups and logs), apart from
the app, so updating or reinstalling never touches them.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from .settings_files import env_value, set_commentary

log = logging.getLogger(__name__)

APP = "Shotcast"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STATS_PORT = 8765
RESTARTS, RESTART_WINDOW_S = 3, 600      # crashes in that window before it stops restarting
UPDATE_EVERY_S = 6 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


# ---- the home folder ----------------------------------------------------------------------

def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP


def _templates() -> Path:
    """Where config.example.yaml and .env.example are: next to the app's Python when installed, the
    repo root in a checkout."""
    for d in (Path(sys.executable).resolve().parent.parent, Path(__file__).resolve().parents[1]):
        if (d / "config.example.yaml").is_file():
            return d
    raise FileNotFoundError("config.example.yaml isn't next to the app")


def prepare_home(home: str | Path | None = None) -> tuple[Path, bool]:
    """Make the home folder, with config.yaml and .env from their templates if they aren't there.
    A new config.yaml starts with commentary off: tracking needs no API keys and costs nothing.
    Returns the folder and whether this is the first run."""
    home = (Path(home) if home else default_home()).resolve()
    (home / "logs").mkdir(parents=True, exist_ok=True)
    first = not (home / "config.yaml").exists()
    if first:
        text = (_templates() / "config.example.yaml").read_text(encoding="utf-8")
        (home / "config.yaml").write_text(set_commentary(text, False), encoding="utf-8")
    if not (home / ".env").exists():
        shutil.copyfile(_templates() / ".env.example", home / ".env")
    return home, first


# ---- small pieces the menu shows -------------------------------------------------------------

def python_exe(windowless: bool = False) -> str:
    """This app's Python: python.exe for child processes (their output is caught), pythonw.exe for
    the tray itself (no console window)."""
    exe = Path(sys.executable)
    want = "pythonw.exe" if windowless else "python.exe"
    return str(exe.with_name(want)) if exe.with_name(want).exists() else str(exe)


def run_command(home: Path | None = None) -> str:
    """What "Start with Windows" runs (with --home when it isn't the default folder)."""
    cmd = f'"{python_exe(windowless=True)}" -m commentator tray'
    if home is not None and Path(home).resolve() != default_home().resolve():
        cmd += f' --home "{Path(home).resolve()}"'
    return cmd


def starts_with_windows() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, APP)[0])
    except OSError:
        return False


def set_starts_with_windows(on: bool, home: Path | None = None) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, run_command(home))
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass


SETTINGS_KEY = r"Software\Shotcast"


def checks_for_updates() -> bool:
    """Whether to ask GitHub for updates by itself (the installer's tick box, then the menu's)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
            return bool(winreg.QueryValueEx(key, "CheckForUpdates")[0])
    except (OSError, ImportError):
        return True


def set_checks_for_updates(on: bool) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, "CheckForUpdates", 0, winreg.REG_DWORD, int(on))


def health_issues(log_dir: str | Path) -> list[tuple[str, str]]:
    """(level, message) for each open issue in the booth's health file, failures first."""
    import json

    try:
        data = json.loads((Path(log_dir) / "health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    issues = [(i.get("level", "warn"), i.get("message", "")) for i in data.get("issues") or []]
    return sorted(issues, key=lambda i: i[0] != "fail")


def other_booths() -> list[int]:
    """Booths not started by this app (start-booth.bat, a scheduled task): two would both commentate."""
    if os.name != "nt":
        return []
    # Python processes only: this PowerShell's own command line holds the pattern too.
    script = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
              "| Where-Object { $_.CommandLine -match 'commentator.* run' } | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=30, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.split() if p.isdigit()]


def open_running_stats(home: Path) -> None:
    """The stats page of the tray that's already running (a second launch opens it instead)."""
    try:
        url = (home / "logs" / "stats-url.txt").read_text(encoding="utf-8").strip()
    except OSError:
        url = f"http://127.0.0.1:{STATS_PORT}/"
    webbrowser.open(url)


def open_file(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)   # noqa: S606 (the user's own file, in their default editor)
    else:
        webbrowser.open(path.as_uri())


def edit_file(path: Path) -> None:
    """Notepad for .yaml and .env (Windows has no default app for either)."""
    if os.name == "nt":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        open_file(path)


# ---- the booth, as a child process -----------------------------------------------------------

class Booth:
    """Runs `commentator run` in the home folder and restarts it if it dies, up to RESTARTS times in
    RESTART_WINDOW_S (then it waits for Restart in the menu, so a broken setup doesn't spin)."""

    def __init__(self, home: Path, on_change=None, popen=subprocess.Popen, clock=time.monotonic) -> None:
        self.home, self.on_change, self._popen, self._clock = home, on_change or (lambda: None), popen, clock
        self.proc = None
        self.crashes: deque[float] = deque()
        self.state = "starting"   # running / stopped / crashed / other
        self._lock = threading.RLock()
        self._wanted = False

    def start(self) -> None:
        with self._lock:
            self._wanted = True
            self._spawn()
        self.on_change()

    def _spawn(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            err = open(self.home / "logs" / "booth-errors.log", "a", encoding="utf-8")
            try:
                self.proc = self._popen([python_exe(), "-m", "commentator", "run"], cwd=self.home,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                        env={**os.environ, "SHOTCAST_FILE_LOG_ONLY": "1"},
                                        creationflags=NO_WINDOW)
            finally:
                err.close()   # the child has its own handle
            self.state = "running"

    def stop(self) -> None:
        with self._lock:
            self._wanted = False
            proc, self.proc = self.proc, None
            self.state = "stopped"
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.on_change()

    def restart(self) -> None:
        self.stop()
        self.crashes.clear()
        self.start()

    def poll(self) -> None:
        """Called every few seconds: restart a booth that died, within the limit."""
        with self._lock:
            if not self._wanted or self.proc is None or self.proc.poll() is None:
                return
            code = self.proc.returncode
            now = self._clock()
            self.crashes.append(now)
            while self.crashes and now - self.crashes[0] > RESTART_WINDOW_S:
                self.crashes.popleft()
            log.warning("The booth stopped (exit code %s)", code)
            if len(self.crashes) > RESTARTS:
                self.proc, self.state = None, "crashed"
                self._wanted = False
                log.error("The booth keeps stopping: not restarting it until Restart in the menu")
            else:
                self._spawn()   # under the lock, so a Quit meanwhile can't be undone
        self.on_change()


# ---- the tray --------------------------------------------------------------------------------

class Tray:
    def __init__(self, home: Path, cfg, first_run: bool = False) -> None:
        from .updater import current_version

        self.home, self.cfg, self.first_run = home, cfg, first_run
        self.version = current_version()
        self.booth = Booth(home, on_change=self._refresh)
        self.stats_url: str | None = None
        self.update = None            # a downloaded, checked updater.Release and its file
        self.update_note = ""
        self.icon = None
        self._stop = threading.Event()

    # -- status
    def status(self) -> str:
        s = self.booth.state
        if s == "other":
            return "Another booth is running (start-booth.bat?)"
        if s == "crashed":
            return "The booth keeps stopping: see Open logs"
        if s == "stopped":
            return "Stopped"
        issues = health_issues(self.cfg.log_dir)
        mode = "Commentary on" if self._commentary_on() else "Recording shots"
        return f"{mode} \u00b7 {len(issues)} warning{'s' * (len(issues) != 1)}" if issues else f"{mode} \u00b7 all good"

    def _commentary_on(self) -> bool:
        from .config import load_config

        try:
            return load_config(self.home / "config.yaml").commentary.enabled
        except Exception:
            return False

    # -- actions
    def open_stats(self, *_) -> None:
        if self.stats_url:
            webbrowser.open(self.stats_url)

    def restart_booth(self, *_) -> None:
        if self.booth.state == "other":
            for pid in other_booths():
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        self.booth.restart()

    def toggle_commentary(self, *_) -> None:
        path = self.home / "config.yaml"
        on = not self._commentary_on()
        path.write_text(set_commentary(path.read_text(encoding="utf-8"), on), encoding="utf-8")
        env = (self.home / ".env").read_text(encoding="utf-8") if (self.home / ".env").exists() else ""
        if on and not (env_value(env, "OPENAI_API_KEY") or env_value(env, "ANTHROPIC_API_KEY")):
            edit_file(self.home / ".env")
            self.notify("Add your OpenAI and Anthropic API keys to .env and save it, then choose Restart "
                        "the booth. Until then it records with a warning on the ON AIR badge.")
        self.restart_booth()

    def screen_check(self, *_) -> None:
        subprocess.Popen(["cmd", "/k", python_exe(), "-m", "commentator", "screen-check", "--delay", "8"],
                         cwd=self.home, creationflags=NEW_CONSOLE)
        self.notify("Bring GSPro to the front: the screen check reads it in 8 seconds.")

    def command_prompt(self, *_) -> None:
        """A terminal in the home folder where `shotcast <command>` runs this app's Python."""
        py = Path(python_exe()).parent
        env = {**os.environ, "PATH": os.pathsep.join([str(py), str(py / "Scripts"), os.environ.get("PATH", "")])}
        hello = "Shotcast commands: shotcast --help (for example shotcast publish, shotcast backup)"
        subprocess.Popen(["cmd", "/k", "echo", hello], cwd=self.home, env=env, creationflags=NEW_CONSOLE)

    def back_up(self, *_) -> None:
        def work():
            from . import backup

            try:
                path = backup.run(self.cfg)
                self.notify(f"Backed up to {path.name}." if path else "Nothing changed since the last backup.")
            except Exception as e:
                log.exception("Backup failed")
                self.notify(f"Backup failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def toggle_start_with_windows(self, *_) -> None:
        set_starts_with_windows(not starts_with_windows(), self.home)
        self._refresh()

    def toggle_update_checks(self, *_) -> None:
        set_checks_for_updates(not checks_for_updates())
        self._refresh()

    def check_updates(self, *_) -> None:
        threading.Thread(target=self._check_updates, args=(True,), daemon=True).start()

    def _check_updates(self, told: bool = False) -> None:
        from . import updater

        try:
            release = updater.check(self.version)
            if release is None:
                self.update_note = ""
                if told:
                    self.notify(f"Shotcast {self.version} is the latest.")
                return
            path = updater.download(release, self.home / "updates")
            self.update = (release, path)
            self.notify(f"Shotcast {release.version} is ready. Choose Restart to update in the menu "
                        "(not mid-round: it restarts the booth).")
        except Exception as e:
            log.warning("Update check failed: %r", e)
            if told:
                self.notify(f"Couldn't check for updates: {e}")
        finally:
            self._refresh()

    def apply_update(self, *_) -> None:
        from . import updater

        if not self.update:
            return
        self.booth.stop()
        custom = self.home.resolve() != default_home().resolve()
        updater.install(self.update[1], self.home.resolve() if custom else None)
        self.quit()

    def open_folder(self, *_) -> None:
        open_file(self.home)

    def open_logs(self, *_) -> None:
        open_file(self.home / "logs")

    def edit_config(self, *_) -> None:
        edit_file(self.home / "config.yaml")

    def edit_keys(self, *_) -> None:
        edit_file(self.home / ".env")

    def about(self, *_) -> None:
        webbrowser.open("https://github.com/jjmerri/gspro-shotcast#readme")

    def quit(self, *_) -> None:
        self._stop.set()
        self.booth.stop()
        if self.icon is not None:
            self.icon.stop()

    def notify(self, text: str) -> None:
        log.info("Notice: %s", text)
        if self.icon is not None:
            try:
                self.icon.notify(text, APP)
            except Exception:
                pass

    # -- menu
    def menu(self):
        import pystray

        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        warnings = lambda: health_issues(self.cfg.log_dir)  # noqa: E731

        def warning_items():
            return [item(f"{'Problem' if lvl == 'fail' else 'Warning'}: {msg}"[:120], None, enabled=False)
                    for lvl, msg in warnings()[:10]]

        return pystray.Menu(
            item(lambda _: self.status(), None, enabled=False),
            item("Warnings", pystray.Menu(warning_items), visible=lambda _: bool(warnings())),
            item("Open the stats page", self.open_stats, default=True, enabled=lambda _: bool(self.stats_url)),
            sep,
            item("Restart the booth", self.restart_booth),
            item(lambda _: "Turn the AI booth off" if self._commentary_on() else "Turn the AI booth on",
                 self.toggle_commentary),
            item("Settings", pystray.Menu(
                item("Edit settings (config.yaml)", self.edit_config),
                item("Edit API keys (.env)", self.edit_keys),
                item("Open the Shotcast folder", self.open_folder),
                item("Open logs", self.open_logs),
                item("Command prompt (shotcast commands)", self.command_prompt),
            )),
            item("Check the screen read...", self.screen_check),
            item("Back up now", self.back_up),
            sep,
            item(lambda _: f"Restart to update to {self.update[0].version}" if self.update else "Check for updates",
                 lambda *_: self.apply_update() if self.update else self.check_updates()),
            item("Check for updates automatically", self.toggle_update_checks, checked=lambda _: checks_for_updates()),
            item("Start with Windows", self.toggle_start_with_windows, checked=lambda _: starts_with_windows()),
            item(f"About Shotcast {self.version}", self.about),
            item("Quit", self.quit),
        )

    def _refresh(self) -> None:
        if self.icon is not None:
            try:
                self.icon.icon = icon_image(self._light())
                self.icon.title = f"{APP}: {self.status()}"[:127]
                self.icon.update_menu()
            except Exception:
                log.debug("Tray refresh failed", exc_info=True)

    def _light(self) -> str:
        if self.booth.state in ("crashed", "stopped", "other"):
            return "red"
        issues = health_issues(self.cfg.log_dir)
        return "orange" if issues else "green"

    # -- running
    def start_stats(self) -> None:
        from .stats_web import make_server
        from .store import Store

        db = Path(self.cfg.db_path)
        if not db.exists():
            Store(str(db), new_session=False).db.close()   # an empty season, so the page opens before round one
        names = {key: persona.name for key, persona in self.cfg.personas.items()}
        site = Path(self.cfg.log_dir) / "stats-web-site"
        try:
            server = make_server(db, STATS_PORT, names, site)
        except OSError:
            server = make_server(db, 0, names, site)   # the usual port is taken
        self.stats_url = f"http://127.0.0.1:{server.server_address[1]}/"
        (Path(self.cfg.log_dir) / "stats-url.txt").write_text(self.stats_url, encoding="utf-8")
        threading.Thread(target=server.serve_forever, name="stats", daemon=True).start()
        log.info("Stats page at %s", self.stats_url)

    def _watch(self) -> None:
        next_update = time.monotonic() + 60
        last = None
        while not self._stop.wait(3):
            self.booth.poll()
            now = (self.booth.state, self._light(), self.status())
            if now != last:
                last = now
                self._refresh()
            if time.monotonic() >= next_update:
                next_update = time.monotonic() + UPDATE_EVERY_S
                if checks_for_updates():
                    threading.Thread(target=self._check_updates, daemon=True).start()

    def run(self) -> None:
        import pystray

        self.start_stats()
        if other_booths():
            self.booth.state = "other"
            log.warning("Another booth is already running; not starting one (Restart the booth replaces it)")
        else:
            self.booth.start()
        self.icon = pystray.Icon(APP, icon_image(self._light()), f"{APP}: {self.status()}", self.menu())

        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._watch, name="watch", daemon=True).start()
            if self.first_run:
                self.notify("Shotcast is recording your shots. Right-click this icon for the stats page "
                            "and settings, then run Check the screen read with GSPro on a hole.")

        self.icon.run(setup)


def icon_image(light: str = "green", size: int = 64):
    """The tray icon: a golf ball on a green disc, with a status light."""
    from PIL import Image, ImageDraw

    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=(20, 110, 60, 255))
    d.ellipse((s * .2, s * .2, s * .8, s * .8), fill=(250, 250, 250, 255))
    for x, y in ((.4, .38), (.6, .38), (.5, .5), (.4, .62), (.6, .62)):
        r = s * .045
        d.ellipse((s * x - r, s * y - r, s * x + r, s * y + r), fill=(200, 200, 200, 255))
    color = {"green": (40, 200, 90), "orange": (245, 160, 20), "red": (220, 40, 40)}.get(light, (40, 200, 90))
    d.ellipse((s * .62, s * .62, s - 1, s - 1), fill=color + (255,), outline=(255, 255, 255, 255), width=max(1, s // 32))
    return img


def write_icon(path: str | Path) -> None:
    """The app's .ico (shortcuts, the installer), from the same drawing as the tray icon."""
    img = icon_image("green", 256)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def single_instance() -> bool:
    """True if no other tray is running for this Windows user."""
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    single_instance.handle = kernel32.CreateMutexW(None, False, "Local\\ShotcastTray")
    return kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def self_test(home: Path, cfg) -> None:
    """For the release build: everything the tray needs loads, the stats page serves, and the booth
    starts from this Python. Prints OK or raises."""
    import urllib.request

    import pystray  # noqa: F401
    from . import engine, overlay  # noqa: F401
    from .screen import Screen  # noqa: F401

    tray = Tray(home, cfg)
    tray.start_stats()
    with urllib.request.urlopen(tray.stats_url, timeout=30) as r:
        assert r.status == 200, r.status
    tray.menu()
    out = subprocess.run([python_exe(), "-m", "commentator", "check", "--help"], cwd=home,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    print(f"OK: Shotcast {tray.version}, home {home}, stats at {tray.stats_url}")
