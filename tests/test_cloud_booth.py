import asyncio
import json
import os
import sys
import time
import types
import zipfile
from pathlib import Path

import pytest

from commentator import __main__ as cli
from commentator import cloud, cloud_booth, cloud_rounds
from commentator.store import Store
from tests.test_cloud import API, FakeAPI, base_cfg, mock_http, paired, paired_cfg
from tests.test_round_record import _hit


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    mock_http(monkeypatch, fake)
    return fake


@pytest.fixture(autouse=True)
def _own_environment():
    """Paired startup changes os.environ itself (cloud.clean_env, the profile's .env), and the working
    directory (the profile's home): put them back."""
    before, cwd = dict(os.environ), os.getcwd()
    yield
    os.chdir(cwd)
    for k in set(os.environ) - set(before):
        del os.environ[k]
    os.environ.update(before)


def _round(s, start):
    u = s.begin_round(f"r{start}", course="Pebble", players={"Ann": None})
    _hit(s, "Ann", start)
    s.end_round(u, complete=True)
    s.db.commit()
    return u


def _health(cfg):
    from commentator.tray import health_issues

    return health_issues(cfg.log_dir, cloud_booth.HEALTH)


# ---- the upload loop ---------------------------------------------------------------------------

def test_unpaired_or_replaying_nothing_goes_up(tmp_path, api):
    assert cloud_booth.start(base_cfg(tmp_path / "a")) is None
    cfg = paired_cfg(tmp_path / "b", api)
    cfg.source.type = "replay"
    assert cloud_booth.start(cfg) is None


def test_a_profile_the_cloud_no_longer_accepts_captures_sends_nothing_and_tells_the_tray(tmp_path, api):
    from commentator.config import load_config

    prof = paired(api)
    store = cloud.store()
    store.write(store.read() | {"refused": 1.0})
    got = cloud.locate()
    cfg = cloud.booth_config(load_config(got.config), got)
    assert cfg.db_path == str(prof.db)   # (capture goes on in the profile)
    assert cloud_booth.start(cfg, got) is None
    [(level, msg)] = _health(cfg)
    assert level == "fail" and "nothing is sent" in msg


async def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end
        await asyncio.sleep(0.01)


def test_the_running_booth_sends_settled_rounds_and_stops_for_good_when_the_pairing_changes(tmp_path, api, monkeypatch):
    monkeypatch.setattr(cloud_booth, "DRAIN_S", 0.01)
    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    u = _round(s, 1)

    async def go():
        task = cloud_booth.start(cfg, cloud.locate())
        await _until(lambda: u in api.rounds)
        store = cloud.store()
        store.write(store.read() | {"sim_name": "Bay 2"})   # another bay now: takes effect at restart
        _round(s, 100)
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert set(api.rounds) == {u}   # the second round never went
    [(level, msg)] = _health(cfg)
    assert level == "fail" and "uploads stopped" in msg


def test_a_season_file_that_isnt_the_pairings_stops_the_loop(tmp_path, api, monkeypatch):
    monkeypatch.setattr(cloud_booth, "DRAIN_S", 0.01)
    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    _round(s, 1)
    prof = cloud.locate()
    s.db.execute("DELETE FROM cloud_pairing")
    s.db.commit()
    up = cloud_booth.Uploads(cfg, prof)
    asyncio.run(asyncio.wait_for(up.run(), 5))
    assert up.stopped and api.puts == [] and _health(cfg)[0][0] == "fail"


def _loop(cfg, rounds, up=None):
    """Run the upload loop (`up`, else a new one) for `rounds` drains."""
    up = up or cloud_booth.Uploads(cfg, cloud.locate())
    n = 0

    async def sleep(s):
        nonlocal n
        n += 1
        if n >= rounds:
            raise asyncio.CancelledError

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await up.run()
    real = asyncio.sleep
    asyncio.sleep = sleep
    try:
        asyncio.run(go())
    finally:
        asyncio.sleep = real


def test_an_api_that_needs_a_newer_shotcast_shows_in_the_tray_till_it_takes_rounds_again(tmp_path, api, monkeypatch):
    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    u = _round(Store(cfg.db_path), 1)
    api.minimum = "9.0.0"
    _loop(cfg, 1)
    [(level, msg)] = _health(cfg)
    assert level == "fail" and "9.0.0" in msg and "update Shotcast" in msg
    api.minimum = None
    _loop(cfg, 1)
    assert _health(cfg) == [] and u in api.rounds


