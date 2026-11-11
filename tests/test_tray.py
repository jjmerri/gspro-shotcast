import json
import os
import sys

import pytest

from commentator import tray, updater
from commentator.config import load_config


def test_versions_compare_as_numbers():
    assert updater.newer("v0.10.0", "0.9.9")
    assert updater.newer("1.0.1", "1.0")
    assert not updater.newer("v1.0.0", "1.0")
    assert not updater.newer("0.1.0", "0.1.0.dev7")   # a test build isn't offered its own release


def test_release_needs_an_installer_and_its_hash():
    asset = lambda name: {"name": name, "browser_download_url": f"https://x/{name}"}  # noqa: E731
    data = {"tag_name": "v0.2.0", "html_url": "https://page",
            "assets": [asset("Shotcast-Setup-0.2.0.exe"), asset("Shotcast-Setup-0.2.0.exe.sha256")]}
    got = updater.pick(data)
    assert (got.version, got.url, got.sha256_url) == ("0.2.0", "https://x/Shotcast-Setup-0.2.0.exe",
                                                       "https://x/Shotcast-Setup-0.2.0.exe.sha256")
    assert updater.pick({**data, "assets": data["assets"][:1]}) is None
    assert updater.pick({**data, "prerelease": True}) is None


class _Http:
    def __init__(self, body: bytes, published: str):
        self.body, self.published = body, published

    def get(self, url, **_):
        return type("R", (), {"text": self.published, "raise_for_status": lambda self: None})()

    def stream(self, method, url, **_):
        body = self.body

        class Ctx:
            def __enter__(self):
                return type("R", (), {"raise_for_status": lambda self: None,
                                      "iter_bytes": lambda self, n: [body]})()

            def __exit__(self, *a):
                return False
        return Ctx()


def test_download_keeps_only_a_file_matching_its_hash(tmp_path):
    import hashlib

    release = updater.Release("0.2.0", "u", "u.sha256", "Shotcast-Setup-0.2.0.exe", "")
    good = b"installer"
    out = updater.download(release, tmp_path, _Http(good, hashlib.sha256(good).hexdigest() + "  x.exe\n"))
    assert out.read_bytes() == good
    out.unlink()
    with pytest.raises(RuntimeError):
        updater.download(release, tmp_path, _Http(b"tampered", hashlib.sha256(good).hexdigest()))
    assert list(tmp_path.iterdir()) == []


def test_a_new_home_records_only_and_keeps_existing_files(tmp_path):
    home, first = tray.prepare_home(tmp_path / "Shotcast")
    assert first and (home / ".env").exists() and (home / "logs").is_dir()
    assert load_config(home / "config.yaml").commentary.enabled is False
    (home / "config.yaml").write_text("commentary:\n  enabled: true\n")
    home, first = tray.prepare_home(home)
    assert not first and "enabled: true" in (home / "config.yaml").read_text()


def test_health_lists_failures_first(tmp_path):
    (tmp_path / "health.json").write_text(json.dumps({"issues": [
        {"key": "a", "level": "warn", "message": "slow"}, {"key": "b", "level": "fail", "message": "no screen"}]}))
    assert tray.health_issues(tmp_path) == [("fail", "no screen"), ("warn", "slow")]
    assert tray.health_issues(tmp_path / "missing") == []


class _Proc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


def test_the_booth_restarts_after_a_crash_but_not_endlessly(tmp_path):
    (tmp_path / "logs").mkdir()
    started, now = [], [0.0]

    def popen(*a, **k):
        started.append(_Proc())
        return started[-1]
    booth = tray.Booth(tmp_path, popen=popen, clock=lambda: now[0])
    booth.start()
    for _ in range(tray.RESTARTS):
        started[-1].returncode = 1
        now[0] += 10
        booth.poll()
    assert len(started) == tray.RESTARTS + 1 and booth.state == "starting"
    started[-1].returncode = 1
    booth.poll()
    assert len(started) == tray.RESTARTS + 1 and booth.state == "crashed"
    booth.restart()   # the menu's Restart tries again
    assert len(started) == tray.RESTARTS + 2 and booth.state == "starting"


