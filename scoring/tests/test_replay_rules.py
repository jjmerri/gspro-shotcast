"""Hole replays with no database (shotcast_scoring.replay): what the app and the cloud both draw a
hole from. The app's own tests (tests/test_hole_replay.py, tests/test_replay.py) cover it through its
tables; these use plain mappings, as the cloud does."""

import math

from shotcast_scoring import replay


def shot(id, n, start, **over):
    row = {"id": id, "ts": 1000.0 + id, "player": "Pat", "course": "Links", "hole": "Hole 1, par 4",
           "shot_number": n, "club": "DR" if n == 1 else "7I", "is_putt": 0, "lie_before": "tee" if n == 1 else "fairway",
           "to_pin_yards": None, "total_yards": None, "offline_yards": None, "outcome": None, "mulligan": 0,
           "shared_ball": 0, "fmt": "stroke", "tags": "[]", "start_x": start and start[0], "start_z": start and start[1],
           "aim_map_x": None, "aim_map_y": None, "hazard_x": None, "hazard_z": None, "start_direction": None,
           "spin_axis": None, "handed": None, "picked": None, "carry": None}
    return {**row, **over}


LAYOUT = {"course": "Links", "hole": 1, "session_id": 1, "ts": 2000.0, "tee_x": 0.0, "tee_z": 0.0,
          "pin_x": 0.0, "pin_z": 380.0, "wp_x": 0.0, "wp_z": 250.0, "par": 4}


def test_a_hole_runs_from_the_tee_shot_to_shot_into_the_pin():
    rows = [shot(1, 1, None), shot(2, 2, (5.0, 250.0)), shot(3, 3, (1.0, 375.0), is_putt=1, outcome="holed")]
    rp = replay._replay(rows, 1, [LAYOUT], {1})
    assert (rp["tee"], rp["pin"], rp["par"], rp["pin_estimated"]) == ([0.0, 0.0], [0.0, 380.0], 4, False)
    [pat] = rp["players"]
    assert [(s["from"], s["to"]) for s in pat["shots"]] == [
        ([0.0, 0.0], [5.0, 250.0]), ([5.0, 250.0], [1.0, 375.0]), ([1.0, 375.0], [0.0, 380.0])]
    assert pat["strokes"] == 3


def test_with_no_layout_the_pin_is_found_from_the_logged_distances():
    pin = (10.0, 400.0)
    starts = [(0.0, 0.0), (20.0, 230.0), (12.0, 380.0)]
    rows = [shot(i + 1, i + 1, s, to_pin_yards=math.dist(s, pin) * replay.M_TO_YD) for i, s in enumerate(starts)]
    rp = replay._replay(rows, 1, [], set())
    assert rp["pin_estimated"] and math.dist(rp["pin"], pin) < 0.5


def test_a_tracked_shot_is_drawn_as_the_ball_went():
    trk = replay.track({"flight": [[0, 0, 0, 0], [50, 30, 100, 2.0], [100, 0, 200, 4.0]], "roll": [[105, 0, 210, 5.0]],
                        "flight_s": 4.0, "land_x": 100, "land_y": 0, "land_z": 200, "end_x": 105, "end_y": 0,
                        "end_z": 210, "start_x": 0, "start_y": 0, "start_z": 0})
    assert trk["end"] == (105, 210) and trk["path"][0] == [0, 0, 0.0, 0.0] and trk["path"][-1][:2] == [105, 210]
    assert trk["path"][trk["land"]][:2] == [100, 200] and trk["descent"] == 15  # (30 m down over 112 m)
    lost = replay.track({**{"flight": [[0, 0, 0, 0], [80, 20, 150, 3.0]], "roll": []}, "flight_s": 3.0, "land_x": None,
                         "land_y": None, "land_z": None, "end_x": None, "end_y": None, "end_z": None,
                         "start_x": 0, "start_y": 0, "start_z": 0})
    assert lost["lost"] and lost["end"] == (80, 150)
    assert replay.track({"flight": [], "roll": [], "flight_s": None, "land_x": None, "land_y": None, "land_z": None,
                         "end_x": None, "end_y": None, "end_z": None, "start_x": 0, "start_y": 0, "start_z": 0}) == {}