def _clock(monkeypatch):
    """The upload queue's clock (cloud_rounds' time.time), moved by hand."""
    now = [time.time()]
    monkeypatch.setattr(cloud_rounds, "time", types.SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(cloud_booth, "time", types.SimpleNamespace(time=lambda: now[0]))
    return now


def test_rounds_that_arent_going_up_turn_the_tray_red_whatever_the_cause_and_clear_once_they_go(tmp_path, api,
                                                                                               monkeypatch):
    import httpx

    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    now = _clock(monkeypatch)
    up = cloud_booth.Uploads(cfg, cloud.locate())
    u = _round(Store(cfg.db_path), 1)
    now[0] += 1   # (the round is over)
    api.answers = [httpx.Response(502), httpx.Response(404, json={"detail": "Not Found"})]
    _loop(cfg, 1, up)
    assert _health(cfg) == []   # (behind, but not for long yet)
    now[0] += cloud_booth.BEHIND_S - 5
    _loop(cfg, 1, up)
    assert _health(cfg) == [] and len(api.puts) == 2
    now[0] += 10
    _loop(cfg, 1, up)   # (waiting for its retry: nothing tried, and now it shows)
    [(level, msg)] = _health(cfg)
    assert level == "fail" and "haven't gone up for 15 minutes" in msg and len(api.puts) == 2
    now[0] += cloud_rounds.RETRY_MAX_S + 1
    _loop(cfg, 1, up)
    assert _health(cfg) == [] and u in api.rounds
    now[0] += cloud_booth.BEHIND_S + 1   # (all on Shotcast Cloud since: a new round starts the clock again)
    _loop(cfg, 1, up)
    assert _health(cfg) == []


@pytest.mark.parametrize("failure", [TimeoutError("another upload holds the lock"), OSError("the season file is busy"),
                                     cloud.CloudError("Shotcast Cloud refused to refresh this PC's pass")])
def test_drains_that_cant_even_run_turn_the_tray_red_once_rounds_wait_too_long(tmp_path, api, monkeypatch, failure):
    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    now = _clock(monkeypatch)
    up = cloud_booth.Uploads(cfg, cloud.locate())
    _round(Store(cfg.db_path), 1)
    now[0] += 1
    real = cloud_rounds.drain

    def broken(*a, **kw):
        raise failure
    monkeypatch.setattr(cloud_rounds, "drain", broken)
    _loop(cfg, 2, up)
    assert _health(cfg) == []
    now[0] += cloud_booth.BEHIND_S + 1
    _loop(cfg, 1, up)
    [(level, msg)] = _health(cfg)
    assert level == "fail" and "haven't gone up" in msg
    monkeypatch.setattr(cloud_rounds, "drain", real)
    _loop(cfg, 1, up)
    assert _health(cfg) == []


def test_a_season_file_that_cant_be_read_counts_as_behind(tmp_path, api, monkeypatch):
    cfg = paired_cfg(tmp_path, api)
    now = _clock(monkeypatch)
    up = cloud_booth.Uploads(cfg, cloud.locate())

    def broken(*a, **kw):
        raise OSError("disk")
    monkeypatch.setattr(cloud_rounds, "behind", broken)
    assert up._behind() == []
    now[0] += cloud_booth.BEHIND_S + 1
    [(level, msg)] = up._behind()
    assert level == "fail" and "some round(s)" in msg


@pytest.mark.parametrize("answer, shown", [
    ({"status_code": 409, "json": {"error": "revision_conflict", "revision": 1, "sha256": "other"}}, "conflicts"),
    ({"status_code": 422, "json": {"detail": "not a round"}}, "refused")])
def test_a_round_held_or_refused_keeps_the_tray_red_till_it_is_resolved(tmp_path, api, monkeypatch, answer, shown):
    import httpx

    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    u = _round(Store(cfg.db_path), 1)
    api.answers = [httpx.Response(**answer)]
    up = cloud_booth.Uploads(cfg, cloud.locate())
    for _ in range(3):   # (the drains after it send nothing, and it's still shown)
        _loop(cfg, 1, up)
        [(level, msg)] = _health(cfg)
        assert level == "fail" and shown in msg
    db = cloud_rounds.connect(cfg.db_path)
    db.execute("DELETE FROM cloud_uploads WHERE uuid = ?", (u,))   # (reconciled)
    db.close()
    _loop(cfg, 1, up)
    assert _health(cfg) == [] and u in api.rounds


# ---- the command line --------------------------------------------------------------------------

def _self_hosted_home(tmp_path, monkeypatch):
    """A self-hosted Shotcast folder (config.yaml, .env), as the working directory."""
    home = tmp_path / "self-hosted"
    home.mkdir()
    monkeypatch.chdir(home)
    (home / "config.yaml").write_text("log_dir: logs\ndb_path: logs/season.db\n", encoding="utf-8")
    (home / ".env").write_text("AWS_ACCESS_KEY_ID=SELF-HOSTED\n", encoding="utf-8")
    return home


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["shotcast", *argv])
    return cli.main()


