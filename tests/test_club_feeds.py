import asyncio
import json

import pytest

from commentator.models import Shot
from commentator.sources import club_feeds
from commentator.sources.club_feeds import ClubFeed, _Parser, parse_flightscope, parse_rela

FS = ('2023-10-11 15:46:35,862 [1] INFO  VGPconnect.FlightscopeForm [(null)] - '
      + json.dumps({"guid": "{x}", "launchSpeed": 66.2, "launchSpeedAvailable": True, "clubSpeed": 44.449,
                    "clubSpeedAvailable": True, "attackAngleDeg": 3.4, "attackAngleDegAvailable": True,
                    "clubPathDeg": 0.0, "clubPathDegAvailable": False, "clubFaceAngleDeg": 0.0,
                    "clubFaceAngleDegAvailable": False, "launchElevDeg": 14.177, "launchElevDegAvailable": True,
                    "launchAzimDeg": -1.324, "launchAzimDegAvailable": True, "spinRPM": 2436,
                    "spinRPMandEstimateAvailable": True, "spinAxisDeg": -3.86, "spinAxisDegAvailable": True,
                    "carryDistance": 219.63, "carryDistanceAvailable": True, "BounceRoll_X": [1, 2, 3]}))
RELA_TEXT = ("[2026-07-28T15:51:07.1181010Z] SEND=> [ShotEnded] OUT type=FULL mode=NORMAL club=DR ball:speed=148.1 "
             "hla=-5.4 vla=11.2 spin=2639 back=2236 side=-1400 axis=-32.1 carry=240.1 sie=SIE-Assisted "
             "club:speed=99.4(measured) aoa=7.22(SIE-physics+MLP) path=-2.21(SIE-physics+MLP) face=n/a "
             "infScore=speed:1.00,aoa:0.73")
RELA_JSON = ('[2026-08-27T17:51:59.2626768Z] SEND=> [ShotEnded] OUT_JSON {"DeviceID":"GOLF","Units":"Yards",'
             '"ShotNumber":2,"BallData":{"Speed":113.92,"VLA":17.61},"ClubData":{"Speed":82.81,"Path":-7.62},'
             '"ShotDataOptions":{"ContainsBallData":true,"ContainsClubData":true,"IsHeartBeat":false}}')
OSP2 = ["[Info   :OpenSkyPlus 2] [OpenSkyPlus2] [INFO] Launch: BallSpeed=43.37051, LaunchAngle=24.59, HorizontalAngle=0",
        "[Info   :OpenSkyPlus 2] [OpenSkyPlus2] [INFO] Spin: TotalSpin=6425.2, BackSpin=-6119.8, SideSpin=-1957.1, SpinAxis=-17.7",
        "[Info   :OpenSkyPlus 2] [OpenSkyPlus2] [INFO] Club: ClubHeadSpeed=34.133354, SmashFactor=0, AttackAngle=0, ClubSpeedConfidence=1",
        "--- CLUB ---", "ClubHeadSpeed: 34.133354 (Conf: 1)",
        "[Info   :OpenSkyPlus 2] [OpenSkyPlus2] [INFO] Speed: ClubHeadSpeed=79.352905, ClubSpeedConfidence=1"]
OSP1 = """[2024-05-25 19:40:25.253 | Debug     :         OpenSkyPlus] GsPro4Osp> 
Sending request:
{
  "APIversion": "1",
  "Units": "Yards",
  "BallData": {
    "Speed": 23.3710041,
    "VLA": 23.557703
  },
  "ClubData": {
    "Speed": 44.89674
  },
  "ShotDataOptions": {
    "ContainsBallData": true,
    "ContainsClubData": true,
    "IsHeartBeat": false
  }
}
[2024-05-25 19:40:25.408 | Debug     :         OpenSkyPlus] GsPro4Osp> 
Response:
{"Code":200,"Message":"Club & Ball Data received","Player":null}""".splitlines()


def test_flightscope_converts_to_mph_and_keeps_only_what_the_radar_measured():
    r = parse_flightscope(FS)
    assert r.source == "FlightScope"
    assert r.ball_mph == pytest.approx(148.09, abs=0.01) and r.club_mph == pytest.approx(99.43, abs=0.01)
    assert r.angle_of_attack == 3.4
    assert r.path is None and r.face_to_target is None and r.loft is None
    assert r.launch == pytest.approx({"sp": r.ball_mph, "el": 14.177, "az": -1.324, "ts": 2436, "sa": -3.86,
                                      "cy": 240.19}, abs=0.01)   # carry in yards
    no_carry = json.loads(FS[FS.index("{"):]) | {"carryDistanceAvailable": False}
    assert parse_flightscope("FlightscopeForm - " + json.dumps(no_carry)).launch is None


