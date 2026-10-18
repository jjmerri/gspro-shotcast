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