@pytest.mark.parametrize("argv", [["stats"], ["stats-web"], ["publish"], ["restore", "x.db.gz"],
                                  ["backfill-log", "out.txt"], ["snap-minimap", "--course", "Pebble", "--hole", "1"],
                                  ["import-league", "a.xlsx"], ["squabbit"], ["fetch-report", "ABCD-EFGH"],
                                  ["notify-test"]])
def test_paired_the_self_hosted_commands_refuse_to_run(tmp_path, api, monkeypatch, argv):
    _self_hosted_home(tmp_path, monkeypatch)
    paired(api)
    ran = []
    for name in dir(cli):
        if name.startswith("cmd_") and name != "cmd_tray":
            monkeypatch.setattr(cli, name, lambda *a, n=name: ran.append(n))
    with pytest.raises(SystemExit, match="paired with Shotcast Cloud"):
        _run(monkeypatch, *argv)
    assert ran == []
    assert set(cli.SELF_HOSTED) == {"stats", "stats-web", "publish", "restore", "backfill-log", "snap-minimap",
                                    "import-league", "squabbit", "fetch-report", "notify-test"}


def test_paired_another_config_is_refused(tmp_path, api, monkeypatch, capsys):
    home = _self_hosted_home(tmp_path, monkeypatch)
    paired(api)
    monkeypatch.setattr(cli, "cmd_backup", lambda cfg, args: pytest.fail("ran"))
    with pytest.raises(SystemExit):
        _run(monkeypatch, "-c", str(home / "config.yaml"), "backup")
    assert "not -c" in capsys.readouterr().err


def test_paired_every_command_runs_in_the_profile_and_never_opens_the_self_hosted_files(tmp_path, api, monkeypatch):
    import os

    from commentator import config, dotenv

    home = _self_hosted_home(tmp_path, monkeypatch)
    prof = paired(api)
    prof.env.write_text("OPENAI_API_KEY=profile-key\n", encoding="utf-8")
    opened = []
    real_cfg, real_env, real_profile = config.load_config, dotenv.load_env, cloud.load_profile_env
    monkeypatch.setattr(cli, "load_config", lambda p: opened.append(Path(p).resolve()) or real_cfg(p))
    monkeypatch.setattr(dotenv, "load_env", lambda p: opened.append(Path(p).resolve()) or real_env(p))
    monkeypatch.setattr(cloud, "load_profile_env", lambda p: opened.append(Path(p).resolve()) or real_profile(p))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    got = []
    (home / "shots.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: got.append((cfg, args, Path.cwd())))
    _run(monkeypatch, "run", "--replay", "shots.jsonl")
    cfg, args, cwd = got[-1]
    assert cwd.resolve() == prof.home.resolve() and Path(args.config).resolve() == prof.config.resolve()
    assert Path(args.replay).resolve() == (home / "shots.jsonl").resolve()
    assert Path(cfg.db_path) == prof.db and not cfg.backup.s3
    assert all(not p.is_relative_to(home.resolve()) for p in opened), opened
    assert [p for p in opened if p.name == ".env"] == [prof.env.resolve()]   # (nor the one beside the code)
    assert os.environ.get("OPENAI_API_KEY") == "profile-key" and "AWS_ACCESS_KEY_ID" not in os.environ


def test_unpaired_every_command_runs_as_before(tmp_path, monkeypatch):
    _self_hosted_home(tmp_path, monkeypatch)
    got = []
    monkeypatch.setattr(cli, "cmd_restore", lambda cfg, args: got.append(cfg.db_path))
    _run(monkeypatch, "restore", "x.db.gz")
    assert got == ["logs/season.db"]


def test_paired_the_booth_logs_in_the_profile(tmp_path, api, monkeypatch):
    _self_hosted_home(tmp_path, monkeypatch)
    prof = paired(api)
    seen = []
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose, log_dir, **kw: seen.append(Path(log_dir).resolve()))
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: None)
    _run(monkeypatch, "run")
    assert seen == [prof.logs.resolve()]