def test_rela_keeps_measured_club_speed_and_drops_what_it_modelled():
    r = parse_rela(RELA_TEXT)
    assert (r.ball_mph, r.club_mph) == (148.1, 99.4)
    assert r.angle_of_attack is None and r.path is None and r.face_to_target is None
    assert r.launch == {"sp": 148.1, "el": 11.2, "az": -5.4, "ts": 2639, "sa": -32.1, "cy": 240.1}
    putt = RELA_TEXT.replace("type=FULL", "type=PUTT").replace("carry=240.1", "carry=0.0")
    assert parse_rela(putt).launch is None
    j = parse_rela(RELA_JSON)
    assert (j.ball_mph, j.club_mph, j.path) == (113.92, 82.81, None)
    assert j.launch is None   # no carry in this message
    ball_only = RELA_JSON.replace('"BallData":{"Speed":113.92,"VLA":17.61}',
                                  '"BallData":{"Speed":113.92,"VLA":17.61,"HLA":0,"TotalSpin":1886.79,'
                                  '"SpinAxis":-2.73,"CarryDistance":163.8}').replace(
                                      '"ContainsClubData":true', '"ContainsClubData":false')
    b = parse_rela(ball_only)
    assert b.club_mph is None and not b.has_club and b.launch["cy"] == 163.8
    heartbeat = '[2026-10-26T01:00:00Z] SEND=> {"ContainsBallData":false,"ContainsClubData":false,"IsHeartBeat":true}'
    assert parse_rela(heartbeat) is None


def test_openskyplus_both_versions():
    p = _Parser()
    got = [r for line in OSP2 + OSP1 if (r := p.feed(line))]
    assert [r.source for r in got] == ["OpenSkyPlus", "OpenSkyPlus"]
    assert got[0].ball_mph == pytest.approx(97.02, abs=0.01) and got[0].club_mph == pytest.approx(76.35, abs=0.01)
    assert (got[1].ball_mph, got[1].club_mph) == (23.3710041, 44.89674)
    assert got[0].launch is None and got[1].launch is None   # no carry: GSPro's files still air these


def test_an_impossible_club_speed_is_dropped():
    line = OSP2[0] + "\n" + OSP2[2].replace("34.133354", "5.0")   # 97 mph ball off 11 mph of club
    p = _Parser()
    assert [p.feed(x) for x in line.split("\n")] == [None, None]


def _feed(tmp_path, *names):
    return ClubFeed([(tmp_path, n) for n in names])


def test_feed_skips_history_then_gives_the_matching_shot_its_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(club_feeds, "WAIT_SECONDS", 0.0)
    rela = tmp_path / "rela.log"
    rela.write_text(RELA_TEXT + "\n")             # there at startup: an old shot
    feed = _feed(tmp_path, "rela.log", "LogOutput.log")
    assert feed.poll() == []
    with open(rela, "a") as fh:
        fh.write(RELA_TEXT.replace("ball:speed=148.1", "ball:speed=120.0").replace("99.4", "85.0") + "\n")
    (tmp_path / "LogOutput.log").write_text("\n".join(OSP2[:3]) + "\n")   # created after startup: all new

    async def go():
        other = Shot(ball_speed=148.1)
        assert not await feed.fill(other) and other.club_speed is None and not other.club_measured
        shot = Shot(ball_speed=120.3)
        assert await feed.fill(shot)
        assert shot.club_measured and shot.club_speed == 85.0
        again = Shot(ball_speed=120.0)
        assert not await feed.fill(again)          # each reading goes to one shot
        sky = Shot(ball_speed=97.0)
        assert await feed.fill(sky) and sky.club_speed == pytest.approx(76.35, abs=0.01)
    asyncio.run(go())


def test_a_shot_with_club_data_is_left_alone(tmp_path):
    feed = _feed(tmp_path, "rela.log")
    feed.poll()
    (tmp_path / "rela.log").write_text(RELA_TEXT + "\n")
    shot = Shot(ball_speed=148.1, club_speed=101.0, club_measured=True)
    assert not asyncio.run(feed.fill(shot)) and shot.club_speed == 101.0


def test_a_replaced_log_is_read_from_the_top(tmp_path, monkeypatch):
    monkeypatch.setattr(club_feeds, "WAIT_SECONDS", 0.0)
    log = tmp_path / "LogOutput.log"
    log.write_text("x" * 5000 + "\n")
    feed = _feed(tmp_path, "LogOutput.log")
    feed.poll()
    log.write_text("\n".join(OSP2[:3]) + "\n")   # BepInEx starts its log over: smaller than before
    assert [r.source for r in feed.poll()] == ["OpenSkyPlus"]


def test_a_used_readings_second_copy_isnt_given_to_the_next_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(club_feeds, "WAIT_SECONDS", 0.0)
    rela = tmp_path / "rela.log"
    rela.write_text("")
    feed = _feed(tmp_path, "rela.log")
    feed.poll()
    rela.write_text(RELA_TEXT.replace("[ShotEnded]", "[BallData]") + "\n")

    async def go():
        assert await feed.fill(Shot(ball_speed=148.1))
        with open(rela, "a") as fh:
            fh.write(RELA_TEXT + "\n")             # rela's shot-ended copy of the same swing
        assert not await feed.fill(Shot(ball_speed=148.3))
    asyncio.run(go())


def test_a_shot_that_waits_takes_its_impact_state_first(tmp_path, monkeypatch):
    monkeypatch.setattr(club_feeds, "WAIT_SECONDS", 0.3)
    rela = tmp_path / "rela.log"
    rela.write_text("")
    feed = _feed(tmp_path, "rela.log")
    feed.poll()
    rela.write_text(RELA_TEXT + "\n")
    feed.poll()                                    # a monitor is logging
    calls = []
    assert not asyncio.run(feed.fill(Shot(ball_speed=60.0), lambda: calls.append(1)))
    assert calls == [1]
