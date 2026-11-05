"""The Windows app: a tray icon that runs Shotcast in the background.

`shotcast tray` (what the installer's shortcut and "Start with Windows" run) keeps the booth running
as a child process (restarting it if it crashes), serves the stats page on this PC, and puts the
rest in the tray menu: the stats page, health warnings, settings, the screen check, a backup, updates
and starting with Windows. Your files live in the home folder (%LOCALAPPDATA%\\Shotcast by default):
config.yaml, .env, the league file and logs\\ (the season database, backups and logs), apart from
the app, so updating or reinstalling never touches them.
"""
from __future__ import annotations

import json
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

from . import changelog
from .config import ROUND_STATUS, ROUND_STATUS_EVERY_S
from .settings_files import env_value, set_commentary, set_env, set_yaml

log = logging.getLogger(__name__)

APP = "Shotcast"
CHANGELOG_URL = "https://github.com/jjmerri/gspro-shotcast/blob/main/commentator/changelog.md"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STATS_PORT = 8765
PUBLISH_AFTER_EDIT_S = 60.0   # a run of edits on the local page goes up once, this long after the last
ROUND_STATUS_STALE_S = 4 * ROUND_STATUS_EVERY_S   # older than this: the booth may have stopped
RESTARTS, RESTART_WINDOW_S = 3, 600      # crashes in that window before it stops restarting
STARTING_LIMIT_S = 120   # a booth that hasn't said it's ready by then is taken as running anyway
UPDATE_EVERY_S = 6 * 3600
IDLE_INSTALL_S = 30 * 60   # a downloaded update installs by itself once GSPro's been closed (or unplayed) this long
GSPRO_CHECK_S = 60         # how often the tray looks for GSPro running
INSTALL_WARN_S = 60        # with GSPro open, the countdown (with Postpone) before an update installs
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


def _flag(name: str, default: bool) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
            return bool(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError):
        return default


def _set_flag(name: str, on: bool) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(on))


def notifies_when_ready() -> bool:
    """Whether a notification says the booth is ready after it starts (the menu's tick box)."""
    return _flag("NotifyWhenReady", True)


def set_notifies_when_ready(on: bool) -> None:
    _set_flag("NotifyWhenReady", on)


def installs_updates() -> bool:
    """Whether a downloaded update installs by itself: as Shotcast starts, or once GSPro's been closed
    (or nobody's struck a ball) IDLE_INSTALL_S (the menu's tick box). Off, it waits for Restart to update."""
    return _flag("InstallUpdates", True)


def set_installs_updates(on: bool) -> None:
    _set_flag("InstallUpdates", on)


def gspro_running() -> bool:
    """GSPro open (3.1's and 4.0's GSPro.exe, the beta's GSpro.exe). Not Windows: never."""
    if os.name != "nt":
        return False
    try:
        done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq gspro.exe", "/NH"], capture_output=True, text=True,
                              timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return True   # can't tell: don't install under it
    return done.returncode != 0 or "gspro.exe" in done.stdout.lower()


def remember_gspro_dir(folder: str) -> None:
    """The GSPro folder chosen in Settings, so the installer offers it next time."""
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
            winreg.SetValueEx(key, "GSProDir", 0, winreg.REG_SZ, folder)
            winreg.SetValueEx(key, "GSProDirApplied", 0, winreg.REG_SZ, folder)
    except (OSError, ImportError):
        pass


def apply_installer_gspro_dir(home: Path) -> None:
    """Point config.yaml at the GSPro folder picked in the installer, once per pick (a later change
    in Settings or by hand stands until the installer is given a different folder)."""
    from . import gspro_dir
    from .config import load_config

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
            folder = winreg.QueryValueEx(key, "GSProDir")[0]
            try:
                applied = winreg.QueryValueEx(key, "GSProDirApplied")[0]
            except OSError:
                applied = ""
    except (OSError, ImportError):
        return
    if not folder or gspro_dir.same(folder, applied):
        return
    path = home / "config.yaml"
    try:
        if not gspro_dir.same(gspro_dir.folder_of(load_config(path).source.connector_log), folder):
            path.write_text(gspro_dir.apply(path.read_text(encoding="utf-8"), folder), encoding="utf-8")
            log.info("GSPro folder from the installer: %s", folder)
    except Exception:
        log.exception("Couldn't set the GSPro folder from the installer in config.yaml")
        return
    remember_gspro_dir(folder)