def test_paired_a_problem_report_is_the_profiles_and_leaves_out_gspros_history(tmp_path, api, monkeypatch):
    from commentator import report
    from commentator.config import load_config

    home = _self_hosted_home(tmp_path, monkeypatch)
    (home / "logs").mkdir()
    (home / "logs" / "booth-2099-01-01.log").write_text("SELF-HOSTED NIGHT\n", encoding="utf-8")
    prof = paired(api)
    prof.env.write_text("OPENAI_API_KEY=sk-profile-secret\n", encoding="utf-8")
    cfg = cloud.booth_config(load_config(prof.config), prof)
    (prof.logs / "booth-2099-01-02.log").write_text("PAIRED NIGHT sk-profile-secret\n", encoding="utf-8")
    gspro = tmp_path / "GSPro"
    gspro.mkdir()
    (gspro / "output_log.txt").write_text("OLD GSPRO NIGHT\n", encoding="utf-8")
    (gspro / "currentRound.dat").write_text("OLD ROUND\n", encoding="utf-8")
    (gspro / "ConnectDebug.txt").write_text("OLD CONNECTOR\n", encoding="utf-8")
    cfg.source.gspro_log = str(gspro / "output_log.txt")
    cfg.source.connector_log = str(gspro / "ConnectDebug.txt")
    # The copies the engine keeps at round end: GSPro's log, and the round file.
    from commentator import round_file
    from commentator.engine import Engine
    from tests.test_round_file import _entry

    (gspro / "output_log.txt").write_text("KEPT GSPRO NIGHT\n", encoding="utf-8")
    Engine._keep_gspro_log(types.SimpleNamespace(_follower=None, cfg=cfg))
    assert list(prof.logs.glob("gspro-*.log"))
    dat = gspro / "currentRound.dat"
    dat.write_text(json.dumps([_entry(1, (0, 0), (0, 200), sp=150.0, name="KEPTROUNDPLAYER")]), encoding="utf-8")
    kept = round_file.archive(dat, cfg.log_dir, time.time() - 60, "test_links", ["KEPTROUNDPLAYER"], [150.0])
    assert kept and kept.is_file()
    dat.write_text("OLD ROUND\n", encoding="utf-8")
    (gspro / "output_log.txt").write_text("OLD GSPRO NIGHT\n", encoding="utf-8")
    assert report.env_files(prof.config, True) == [prof.env.resolve()]
    path = report.build(cfg, prof.config, paired=True)
    assert path.resolve().is_relative_to(prof.home.resolve())
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        text = "".join(z.read(n).decode(errors="replace") for n in names)
    assert "PAIRED NIGHT" in text and "SELF-HOSTED NIGHT" not in text and "sk-profile-secret" not in text
    for old in ("OLD GSPRO NIGHT", "OLD ROUND", "OLD CONNECTOR", "KEPT GSPRO NIGHT", "KEPTROUNDPLAYER"):
        assert old not in text
    assert "left out (paired with Shotcast Cloud)" in text


# ---- the tray ------------------------------------------------------------------------------------

def _tray(api, monkeypatch, default="lg1", prof=None):
    from commentator import tray
    from commentator.config import load_config

    api.default = default
    prof = prof or paired(api)
    opened = []
    monkeypatch.setattr(tray.webbrowser, "open", opened.append)
    monkeypatch.setattr(tray, "AutoPublisher", lambda *a, **kw: None)
    return tray.Tray(prof.home, load_config(prof.config), paired=prof), opened


def test_the_paired_tray_has_no_stats_server_and_its_stats_opens_the_clouds_page(api, monkeypatch):
    from commentator import tray

    t, opened = _tray(api, monkeypatch)
    assert t.cloud is not None and t._publish_command() is None
    t.start_stats()
    t.open_stats()
    assert opened == [f"{API}/leagues/lg1/"] and not (t.home / "logs" / "stats-url.txt").exists()
    del tray


def test_the_paired_tray_with_no_cloud_stats_page_opens_nothing(api, monkeypatch):
    from commentator import tray

    t, opened = _tray(api, monkeypatch, default=None)
    t.start_stats()
    t.open_stats()
    assert t.stats_url is None and opened == []
    del tray


def test_the_paired_tray_offers_no_live_stream_and_keeps_its_mode_till_restarted(api, monkeypatch):
    import types

    t, _ = _tray(api, monkeypatch)
    items = []

    def item(text, action=None, *a, visible=True, **kw):
        items.append((text, visible))
        return action

    class Menu:
        SEPARATOR = None

        def __init__(self, *a):
            pass

    monkeypatch.setitem(sys.modules, "pystray", types.SimpleNamespace(MenuItem=item, Menu=Menu))
    t.menu()
    shown = {text: visible(None) if callable(visible) else visible for text, visible in items if isinstance(text, str)}
    assert shown["Set up the live stream..."] is False and shown["Open logs"] is True
    ran = []
    monkeypatch.setattr("commentator.live_stream.Obs", lambda *a, **kw: ran.append("obs"))
    monkeypatch.setattr("commentator.local_video.folder", lambda *a, **kw: ran.append("recordings"))
    monkeypatch.setattr(t, "_setup_live", lambda: ran.append("setup"))
    t.toggle_live()
    t.setup_live()
    t.open_recordings()
    assert ran == []
    cloud.marker().unlink()   # unpaired while the tray runs: it keeps its mode till restarted
    t._refresh = lambda *a: None
    t.settings_saved()
    assert t.cfg.db_path == str(t.cloud.db) and t.republish_for([("publish", "bucket")]) is False