def test_crashes_spread_out_keep_restarting(tmp_path):
    (tmp_path / "logs").mkdir()
    started, now = [], [0.0]
    booth = tray.Booth(tmp_path, popen=lambda *a, **k: started.append(_Proc()) or started[-1], clock=lambda: now[0])
    booth.start()
    for _ in range(tray.RESTARTS * 3):
        started[-1].returncode = 1
        now[0] += tray.RESTART_WINDOW_S
        booth.poll()
    assert booth.state == "starting"


def test_the_booth_is_starting_until_it_says_its_ready(tmp_path):
    (tmp_path / "logs").mkdir()
    started, now = [], [0.0]
    booth = tray.Booth(tmp_path, popen=lambda *a, **k: started.append((_Proc(), k["env"])) or started[-1][0],
                       clock=lambda: now[0])
    booth.ready_file.write_text("1")   # the last run's
    booth.start()
    assert booth.state == "starting" and started[0][1]["SHOTCAST_READY_FILE"] == str(booth.ready_file)
    assert not booth.poll() and booth.state == "starting"
    booth.ready_file.write_text("1")
    assert booth.poll() and booth.state == "running"   # started from the menu: say so
    assert not booth.poll()
    started[-1][0].returncode = 1
    booth.poll()                                        # crashed and restarted: starting, quietly
    assert booth.state == "starting"
    booth.ready_file.write_text("1")
    assert not booth.poll() and booth.state == "running"
    started[-1][0].returncode = 1
    booth.poll()
    now[0] += tray.STARTING_LIMIT_S + 1                 # never said: taken as running, not announced
    assert not booth.poll() and booth.state == "running"


def test_env_beside_the_config_wins(tmp_path, monkeypatch):
    from commentator.__main__ import _load_env

    monkeypatch.setenv("SHOTCAST_TEST_KEY", "system")
    (tmp_path / ".env").write_text("SHOTCAST_TEST_KEY=home\n")
    _load_env(str(tmp_path / "config.yaml"))
    assert os.environ["SHOTCAST_TEST_KEY"] == "home"


