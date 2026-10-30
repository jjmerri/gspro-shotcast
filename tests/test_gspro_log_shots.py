import asyncio

from commentator.config import Config
from commentator.sources import build_source
from commentator.sources.gspro_log_shots import GsproLogShotSource, parse_launch, shot_from_launch

DRIVE = "LaunchExt Vars: sp 138.37, el 15.21, az -4.52, ts 1588.58, sa -15.74, cy 237.54, id 1467"
PUTT = "LaunchExt Vars: sp 13.12, el 0, az 4.41, ts 0, sa 45, cy 0, id 1475"


def test_full_shot_ball_data_from_the_log():
    shot = shot_from_launch(parse_launch(DRIVE))
    assert (shot.ball_speed, shot.vla, shot.hla, shot.total_spin, shot.spin_axis, shot.carry) == (
        138.37, 15.21, -4.52, 1588.58, -15.74, 237.54)
    assert shot.shot_number == 1467 and shot.units == "Yards"
    assert shot.club_speed is None and not shot.club_measured and not shot.is_putt


def test_putt_from_the_log():
    shot = shot_from_launch(parse_launch(PUTT))
    assert shot.ball_speed == 13.12 and shot.carry == 0.0 and shot.is_putt


def test_other_lines_are_not_shots():
    assert parse_launch(" * ------- Shot Fired! ------- *") is None
    assert parse_launch("LaunchExt - Regular Mode") is None
    assert parse_launch("LaunchExt Vars: el 12") is None


def test_source_yields_new_shots_only(tmp_path):
    log = tmp_path / "output_log.txt"
    log.write_text("old stuff\n" + DRIVE + "\n", encoding="utf-8")
    src = GsproLogShotSource(str(log), poll_seconds=0.01)

    async def first():
        it = src.events().__aiter__()
        task = asyncio.ensure_future(it.__anext__())
        await asyncio.sleep(0.05)   # primed at the end: the old drive is history
        with log.open("a", encoding="utf-8") as f:
            f.write(" * ------- Shot Fired! ------- *\n" + PUTT + "\nLaunchExt - Putting Mode\n")
        return await asyncio.wait_for(task, 2)

    ev = asyncio.run(first())
    assert ev.shot.shot_number == 1475 and ev.shot.is_putt


def test_connector_log_falls_back_to_the_gspro_log(tmp_path):
    cfg = Config()
    cfg.source.connector_log = str(tmp_path / "GSPC" / "ConnectDebug.txt")
    cfg.source.gspro_log = str(tmp_path / "output_log.txt")
    assert type(build_source(cfg)).__name__ == "GsproLogShotSource"
    (tmp_path / "GSPC").mkdir()
    (tmp_path / "GSPC" / "ConnectDebug.txt").write_text("")
    assert type(build_source(cfg)).__name__ == "ConnectorWithLogFallback"
    cfg.source.type = "gspro_log"
    assert type(build_source(cfg)).__name__ == "GsproLogShotSource"


class _Feed:
    """A shot feed that yields the given (delay, event) pairs, then stays open like a live tail."""

    def __init__(self, items, heard_at=None):
        self.items, self.heard_at = items, heard_at

    async def events(self):
        for delay, ev in self.items:
            await asyncio.sleep(delay)
            yield ev
        await asyncio.sleep(3600)


def _take(src, n):
    async def run():
        out = []
        async for ev in src.events():
            out.append(ev)
            if len(out) == n:
                return out
    return asyncio.run(asyncio.wait_for(run(), 2))


def test_a_quiet_gspconnect_log_hands_shots_to_gsprolog():
    import time

    from commentator.sources.fallback import ConnectorWithLogFallback
    drive = shot_from_launch(parse_launch(DRIVE))
    from commentator.models import ShotEvent
    # A leftover ConnectDebug.txt nobody writes: GSPro's log is the feed.
    src = ConnectorWithLogFallback(_Feed([]), _Feed([(0, ShotEvent(shot=drive))]))
    assert _take(src, 1)[0].shot is drive and src.using_log
    # GSPconnect heartbeating: its shot is the one kept, the log's copy of it dropped.
    conn = _Feed([(0.05, ShotEvent(shot=drive))], heard_at=time.monotonic())
    log_copy = shot_from_launch(parse_launch(PUTT))
    src = ConnectorWithLogFallback(conn, _Feed([(0, ShotEvent(shot=log_copy))]))
    assert [e.shot for e in _take(src, 1)] == [drive] and not src.using_log
    # Quiet for longer than the window: the log's shot is taken.
    src = ConnectorWithLogFallback(_Feed([], heard_at=time.monotonic() - 60), _Feed([(0, ShotEvent(shot=log_copy))]))
    assert _take(src, 1)[0].shot is log_copy