def test_the_paired_tray_shows_how_uploads_stand(api, monkeypatch):
    t, _ = _tray(api, monkeypatch)
    t.booth.state = "running"
    assert t.issues() == [] and t._light() == "green"
    cloud_booth.set_health(t.cfg.log_dir, [("warn", "Shotcast Cloud: rounds aren't going up")])
    assert t._light() == "orange" and "1 warning" in t.status()
    cloud_booth.set_health(t.cfg.log_dir, [("fail", "Shotcast Cloud needs Shotcast 9.0 or later: update Shotcast")])
    assert t._light() == "red" and t.issues()[0][1].endswith("update Shotcast")
    cloud_booth.set_health(t.cfg.log_dir, [])
    assert t._light() == "green"


def test_the_tray_of_a_profile_that_needs_pairing_again_is_red_and_says_so(api, monkeypatch):
    prof = paired(api)
    store = cloud.store()
    store.write(store.read() | {"refused": 1.0})
    t, _ = _tray(api, monkeypatch, prof=cloud.locate())
    t.booth.state = "running"
    assert t._light() == "red" and t.issues() == [("fail", f"Shotcast Cloud: {cloud.REPAIR}")]
    assert prof.home == t.home


def test_the_unpaired_tray_has_no_cloud_issues(tmp_path, monkeypatch):
    from commentator import tray

    monkeypatch.setattr(tray, "AutoPublisher", lambda *a, **kw: None)
    cfg = base_cfg(tmp_path)
    Path(cfg.log_dir).mkdir()
    (Path(cfg.log_dir) / cloud_booth.HEALTH).write_text(json.dumps({"issues": [{"level": "fail", "message": "x"}]}))
    t = tray.Tray(tmp_path, cfg)
    assert t.cloud is None and t.cloud_issues() == [] and t.cfg is cfg


# ---- Settings -------------------------------------------------------------------------------------

def test_paired_settings_offer_and_save_only_what_a_cloud_profile_keeps():
    from commentator import tray_ui

    assert tray_ui.fields_for(False) == (tray_ui.TABS, tray_ui.CONFIG_FIELDS, tray_ui.ENV_FIELDS)
    tabs, config, env = tray_ui.fields_for(True)
    assert {"Sharing", "Video", "Chat"}.isdisjoint(tabs)
    assert all(f[2][0] in cloud.CARRIED for f in config) and {f[2] for f in env} <= set(cloud.ENV_KEYS)
    tray_ui.check_paired_changes({("llm", "live", "model"): "x"}, {"OPENAI_API_KEY": "k"})
    for config_change, env_change in (({("publish", "bucket"): "b"}, {}), ({}, {"AWS_ACCESS_KEY_ID": "a"}),
                                      ({("live", "enabled"): True}, {}), ({}, {"NTFY_TOPIC": "t"})):
        with pytest.raises(ValueError, match="paired with Shotcast Cloud"):
            tray_ui.check_paired_changes(config_change, env_change)


@pytest.mark.parametrize("key", ["AWS_ACCESS_KEY_ID", "NTFY_TOPIC", "OBS_WEBSOCKET_PASSWORD"])
def test_paired_the_self_hosted_services_checks_are_refused(key, monkeypatch):
    import httpx

    from commentator import tray_ui

    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("tried"))
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: pytest.fail("tried"), raising=False)
    assert "self-hosted" in tray_ui.check_key(key, {key: "v"}, base_cfg(Path(".")), paired=True)


# ---- starting up: what a paired Shotcast refuses, holds and leaves behind -------------------------

def test_a_damaged_pairing_starts_no_booth_and_says_how_to_repair_it(tmp_path, api, monkeypatch):
    from commentator import tray

    _self_hosted_home(tmp_path, monkeypatch)
    prof = paired(api)
    prof.db.unlink()   # (an established profile's season file gone)
    ran, told = [], []
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: ran.append("run"))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: ran.append("logging"))
    monkeypatch.setattr(tray, "Tray", lambda *a, **kw: ran.append("tray"))
    monkeypatch.setattr(tray, "fatal", told.append)
    for argv in (["run"], ["tray"]):
        with pytest.raises(SystemExit, match="unpair --force"):
            _run(monkeypatch, *argv)
    assert ran == [] and len(told) == 1 and "unpair --force" in told[0]
    assert not prof.db.exists()   # (startup never makes one)


