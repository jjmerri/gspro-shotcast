import datetime
import json

from commentator.sources.connector_log import ConnectorLogSource


def _line(n: int, ball: bool = False, speed: float = 150.0, vla: float = 12, carry: float = 260) -> str:
    msg = {"DeviceID": "x", "ShotNumber": n,
           "ShotDataOptions": {"ContainsBallData": ball, "IsHeartBeat": not ball}}
    if ball:
        msg["BallData"] = {"Speed": speed, "VLA": vla, "HLA": 1, "CarryDistance": carry}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]  # "now", so it isn't stale
    return f"{stamp} {json.dumps(msg)}"


def _poll(src: ConnectorLogSource) -> list[dict]:
    lines = src._read_new_lines()
    return src.new_messages("\n".join(lines)) if lines else []


def _follow(src: ConnectorLogSource, expected: int) -> list:
    """The feed's events: until `expected` are out (however slow the machine), then ten more polls
    for any that shouldn't be."""
    import asyncio

    async def run():
        out = []

        async def pump():
            async for ev in src.events():
                out.append(ev)

        task = asyncio.create_task(pump())
        for _ in range(500):
            if len(out) >= expected:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        task.cancel()
        return out

    return asyncio.run(run())


def test_connector_tails_incrementally_through_rollover(tmp_path):
    p = tmp_path / "ConnectDebug.txt"
    p.write_text("\n".join(_line(n) for n in range(100, 110)) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p))

    assert _poll(src) == []  # the history present at startup is skipped, not replayed

    with p.open("a", encoding="utf-8") as f:
        f.write(_line(110) + "\n" + _line(111, ball=True) + "\n")
    got = _poll(src)
    assert [(m["ShotNumber"], bool(m.get("BallData"))) for m in got] == [(110, False), (111, True)]

    # The log rolls over at ~10 MB: the file shrinks and keeps numbering. Shots must survive it.
    p.write_text(_line(112) + "\n" + _line(113, ball=True) + "\n", encoding="utf-8")
    got = _poll(src)
    assert [(m["ShotNumber"], bool(m.get("BallData"))) for m in got] == [(112, False), (113, True)]


def test_connector_waits_for_a_whole_line(tmp_path):
    p = tmp_path / "ConnectDebug.txt"
    p.write_text(_line(100) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p))
    _poll(src)
    with p.open("a", encoding="utf-8") as f:  # a line caught mid-write
        f.write('2026-10-05 14:00:00,000 {"DeviceID":"x","Shot')
    assert _poll(src) == []
    with p.open("a", encoding="utf-8") as f:
        f.write('Number":101,"ShotDataOptions":{"ContainsBallData":true,"IsHeartBeat":false},"BallData":{"Speed":150}}\n')
    got = _poll(src)
    assert [m["ShotNumber"] for m in got] == [101] and got[0].get("BallData")


def test_connector_emits_a_shot_once_per_number(tmp_path):
    # GSPconnect logs one shot in more than one message with the same ShotNumber.
    p = tmp_path / "ConnectDebug.txt"
    p.write_text(_line(50) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p), poll_seconds=0.02)
    _poll(src)  # prime: skip the history, remember we're at ShotNumber 50
    with p.open("a", encoding="utf-8") as f:
        f.write(_line(51, ball=True) + "\n" + _line(51, ball=True) + "\n")

    assert len(_follow(src, 1)) == 1  # the duplicate second message is not re-emitted


def test_connector_reads_shots_from_any_rollover_file(tmp_path):
    # GSPconnect round-robins the active log through ConnectDebug.txt and .txt.1..N; a shot can land
    # in any of them, so the feed must follow them all.
    p = tmp_path / "ConnectDebug.txt"
    p1 = tmp_path / "ConnectDebug.txt.1"
    p.write_text(_line(100) + "\n", encoding="utf-8")
    p1.write_text(_line(101) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p), poll_seconds=0.02)
    _poll(src)  # prime both files
    with p1.open("a", encoding="utf-8") as f:  # the next shot is written to the .1 file, not .txt
        f.write(_line(102, ball=True) + "\n")

    events = _follow(src, 1)
    assert len(events) == 1 and events[0].shot.shot_number == 102


def test_connector_emits_a_shot_behind_the_heartbeat_high_water_mark(tmp_path):
    # GSPconnect writes frequent heartbeats to the active file and can log a real shot to a lagging
    # rollover file with a ShotNumber the heartbeats have already pushed past. The shot must still
    # fire: deduping on a global "> last_number" mark dropped exactly these and lost live commentary.
    p = tmp_path / "ConnectDebug.txt"
    p1 = tmp_path / "ConnectDebug.txt.1"
    p.write_text(_line(100) + "\n", encoding="utf-8")
    p1.write_text(_line(101) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p), poll_seconds=0.02)
    _poll(src)  # prime both files

    with p.open("a", encoding="utf-8") as f:  # a burst of heartbeats races the mark forward
        f.write("\n".join(_line(n) for n in range(102, 200)) + "\n")
    _poll(src)
    assert src.last_number >= 199

    with p1.open("a", encoding="utf-8") as f:  # the shot lands in the lagging file, number behind
        f.write(_line(150, ball=True) + "\n")

    events = _follow(src, 1)
    assert [e.shot.shot_number for e in events] == [150]


def test_connector_feed_runs_past_the_alive_log(tmp_path):
    # The periodic "feed alive" log fires every 100 reads; make sure the loop survives it.
    import asyncio

    p = tmp_path / "ConnectDebug.txt"
    p.write_text(_line(100) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p), poll_seconds=0)
    errors = []

    async def run():
        async def pump():
            try:
                async for _ in src.events():
                    pass
            except Exception as e:  # a stray attribute in the loop would surface here
                errors.append(e)

        task = asyncio.create_task(pump())
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(run())
    assert not errors


def test_connector_keeps_putts_and_drops_speed_glitches(tmp_path):
    p = tmp_path / "ConnectDebug.txt"
    p.write_text(_line(10) + "\n", encoding="utf-8")
    src = ConnectorLogSource(path=str(p), poll_seconds=0.02)
    _poll(src)
    with p.open("a", encoding="utf-8") as f:
        f.write(_line(11, ball=True, speed=5, vla=99999, carry=109360) + "\n")  # a putt: valid speed, sentinels
        f.write(_line(12, ball=True, speed=999) + "\n")  # a genuine speed glitch
        f.write(_line(13, ball=True) + "\n")  # a normal shot

    events = _follow(src, 2)
    nums = [e.shot.shot_number for e in events]
    assert 11 in nums and 13 in nums and 12 not in nums  # putt kept, speed glitch dropped
    putt = next(e for e in events if e.shot.shot_number == 11)
    assert putt.shot.vla is None and putt.shot.carry == 0.0  # sentinels cleaned