def test_a_shot_gspconnect_skips_while_heartbeating_comes_from_gspros_log():
    import dataclasses
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback
    drive = shot_from_launch(parse_launch(DRIVE))
    wedge = dataclasses.replace(drive, ball_speed=98.55)
    putt = shot_from_launch(parse_launch(PUTT))
    conn_drive = dataclasses.replace(drive, ball_speed=drive.ball_speed + 0.004)   # GSPconnect's unrounded copy
    # GSPconnect sends the drive (after the log's copy) and the putt (before it), but never the wedge.
    conn = _Feed([(0.05, ShotEvent(shot=conn_drive)), (0.3, ShotEvent(shot=putt))], heard_at=time.monotonic())
    gl = _Feed([(0, ShotEvent(shot=drive)), (0.1, ShotEvent(shot=wedge)), (0.3, ShotEvent(shot=putt))])
    src = ConnectorWithLogFallback(conn, gl, missed_after=0.15)
    got = [e.shot for e in _take(src, 3)]
    assert got == [conn_drive, wedge, putt] and not src.using_log


def _take_for(src, seconds):
    out = []

    async def run():
        async for ev in src.events():
            out.append(ev)
    try:
        asyncio.run(asyncio.wait_for(run(), seconds))
    except asyncio.TimeoutError:
        pass
    return out


def test_a_late_gspconnect_copy_of_a_filled_in_shot_is_not_called_twice():
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback
    drive = shot_from_launch(parse_launch(DRIVE))
    conn = _Feed([(0.3, ShotEvent(shot=drive))], heard_at=time.monotonic())
    src = ConnectorWithLogFallback(conn, _Feed([(0, ShotEvent(shot=drive))]), missed_after=0.15)
    got = _take_for(src, 0.6)
    assert len(got) == 1 and got[0].shot == drive


def test_feed_copies_pair_one_to_one():
    import dataclasses
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback
    putt = shot_from_launch(parse_launch(PUTT))
    putt2 = dataclasses.replace(putt, ball_speed=putt.ball_speed + 0.03)   # a second, near-identical putt
    # GSPconnect sends the first putt; its log copy follows, then a second putt GSPconnect skips.
    conn = _Feed([(0, ShotEvent(shot=putt))], heard_at=time.monotonic())
    gl = _Feed([(0.05, ShotEvent(shot=putt)), (0.05, ShotEvent(shot=putt2))])
    captured = []
    src = ConnectorWithLogFallback(conn, gl, missed_after=0.15)
    src.on_impact = lambda ev: captured.append(ev.shot)
    got = [e.shot for e in _take_for(src, 0.6)]
    assert got == [putt, putt2]
    assert captured == [putt2]   # the held shot's impact state was taken when it was held


def test_the_booth_snapshots_a_held_shot_at_impact(tmp_path):
    from commentator.config import Config
    from commentator.engine import Engine
    from commentator.league import League
    from commentator.llm import LLMClient
    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback
    cfg = Config(log_dir=str(tmp_path))
    class _LLM(LLMClient):
        async def complete_json(self, *a, **k):
            raise AssertionError

    llm = _LLM(cfg.llm.live)
    src = ConnectorWithLogFallback(_Feed([]), _Feed([]))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=src,
                    live_llm=llm, segment_llm=llm, speech=None)
    ev = ShotEvent(shot=shot_from_launch(parse_launch(PUTT)))
    src.on_impact(ev)
    engine.gspro.hole_outs_by[0] = engine.gspro.hole_outs_by.get(0, 0) + 1   # it drops during the hold
    (snap, *_), hole, _par, _round, _seq = ev.impact_state
    assert ev.impact_at is not None and snap[2].get(0, 0) == engine.gspro.hole_outs_by[0] - 1
    assert hole == engine.state.current_hole


def test_a_late_copy_pairs_with_the_shot_already_aired_not_a_newer_held_one():
    import dataclasses
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback
    a = shot_from_launch(parse_launch(PUTT))
    b = dataclasses.replace(a, ball_speed=a.ball_speed + 0.03)
    # GSPconnect skips nothing but sends A very late, while the log's B is held.
    conn = _Feed([(0.35, ShotEvent(shot=a))], heard_at=time.monotonic())
    gl = _Feed([(0, ShotEvent(shot=a)), (0.3, ShotEvent(shot=b))])
    src = ConnectorWithLogFallback(conn, gl, missed_after=0.15)
    assert [e.shot for e in _take_for(src, 0.8)] == [a, b]