def test_paired_startup_drops_the_credentials_it_inherited(tmp_path, api, monkeypatch):
    from commentator import tray

    _self_hosted_home(tmp_path, monkeypatch)
    prof = paired(api)
    prof.env.write_text("AZURE_SPEECH_KEY=profile-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited")
    monkeypatch.setenv("TYPESAFE_API_KEY", "inherited")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.test")
    seen = []

    def look(*a, **kw):
        seen.append({k: os.environ.get(k) for k in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "OPENAI_BASE_URL",
                                                     "AZURE_SPEECH_KEY")})
    monkeypatch.setattr(cli, "cmd_run", look)
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: None)
    _run(monkeypatch, "run")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited")
    monkeypatch.setenv("TYPESAFE_API_KEY", "inherited")
    monkeypatch.setattr(tray, "single_instance", lambda: True)
    monkeypatch.setattr(tray, "Tray", lambda *a, **kw: types.SimpleNamespace(run=look))
    _run(monkeypatch, "tray")
    want = {"OPENAI_API_KEY": None, "TYPESAFE_API_KEY": None, "OPENAI_BASE_URL": None,
            "AZURE_SPEECH_KEY": "profile-key"}
    assert seen == [want, want]


def test_unpaired_startup_keeps_the_credentials_it_inherited(tmp_path, monkeypatch):
    _self_hosted_home(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "inherited")
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: seen.append(os.environ.get("OPENAI_API_KEY")))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: None)
    _run(monkeypatch, "run")
    assert seen == ["inherited"]


def test_a_running_paired_booth_or_tray_cant_be_unpaired_from_under_it(tmp_path, api, monkeypatch):
    from commentator import tray

    _self_hosted_home(tmp_path, monkeypatch)
    paired(api)
    refused = []

    def unpair(*a, **kw):
        with pytest.raises(SystemExit, match="stop Shotcast first") as e:
            cloud.main(["unpair"])
        refused.append(str(e.value))
    monkeypatch.setattr(cli, "cmd_run", unpair)
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: None)
    _run(monkeypatch, "run")
    monkeypatch.setattr(tray, "single_instance", lambda: True)
    monkeypatch.setattr(tray, "Tray", lambda *a, **kw: types.SimpleNamespace(run=unpair))
    _run(monkeypatch, "tray")
    assert len(refused) == 2 and cloud.marker().exists()
    cloud.main(["unpair"])   # (stopped: now it can)
    assert not cloud.marker().exists()


def test_the_paired_trays_start_with_windows_keeps_the_self_hosted_launch_folder(tmp_path, api, monkeypatch):
    from commentator import tray

    home = _self_hosted_home(tmp_path, monkeypatch)
    paired(api)
    made, saved = [], []
    monkeypatch.setattr(tray, "single_instance", lambda: True)
    monkeypatch.setattr(tray, "starts_with_windows", lambda: False)
    monkeypatch.setattr(tray, "set_starts_with_windows", lambda on, home=None: saved.append((on, Path(home))))
    monkeypatch.setattr(tray, "AutoPublisher", lambda *a, **kw: None)
    real = tray.Tray

    def build(*a, **kw):
        t = real(*a, **kw)
        made.append(t)
        return types.SimpleNamespace(run=lambda: None)
    monkeypatch.setattr(tray, "Tray", build)
    _run(monkeypatch, "tray", "--home", str(home))
    [t] = made
    t._refresh = lambda: None
    t.toggle_start_with_windows()
    assert saved == [(True, home.resolve())]


def test_paired_the_release_self_test_runs_only_against_a_folder_it_is_given(tmp_path, api, monkeypatch):
    from commentator import tray

    paired(api)
    ran = []
    monkeypatch.setattr(tray, "self_test", lambda home, cfg: ran.append((Path(home), cfg.db_path)))
    with pytest.raises(SystemExit, match="--home"):
        _run(monkeypatch, "tray", "--self-test")
    assert ran == []
    scratch = tmp_path / "scratch"
    _run(monkeypatch, "tray", "--self-test", "--home", str(scratch))
    [(home, db)] = ran
    assert home.resolve() == scratch.resolve() and not Path(db).resolve().is_relative_to(cloud.root().resolve())


