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
    assert len(started) == tray.RESTARTS + 1 and booth.state == "running"
    started[-1].returncode = 1
    booth.poll()
    assert len(started) == tray.RESTARTS + 1 and booth.state == "crashed"
    booth.restart()   # the menu's Restart tries again
    assert len(started) == tray.RESTARTS + 2 and booth.state == "running"


def test_crashes_spread_out_keep_restarting(tmp_path):
    (tmp_path / "logs").mkdir()
    started, now = [], [0.0]
    booth = tray.Booth(tmp_path, popen=lambda *a, **k: started.append(_Proc()) or started[-1], clock=lambda: now[0])
    booth.start()
    for _ in range(tray.RESTARTS * 3):
        started[-1].returncode = 1
        now[0] += tray.RESTART_WINDOW_S
        booth.poll()
    assert booth.state == "running"


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