def test_a_connector_copy_after_the_capture_keeps_the_impact_state():
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback
    from commentator.sources import fallback
    putt = shot_from_launch(parse_launch(PUTT))
    conn = _Feed([(0.25, ShotEvent(shot=putt, logged_at=1.0))], heard_at=time.monotonic())
    old = fallback.CAPTURE_AFTER
    fallback.CAPTURE_AFTER = 0.1
    try:
        src = ConnectorWithLogFallback(conn, _Feed([(0, ShotEvent(shot=putt))]), missed_after=0.5)

        def capture(ev):
            ev.impact_at, ev.impact_state, ev.impact_read = 123.0, "state", "read"
        src.on_impact = capture
        got = _take_for(src, 0.8)
    finally:
        fallback.CAPTURE_AFTER = old
    assert len(got) == 1 and got[0].logged_at == 1.0   # GSPconnect's copy
    assert (got[0].impact_at, got[0].impact_state, got[0].impact_read) == (123.0, "state", "read")


# ---- GSPro 3.59: the shot comes from the files GSPro saves once the ball stops ----------------

FIRE = " * ------- Shot Fired! ------- *\n"


def _entry(sid, sp, *, player="Josh", hole=0, shot=1, result=0, hole_result=3, el=12.6, cy=200.0):
    return {"ShotID": sid, "PlayerName": player, "Hole": hole, "HoleShot": shot, "ShotResult": result,
            "HoleResult": hole_result,
            "GhostData": {"sp": sp, "el": el, "az": -6.2, "ts": 4604.9, "sa": 17.5, "cy": cy, "id": 37}}


def test_only_strokes_count_from_the_round_file():
    from commentator.sources.gspro_files import swings
    entries = [
        _entry("drive", 135.5),
        _entry("mulliganed", 137.5, result=1),         # replaced by a mulligan
        _entry("water", 124.8, shot=2),
        _entry("penalty", 124.8, shot=3, result=2),     # the water penalty: a copy, not a swing
        _entry("putt", 17.7, shot=4),
        _entry("gimme", 17.7, shot=5, hole_result=7),   # GSPro's entry when the ball drops: a copy
        _entry("chip-in", 52.3, shot=1, hole=1, hole_result=7),   # holed outright: a real stroke
        _entry("chip-in 2", 52.3, shot=1, hole=2, hole_result=7, el=40.0),   # same speed, another stroke
    ]
    assert [sid for sid, _ in swings(entries)] == ["drive", "water", "putt", "chip-in", "chip-in 2"]


class _Beta:
    """A GSPro 3.59 log folder: Player.log, currentRound.dat and GSPro.db's LastShot."""

    def __init__(self, tmp_path):
        import sqlite3
        self.dir = tmp_path
        self.log = tmp_path / "Player.log"
        self.log.write_text("Initialize engine version\n", encoding="utf-8")
        self.entries = [_entry("old", 99.0)]   # an earlier shot: history
        self.write_round()
        con = sqlite3.connect(tmp_path / "GSPro.db")
        con.execute("CREATE TABLE LastShot (ID integer primary key autoincrement, DateCreated datetime, ShotData varchar)")
        con.commit()
        con.close()

    def write_round(self):
        import json
        (self.dir / "currentRound.dat").write_text(json.dumps(self.entries), encoding="utf-8")

    def last_shot(self, **data):
        import json
        import sqlite3
        con = sqlite3.connect(self.dir / "GSPro.db")
        con.execute("INSERT INTO LastShot (DateCreated, ShotData) VALUES ('now', ?)", (json.dumps(data),))
        con.commit()
        con.close()

    def fire(self, mode="Regular"):
        with self.log.open("a", encoding="utf-8") as f:
            f.write(FIRE + f"LaunchExt - {mode} Mode\n")


def _run_beta(src, steps, seconds):
    """Run the source while `steps` [(delay, fn)] change GSPro's files; the events it yields."""
    out = []

    async def feed():
        async for ev in src.events():
            out.append(ev)

    async def run():
        task = asyncio.create_task(feed())
        for delay, fn in steps:
            await asyncio.sleep(delay)
            fn()
        await asyncio.sleep(seconds)
        task.cancel()
    asyncio.run(run())
    return out