# ---- review round 5 ----------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["run"], ["tray"]])
def test_an_unpair_that_finishes_as_shotcast_starts_starts_nothing(tmp_path, api, monkeypatch, argv):
    from commentator import tray

    _self_hosted_home(tmp_path, monkeypatch)
    prof = paired(api)
    ran, told = [], []
    real = cloud.locate

    def locate():
        got = real()
        cloud.store().delete()   # (an unpair finished between this look and the lifecycle lock)
        return got
    monkeypatch.setattr(cloud, "locate", locate)
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: ran.append("run"))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: ran.append("logging"))
    monkeypatch.setattr(tray, "single_instance", lambda: True)
    monkeypatch.setattr(tray, "Tray", lambda *a, **kw: ran.append("tray"))
    monkeypatch.setattr(tray, "fatal", told.append)
    with pytest.raises(SystemExit, match="unpaired"):
        _run(monkeypatch, *argv)
    assert ran == [] and len(told) == (argv == ["tray"])
    assert not list(prof.logs.glob("*.log")) and Path.cwd().resolve() != prof.home.resolve()


def _booth_env(monkeypatch, tmp_path, expect):
    ready = tmp_path / "ready" / "booth-ready"
    ready.parent.mkdir(exist_ok=True)
    monkeypatch.setenv("SHOTCAST_READY_FILE", str(ready))
    monkeypatch.setenv(cloud.EXPECT_ENV, expect)
    return ready


def test_a_booth_the_paired_tray_restarts_after_an_unpair_never_runs_self_hosted_in_the_profile(tmp_path, api,
                                                                                               monkeypatch):
    from commentator.config import BOOTH_STOP_CODE, BOOTH_STOPPED

    _self_hosted_home(tmp_path, monkeypatch)
    t, _ = _tray(api, monkeypatch)
    prof = t.cloud
    started = []
    t.booth._popen = lambda *a, **kw: started.append(kw) or types.SimpleNamespace(poll=lambda: None)
    t.booth._spawn()
    assert started[0]["env"][cloud.EXPECT_ENV] == cloud.expectation(prof) == json.dumps([API, "t1", "d1"])
    ran = []
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: ran.append(Path.cwd()))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: None)
    ready = _booth_env(monkeypatch, tmp_path, started[0]["env"][cloud.EXPECT_ENV])
    monkeypatch.chdir(prof.home)            # (as the tray starts it: in the profile)
    _run(monkeypatch, "run")
    assert len(ran) == 1                    # still paired as the tray was: it runs
    cloud.main(["unpair", "--force"])
    monkeypatch.chdir(prof.home)
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "run")            # the tray's "Restart the booth", or its restart after a crash
    assert e.value.code == BOOTH_STOP_CODE and len(ran) == 1
    assert "start it again" in ready.with_name(BOOTH_STOPPED).read_text()


def test_a_booth_the_self_hosted_tray_starts_after_a_pairing_waits_for_shotcast_to_restart(tmp_path, api,
                                                                                          monkeypatch):
    from commentator import tray
    from commentator.config import BOOTH_STOP_CODE, load_config

    home = _self_hosted_home(tmp_path, monkeypatch)
    monkeypatch.setattr(tray, "AutoPublisher", lambda *a, **kw: None)
    t = tray.Tray(home, load_config(home / "config.yaml"))
    assert t.booth._expect == {cloud.EXPECT_ENV: "self-hosted"} == {cloud.EXPECT_ENV: cloud.expectation(None)}
    ran = []
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: ran.append(cfg.db_path))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: None)
    _booth_env(monkeypatch, tmp_path, "self-hosted")
    _run(monkeypatch, "run")
    assert ran == ["logs/season.db"]        # unpaired, as its tray: exactly as before
    paired(api)
    monkeypatch.chdir(home)
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "run")
    assert e.value.code == BOOTH_STOP_CODE and len(ran) == 1
    monkeypatch.delenv(cloud.EXPECT_ENV)    # (started by hand, or by a restarted Shotcast: paired)
    _run(monkeypatch, "run")
    assert len(ran) == 2 and Path(ran[1]).is_relative_to(cloud.root())


def test_an_update_from_the_paired_tray_restarts_shotcast_in_the_self_hosted_folder(tmp_path, api, monkeypatch):
    from commentator import backup, tray, updater

    t, _ = _tray(api, monkeypatch)
    calls = []
    monkeypatch.setattr(backup, "pre_update", lambda cfg, version: None)
    monkeypatch.setattr(updater, "install", lambda installer, version, home=None: calls.append(home))
    monkeypatch.setattr(tray, "default_home", lambda: tmp_path / "default")
    t.launch_home = tmp_path / "custom"
    t._install("9.9.9", t.home / "updates" / "Shotcast-Setup-9.9.9.exe")
    t.launch_home = tmp_path / "default"
    t._install("9.9.9", t.home / "updates" / "Shotcast-Setup-9.9.9.exe")
    assert calls == [(tmp_path / "custom").resolve(), None]   # (never the cloud profile)