def last_backup(cfg) -> str:
    """When the last backup was taken, for the menu."""
    import json
    from datetime import datetime

    from .backup import STATE, folder

    try:
        at = datetime.fromisoformat(json.loads((folder(cfg) / STATE).read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return "No backup yet"
    return f"Last backup {_when(at)}"


def _when(at) -> str:
    from datetime import datetime

    days = (datetime.now().date() - at.date()).days
    clock = at.strftime("%H:%M")
    return f"today {clock}" if days == 0 else f"yesterday {clock}" if days == 1 else at.strftime("%b %d").replace(" 0", " ")


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
    """Booths not started by this app (a source checkout, a scheduled task): two would both commentate."""
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
        self.state = "starting"   # starting / running / stopped / crashed / other
        self._lock = threading.RLock()
        self._wanted = False
        # The booth writes this once it's up (its startup checks done): until then it's "starting".
        self.ready_file = home / "logs" / "booth-ready"
        self._started_at = 0.0
        self._announce = False   # say so when it's ready: started from here, not a crash's restart

    def start(self) -> None:
        with self._lock:
            self._wanted = True
            self._spawn()
            self._announce = True
        self.on_change()

    def _spawn(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            try:
                self.ready_file.unlink(missing_ok=True)
            except OSError:
                pass
            err = open(self.home / "logs" / "booth-errors.log", "a", encoding="utf-8")
            try:
                self.proc = self._popen([python_exe(), "-m", "commentator", "run"], cwd=self.home,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                        env={**os.environ, "SHOTCAST_FILE_LOG_ONLY": "1",
                                             "SHOTCAST_READY_FILE": str(self.ready_file)},
                                        creationflags=NO_WINDOW)
            finally:
                err.close()   # the child has its own handle
            self.state, self._started_at, self._announce = "starting", self._clock(), False

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

    def poll(self) -> bool:
        """Called every few seconds: restart a booth that died, within the limit, and see whether a
        starting one is up. True when one started from here (not a crash's restart) just got ready."""
        with self._lock:
            if not self._wanted or self.proc is None:
                return False
            if self.proc.poll() is None:
                if self.state != "starting":
                    return False
                if self.ready_file.exists():
                    self.state = "running"
                elif self._clock() - self._started_at > STARTING_LIMIT_S:
                    log.warning("The booth hasn't said it's ready after %ds: taking it as running", STARTING_LIMIT_S)
                    self.state, self._announce = "running", False
                else:
                    return False
                announce, self._announce = self._announce, False
            else:
                self._restart_crashed()
                announce = False
        self.on_change()
        return announce

    def _restart_crashed(self) -> None:
        with self._lock:
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


# ---- publishing after an edit on the local page ---------------------------------------------

class AutoPublisher:
    """Publishes the stats site PUBLISH_AFTER_EDIT_S after the last edit on the local page (an
    exclude, a delete, a league/practice switch, a shot left out), so a run of edits goes up once.
    The publish is the booth's own command (stats_site.publish_command) in its own process, which
    takes stats_site.site_lock, so it never overlaps the booth's after-round publish. `command()`
    is asked when it runs, so a change in Settings counts; None means publishing is off."""

    def __init__(self, command, cwd: Path, delay: float = 60.0, run=subprocess.run, clock=time.time) -> None:
        self._command, self._cwd, self.delay, self._run, self._clock = command, cwd, delay, run, clock
        self._lock = threading.Lock()        # the status
        self._one = threading.Lock()         # one publish at a time
        self._timer: threading.Timer | None = None
        self.due: float | None = None
        self.publishing = False
        self.last: dict | None = None        # {"ok", "at", "message"}

    def changed(self) -> None:
        if self._command() is None:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self.due = self._clock() + self.delay
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self._command() is not None, "due": self.due, "publishing": self.publishing,
                    "last": self.last, "now": self._clock()}

    def _fire(self) -> None:
        with self._one:
            with self._lock:
                if self.due is None or self.due > self._clock() + 0.5:
                    return   # a newer edit moved it: that timer publishes
                self.due, self.publishing = None, True
            ok, message = self.publish()
            with self._lock:
                self.publishing = False
                self.last = {"ok": ok, "at": self._clock(), "message": message}

    def publish(self) -> tuple[bool, str]:
        cmd = self._command()
        if cmd is None:
            return False, "publishing is off"
        try:
            done = self._run(cmd, cwd=self._cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=900, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Publishing after an edit failed: %s", e)
            return False, str(e)
        text = ((done.stdout or "") + (done.stderr or "")).strip()
        last = text.splitlines()[-1] if text else f"exit {done.returncode}"
        if done.returncode == 0:
            log.info("Stats page after an edit: %s", last)
            return True, last
        log.warning("Publishing after an edit failed: %s", text[-2000:])
        return False, last


# ---- the tray --------------------------------------------------------------------------------

class Tray:
    def __init__(self, home: Path, cfg, first_run: bool = False) -> None:
        from .updater import current_version

        self.home, self.cfg, self.first_run = home, cfg, first_run
        self.version = current_version()
        self.booth = Booth(home, on_change=self._refresh)
        self.stats_url: str | None = None
        self.update = None            # a downloaded, checked updater.Release and its file
        self.checked_at = None        # when GitHub last said this is the latest
        self._clock = time.monotonic
        self._gspro_at = self._clock()   # GSPro last seen running (as if just closed, at the start)
        self.icon = None
        self.ui = None
        self._stop = threading.Event()
        self._live_setup = threading.Lock()
        self.publisher = AutoPublisher(self._publish_command, home, PUBLISH_AFTER_EDIT_S)

    def _publish_command(self) -> list[str] | None:
        from .stats_site import publish_command

        if not self.cfg.publish.enabled:   # self.cfg as Settings last saved it
            return None
        cmd = publish_command(self.cfg)
        return cmd and [python_exe(), *cmd[1:]]   # python.exe: its output is caught

    # -- status
    def status(self) -> str:
        s = self.booth.state
        if s == "other":
            return "Another booth is running"
        if s == "crashed":
            return "The booth keeps stopping: see Open logs"
        if s == "stopped":
            return "Stopped"
        if s == "starting":
            return "Starting the booth\u2026"
        issues = health_issues(self.cfg.log_dir)
        mode = "Commentary on" if self._commentary_on() else "Recording shots"
        return f"{mode} \u00b7 {len(issues)} warning{'s' * (len(issues) != 1)}" if issues else f"{mode} \u00b7 all good"

    def _ready_notice(self) -> str:
        issues = health_issues(self.cfg.log_dir)
        if not issues:
            return "The booth is ready: all good."
        n = len(issues)
        return f"The booth is ready, with {n} warning{'s' * (n != 1)}: {issues[0][1]}"[:250]

    def _commentary_on(self) -> bool:
        from .config import load_config

        try:
            return load_config(self.home / "config.yaml").commentary.enabled
        except Exception:
            return False

    def _safe(self, fn):
        """A menu action that shows what went wrong instead of failing silently."""
        def run(*_):
            try:
                fn()
            except Exception as e:
                log.exception("%s failed", getattr(fn, "__name__", "A menu action"))
                self.error(f"That didn't work: {e}\n\nThe details are in logs\\tray-*.log.")
        return run

    def settings_saved(self) -> None:
        from .config import load_config

        try:
            self.cfg = load_config(self.home / "config.yaml")
        except Exception:
            log.exception("Reloading config.yaml failed")
        self._refresh()

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
        self.settings_saved()
        env = (self.home / ".env").read_text(encoding="utf-8") if (self.home / ".env").exists() else ""
        if on and not (env_value(env, "OPENAI_API_KEY") or env_value(env, "ANTHROPIC_API_KEY")):
            self.info("The AI booth is on, but it needs API keys to talk: add them in Settings (API keys), "
                      "save and restart the booth. Until then it records shots as before.")
            self.settings("API keys")
        self.restart_booth()

    def round_in_progress(self) -> bool | None:
        """Whether the booth is in a round (or still talking after one), from the status it keeps in
        log_dir/round.json; None when that can't be told (no status, or none written lately)."""
        try:
            status = json.loads((Path(self.cfg.log_dir) / ROUND_STATUS).read_text(encoding="utf-8"))
            if time.time() - float(status["updated"]) > ROUND_STATUS_STALE_S:
                return None
            return bool(status["round"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def struck_ago(self) -> float | None:
        """Seconds since the booth last saw a ball struck (anywhere: range, practice, a round; its start
        if none since), from log_dir/round.json; None when that can't be told."""
        try:
            status = json.loads((Path(self.cfg.log_dir) / ROUND_STATUS).read_text(encoding="utf-8"))
            if time.time() - float(status["updated"]) > ROUND_STATUS_STALE_S:
                return None
            return max(0.0, time.time() - float(status["struck"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def live_now(self) -> bool:
        """On air, as the booth's stream mirror last wrote it."""
        try:
            return bool(json.loads((Path(self.cfg.log_dir) / "live.json").read_text(encoding="utf-8")).get("live"))
        except (OSError, ValueError):
            return False

    def surely_off_air(self) -> bool:
        """The stream's off air for sure: live.json says so, or there's none. One being rewritten (read
        half-written) isn't sure."""
        try:
            return not json.loads((Path(self.cfg.log_dir) / "live.json").read_text(encoding="utf-8")).get("live")
        except FileNotFoundError:
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def toggle_live(self, *_) -> None:
        from .live_stream import Obs, ObsError

        obs = Obs(self.cfg.live.obs_host, self.cfg.live.obs_port)
        try:
            up = obs.live()
        except ObsError:
            up = None   # OBS isn't running (the booth starts it only once the stats page is published)
        if up:
            obs.stop()
            self.notify("Stopping the live stream.")
            return
        if not (self.cfg.publish.bucket or self.cfg.live.local_keep_days):
            self.error("The stream plays on the stats page's Live tab once the stats page is published "
                       "(Settings, Sharing), or is kept on this PC (Settings, Video): neither is on.")
            return
        if up is False:
            obs.start()
            self.notify(self._live_starting())
            return
        threading.Thread(target=self._start_obs_and_stream, args=(obs,), name="live-start", daemon=True).start()

    def _start_obs_and_stream(self, obs) -> None:
        from . import live_stream, obs_setup

        try:
            self.notify("Starting OBS, then the live stream.")
            portable = obs_setup.portable_exe(self.home)
            exe = self.cfg.live.obs_path or (portable if portable.exists() else None)
            if not live_stream.launch_obs(exe) and not live_stream.obs_running():
                raise RuntimeError("OBS isn't installed: set up the live stream again (More menu)")
            obs_setup.wait_for(obs, 60)
            obs.start()
            self.notify(self._live_starting())
        except Exception as e:
            log.exception("Starting the live stream failed")
            self.error(f"That didn't work: {e}\n\nThe details are in logs\\tray-*.log.")

    def _live_starting(self) -> str:
        if self.cfg.publish.bucket:
            return "Starting the live stream: it's on the stats page in about 20 seconds."
        return "Recording GSPro: the video's kept on this PC (More, Open the recordings folder)."

    def open_recordings(self, *_) -> None:
        from . import local_video

        where = local_video.folder(self.cfg.live, self.home)
        if where is None:
            return self.info("Round videos aren't kept on this PC: turn them on in Settings, Video.")
        where.mkdir(parents=True, exist_ok=True)
        open_file(where)

    def setup_live(self, *_) -> None:
        from . import obs_setup

        if not self._live_setup.acquire(blocking=False):
            self.notify("The live stream is already being set up.")
            return
        try:
            if obs_setup.installed(self.home) or self.ui is None or self.ui.ask(
                    f"Set up the live stream on the stats page?\n\nThis downloads OBS Studio {obs_setup.OBS_VERSION} "
                    f"({obs_setup.OBS_ZIP_MB} MB) into the Shotcast folder, apart from any other OBS, and sets it up "
                    f"to stream GSPro's screen and sound while rounds are played."):
                threading.Thread(target=self._setup_live, name="live-setup", daemon=True).start()
                return
        except BaseException:
            self._live_setup.release()
            raise
        self._live_setup.release()

    def _setup_live(self) -> None:
        from . import live_stream, obs_setup
        from .config import load_config
        from .tray_ui import write_atomic

        try:
            if live_stream.obs_running():
                self.error("OBS is running: close it first (right-click its icon by the clock, then Exit), "
                           "then set up the live stream again.")
                return
            if not obs_setup.installed(self.home):
                self.notify(f"Downloading OBS ({obs_setup.OBS_ZIP_MB} MB): this takes a few minutes.")
                obs_setup.download(self.home)
            cfg_path, env_path = self.home / "config.yaml", self.home / ".env"
            cfg = load_config(cfg_path)
            env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            password = env_value(env, live_stream.PASSWORD_ENV) or obs_setup.new_password()
            playlist = self.home / cfg.live.dir / cfg.live.playlist
            playlist.parent.mkdir(parents=True, exist_ok=True)
            obs_setup.write_settings(self.home, password, playlist, cfg.live.obs_port)
            write_atomic(env_path, set_env(env, live_stream.PASSWORD_ENV, password))
            os.environ[live_stream.PASSWORD_ENV] = password
            if not cfg.live.enabled:
                write_atomic(cfg_path, set_yaml(cfg_path.read_text(encoding="utf-8"), ("live", "enabled"), True))
            self.settings_saved()
            obs_setup.launch(self.home)
            obs = live_stream.Obs(cfg.live.obs_host, cfg.live.obs_port, password=password)
            obs_setup.wait_for(obs)
            obs_setup.configure(obs)
            self.restart_booth()
            kept = (f" Each round's video is kept on this PC for {cfg.live.local_keep_days} days "
                    f"(More, Open the recordings folder)." if cfg.live.local_keep_days else "")
            if cfg.publish.bucket:
                self.info("The live stream is set up. OBS starts with Shotcast, runs by the clock and streams "
                          "GSPro while rounds are played (on the stats page's Live tab); \"Start the live "
                          "stream\" in this menu tries it now." + kept)
            elif kept:
                self.info("OBS is set up. It starts with Shotcast, runs by the clock and records GSPro while "
                          "rounds are played." + kept + " Publishing the stats page (Settings, Sharing) "
                          "streams it on the page's Live tab too.")
            else:
                self.info("OBS is set up. The live stream plays on the stats page's Live tab, so it starts "
                          "once the stats page is published (Settings, Sharing): then OBS starts with Shotcast "
                          "and streams GSPro while rounds are played.")
        except Exception as e:
            log.exception("Setting up the live stream failed")
            self.error(f"Setting up the live stream didn't finish: {e}\n\nThe details are in logs\\tray-*.log.")
        finally:
            self._live_setup.release()
            self._refresh()

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
                where = backup.folder(self.cfg)
                s3 = " It's in the S3 bucket too." if self.cfg.backup.s3 else ""
                self.info(f"Backed up the season to {path.name} in {where}.{s3}" if path else
                          f"Nothing changed since the last backup ({last_backup(self.cfg).split(' ', 2)[-1]}), "
                          f"so there's nothing new to save.")
            except Exception as e:
                log.exception("Backup failed")
                self.error(f"The backup failed: {e}")
            finally:
                self._refresh()
        threading.Thread(target=work, daemon=True).start()

    def problem_report(self, *_) -> None:
        def work():
            from . import report

            try:
                path = report.build(self.cfg, self.home / "config.yaml")
            except Exception as e:
                log.exception("Problem report failed")
                self.error(f"Couldn't make the problem report: {e}")
                return
            what = ("It has the booth's logs, GSPro's log, the screens the booth kept and the season's "
                    "player names, but no API keys.")
            url = self.cfg.report.upload_url
            if url and self.ui is not None and self.ui.ask(
                    f"Saved {path.name}. {what}\n\nSend it to Shotcast support now? You'll get a short "
                    "code to pass on to whoever is helping you. Reports are deleted after two weeks."):
                try:
                    code = report.show_code(report.upload(path, url))
                    self.ui.code("Sent. Your report code is:", code,
                                 "Pass it on to whoever is helping you (it's in the tray log too). "
                                 f"The zip is also saved in {path.parent}.")
                    return
                except Exception as e:
                    log.exception("Sending the problem report failed")
                    self.error(f"Couldn't send the report ({e}). It's saved in {path.parent} (opening it "
                               "now): send the zip some other way.")
                    open_file(path.parent)
                    return
            open_file(path.parent)
            self.info(f"Saved {path.name} in {path.parent} (opened). {what} "
                      "Send it to whoever is helping you.")
        threading.Thread(target=work, daemon=True).start()

    def settings(self, tab: str | None = None) -> None:
        if self.ui is not None:
            self.ui.open_settings(self, tab)
        else:
            edit_file(self.home / "config.yaml")

    def toggle_start_with_windows(self, *_) -> None:
        set_starts_with_windows(not starts_with_windows(), self.home)
        self._refresh()

    def toggle_ready_notice(self, *_) -> None:
        set_notifies_when_ready(not notifies_when_ready())
        self._refresh()

    def toggle_update_checks(self, *_) -> None:
        set_checks_for_updates(not checks_for_updates())
        self._refresh()

    def toggle_update_installs(self, *_) -> None:
        set_installs_updates(not installs_updates())
        self._refresh()

    def check_updates(self, *_) -> None:
        threading.Thread(target=self._check_updates, args=(True,), daemon=True).start()

    def _check_updates(self, told: bool = False) -> None:
        from datetime import datetime

        from . import updater

        try:
            release = updater.check(self.version)
            if release is None:
                self.checked_at = datetime.now()
                if told:
                    self.info(f"Shotcast {self.version} is the latest version.")
                return
            path = updater.download(release, self.home / "updates")
            self.update = (release, path)
            self._refresh()
            later = ("it installs itself when Shotcast next starts, or once nobody's played for a while"
                     if installs_updates() else "choose Restart to update in the tray menu")
            text = (f"Shotcast {release.version} is downloaded and ready (you have {self.version}).\n\n"
                    "Update now? Shotcast restarts for it, so not in the middle of a round. "
                    f"Later: {later}.")
            if told:
                if self.ui is not None and self.ui.ask(text):
                    self.apply_update()
            else:
                self.notify(f"Shotcast {release.version} is ready: {later}.")
        except Exception as e:
            log.warning("Update check failed: %r", e)
            if told:
                self.error(f"Couldn't check for updates: {e}")
        finally:
            self._refresh()

    def apply_update(self, *_) -> None:
        if not self.update:
            return
        was = self.booth.state
        self.booth.stop()
        try:
            self._install(self.update[0].version, self.update[1])
        except Exception:
            if was in ("running", "starting"):
                self.booth.start()   # the installer never ran: back to recording
            else:                    # not running from here (another's booth, a crashed or stopped one): as it was
                self.booth.state = was
            raise
        self.quit()

    def _install(self, version: str, installer: Path) -> None:
        """Back the season up and run the installer, which closes this app (the booth already stopped)."""
        from . import updater

        try:
            from . import backup

            path = backup.pre_update(self.cfg, self.version)
            log.info("Season backup before the update: %s", path.name if path else "no database yet")
        except Exception:
            log.exception("Season backup before the update failed (updating anyway: it doesn't touch the season)")
        custom = self.home.resolve() != default_home().resolve()
        updater.install(installer, version, self.home.resolve() if custom else None)

    def install_at_start(self) -> bool:
        """As Shotcast starts, before the booth: an update downloaded earlier installs now, unless
        something's on anyway (GSPro open, a booth left from before, a round or the stream). True when
        it's installing, and this app should end."""
        from . import updater

        folder = self.home / "updates"
        found = updater.downloaded(folder, self.version) if installs_updates() else None
        if not found or updater.auto_tried(folder, found[0]):
            return False
        if gspro_running() or other_booths() or self.round_in_progress() or not self.surely_off_air():
            log.info("Not installing the downloaded update to %s as Shotcast starts: GSPro or a booth is running", found[0])
            # Offered in the menu, and installed once idle, without waiting for the next check.
            self.update = (updater.Release(version=found[0], url="", sha256_url="", name=found[1].name, page=""), found[1])
            return False
        log.info("Installing the downloaded update to %s as Shotcast starts", found[0])
        updater.mark_auto(folder, found[0])
        self._install(*found)
        return True

    def install_when_idle(self) -> bool:
        """Every GSPRO_CHECK_S: a downloaded update installs once GSPro's been closed IDLE_INSTALL_S, or
        is open but nobody's struck a ball in that long (then only after a countdown on screen that can
        postpone it), with no round on, the stream surely off air and no booth this tray didn't start.
        True when it's installing."""
        from . import updater

        now = self._clock()
        open_ = gspro_running()
        if open_:
            self._gspro_at = now
        if not self.update or not installs_updates() or now < getattr(self, "_postponed_until", 0.0):
            return False
        if open_:
            ago = self.struck_ago()
            if ago is None or ago < IDLE_INSTALL_S or self.ui is None or not self.ui.ok:
                return False
        elif now - self._gspro_at < IDLE_INSTALL_S:
            return False
        version = self.update[0].version
        if not self._clear_to_install(version):
            return False
        if open_:
            mins = int(ago // 60)
            if not self.ui.countdown(
                    f"Shotcast {version} is ready, and nobody's hit a shot in {mins} minutes, so it installs "
                    "in {s} seconds. GSPro stays open; the booth is back in about a minute.",
                    INSTALL_WARN_S, "Install now", "Postpone"):
                self._postponed_until = self._clock() + IDLE_INSTALL_S
                log.info("Update to %s postponed from its countdown", version)
                return False
            # A shot, round or stream since the countdown began calls it off, and so does unticking it.
            ago = self.struck_ago()
            if ago is None or ago < IDLE_INSTALL_S or not installs_updates() or not self._clear_to_install(version):
                log.info("Update to %s not installed: play started during its countdown", version)
                return False
            log.info("Installing the update to %s: GSPro's open but nobody's hit a shot in %d minutes", version, mins)
        else:
            log.info("Installing the update to %s: GSPro's been closed %d minutes", version, (now - self._gspro_at) // 60)
        updater.mark_auto(self.home / "updates", version)
        try:
            self.apply_update()
        except Exception:
            log.exception("Installing the update to %s failed", version)
            self.notify(f"Shotcast {version} didn't install: choose Restart to update in the menu.")
            return False
        return True

    def _clear_to_install(self, version: str) -> bool:
        """Nothing an install would interrupt: no round, the stream surely off air, no booth this tray
        didn't start, and this version not already tried."""
        from . import updater

        if self.booth.state == "other" or self.round_in_progress() or not self.surely_off_air() \
                or updater.auto_tried(self.home / "updates", version):
            return False
        own = getattr(self.booth.proc, "pid", None)
        return not [pid for pid in other_booths() if pid != own]   # one started since, not from here

    def _after_update(self) -> None:
        from . import updater

        try:
            done = updater.after_update(self.home / "updates", self.version)
        except Exception as e:
            log.warning("Couldn't read the last update's note: %r", e)
            return
        if done is not None:
            ok, text, was = done
            log.info("Update: %s", text)
            if ok:
                self.publisher.changed()   # the new version's page and figures, without waiting for a round
                if self._whats_new(text, changelog.between(was or "0", self.version)):
                    return
            (self.info if ok else self.error)(text)

    def _whats_new(self, heading: str, releases: list) -> bool:
        """The What's new window: `releases`' changes under `heading`. False with nothing to show it in."""
        if not releases or self.ui is None or not self.ui.ok:
            return False
        self.ui.whats_new(heading, changelog.text(releases))
        return True

    def whats_new(self, *_) -> None:
        """What's new, from the menu: the last few releases."""
        if not self._whats_new(f"What's new in Shotcast {self.version}",
                               [r for r in changelog.releases() if not changelog.newer(r.version, self.version)][:5]):
            webbrowser.open(CHANGELOG_URL)

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

    def quit_asked(self, *_) -> None:
        """Quit from the menu: it's the last item, easy to hit by mistake, and takes the booth and the
        stream off air, so it asks first."""
        if self.ui is not None and self.ui.ok and not self.ui.ask(
                "Quit Shotcast?\n\nThe booth stops commenting and the live stream goes off air until it's started again."):
            log.info("Quit from the menu: cancelled")
            return
        log.info("Quit from the menu")
        self.quit()

    def quit(self, *_) -> None:
        self._stop.set()
        self.booth.stop()
        if self.cfg.live.enabled and (self.cfg.publish.bucket or self.cfg.live.local_keep_days):
            # Stopping the booth ends it outright, so it never got to take the stream off air.
            from .live_stream import off_air

            try:
                off_air(self.cfg)
            except Exception:
                log.exception("Taking the live stream off air failed")
        if self.icon is not None:
            self.icon.stop()

    def notify(self, text: str) -> None:
        """A Windows notification, for things nobody asked about (they may not show: Focus assist, or
        notifications off for Python)."""
        log.info("Notice: %s", text)
        if self.icon is not None:
            try:
                self.icon.notify(text, APP)
            except Exception:
                pass

    def info(self, text: str) -> None:
        """A message box, for the result of something chosen in the menu."""
        if self.ui is not None:
            self.ui.info(text)
        else:
            self.notify(text)

    def error(self, text: str) -> None:
        if self.ui is not None:
            self.ui.error(text)
        else:
            self.notify(text)

    def update_label(self) -> str:
        if self.update:
            return f"Restart to update to {self.update[0].version}"
        if self.checked_at:
            return f"Check for updates (up to date, {_when(self.checked_at)})"
        return "Check for updates"

    # -- menu
    def menu(self):
        import pystray

        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        warnings = lambda: health_issues(self.cfg.log_dir)  # noqa: E731

        def warning_items():
            return [item(f"{'Problem' if lvl == 'fail' else 'Warning'}: {msg}"[:120], None, enabled=False)
                    for lvl, msg in warnings()[:10]]

        safe = self._safe
        return pystray.Menu(
            item(lambda _: self.status(), None, enabled=False),
            item(lambda _: last_backup(self.cfg), None, enabled=False),
            item("Warnings", pystray.Menu(warning_items), visible=lambda _: bool(warnings())),
            item("Open the stats page", safe(self.open_stats), default=True, enabled=lambda _: bool(self.stats_url)),
            sep,
            item("Restart the booth", safe(self.restart_booth)),
            item(lambda _: "Turn the AI booth off" if self._commentary_on() else "Turn the AI booth on",
                 safe(self.toggle_commentary)),
            item(lambda _: "Stop the live stream" if self.live_now() else "Start the live stream",
                 safe(self.toggle_live), visible=lambda _: self.cfg.live.enabled),
            item("Set up the live stream...", safe(self.setup_live), visible=lambda _: not self.cfg.live.enabled),
            item("Settings...", safe(self.settings)),
            item("More", pystray.Menu(
                item("Set up the live stream again (OBS)...", safe(self.setup_live),
                     visible=lambda _: self.cfg.live.enabled),
                item("Open the Shotcast folder", safe(self.open_folder)),
                item("Open the recordings folder", safe(self.open_recordings),
                     visible=lambda _: self.cfg.live.enabled and bool(self.cfg.live.local_keep_days)),
                item("Open logs", safe(self.open_logs)),
                item("Edit config.yaml", safe(self.edit_config)),
                item("Edit .env (API keys)", safe(self.edit_keys)),
                item("Command prompt (shotcast commands)", safe(self.command_prompt)),
            )),
            item("Check the screen read...", safe(self.screen_check)),
            item("Back up now", safe(self.back_up)),
            item("Create a problem report", safe(self.problem_report)),
            sep,
            item(lambda _: self.update_label(),
                 safe(lambda: self.apply_update() if self.update else self.check_updates())),
            item("Check for updates automatically", safe(self.toggle_update_checks),
                 checked=lambda _: checks_for_updates()),
            item("Install updates automatically (as Shotcast starts, or when nobody's playing)",
                 safe(self.toggle_update_installs), checked=lambda _: installs_updates()),
            item("Start with Windows", safe(self.toggle_start_with_windows), checked=lambda _: starts_with_windows()),
            item("Notify when the booth is ready", safe(self.toggle_ready_notice), checked=lambda _: notifies_when_ready()),
            item("What's new", safe(self.whats_new)),
            item(f"About Shotcast {self.version}", safe(self.about)),
            item("Quit...", self.quit_asked),
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
        if self.booth.state == "starting":
            return "grey"
        issues = health_issues(self.cfg.log_dir)
        return "orange" if issues else "green"

    # -- running
    def start_stats(self) -> None:
        from . import local_video
        from .stats_web import make_server
        from .store import Store

        db = Path(self.cfg.db_path)
        if not db.exists():
            Store(str(db), new_session=False).db.close()   # an empty season, so the page opens before round one
        names = {key: persona.name for key, persona in self.cfg.personas.items()}
        site = Path(self.cfg.log_dir) / "stats-web-site"
        def videos():   # self.cfg as Settings last saved it
            return local_video.folder(self.cfg.live, self.home), self.cfg.live.local_keep_days
        def brand():
            return self.cfg.publish.brand
        publisher = self.publisher
        def video_deleted(rec):   # the bucket's copy, if streams are kept there, goes too
            if self.cfg.publish.bucket and self.cfg.live.keep_days:
                from .live_stream import request_delete

                request_delete(self.cfg.log_dir, rec["session"], rec["since"], rec["ended"])
        def bucket_videos():   # the bucket's kept streams, as the booth's mirror last saved them
            if not (self.cfg.publish.bucket and self.cfg.live.keep_days):
                return []
            try:
                recs = json.loads((Path(self.cfg.log_dir) / "live-recordings.json").read_text(encoding="utf-8"))
                recs = recs.get("recordings") if isinstance(recs, dict) else None
            except (OSError, ValueError):
                return []
            return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []
        hooks = {"on_change": publisher.changed, "publish_status": publisher.status, "video_deleted": video_deleted,
                 "bucket_videos": bucket_videos}
        try:
            server = make_server(db, STATS_PORT, names, site, brand, videos, **hooks)
        except OSError:
            server = make_server(db, 0, names, site, brand, videos, **hooks)   # the usual port is taken
        self.stats_url = f"http://127.0.0.1:{server.server_address[1]}/"
        (Path(self.cfg.log_dir) / "stats-url.txt").write_text(self.stats_url, encoding="utf-8")
        threading.Thread(target=server.serve_forever, name="stats", daemon=True).start()
        log.info("Stats page at %s", self.stats_url)

    def _watch(self) -> None:
        next_update = time.monotonic() + 60
        next_idle = time.monotonic() + GSPRO_CHECK_S
        last = None
        while not self._stop.wait(1 if self.booth.state == "starting" else 3):
            if self.booth.poll() and notifies_when_ready():
                self.notify(self._ready_notice())
            now = (self.booth.state, self._light(), self.status())
            if now != last:
                last = now
                self._refresh()
            if time.monotonic() >= next_idle:
                next_idle = time.monotonic() + GSPRO_CHECK_S
                try:
                    if self.install_when_idle():
                        return
                except Exception:
                    log.exception("Installing the update while idle failed")
            if time.monotonic() >= next_update:
                next_update = time.monotonic() + UPDATE_EVERY_S
                if checks_for_updates():
                    threading.Thread(target=self._check_updates, daemon=True).start()

    def run(self) -> None:
        import pystray

        from .tray_ui import Ui

        self.ui = Ui(fallback=self.notify)
        try:
            if self.install_at_start():
                return
        except Exception:
            log.exception("Installing the downloaded update as Shotcast starts failed")
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
                self.info("Shotcast is running and recording your shots. Its icon is in the taskbar's "
                          "notification area (under ^ if it's hidden): right-click it for the stats page and "
                          "Settings. Then, with GSPro on a hole, choose Check the screen read.")
            self._after_update()

        self.icon.run(setup)


def _logo(size: int):
    """The app's logo: a golf ball sending out broadcast waves, on a green rounded square. Drawn at
    four times the size and scaled down; small sizes leave out the dimples and the outer wave."""
    import math

    from PIL import Image, ImageDraw

    small = size <= 24
    S = max(size, 32) * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bg = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(bg)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(18 + 10 * t), int(122 - 50 * t), int(70 - 25 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * (.16 if small else .22)), fill=255)
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)
    cx, cy = S * (.38 if small else .40), S * (.64 if small else .62)
    waves = (.40, .60) if small else (.36, .50, .64)
    for i, r in enumerate(S * w for w in waves):
        d.arc((cx - r, cy - r, cx + r, cy + r), start=-64, end=-16, fill=(255, 255, 255, 245 - i * 40),
              width=int(S * (.09 if small else .055)))
    r = S * (.30 if small else .25)
    ball = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(ball)
    for k in range(40):   # shaded, lit from the upper left
        f = 1 - k / 40
        rr, off, v = r * f, -r * .25 * (1 - f), int(212 + 43 * (1 - f))
        bd.ellipse((cx + off - rr, cy + off - rr, cx + off + rr, cy + off + rr), fill=(v, v, min(255, v + 3), 255))
    if not small:
        for row in range(-3, 4):
            for col in range(-3, 4):
                x, y = cx + col * r * .27 + (r * .135 if row % 2 else 0), cy + row * r * .24
                dist = math.hypot(x - cx, y - cy) / r
                if dist < .84:
                    s = r * .07 * (1 - .5 * dist ** 2)
                    bd.ellipse((x - s, y - s, x + s, y + s), fill=(196, 201, 206, 255))
    bmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bmask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    img.paste(ball, (0, 0), bmask)
    return img.resize((size, size), Image.LANCZOS)


def icon_image(light: str = "green", size: int = 64):
    """The tray icon: the logo with a status light (green, orange, red, or grey while starting) in the corner."""
    from PIL import ImageDraw

    img = _logo(size)
    s = size
    color = {"green": (40, 200, 90), "orange": (245, 160, 20), "red": (220, 40, 40),
             "grey": (150, 150, 150)}.get(light, (40, 200, 90))
    ImageDraw.Draw(img).ellipse((s * .60, s * .60, s - 1, s - 1), fill=color + (255,),
                                outline=(255, 255, 255, 255), width=max(1, s // 20))
    return img


def write_icon(path: str | Path) -> None:
    """The app's .ico (shortcuts, the installer, Settings), each size drawn for itself."""
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big = _logo(256)
    big.save(path, format="ICO", sizes=[(n, n) for n in sizes],
             append_images=[_logo(n) for n in sizes if n != 256])


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