def test_a_beta_shot_comes_from_the_round_file_with_its_impact_snapshot(tmp_path, monkeypatch):
    from commentator.sources import gspro_log_shots
    monkeypatch.setattr(gspro_log_shots, "CAPTURE_AFTER", 0.05)
    beta = _Beta(tmp_path)
    src = GsproLogShotSource(str(tmp_path / "output_log.txt"), poll_seconds=0.01)
    assert src.path == str(beta.log)
    snaps = []

    def capture(ev):
        ev.impact_at, ev.impact_state = 7.0, f"in the air {len(snaps)}"
        snaps.append(ev)
    src.on_impact = capture

    def land_drive():
        beta.entries.append(_entry("drive", 135.51))
        beta.write_round()

    def putt_drops():   # the putt and GSPro's copy of it for the ball dropping, in one write
        beta.entries += [_entry("putt", 6.0, shot=2), _entry("holed", 6.0, shot=3, hole_result=7)]
        beta.write_round()
    got = _run_beta(src, [(0.1, beta.fire), (0.2, land_drive), (0.05, lambda: beta.fire("Putting")),
                          (0.2, putt_drops)], 0.2)
    assert [round(e.shot.ball_speed, 2) for e in got] == [135.51, 6.0]
    drive, putt = got
    assert (drive.shot.vla, drive.shot.carry, drive.shot.putter) == (12.6, 200.0, False)
    assert putt.shot.putter and putt.shot.is_putt
    assert [(e.impact_at, e.impact_state) for e in got] == [(7.0, "in the air 0"), (7.0, "in the air 1")]


def test_a_practice_shot_comes_from_lastshot_but_an_older_row_never_does(tmp_path, monkeypatch):
    from commentator.sources import gspro_files, gspro_log_shots
    monkeypatch.setattr(gspro_log_shots, "LAST_SHOT_GRACE", 0.1)
    monkeypatch.setattr(gspro_files, "DB_SETTLE", 0.02)
    beta = _Beta(tmp_path)
    src = GsproLogShotSource(str(beta.log), poll_seconds=0.01)
    wedge = dict(BallSpeed=52.3, VLA=38.8, HLA=-2.8, BackSpin=8245.0, SideSpin=-500.0, rawCarryLM=48.2, Carry=50.0)

    def drive_lands():   # a round shot: its round file entry and its LastShot row
        beta.entries.append(_entry("drive", 135.51))
        beta.write_round()
        beta.last_shot(BallSpeed=135.51, VLA=12.6, HLA=-6.2, BackSpin=4392.0, SideSpin=1384.0, rawCarryLM=200.0)
    got = _run_beta(src, [(0.1, beta.fire), (0.1, drive_lands), (0.3, beta.fire),
                          (0.3, lambda: beta.last_shot(**wedge))], 0.4)
    assert [e.shot.ball_speed for e in got] == [135.51, 52.3]   # the drive's row isn't the wedge's
    assert got[1].shot.carry == 48.2 and round(got[1].shot.total_spin) == 8260


def test_a_round_file_entry_goes_to_the_fire_before_it_not_a_later_one(tmp_path):
    import time
    from commentator.sources.gspro_log_shots import _Fire
    beta = _Beta(tmp_path)
    src = GsproLogShotSource(str(beta.log), poll_seconds=0.01)
    src._files_events(time.monotonic())   # primes
    now = time.monotonic()
    # The drive's fire, then (read in the same poll as the drive's entry) the putt's.
    src._waiting = [_Fire(at=now, putter=False, wall=0.0), _Fire(at=now, putter=True, wall=time.time() + 60)]
    beta.entries.append(_entry("drive", 135.51))
    beta.write_round()
    [drive] = src._files_events(now)
    assert (drive.shot.ball_speed, drive.shot.putter) == (135.51, False)
    beta.entries.append(_entry("putt", 6.0, shot=2))
    beta.write_round()
    import os
    os.utime(tmp_path / "currentRound.dat", (time.time() + 120, time.time() + 120))
    [putt] = src._files_events(now)
    assert (putt.shot.ball_speed, putt.shot.putter) == (6.0, True)