@pytest.mark.parametrize("default, opens", [("lg1", [f"{API}/leagues/lg1/"]), (None, [])])
def test_a_second_launch_while_paired_opens_the_pairings_own_stats_page_or_nothing(tmp_path, api, monkeypatch,
                                                                                  default, opens):
    from commentator import tray

    home = _self_hosted_home(tmp_path, monkeypatch)
    (home / "logs").mkdir()
    (home / "logs" / "stats-url.txt").write_text("http://self-hosted.test/", encoding="utf-8")
    api.default = default
    prof = paired(api)
    (prof.logs / "stats-url.txt").write_text("http://stale.test/", encoding="utf-8")
    opened, ran = [], []
    monkeypatch.setattr(tray.webbrowser, "open", opened.append)
    monkeypatch.setattr(tray, "single_instance", lambda: False)
    monkeypatch.setattr(tray, "Tray", lambda *a, **kw: ran.append("tray"))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: ran.append("logging"))
    _run(monkeypatch, "tray")
    assert opened == opens and ran == []


def test_paired_commentary_uses_only_the_profiles_anthropic_key(api, monkeypatch):
    import importlib

    from commentator.config import LLMConfig

    made = []
    fake = types.ModuleType("anthropic")
    fake.BadRequestError = type("BadRequestError", (Exception,), {})
    fake.AsyncAnthropic = lambda **kw: made.append(kw) or object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.delitem(sys.modules, "commentator.llm.anthropic_client", raising=False)
    client = importlib.import_module("commentator.llm.anthropic_client")
    try:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client.AnthropicClient(LLMConfig())
        assert "api_key" not in made[-1]    # unpaired: the SDK finds the key itself, as before
        monkeypatch.setattr(cloud, "startup", paired(api))
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
            client.AnthropicClient(LLMConfig())   # (never the SDK's own search: a login on disk)
        assert len(made) == 1
        monkeypatch.setenv("ANTHROPIC_API_KEY", "profile-key")
        client.AnthropicClient(LLMConfig())
        assert made[-1]["api_key"] == "profile-key"
    finally:
        sys.modules.pop("commentator.llm.anthropic_client", None)


def test_a_self_hosted_env_put_in_the_profile_gives_the_paired_booth_only_its_ai_keys(tmp_path, api, monkeypatch):
    _self_hosted_home(tmp_path, monkeypatch)
    prof = paired(api)
    prof.env.write_text("OPENAI_API_KEY=profile-key\nAWS_ACCESS_KEY_ID=AKIA\nSQUABBIT_PASSWORD=p\n"
                        "OBS_WEBSOCKET_PASSWORD=o\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda cfg, args: seen.append(
        {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "SQUABBIT_PASSWORD",
                                        "OBS_WEBSOCKET_PASSWORD")}))
    monkeypatch.setattr(cli, "_setup_logging", lambda *a, **kw: None)
    _run(monkeypatch, "run")
    assert seen == [{"OPENAI_API_KEY": "profile-key", "AWS_ACCESS_KEY_ID": None, "SQUABBIT_PASSWORD": None,
                     "OBS_WEBSOCKET_PASSWORD": None}]


# ---- review round 8 ----------------------------------------------------------------------------

def test_restarting_the_booth_doesnt_give_a_stuck_queue_more_time_before_the_tray_goes_red(tmp_path, api, monkeypatch):
    import httpx

    monkeypatch.setattr(cloud_rounds, "CLOSER_SECONDS", 0.0)
    cfg = paired_cfg(tmp_path, api)
    now = _clock(monkeypatch)
    _round(Store(cfg.db_path), 1)
    now[0] += cloud_booth.BEHIND_S + 3600   # (the round has been waiting since before this booth started)
    api.answers = [httpx.Response(502)]
    up = cloud_booth.Uploads(cfg, cloud.locate())
    _loop(cfg, 1, up)
    [(level, msg)] = _health(cfg)
    assert level == "fail" and "haven't gone up" in msg
    fresh = cloud_booth.Uploads(paired_cfg_again := cfg, cloud.locate())   # (and nothing waiting: from now)
    del paired_cfg_again
    now[0] += cloud_rounds.RETRY_MAX_S + 1
    _loop(cfg, 1, fresh)
    assert _health(cfg) == []
    assert cloud_booth.Uploads(cfg, cloud.locate()).clear_at == now[0]