def test_start_with_windows_runs_the_windowless_python(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert tray.run_command() == f'"{tmp_path / "pythonw.exe"}" -m commentator tray'
    assert tray.run_command(tmp_path / "home").endswith(f' tray --home "{(tmp_path / "home").resolve()}"')


def test_update_says_whether_it_took(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: runs.append(args))
    monkeypatch.setattr(updater, "current_version", lambda: "0.1.0")
    updater.install(tmp_path / "Shotcast-Setup-0.2.0.exe", "0.2.0")
    assert "/SILENT" in runs[0] and f"/LOG={tmp_path / 'install.log'}" in runs[0]
    ok, text, was = updater.after_update(tmp_path, "0.2.0")
    assert ok and "0.2.0" in text and was == "0.1.0"
    assert updater.after_update(tmp_path, "0.2.0") is None      # said once

    updater.install(tmp_path / "Shotcast-Setup-0.2.0.exe", "0.2.0")
    ok, text, _ = updater.after_update(tmp_path, "0.1.0")
    assert not ok and "install.log" in text
    assert updater.after_update(tmp_path / "none", "0.1.0") is None


def test_an_update_backs_up_the_season_first_and_publishes_once_it_took(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from commentator import backup, tray

    order = []
    monkeypatch.setattr(backup, "pre_update", lambda cfg, version: order.append(("backup", version)))
    monkeypatch.setattr(updater, "install", lambda *a: order.append("install"))
    app = SimpleNamespace(update=(SimpleNamespace(version="0.2.0"), tmp_path / "x.exe"), version="0.1.0",
                          home=tray.default_home(), cfg=None, booth=SimpleNamespace(stop=lambda: order.append("stop"), state="running"),
                          quit=lambda: order.append("quit"))
    app._install = lambda version, installer: tray.Tray._install(app, version, installer)
    tray.Tray.apply_update(app)
    assert order == ["stop", ("backup", "0.1.0"), "install", "quit"]

    monkeypatch.setattr(backup, "pre_update", lambda cfg, version: 1 / 0)   # a failed copy doesn't stop it
    order.clear()
    tray.Tray.apply_update(app)
    assert order == ["stop", "install", "quit"]

    def broken(*a):
        raise OSError("blocked")
    monkeypatch.setattr(updater, "install", broken)   # the installer won't run: the booth starts again
    app.booth.start = lambda: order.append("start")
    app.booth.state = "running"
    order.clear()
    with pytest.raises(OSError):
        tray.Tray.apply_update(app)
    assert order == ["stop", "start"]
    app.booth.state = "other"   # another's booth: not one of this tray's started beside it
    order.clear()
    with pytest.raises(OSError):
        tray.Tray.apply_update(app)
    assert order == ["stop"] and app.booth.state == "other"

    published, said = [], []
    app.publisher = SimpleNamespace(changed=lambda: published.append(1))
    app.info = app.error = said.append
    app._whats_new = lambda heading, rels: False
    for took in (True, False):
        monkeypatch.setattr(updater, "after_update", lambda folder, version: (took, "text", "0.1.0"))
        tray.Tray._after_update(app)
    assert published == [1] and said == ["text", "text"]


def test_a_downloaded_update_installs_itself_as_shotcast_starts_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from commentator import tray

    folder = tmp_path / "updates"
    folder.mkdir()
    for name in ("Shotcast-Setup-0.1.9.exe", "Shotcast-Setup-0.1.12.exe", "Shotcast-Setup-0.1.13.exe.part"):
        (folder / name).write_text("")
    installed = []
    app = SimpleNamespace(home=tmp_path, version="0.1.10", _install=lambda v, p: installed.append((v, p.name)),
                          round_in_progress=lambda: None, surely_off_air=lambda: True)
    monkeypatch.setattr(tray, "installs_updates", lambda: True)
    monkeypatch.setattr(tray, "other_booths", lambda: [])
    monkeypatch.setattr(tray, "gspro_running", lambda: True)
    assert not tray.Tray.install_at_start(app) and not installed   # GSPro's open: not now, nor marked tried
    assert app.update[0].version == "0.1.12"                         # but offered, for when it's closed a while
    monkeypatch.setattr(tray, "gspro_running", lambda: False)
    assert tray.Tray.install_at_start(app)
    assert installed == [("0.1.12", "Shotcast-Setup-0.1.12.exe")]
    # It didn't take (still 0.1.10 at the next start): it waits for Restart to update, not a loop.
    assert not tray.Tray.install_at_start(app) and len(installed) == 1
    app.version = "0.1.12"
    assert not tray.Tray.install_at_start(app)
    monkeypatch.setattr(tray, "installs_updates", lambda: False)
    (folder / "Shotcast-Setup-0.1.14.exe").write_text("")
    assert not tray.Tray.install_at_start(app) and len(installed) == 1


def test_a_downloaded_update_installs_itself_once_gspro_has_been_closed_a_while(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from commentator import tray

    clock, running, applied = [0.0], [True], []
    app = SimpleNamespace(home=tmp_path, _clock=lambda: clock[0], _gspro_at=0.0, round_in_progress=lambda: None,
                          surely_off_air=lambda: True, apply_update=lambda: applied.append(1),
                          booth=SimpleNamespace(state="running", proc=SimpleNamespace(pid=7)),
                          update=(SimpleNamespace(version="0.2.0"), tmp_path / "x.exe"),
                          struck_ago=lambda: 0.0, ui=None)
    app._clear_to_install = lambda v: tray.Tray._clear_to_install(app, v)
    monkeypatch.setattr(tray, "installs_updates", lambda: True)
    monkeypatch.setattr(tray, "gspro_running", lambda: running[0])
    others = [7]                                             # its own booth, which doesn't count
    monkeypatch.setattr(tray, "other_booths", lambda: others)
    idle = lambda t: (clock.__setitem__(0, t), tray.Tray.install_when_idle(app))[1]   # noqa: E731
    assert not idle(100.0)                                   # GSPro's open, and someone's playing
    running[0] = False
    assert not idle(100.0 + tray.IDLE_INSTALL_S - 1)         # closed, not long enough
    app.round_in_progress = lambda: True
    assert not idle(100.0 + tray.IDLE_INSTALL_S)             # a round still on
    app.round_in_progress, app.surely_off_air = (lambda: False), (lambda: False)
    assert not idle(100.0 + tray.IDLE_INSTALL_S)             # on air, or can't tell
    app.surely_off_air, app.booth.state = (lambda: True), "other"
    assert not idle(100.0 + tray.IDLE_INSTALL_S)             # a booth this tray didn't start
    app.booth.state, others[:] = "running", [7, 9]
    assert not idle(100.0 + tray.IDLE_INSTALL_S)             # one started since, not from here
    others[:] = [7]
    assert idle(100.0 + tray.IDLE_INSTALL_S) and applied == [1]
    assert not idle(200.0 + tray.IDLE_INSTALL_S) and applied == [1]   # tried once


def test_an_update_installs_with_gspro_open_after_a_countdown_once_nobodys_played_a_while(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from commentator import tray

    clock, applied, struck, shown, answers = [0.0], [], [0.0], [], []

    def countdown(text, seconds, go, wait):
        shown.append(text)
        return answers.pop(0)()
    app = SimpleNamespace(home=tmp_path, _clock=lambda: clock[0], _gspro_at=0.0, round_in_progress=lambda: False,
                          surely_off_air=lambda: True, apply_update=lambda: applied.append(1),
                          booth=SimpleNamespace(state="running", proc=SimpleNamespace(pid=7)),
                          update=(SimpleNamespace(version="0.2.0"), tmp_path / "x.exe"),
                          struck_ago=lambda: struck[0], ui=SimpleNamespace(ok=True, countdown=countdown))
    app._clear_to_install = lambda v: tray.Tray._clear_to_install(app, v)
    monkeypatch.setattr(tray, "installs_updates", lambda: True)
    monkeypatch.setattr(tray, "gspro_running", lambda: True)
    monkeypatch.setattr(tray, "other_booths", lambda: [7])
    idle = lambda t: (clock.__setitem__(0, t), tray.Tray.install_when_idle(app))[1]   # noqa: E731
    struck[0] = tray.IDLE_INSTALL_S - 1
    assert not idle(100.0) and not shown                     # a shot under half an hour ago
    struck[0], app.ui.ok = tray.IDLE_INSTALL_S, False
    assert not idle(100.0) and not shown                     # nowhere to warn them: not with GSPro open
    app.ui.ok = True
    answers.append(lambda: False)                            # Postpone
    assert not idle(100.0) and len(shown) == 1 and "{s}" in shown[0]
    assert not idle(100.0 + tray.IDLE_INSTALL_S - 1) and len(shown) == 1   # not asked again for a while
    answers.append(lambda: struck.__setitem__(0, 5.0) or True)   # a shot while it counted down
    assert not idle(100.0 + tray.IDLE_INSTALL_S) and len(shown) == 2 and not applied
    struck[0], auto = tray.IDLE_INSTALL_S, [True]
    monkeypatch.setattr(tray, "installs_updates", lambda: auto[0])
    answers.append(lambda: auto.__setitem__(0, False) or True)   # unticked in the menu while it counted
    assert not idle(100.0 + tray.IDLE_INSTALL_S) and len(shown) == 3 and not applied
    auto[0] = True
    answers.append(lambda: True)                             # ran out, or Install now
    assert idle(200.0 + tray.IDLE_INSTALL_S) and applied == [1]
    assert not idle(300.0 + tray.IDLE_INSTALL_S) and len(shown) == 4   # tried once


def test_start_the_live_stream_starts_obs_when_it_isnt_running(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from commentator import live_stream, obs_setup, tray
    from commentator.live_stream import ObsError

    calls = []

    class Obs:
        running = False

        def __init__(self, *a, **kw):
            pass

        def live(self):
            if not Obs.running:
                raise ObsError("OBS isn't answering")
            return False

        def start(self):
            calls.append("start")

    monkeypatch.setattr(live_stream, "Obs", Obs)
    monkeypatch.setattr(live_stream, "launch_obs", lambda exe: calls.append("launch") or True)
    monkeypatch.setattr(obs_setup, "wait_for", lambda obs, seconds: setattr(Obs, "running", True))
    errors, notes = [], []
    app = SimpleNamespace(cfg=SimpleNamespace(live=SimpleNamespace(obs_host="h", obs_port=1, obs_path=None,
                                                                   local_keep_days=0),
                                              publish=SimpleNamespace(bucket=None)),
                          home=tmp_path, error=errors.append, notify=notes.append)
    app._start_obs_and_stream = lambda obs: tray.Tray._start_obs_and_stream(app, obs)
    app._live_starting = lambda: tray.Tray._live_starting(app)
    tray.Tray.toggle_live(app)
    assert "Sharing" in errors[0] and not calls       # nowhere for it to go yet

    app.cfg.live.local_keep_days = 14                 # kept on the PC, with no stats page online
    monkeypatch.setattr(tray.threading, "Thread", lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    tray.Tray.toggle_live(app)
    assert calls == ["launch", "start"] and len(errors) == 1 and "recordings folder" in notes[-1]


def test_a_run_of_edits_on_the_local_page_publishes_once_after_the_last(tmp_path):
    import threading
    import time

    ran, done = [], threading.Event()

    class Done:
        returncode, stdout, stderr = 0, "Published v1 to s3://b/\n", ""

    def run(cmd, **kw):
        ran.append(cmd)
        done.set()
        return Done()

    pub = tray.AutoPublisher(lambda: ["py", "-m", "commentator.stats_site"], tmp_path, delay=0.2, run=run)
    for _ in range(3):
        pub.changed()
        time.sleep(0.05)
    assert pub.status()["due"] is not None and not ran
    assert done.wait(2)
    time.sleep(0.3)   # the replaced timers never publish
    assert ran == [["py", "-m", "commentator.stats_site"]]
    status = pub.status()
    assert status["due"] is None and not status["publishing"]
    assert status["last"]["ok"] and status["last"]["message"] == "Published v1 to s3://b/"

    off = tray.AutoPublisher(lambda: None, tmp_path, delay=0.01, run=run)
    assert not off.changed()   # publishing isn't set up: nothing waits
    assert not off.status()["enabled"] and off.status()["due"] is None


def test_saving_the_stats_pages_settings_republishes_it_and_says_so():
    from types import SimpleNamespace

    def app(on):
        a = SimpleNamespace(scheduled=[], notices=[])
        a.publisher = SimpleNamespace(changed=lambda: a.scheduled.append(1) or on)
        a.notify = a.notices.append
        return a

    a = app(True)
    assert tray.Tray.republish_for(a, {("publish", "theme", "accent"): "#ffd21f", ("tts", "enabled"): True})
    assert a.scheduled == [1] and "republishes" in a.notices[0]
    a = app(True)
    assert not tray.Tray.republish_for(a, {("live", "enabled"): True}) and not a.scheduled and not a.notices
    a = app(False)   # publishing is off: nothing to say
    assert not tray.Tray.republish_for(a, {("publish", "site_name"): "X"}) and a.scheduled == [1] and not a.notices


def test_the_booth_restarts_unasked_only_when_its_status_says_between_rounds(tmp_path):
    import time
    from types import SimpleNamespace

    app = SimpleNamespace(cfg=SimpleNamespace(log_dir=str(tmp_path)))
    status = tmp_path / tray.ROUND_STATUS
    assert tray.Tray.round_in_progress(app) is None                    # no status: can't tell
    status.write_text(json.dumps({"round": False, "updated": time.time()}))
    assert tray.Tray.round_in_progress(app) is False
    status.write_text(json.dumps({"round": True, "updated": time.time()}))
    assert tray.Tray.round_in_progress(app) is True
    status.write_text(json.dumps({"round": False, "updated": time.time() - 60}))
    assert tray.Tray.round_in_progress(app) is None                    # stale: the booth may be gone


async def test_the_booth_says_its_ready_once_its_startup_checks_are_done(tmp_path):
    import asyncio

    from commentator.engine import Engine

    checks = asyncio.Event()

    async def startup_health():
        await checks.wait()
    health = asyncio.create_task(startup_health())
    told = asyncio.create_task(Engine._say_ready(str(tmp_path / "booth-ready"), health))
    await asyncio.sleep(0.05)
    assert not (tmp_path / "booth-ready").exists()
    checks.set()
    await told
    assert (tmp_path / "booth-ready").exists()


def test_quit_from_the_menu_asks_first():
    from types import SimpleNamespace

    quits, answers = [], [False, True]
    app = SimpleNamespace(ui=SimpleNamespace(ok=True, ask=lambda text: answers.pop(0)), quit=lambda: quits.append(1))
    tray.Tray.quit_asked(app)
    assert not quits   # said no
    tray.Tray.quit_asked(app)
    assert quits == [1]
    app.ui.ok = False   # no window to ask in: quits all the same
    tray.Tray.quit_asked(app)
    assert quits == [1, 1]