def test_a_round_shots_late_lastshot_row_doesnt_take_the_next_fire(tmp_path, monkeypatch):
    from commentator.sources import gspro_files, gspro_log_shots
    monkeypatch.setattr(gspro_log_shots, "LAST_SHOT_GRACE", 0.1)
    monkeypatch.setattr(gspro_files, "DB_SETTLE", 0.02)
    beta = _Beta(tmp_path)
    src = GsproLogShotSource(str(beta.log), poll_seconds=0.01)

    def drive_lands():
        beta.entries.append(_entry("drive", 135.51))
        beta.write_round()

    def putt_lands():
        beta.entries.append(_entry("putt", 6.0, shot=2))
        beta.write_round()
    # The drive's row is read only after the putt's fire (GSPro held the DB lock).
    got = _run_beta(src, [(0.1, beta.fire), (0.1, drive_lands), (0.05, lambda: beta.fire("Putting")),
                          (0.05, lambda: beta.last_shot(BallSpeed=135.51, VLA=12.6, rawCarryLM=200.0)),
                          (0.3, putt_lands)], 0.2)
    assert [(e.shot.ball_speed, e.shot.putter) for e in got] == [(135.51, False), (6.0, True)]


def test_a_lastshot_read_that_fails_is_tried_again(tmp_path, monkeypatch):
    import time
    from commentator.sources import gspro_files
    monkeypatch.setattr(gspro_files, "DB_SETTLE", 0.0)
    beta = _Beta(tmp_path)
    files = gspro_files.GsproFiles(str(tmp_path))
    files.prime()
    beta.last_shot(BallSpeed=52.3, VLA=38.8, HLA=-2.8, BackSpin=8245.0, SideSpin=-500.0, rawCarryLM=48.2)
    os_mtime = time.time() + 5
    import os
    os.utime(tmp_path / "GSPro.db", (os_mtime, os_mtime))
    real, calls = files._query_last_shot, []

    def locked_once():
        calls.append(1)
        return (False, None) if len(calls) == 1 else real()
    files._query_last_shot = locked_once
    rows = []
    for _ in range(6):
        if files.db_changed(time.monotonic()):
            rows.append(files.new_last_shot())
    assert len(calls) == 2 and rows[-1].data["BallSpeed"] == 52.3


def test_a_31_log_shot_is_not_taken_again_from_the_round_file(tmp_path):
    log = tmp_path / "output_log.txt"
    log.write_text("start\n", encoding="utf-8")
    (tmp_path / "currentRound.dat").write_text("[]", encoding="utf-8")
    src = GsproLogShotSource(str(log), poll_seconds=0.01)

    def fire():
        with log.open("a", encoding="utf-8") as f:
            f.write(FIRE + DRIVE + "\nLaunchExt - Regular Mode\n")

    def saved():
        import json
        (tmp_path / "currentRound.dat").write_text(json.dumps([_entry("drive", 138.37)]), encoding="utf-8")
    got = _run_beta(src, [(0.1, fire), (0.1, saved)], 0.2)
    assert [e.shot.shot_number for e in got] == [1467]


def test_gspconnects_copy_wins_and_the_saved_copy_takes_no_impact_snapshot():
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback

    class SavedFeed(_Feed):
        on_impact = None

        async def events(self):
            await asyncio.sleep(0.05)
            fire = ShotEvent(shot=shot_from_launch(parse_launch(DRIVE)))
            self.on_impact(fire)            # half a second after the fire, the ball in the air
            await asyncio.sleep(0.05)
            yield fire                       # once the ball stops
            await asyncio.sleep(3600)
    drive = shot_from_launch(parse_launch(DRIVE))
    conn = _Feed([(0, ShotEvent(shot=drive, logged_at=1.0))], heard_at=time.monotonic())
    snaps = []
    src = ConnectorWithLogFallback(conn, SavedFeed([]), missed_after=0.15)
    src.on_impact = snaps.append
    got = _take_for(src, 0.5)
    assert [e.logged_at for e in got] == [1.0] and snaps == []


def test_a_saved_shot_gspconnect_skipped_airs_with_its_impact_snapshot():
    import time

    from commentator.models import ShotEvent
    from commentator.sources.fallback import ConnectorWithLogFallback

    class SavedFeed(_Feed):
        on_impact = None

        async def events(self):
            fire = ShotEvent(shot=shot_from_launch(parse_launch(DRIVE)))
            self.on_impact(fire)
            await asyncio.sleep(0.3)         # the ball rolls out: past GSPconnect's two seconds
            yield fire
            await asyncio.sleep(3600)
    src = ConnectorWithLogFallback(_Feed([], heard_at=time.monotonic()), SavedFeed([]), missed_after=0.15)

    def capture(ev):
        ev.impact_at = time.monotonic()
        ev.impact_state = "in the air"
    src.on_impact = capture
    start = time.monotonic()
    got = _take(src, 1)
    assert got[0].impact_state == "in the air" and time.monotonic() - start < 0.45   # aired as it came
