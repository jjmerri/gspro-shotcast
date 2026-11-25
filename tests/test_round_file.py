"""GSPro's round file (currentRound.dat, 3.59+) applied to the stored shots and drawn in hole replays."""
import gzip
import json
import math
import os
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from commentator import round_file
from commentator.hole_replay import hole_replays
from commentator.sources.gspro_log import M_TO_YD, GsproState, parse_events
from commentator.store import CLUB_COLS, Store

HOLE = "Hole 1, par 4, 328 yards"


def _p(x, z, y=1.0):
    return {"x": x, "y": y, "z": z}


def _entry(n, start, end, *, sp, name="Dan", hole=0, putt=False, flight=(), land=None, roll=(), to_pin=None,
           surfaces=(18, 2), result=0, hole_result=3, holed=False):
    """A currentRound.dat entry (Hole is 0-based)."""
    return {"ShotID": f"{name}-{hole}-{n}-{sp}", "RoundID": 7, "CourseKey": "test_links_gsp", "PlayerName": name,
            "Hole": hole, "HoleShot": n, "ShotResult": result, "HoleResult": hole_result,
            "StartingPOS": _p(*start), "EndingPOS": _p(*end), "StartingSurface": surfaces[0],
            "EndingSurface": surfaces[1], "DistanceToPin": to_pin, "BallSpeed": sp,
            "GhostData": {"sp": sp, "el": 12.0, "az": 0.5, "ts": 3000.0, "sa": 2.0, "cy": 200.0},
            "activeShot": {"tdTime": 6.5 if flight else 0.0,
                           "sd": {"isPutt": putt, "isHoled": holed, "pos": [_p(*q) for q in flight],
                                  "tdpoint": _p(*land) if land else _p(0, 0),
                                  "path": [{"pos": _p(*q), "time": 6.5 + i * 0.1} for i, q in enumerate(roll)]}}}


# Dan's par 4: tee (0, 0) -> carries to (4, 180), rolls to (5, 200) -> approach to (2, 295) -> holed putt.
DRIVE = _entry(1, (0, 0), (5, 200), sp=150.0, flight=[(0, 0), (1, 60), (3, 130), (4, 180)], land=(4, 180),
               roll=[(4, 180), (4.5, 190), (5, 200)], to_pin=100.1, surfaces=(18, 2))
APPROACH = _entry(2, (5, 200), (2, 295), sp=100.0, flight=[(5, 200), (3, 280), (2.5, 290)], land=(2.5, 290),
                  roll=[(2.5, 290), (2, 295)], to_pin=5.4, surfaces=(2, 5))
PUTT = _entry(3, (2, 295), (0, 300), sp=8.0, putt=True, roll=[(2, 295), (1, 298), (0, 300)], to_pin=5.4,
              surfaces=(5, 5), hole_result=7, holed=True)


def _season(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    for n, (sp, putt, outcome, lie_after, total) in enumerate(
            [(150.2, 0, None, "rough", 210.0), (99.8, 0, None, "fairway", 90.0), (8.0, 1, "holed", None, 2.0)], 1):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,"
                      " lie_before, lie_after, total_yards, carry_game_yards, outcome, raw)"
                      " VALUES (?, ?, 'Dan Smith', 'Test Links', ?, ?, '7I', ?, ?, ?, ?, ?, 150.0, ?, '{}')",
                      (st.session_id, 1000.0 + n, HOLE, n, putt, sp, "tee" if n == 1 else None, lie_after, total, outcome))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, 2000, '2025-10-20',"
                  " 'Test Links', 'Dan Smith', 1)", (st.session_id,))
    st.db.commit()
    return st


def _row(st, n):
    return st.db.execute("SELECT * FROM shots WHERE shot_number = ?", (n,)).fetchone()


def test_tracks_skip_penalties_and_the_copy_gspro_adds_when_a_ball_drops():
    copy = {**PUTT, "ShotID": "copy", "HoleShot": 4}
    wet = _entry(1, (0, 0), (40, 150), sp=150.0, name="Al", flight=[(0, 0), (40, 150)], land=(40, 150), to_pin=150)
    drop = _entry(2, (35, 140), (35, 140), sp=0.0, name="Al", result=2)
    got = round_file.tracks([DRIVE, APPROACH, PUTT, copy, wet, drop])
    assert [(t.player, t.hole, t.shot_number) for t in got] == [("Dan", 1, 1), ("Dan", 1, 2), ("Dan", 1, 3), ("Al", 1, 1)]
    drive, _, putt, al = got
    assert drive.carry_yards == round(math.hypot(4, 180) * M_TO_YD, 1)
    assert drive.total_yards == round(math.hypot(5, 200) * M_TO_YD, 1)
    assert (drive.finish_yards, drive.lie_before, drive.lie_after) == (round(100.1 * M_TO_YD, 1), "tee", "fairway")
    assert (putt.carry_yards, putt.finish_yards) == (None, 0.0)
    # Where a ball into the water stopped isn't where play went on: only its start and flight count.
    assert (al.penalty, al.end, al.total_yards, al.finish_yards, al.lie_after) == (True, None, None, None, None)
    assert al.carry_yards is not None


def test_the_round_file_replaces_the_booths_reads_and_applying_it_again_changes_nothing(tmp_path):
    st = _season(tmp_path)
    written, pending = st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0)
    assert (written, pending) == (3, [])
    drive, approach, putt = _row(st, 1), _row(st, 2), _row(st, 3)
    # A tee shot gets its start too, and GSPro's carry and total.
    assert (drive["start_x"], drive["start_z"]) == (0.0, 0.0)
    assert drive["carry_game_yards"] == round(math.hypot(4, 180) * M_TO_YD, 1)
    assert (drive["total_yards"], drive["total_from_log"]) == (round(math.hypot(5, 200) * M_TO_YD, 1), 2)
    # The HUD's lie gives way to GSPro's.
    assert (drive["lie_after"], approach["lie_before"], approach["lie_after"]) == ("fairway", "fairway", "green")
    assert approach["finish_yards"] == round(5.4 * M_TO_YD, 1)
    assert putt["total_yards"] == round(math.hypot(2, 5) * M_TO_YD, 1) and putt["finish_yards"] is None
    before = [tuple(r) for r in st.db.execute("SELECT * FROM shots ORDER BY id")]
    assert st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0) == (3, [])
    assert [tuple(r) for r in st.db.execute("SELECT * FROM shots ORDER BY id")] == before


def test_a_stroke_is_matched_only_to_its_players_shot_and_waits_for_its_row(tmp_path):
    st = _season(tmp_path)
    other = {**DRIVE, "ShotID": "al-1", "PlayerName": "Al"}
    late = {**APPROACH, "ShotID": "dan-hole2", "Hole": 1}
    written, pending = st.apply_round_file(round_file.tracks([other, late]), now=1005.0)
    assert written == 0 and [t.shot_id for t in pending] == ["al-1", "dan-hole2"]
    # A roster name the league maps it to.
    written, _ = st.apply_round_file(round_file.tracks([other]), now=1005.0,
                                     same_player=lambda g, p: (g, p) == ("Al", "Dan Smith"))
    assert written == 1
    # Not a round from long before the file was written.
    assert st.apply_round_file(round_file.tracks([APPROACH]), now=1000.0 + 13 * 3600)[0] == 0


def test_a_stroke_the_follow_tied_to_its_shot_is_applied_whatever_the_booths_stroke_count(tmp_path):
    st = _season(tmp_path)
    # A re-tee: the booth counts the approach as its 3rd (the penalty), GSPro's file as its 2nd.
    st.db.execute("UPDATE shots SET shot_number = 4 WHERE shot_number = 3")
    st.db.execute("UPDATE shots SET shot_number = 3 WHERE shot_number = 2")
    approach_id = _row(st, 3)["id"]
    assert st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0)[1] != []
    written, pending = st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0,
                                           pinned={APPROACH["ShotID"]: approach_id})
    assert (written, pending) == (1, [])
    assert _row(st, 3)["lie_after"] == "green" and _row(st, 3)["shot_number"] == 3
    # A pin needs no name: the HUD may have missed whose shot it was.
    st.db.execute("UPDATE shots SET player = NULL WHERE id = ?", (approach_id,))
    st.db.execute("DELETE FROM shot_tracks")
    gimme = st.record_gimme(approach_id)   # (left inside the gimme circle, recorded before the file named it)
    assert st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0, name_of={"Dan": "Dan Smith"}.get,
                               pinned={APPROACH["ShotID"]: approach_id}) == (1, [])
    assert (_row(st, 2)["player"], _row(st, 2)["attributed"]) == ("Dan Smith", 1)   # (named as GSPro counts it)
    assert _row(st, 2)["hole"] == HOLE   # its own label for the hole kept
    assert tuple(st.db.execute("SELECT player, shot_number FROM shots WHERE id = ?", (gimme,)).fetchone()) \
        == ("Dan Smith", 3)
    st.db.execute("DELETE FROM shots WHERE id = ?", (gimme,))
    # Still its shot after a restart (no pins, a new session: a course key matching no course's name).
    st.session_id += 1
    assert st.apply_round_file(round_file.tracks([{**APPROACH, "CourseKey": "zz_gsp"}]), now=1005.0) == (1, [])
    assert st.tracked_shots([APPROACH["ShotID"]]) == {APPROACH["ShotID"]: approach_id}
    # A pin wins over a shot the stroke number and a closer speed fit, and takes it back from one.
    st.db.execute("UPDATE shots SET ball_speed = 100.0, shot_number = 2 WHERE shot_number = 1")
    st.db.execute("UPDATE shot_tracks SET shot_id = (SELECT id FROM shots WHERE shot_number = 2)")
    st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0, pinned={APPROACH["ShotID"]: approach_id})
    assert st.tracked_shots([APPROACH["ShotID"]]) == {APPROACH["ShotID"]: approach_id}
    # A later round's shot like it doesn't take a pinned stroke over.
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, ball_speed, raw)"
                  " VALUES (?, 2100, 'Dan Smith', 'Test Links', ?, 2, 0, 100.0, '{}')", (st.session_id, HOLE))
    st.apply_round_file(round_file.tracks([APPROACH]), now=2105.0, pinned={APPROACH["ShotID"]: approach_id})
    assert st.tracked_shots([APPROACH["ShotID"]]) == {APPROACH["ShotID"]: approach_id}
    # Without the pin, a shot moved to another player since lets the stroke go.
    st.db.execute("UPDATE shots SET player = 'Al Jones' WHERE id = ?", (approach_id,))
    st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0)
    assert st.tracked_shots([APPROACH["ShotID"]]).get(APPROACH["ShotID"]) != approach_id


def test_the_booths_later_reads_leave_the_round_files_numbers_be(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0)
    drive, approach = _row(st, 1), _row(st, 2)
    st.set_shot_tiles(drive["id"], carry_game_yards=140.0, carry_raw_tile_yards=150.0, total_yards=150.0,
                      offline_yards=-3.0)
    st.set_lie_after(approach["id"], "fairway")
    st.set_shot_result(approach["id"], finish_yards=9.0)
    st.fill_log_totals(st.session_id)
    after, approach_after = _row(st, 1), _row(st, 2)
    assert (after["carry_game_yards"], after["total_yards"]) == (drive["carry_game_yards"], drive["total_yards"])
    assert (after["carry_raw_tile_yards"], after["offline_yards"]) == (150.0, -3.0)
    assert (approach_after["lie_after"], approach_after["finish_yards"]) == ("green", approach["finish_yards"])
    assert st.set_lie_after(approach["id"], "pine straw") == "green"   # GSPro's lie stays
    # A read stored before the file came goes.
    st.db.execute("UPDATE shots SET lie_after = 'pine straw' WHERE id = ?", (approach["id"],))
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0)
    assert _row(st, 2)["lie_after"] == "green"


def test_a_tracked_shot_is_replayed_along_its_flight_and_roll(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0)
    shots = hole_replays(st.db)[(1, 1)]["players"][0]["shots"]
    drive = shots[0]
    assert drive["from"] == [0.0, 0.0] and drive["to"] == [5.0, 200.0] and "bend" not in drive
    assert drive["path"][0][:2] == [0.0, 0.0] and drive["path"][-1][:2] == [5.0, 200.0]
    assert drive["path"][drive["land"]][:2] == [4.0, 180.0]
    assert "land" not in shots[2] and shots[2]["path"][-1][:2] == [0.0, 300.0]
    json.dumps(shots)


def test_a_tracked_shot_keeps_its_height_and_timing_for_the_shot_view(tmp_path):
    st = _season(tmp_path)
    arc = [(0, 0, 1.0), (1, 60, 21.0), (2, 120, 31.0), (3, 160, 16.0), (3.8, 176, 2.0)]
    drive = {**DRIVE, "activeShot": {**DRIVE["activeShot"], "sd": {
        **DRIVE["activeShot"]["sd"], "pos": [_p(x, z, y) for x, z, y in arc], "tdpoint": _p(4, 180, -1.0),
        "path": [{"pos": _p(4, 180 + i * 10, -1.0), "time": 6.5 + i * 0.5} for i in range(3)]}}}
    st.apply_round_file(round_file.tracks([drive]), now=1005.0)
    shot = hole_replays(st.db)[(1, 1)]["players"][0]["shots"][0]
    path, land = shot["path"], shot["land"]
    # Height above the tee (which sat at y = 1) and seconds since the strike: the flight's points
    # spread evenly over its 6.5 seconds up to GSPro's touchdown point, the roll's at their own times.
    assert path[land] == [4.0, 180.0, -2.0, 6.5]
    assert [2.0, 120.0, 30.0, 2.6] in path and path[-1] == [5.0, 200.0, 0.0, 7.5]
    assert all(p[3] <= q[3] for p, q in zip(path, path[1:]))
    assert shot["descent"] == round(math.degrees(math.atan2(14, math.hypot(0.8, 16))))


def test_a_steep_flight_keeps_its_climb_and_each_points_time_when_thinned():
    # Almost straight up: top-down its points are a few centimetres apart. GSPro's are 6.5 / 7 s apart
    # (the touchdown is past the last), and the two around the top are too close to keep.
    arc = [(0, 0, 1), (0.1, 0.2, 4), (0.2, 0.4, 6), (0.3, 0.5, 6.2), (0.4, 0.6, 6), (0.5, 0.8, 4), (0.6, 1.0, 1.2)]
    lob = _entry(1, (0, 0), (0.7, 1.5), sp=40.0, flight=arc, land=(0.6, 1.1, 1.0), roll=[(0.6, 1.1), (0.7, 1.5)])
    flight = round_file.tracks([lob])[0].flight
    assert [p[1] for p in flight] == [1, 4, 6, 4, 1.2]
    assert [round(p[3], 3) for p in flight] == [round(6.5 * i / 7, 3) for i in (0, 1, 2, 5, 6)]


def test_a_stroke_is_not_matched_to_another_courses_shot(tmp_path):
    st = _season(tmp_path)
    elsewhere = {**DRIVE, "ShotID": "elsewhere", "CourseKey": "other_hills_gsp"}
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,"
                  " raw) VALUES (?, 1001, 'Dan Smith', 'Other Hills', ?, 9, '7I', 0, 1, '{}')", (st.session_id, HOLE))
    written, pending = st.apply_round_file(round_file.tracks([elsewhere]), now=1005.0)
    assert written == 0 and [t.shot_id for t in pending] == ["elsewhere"]


def test_a_stroke_matched_before_its_course_was_known_moves_to_its_courses_shot(tmp_path):
    st = _season(tmp_path)
    elsewhere = {**DRIVE, "ShotID": "elsewhere", "CourseKey": "other_hills_gsp"}
    first = _row(st, 1)["id"]
    assert st.apply_round_file(round_file.tracks([elsewhere]), now=1005.0)[0] == 1   # (a course like none known)
    assert st.tracked_shots(["elsewhere"]) == {"elsewhere": first}
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,"
                  " raw) VALUES (?, 1004, 'Dan Smith', 'Other Hills', ?, 1, '7I', 0, 150.0, '{}')", (st.session_id, HOLE))
    st.apply_round_file(round_file.tracks([elsewhere]), now=1005.0)
    assert st.tracked_shots(["elsewhere"])["elsewhere"] != first


def test_a_pinned_shot_whose_course_went_unread_still_takes_its_stroke(tmp_path):
    st = _season(tmp_path)   # (Test Links is a course the booth knows)
    cur = st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt,"
                        " ball_speed, raw) VALUES (?, 1004, 'Dan Smith', NULL, ?, 9, '7I', 0, 150.0, '{}')",
                        (st.session_id, HOLE)).lastrowid
    pin = {**DRIVE, "ShotID": "pinned"}
    assert st.apply_round_file(round_file.tracks([pin]), now=1005.0, pinned={"pinned": cur})[0] == 1
    assert st.tracked_shots(["pinned"]) == {"pinned": cur}


def test_a_pin_takes_its_shot_from_an_earlier_files_stroke(tmp_path):
    # GSPro started over on the same course: the old file's drive was matched to the new round's first
    # shot before the new file was written. The live follow pinned the new file's drive to it.
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([DRIVE]), now=1005.0)
    shot = st.tracked_shots([DRIVE["ShotID"]])[DRIVE["ShotID"]]
    new = {**DRIVE, "ShotID": "restart", "RoundID": 8}
    assert st.apply_round_file(round_file.tracks([new]), now=1005.0, pinned={"restart": shot})[0] == 1
    assert st.tracked_shots(["restart", DRIVE["ShotID"]]) == {"restart": shot}


def test_a_stroke_of_another_gspro_round_is_not_matched_to_a_booth_rounds_shot(tmp_path):
    # The booth's round tied a stroke of GSPro's round 7 to one of its shots; GSPro started round 8 on
    # the same course before the booth closed it. Round 8's drive is like the booth round's own.
    st = _season(tmp_path)
    st.db.execute("UPDATE shots SET record_uuid = 'r1'")
    nxt = {**DRIVE, "ShotID": "next", "RoundID": 8}
    written, pending = st.apply_round_file(round_file.tracks([nxt]), now=1005.0, rounds={"r1": {7}})
    assert written == 0 and [t.shot_id for t in pending] == ["next"]
    assert st.apply_round_file(round_file.tracks([nxt]), now=1005.0, rounds={"r1": {8}})[0] == 1
    # Matched before the round's own GSPro round was known, it's taken back once it is.
    written, pending = st.apply_round_file(round_file.tracks([nxt]), now=1005.0, rounds={"r1": {7}})
    assert written == 0 and len(pending) == 1 and st.round_gspro_shots("r1") == []
    # Nor named blind from it.
    st = Store(str(tmp_path / "blind.db"))
    _blind(st, [151.0, 100.2, 8.0])
    st.db.execute("UPDATE shots SET record_uuid = 'r1'")
    files = [{**e, "RoundID": 8} for e in (DRIVE, APPROACH, PUTT)]
    assert st.apply_round_file(round_file.tracks(files), now=1005.0, name_of={"Dan": "Dan Smith"}.get,
                               rounds={"r1": {7}})[0] == 0
    assert st.apply_round_file(round_file.tracks(files), now=1005.0, name_of={"Dan": "Dan Smith"}.get)[0] == 3


def test_a_stroke_finds_its_course_whatever_case_it_was_stored_in_and_old_plays_dont_get_in_the_way(tmp_path):
    st = _season(tmp_path)
    st.db.execute("UPDATE shots SET course = 'TEST LINKS' WHERE shot_number > 1")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,"
                  " raw) VALUES (?, 1, 'Dan Smith', 'Other Hills', ?, 9, '7I', 0, 1, '{}')", (st.session_id, HOLE))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, 5, '2020-01-01',"
                  " 'Other Hills', 'Dan Smith', 1)", (st.session_id,))
    written, pending = st.apply_round_file(round_file.tracks([DRIVE, APPROACH]), now=1005.0)
    assert written == 2 and not pending


def test_a_penalty_that_follows_a_stroke_takes_back_where_the_file_had_it_stop(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0)
    assert _row(st, 2)["lie_after"] == "green"
    drop = _entry(3, (2, 295), (2, 295), sp=0.0, result=2)
    st.apply_round_file(round_file.tracks([APPROACH, drop]), now=1005.0)
    approach = _row(st, 2)
    assert (approach["total_yards"], approach["finish_yards"], approach["lie_after"]) == (None, None, None)
    assert approach["carry_game_yards"] == round(math.hypot(2.5, 90) * M_TO_YD, 1)
    # Nor does the next setup's tile give it one back.
    st.set_shot_tiles(approach["id"], carry_game_yards=None, carry_raw_tile_yards=None, total_yards=90.0,
                      offline_yards=None)
    assert _row(st, 2)["total_yards"] is None


def test_taking_back_a_putts_outcome_keeps_the_files_lie(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([PUTT]), now=1005.0)
    putt = _row(st, 3)
    st.clear_shot_result(putt["id"], "holed", finish=(None, 3.0))
    assert (_row(st, 3)["lie_after"], _row(st, 3)["finish_yards"]) == ("green", 3.0)


def test_a_path_that_rolls_back_keeps_its_turn():
    from commentator.hole_replay import _simplify
    assert _simplify([(0, 0, 0), (0, 10, 1), (0, 5, 2)]) == [0, 1, 2]
    assert _simplify([(0, 0, 0), (0, 10, 1), (0, 5, 1)]) == [0, 1, 2]


def test_a_straight_roll_keeps_the_points_that_slow_it_down():
    from commentator.hole_replay import _simplify
    # Straight on, but 15 m in its first second and 1 m in its last: played evenly it would lag 8 m.
    assert 1 in _simplify([(0, 100, 5), (0, 115, 6), (0, 119, 7), (0, 120, 8)])
    assert _simplify([(0, 100, 5), (0, 110, 6), (0, 120, 7)]) == [0, 2]


def _replay_of(st, ts, sp):
    """The same stroke as the drive, played again later."""
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,"
                  " lie_after, total_yards, raw) VALUES (?, ?, 'Dan Smith', 'Test Links', ?, 1, '7I', 0, ?,"
                  " 'rough', 205.0, '{}')", (st.session_id, ts, HOLE, sp))
    st.db.commit()
    return st.db.execute("SELECT * FROM shots WHERE ts = ?", (ts,)).fetchone()


def test_a_stroke_is_matched_in_the_latest_round_not_a_closer_speed_from_an_earlier_one(tmp_path):
    st = _season(tmp_path)   # its round ended at 2000; the drive's speed there is 150.2
    now = _replay_of(st, 2500.0, 150.9)
    assert st.apply_round_file(round_file.tracks([DRIVE]), now=2505.0) == (1, [])
    assert st.db.execute("SELECT shot_id FROM shot_tracks").fetchone()[0] == now["id"]


def test_a_stroke_matched_to_an_earlier_rounds_shot_moves_and_gives_that_shot_back(tmp_path):
    st = _season(tmp_path)
    before = _row(st, 1)
    st.apply_round_file(round_file.tracks([DRIVE]), now=2400.0)   # the new round's drive isn't recorded yet
    assert _row(st, 1)["total_from_log"] == 2
    now = _replay_of(st, 2500.0, 150.9)
    st.apply_round_file(round_file.tracks([DRIVE]), now=2505.0)
    assert st.db.execute("SELECT shot_id FROM shot_tracks").fetchone()[0] == now["id"]
    cols = ("start_x", "lie_before", "lie_after", "carry_game_yards", "total_yards", "total_from_log", "finish_yards")
    assert tuple(_row(st, 1)[c] for c in cols) == tuple(before[c] for c in cols)


def test_applying_the_same_file_again_only_matches_what_it_hadnt(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([DRIVE]), now=1005.0)
    st.db.execute("UPDATE shots SET lie_before = 'x' WHERE shot_number = 1")
    assert st.apply_round_file(round_file.tracks([DRIVE, APPROACH]), now=1005.0, rewrite=False) == (1, [])
    assert (_row(st, 1)["lie_before"], _row(st, 2)["lie_before"]) == ("x", "fairway")


def test_a_stroke_a_mulligan_takes_back_gives_its_shot_back(tmp_path):
    st = _season(tmp_path)
    before = _row(st, 2)
    st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0)
    entries = [{**APPROACH, "ShotResult": 1}]
    got = round_file.tracks(entries)
    st.apply_round_file(got, now=1005.0, gone=round_file.untracked(entries, got))
    assert st.db.execute("SELECT COUNT(*) FROM shot_tracks").fetchone()[0] == 0
    assert (_row(st, 2)["lie_after"], _row(st, 2)["total_yards"]) == (before["lie_after"], before["total_yards"])


def test_a_shot_a_released_stroke_gave_back_keeps_what_it_had_when_another_takes_it(tmp_path):
    st = _season(tmp_path)
    before = _row(st, 1)
    st.apply_round_file(round_file.tracks([DRIVE]), now=2400.0)   # matched to the earlier round's drive
    retaken = {**DRIVE, "ShotID": "re-hit"}
    entries = [{**DRIVE, "ShotResult": 1}, retaken]
    got = round_file.tracks(entries)
    st.apply_round_file(got, now=2400.0, gone=round_file.untracked(entries, got))
    _replay_of(st, 2500.0, 150.9)
    st.apply_round_file(got, now=2505.0, gone=round_file.untracked(entries, got))
    cols = ("lie_after", "carry_game_yards", "total_yards", "total_from_log", "finish_yards")
    assert tuple(_row(st, 1)[c] for c in cols) == tuple(before[c] for c in cols)


def test_an_alternate_shot_stroke_is_drawn_as_tracked_without_a_pin(tmp_path):
    st = _season(tmp_path)
    st.db.execute("UPDATE shots SET fmt = 'alternate'")
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0)
    putt = hole_replays(st.db)[(1, 1)]["players"][0]["shots"][2]
    assert putt["to"] == [0.0, 300.0] and putt["path"][-1][:2] == [0.0, 300.0]


def test_a_scrambles_finish_stays_the_booths(tmp_path):
    st = _season(tmp_path)
    st.db.execute("UPDATE shots SET finish_yards = 50.0 WHERE shot_number = 2")
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH]), now=1005.0)
    assert _row(st, 2)["finish_yards"] == round(5.4 * M_TO_YD, 1)
    # The round turns out to be a scramble: the booth's finish (the picked ball's) is back.
    st.set_format([_row(st, 1)["id"], _row(st, 2)["id"]], "scramble")
    assert (_row(st, 1)["finish_yards"], _row(st, 2)["finish_yards"]) == (None, 50.0)
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH]), now=1005.0)
    assert _row(st, 2)["finish_yards"] == 50.0
    st.set_shot_result(_row(st, 2)["id"], finish_yards=40.0)
    assert _row(st, 2)["finish_yards"] == 40.0


def test_a_scrambles_drives_out_of_bounds_are_drawn_as_gspro_tracked_them(tmp_path):
    # Dan and Derek's 5th, 11-15: both drives went out of bounds right (GSPro left Dan's ball resting
    # past the stakes, Derek's it took away), and the team played stroke 3 from the tee.
    st = Store(str(tmp_path / "season.db"))
    for who, n, ts, picked, outcome in (("Dan", 1, 1001.0, 1, None), ("Derek", 1, 1002.0, 0, None),
                                        ("Dan", 3, 1003.0, 1, None), ("Derek", 3, 1004.0, 0, None),
                                        ("Dan", 4, 1005.0, 0, "holed")):
        x, z = (0.0, 0.0) if n < 4 else (5.0, 200.0)
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                      " shared_ball, fmt, start_x, start_z, picked, outcome, raw) VALUES (?, ?, ?, 'Test Links', ?, ?,"
                      " 'DR', 0, 300, 1, 'scramble', ?, ?, ?, ?, '{}')",
                      (st.session_id, ts, who, HOLE, n, x, z, picked, outcome))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, 2000, '2025-10-20',"
                  " 'Test Links', 'Dan', 1)", (st.session_id,))
    st.db.commit()
    dan, derek = (r[0] for r in st.db.execute("SELECT id FROM shots WHERE shot_number = 1 ORDER BY id"))
    flight = json.dumps([[0, 1, 0], [20, 30, 100], [45, 1, 200]])
    st.db.execute("INSERT INTO shot_tracks (shot_id, gspro_shot_id, land_x, land_y, land_z, end_x, end_y, end_z, flight,"
                  " flight_s) VALUES (?, 'dan', 45, 1, 200, 50, 1, 210, ?, 6.0)", (dan, flight))
    st.db.execute("INSERT INTO shot_tracks (shot_id, gspro_shot_id, land_x, land_y, land_z, flight, flight_s)"
                  " VALUES (?, 'derek', 45, 1, 200, ?, 6.0)", (derek, flight))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id, ts=1500.0)
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert [(s["to"], s.get("penalty")) for s in players["Dan"]["shots"][:1]] == [([50.0, 210.0], "ob")]
    assert [(s["to"], s.get("penalty")) for s in players["Derek"]["shots"][:1]] == [([45.0, 200.0], "ob")]
    assert players["Derek"]["shots"][0]["path"][-1][:2] == [45.0, 200.0]
    # The counts still add up to the team's 4: Dan's drive, its penalty, stroke 3 and the putt.
    assert sum(players[n]["used"] + players[n]["used_pens"] for n in ("Dan", "Derek")) == 4
    # With no pick logged for the drives, one still carries the stroke and the penalty.
    st.db.execute("UPDATE shots SET picked = NULL WHERE shot_number = 1")
    st.db.commit()
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert sum(players[n]["used"] + players[n]["used_pens"] for n in ("Dan", "Derek")) == 4


def _blind(st, speeds, t0=1000.0):
    """Shots the booth recorded without reading the screen: no player, no stroke number, a stale hole."""
    for n, sp in enumerate(speeds):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, ball_speed,"
                      " attributed, raw) VALUES (?, ?, NULL, 'Test Links', 'Hole 18, par 4, 404 yards', NULL, '7I', 0,"
                      " ?, 0, '{}')", (st.session_id, t0 + n, sp))
    st.db.commit()


def test_shots_recorded_blind_are_named_from_the_round_file_by_order_and_ball_speed(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    _blind(st, [151.0, 149.6, 100.2, 8.0])   # Dan's drive, Al's drive, Dan's approach, Dan's putt
    st.db.execute("UPDATE shots SET record_uuid = 'u1'")
    al = _entry(1, (0, 0), (5, 200), sp=149.8, name="Al", flight=[(0, 0), (4, 180)], land=(4, 180), to_pin=99.0)
    files = [{**DRIVE, "GhostData": {**DRIVE["GhostData"], "sp": 150.9}, "HolePar": 4}, al, APPROACH, PUTT]
    written, pending = st.apply_round_file(round_file.tracks(files), now=1005.0, name_of={"Dan": "Dan Smith"}.get)
    assert (written, pending) == (4, [])
    rows = st.db.execute("SELECT player, hole, shot_number, is_putt, attributed, finish_yards FROM shots ORDER BY ts").fetchall()
    assert [tuple(r)[:5] for r in rows] == [("Dan Smith", "Hole 1, par 4", 1, 0, 1), ("Al", "Hole 1", 1, 0, 1),
                                            ("Dan Smith", "Hole 1", 2, 0, 1), ("Dan Smith", "Hole 1", 3, 1, 1)]
    assert rows[3]["finish_yards"] is None   # a putt keeps no finish
    # Each named shot is on its player's slot in the round's record, as an upload says whose it is.
    slots = dict(st.db.execute("SELECT name, slot_id FROM round_slots WHERE round_uuid = 'u1'").fetchall())
    assert [r[0] for r in st.db.execute("SELECT slot_id FROM shots ORDER BY ts")] == [
        slots["Dan Smith"], slots["Al"], slots["Dan Smith"], slots["Dan Smith"]]
    # Applied again, by name now; a mulligan taking a stroke back gives its shot back its blind read.
    assert st.apply_round_file(round_file.tracks(files), now=1005.0, name_of={"Dan": "Dan Smith"}.get)[1] == []
    st.apply_round_file(round_file.tracks(files[:3]), now=1005.0, gone={PUTT["ShotID"]})
    assert tuple(st.db.execute("SELECT player, hole, shot_number, attributed, slot_id FROM shots WHERE ball_speed = 8.0")
                 .fetchone()) == (None, "Hole 18, par 4, 404 yards", None, 0, None)


def test_blind_shots_that_dont_line_up_are_left_unnamed(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    _blind(st, [151.0, 120.0, 8.0])   # the booth missed the approach and has a practice swing in its place
    written, pending = st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0, name_of=str)
    assert written == 0 and len(pending) == 3
    assert st.db.execute("SELECT COUNT(*) FROM shots WHERE player IS NOT NULL").fetchone()[0] == 0


def test_blind_shots_with_no_course_read_go_by_their_rounds_gspro_course_folder(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    _blind(st, [151.0, 100.2, 8.0])
    st.db.execute("UPDATE shots SET course = NULL, record_uuid = 'r'")
    st.db.commit()
    files = [{**f, "CourseKey": "foreign_gsp"} for f in (DRIVE, APPROACH, PUTT)]
    written, pending = st.apply_round_file(round_file.tracks(files), now=1005.0, name_of=str, folders={"r": "mine_gsp"})
    assert written == 0 and len(pending) == 3
    files = [{**f, "CourseKey": "mine_gsp"} for f in files]
    assert st.apply_round_file(round_file.tracks(files), now=1005.0, name_of=str) == (3, [])


def test_the_round_file_watch_reads_a_write_once_and_again_after_one_caught_half_written(tmp_path):
    from commentator.sources.gspro_files import RoundFileWatch
    w = RoundFileWatch(str(tmp_path))
    assert not w.poll() and w.entries is None   # no file: a GSPro that writes none
    path = tmp_path / "currentRound.dat"
    path.write_text(json.dumps([DRIVE]))
    # (written after the watch began: a file's time comes from a coarser clock than time.time(), and the
    # first read of a file written before the watch counts nothing in it as new)
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert w.poll(now=10.0) and w.version == 1 and not w.poll(now=11.0)   # unchanged: not parsed again
    path.write_text(json.dumps([DRIVE, APPROACH])[:-20])   # GSPro mid-write
    assert not w.poll(now=12.0) and len(w.entries) == 1
    path.write_text(json.dumps([DRIVE, APPROACH]))
    assert w.poll(now=13.0) and len(w.entries) == 2
    assert w.seen_at == {DRIVE["ShotID"]: 10.0, APPROACH["ShotID"]: 13.0}


def test_a_notified_read_waits_out_gspros_write_and_never_takes_half_a_list(tmp_path):
    import threading
    from commentator.sources.gspro_files import RoundFileWatch
    w = RoundFileWatch(str(tmp_path))
    path = tmp_path / "currentRound.dat"
    path.write_text(json.dumps([DRIVE, APPROACH])[:-20])   # truncated and half rewritten
    assert not w.poll(retry_s=0.0) and w.entries is None
    threading.Timer(0.03, lambda: path.write_text(json.dumps([DRIVE, APPROACH]))).start()
    assert w.poll(retry_s=0.5) and len(w.entries) == 2


def test_the_change_notifier_hears_only_the_round_file(tmp_path):
    import struct
    import sys
    from commentator.sources.gspro_files import ChangeNotifier, RoundFileWatch

    def info(*names):   # FILE_NOTIFY_INFORMATION records, as ReadDirectoryChangesW fills its buffer
        out = b""
        for i, n in enumerate(names):
            raw = n.encode("utf-16-le")
            rec = struct.pack("<III", 0, 3, len(raw)) + raw
            rec += b"\0" * (-len(rec) % 4)
            if i < len(names) - 1:
                rec = struct.pack("<I", len(rec)) + rec[4:]
            out += rec
        return out
    n = ChangeNotifier(str(tmp_path), "currentRound.dat", lambda: None)
    assert n._names(info("Player.log", "CurrentRound.dat"))
    assert not n._names(info("Player.log", "GSPro.db"))
    if sys.platform != "win32":   # elsewhere: the backup check alone
        assert not RoundFileWatch(str(tmp_path)).notify(lambda: None)


def test_a_stroke_says_what_its_penalty_was_and_whether_the_ball_dropped():
    wet = _entry(1, (0, 0), (40, 150), sp=150.0, name="Al", flight=[(0, 0), (40, 150)], land=(40, 150))
    water = _entry(2, (35, 140), (35, 140), sp=0.0, name="Al", result=2)
    chip = _entry(3, (35, 140), (36, 141), sp=30.0, name="Al", to_pin=0.3, surfaces=(1, 5))
    gimme = {**chip, "ShotID": "copy", "HoleShot": 4, "HoleResult": 7}
    al_wet, al_chip = round_file.tracks([wet, water, chip, gimme])
    assert (al_wet.penalty_kind, al_wet.dropped) == ("water", False)
    assert (al_chip.penalty_kind, al_chip.dropped, al_chip.in_cup, al_chip.lie_after) == (None, True, False, "green")
    putt = round_file.tracks([PUTT])[0]
    assert putt.in_cup and putt.dropped
    # In the cup before GSPro writes its copy; and a gimme's own stroke marked done keeps how far it was left.
    chip_in = round_file.tracks([{**PUTT, "HoleResult": 3}])[0]
    assert chip_in.in_cup and chip_in.dropped and chip_in.finish_yards == 0.0
    given = round_file.tracks([{**chip, "HoleResult": 7}])[0]
    assert given.dropped and not given.in_cup and given.finish_yards == round(0.3 * M_TO_YD, 1)


def test_a_hole_picked_up_at_the_maximum_is_not_holed_where_the_ball_lay():
    # GSPro's auto-concede marks the last stroke holed with the putter, its EndingPOS where the ball
    # stopped (113 m on, 44 m short) and its DistanceToPin from where it was struck (Evan's 9th on 11).
    iron = _entry(9, (0, 0), (5, 113), sp=120.0, to_pin=157.4, hole_result=7, holed=True,
                  flight=[(0, 0), (5, 113)], land=(5, 113))
    iron["ClubIndex"] = 26
    t = round_file.tracks([iron])[0]
    assert (t.picked_up, t.in_cup, t.dropped, t.holed, t.finish_yards, round_file.file_club(t)) == \
        (True, False, False, False, None, None)
    assert t.end is not None and t.total_yards == round(math.hypot(5, 113) * M_TO_YD, 1)
    holed = round_file.tracks([PUTT])[0]      # start to cup 5.39 m, DistanceToPin 5.4: in
    assert (holed.picked_up, holed.in_cup, holed.finish_yards) == (False, True, 0.0)


def test_a_stroke_rewritten_as_picked_up_keeps_how_far_it_was_left(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH]), now=1005.0)
    # GSPro auto-concedes the hole after the approach: the same stroke, holed, its DistanceToPin stale.
    picked = {**APPROACH, "HoleResult": 7, "DistanceToPin": 100.12,
              "activeShot": {**APPROACH["activeShot"], "sd": {**APPROACH["activeShot"]["sd"], "isHoled": True}}}
    assert st.apply_round_file(round_file.tracks([DRIVE, picked]), now=1006.0)[0] == 2
    assert _row(st, 2)["finish_yards"] == round(5.4 * M_TO_YD, 1)
    assert st.db.execute("SELECT finish_yards FROM shot_tracks WHERE gspro_shot_id = ?",
                         (APPROACH["ShotID"],)).fetchone()[0] == round(5.4 * M_TO_YD, 1)


def test_round_end_keeps_a_copy_named_by_start_and_course_and_prunes_the_oldest(tmp_path):
    import os
    import time

    src = tmp_path / "currentRound.dat"
    src.write_text(json.dumps([_entry(1, (0, 0), (0, 200), sp=150.0)]), encoding="utf-8")
    started = time.mktime((2026, 11, 18, 14, 12, 30, 0, 0, -1))
    os.utime(src, (started + 60, started + 60))
    kept = tmp_path / "logs" / round_file.ARCHIVE_DIR
    kept.mkdir(parents=True)
    for i in range(3):
        (kept / f"2026-01-0{i + 1}-1200-Old.dat").write_text("[]")
    course = "Pebble Beach / Links (GSPro)"

    out = round_file.archive(src, tmp_path / "logs", started, course, ["dan"], [150.2], keep=3)
    assert out.name == "2026-11-18-141230-Pebble-Beach-Links-GSPro.dat" and out.read_text() == src.read_text()
    assert sorted(p.name for p in kept.iterdir()) == [
        "2026-01-02-1200-Old.dat", "2026-01-03-1200-Old.dat", out.name]
    # A second round started the same second never overwrites the first, and is the newer one kept.
    again = round_file.archive(src, tmp_path / "logs", started, course, ["Dan"], keep=1)
    assert again.name == "2026-11-18-141230-Pebble-Beach-Links-GSPro~2.dat"
    assert [p.name for p in kept.iterdir()] == [again.name]
    # One started a few seconds later on another course is newer whatever the course is called.
    later = round_file.archive(src, tmp_path / "logs", started + 20, "Augusta", ["Dan"], keep=1)
    assert [p.name for p in kept.iterdir()] == [later.name] == ["2026-11-18-141250-Augusta.dat"]


def test_no_copy_of_a_missing_unreadable_earlier_or_next_rounds_file(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setattr(round_file, "ARCHIVE_READ_WAIT_S", 0)
    src, logs, now = tmp_path / "currentRound.dat", tmp_path / "logs", time.time()
    dan = json.dumps([_entry(1, (0, 0), (0, 200), sp=150.0), _entry(2, (0, 200), (0, 300), sp=90.0)])
    assert round_file.archive(src, logs, now, "X", ["Dan"]) is None
    src.write_text(dan, encoding="utf-8")
    os.utime(src, (now - round_file.ARCHIVE_STALE_S - 10,) * 2)
    assert round_file.archive(src, logs, now, "X", ["Dan"], [150.0, 90.0]) is None
    # Last written a minute before this round started: this round's only if its strokes are in it.
    os.utime(src, (now - 60,) * 2)
    assert round_file.archive(src, logs, now, "X", ["Dan"]) is None
    src.write_text(dan[:40], encoding="utf-8")   # mid-write
    assert round_file.archive(src, logs, now - 5, "X", ["Dan"]) is None
    src.write_text("[]", encoding="utf-8")
    assert round_file.archive(src, logs, now - 5, "X", ["Dan"]) is None
    # The round end noticed after the next round began: someone else's, or Dan's first strokes of it.
    src.write_text(json.dumps([_entry(1, (0, 0), (0, 200), sp=150.0, name="Sue")]), encoding="utf-8")
    assert round_file.archive(src, logs, now - 5, "X", ["Dan"]) is None
    src.write_text(dan, encoding="utf-8")
    assert round_file.archive(src, logs, now, "X", ["Dan"], [120.0, 60.0, 30.0, 150.0]) is None
    assert round_file.archive(src, logs, now, "X", ["Dan"], [149.5, 150.0, 150.4, 8.0]) is None   # one 150 swing
    assert not logs.exists()
    assert round_file.archive(src, logs, now, "X", ["Dan"], [150.3, 89.8, 30.0]) is not None
    src.write_text(json.dumps([_entry(1, (0, 0), (0, 200), sp=100.0), _entry(2, (0, 200), (0, 300), sp=101.5)]),
                   encoding="utf-8")
    assert round_file.archive(src, logs, now, "X", ["Dan"], [100.7, 99.5, 50.0, 30.0]) is not None
    # GSPro holding the file a moment (a sharing violation on Windows) is waited out.
    read, tries = Path.read_bytes, []

    def busy_once(p):
        tries.append(p)
        if len(tries) == 1:
            raise PermissionError("in use")
        return read(p)
    monkeypatch.setattr(Path, "read_bytes", busy_once)
    assert round_file.archive(src, logs, now + 60, "Y", ["Dan"], [100.0]) is not None and len(tries) == 2


def test_round_file_archive_goes_by_gspros_shot_ids_and_course(tmp_path):
    import time

    src, logs, now = tmp_path / "currentRound.dat", tmp_path / "logs", time.time()
    # Dan quit after one drive and started Augusta: his new round's first drive is like his old one.
    nxt = [_entry(1, (0, 0), (0, 200), sp=150.3)]
    nxt[0]["CourseKey"] = "augusta_national_gsp"
    src.write_text(json.dumps(nxt), encoding="utf-8")
    assert round_file.archive(src, logs, now, "Pebble Beach", ["Dan"], [150.2]) is not None   # (speeds alone)
    assert round_file.archive(src, logs, now, "Pebble Beach", ["Dan"], [150.2],
                              courses=["Pebble Beach", "Augusta National"]) is None
    # A course key like no course the booth has (it can't be told apart) leaves it to the ShotIDs.
    assert round_file.archive(src, logs, now, "Pebble Beach", ["Dan"], [150.2], courses=["Pebble Beach"],
                              shot_ids=["Dan-0-1-150.0"]) is None
    assert round_file.archive(src, logs, now, "Pebble Beach", ["Dan"], [99.0], courses=["Pebble Beach"],
                              shot_ids=[nxt[0]["ShotID"]]) is not None
    # A course whose name starts like the round's: its own exact key is another course's.
    nxt[0]["CourseKey"] = "augusta_national_par_3_gsp"
    src.write_text(json.dumps(nxt), encoding="utf-8")
    both = ["Augusta National", "Augusta National Par 3"]
    assert round_file.archive(src, logs, now, "Augusta National", ["Dan"], [150.2], courses=both) is None
    assert round_file.archive(src, logs, now, "Augusta National Par 3", ["Dan"], [150.2], courses=both) is not None
    assert round_file.course_named("augusta_national_gsp", "Augusta National", both)
    assert not round_file.course_named("augusta_national_par_3_gsp", "Augusta National", both)
    assert round_file.course_named("augusta_national_par_3_gsp", "Augusta National", ["Augusta National"])


def test_round_file_archive_goes_by_the_rounds_gspro_course_folder(tmp_path):
    import time

    # Wentworth by the Sea is played from GSPro's balsams_gsp folder (GSPro lists it as Balsams
    # Panorama), and the booth has shots on the real Balsams Panorama too.
    src, logs, now = tmp_path / "currentRound.dat", tmp_path / "logs", time.time()
    mine = [_entry(1, (0, 0), (0, 200), sp=150.3)]
    mine[0]["CourseKey"] = "balsams_gsp"
    src.write_text(json.dumps(mine), encoding="utf-8")
    both, ids = ["Wentworth by the Sea", "Balsams Panorama"], [mine[0]["ShotID"]]
    assert round_file.archive(src, logs, now, "Wentworth by the Sea", ["Dan"], [150.2], courses=both,
                              shot_ids=ids) is None   # by name alone it's Balsams Panorama's
    assert round_file.archive(src, logs, now, "Wentworth by the Sea", ["Dan"], [150.2], courses=both,
                              shot_ids=ids, folder="balsams_gsp") is not None
    # Another folder is another course, whatever its key's name is like.
    mine[0]["CourseKey"] = "balsams_gsp - bak"
    src.write_text(json.dumps(mine), encoding="utf-8")
    assert round_file.archive(src, logs, now, "Balsams Panorama", ["Dan"], [150.2], courses=both,
                              shot_ids=ids, folder="balsams_gsp") is None


def test_two_full_names_alike_only_in_the_first_are_two_players():
    from commentator.round_file import names_fit
    assert names_fit("Josh", "Josh Merritt") and names_fit("Josh M.", "Josh Merritt")
    assert names_fit("josh merritt", "Josh Merritt")
    assert not names_fit("Alex Jones", "Alex Smith") and not names_fit("Dan", "Danny")
    assert not names_fit("Alex Green", "Alex Greene") and not names_fit("Alex Greene", "Alex Green")
    assert not names_fit("Alex Garcia Lopez", "Alex Garcia") and names_fit("Alex G. L.", "Alex Garcia Lopez")


def test_slot_players_ties_the_logs_credited_shots_to_the_files_names_by_their_ball():
    dan, sue = _entry(1, (0, 0), (5, 200), sp=150.0, name="Dan"), _entry(1, (0, 0), (5, 190), sp=140.0, name="Sue")
    launch = {"sp": 150.0, "el": 12.0, "az": 0.5, "ts": 3000.0, "sa": 2.0}   # as the log rounds them
    credits = [(1, launch), (0, {**launch, "sp": 140.004})]
    assert round_file.slot_players(credits, [dan, sue]) == {1: "Dan", 0: "Sue"}
    assert round_file.slot_players([(1, {**launch, "sp": 151.0})], [dan, sue]) == {}   # no such ball
    # A slot whose shots name two players, or a name two slots claim, is left out.
    assert round_file.slot_players([(1, launch), (1, {**launch, "sp": 140.0})], [dan, sue]) == {}
    assert round_file.slot_players([(1, launch), (0, launch)], [dan, sue]) == {}
    # Al left slot 0 and Dan moved up from slot 1: neither slot is known to be Dan's.
    al = _entry(1, (0, 0), (5, 180), sp=130.0, name="Al")
    dan2 = _entry(2, (0, 0), (5, 20), sp=120.0, name="Dan")
    moved = [(0, {**launch, "sp": 130.0}), (1, launch), (0, {**launch, "sp": 120.0})]
    assert round_file.slot_players(moved, [al, dan, dan2]) == {}


def test_gspros_club_index_fills_only_a_club_the_screen_read_none_for(tmp_path, caplog):
    st = _season(tmp_path)   # every shot read as a 7-iron
    drive, approach, putt = ({**e, "ClubIndex": i} for e, i in ((DRIVE, 0), (APPROACH, 19), (PUTT, 26)))
    got = round_file.tracks([drive, approach, putt])
    assert [t.club for t in got] == ["DR", "7I", "PT"]
    with caplog.at_level("INFO"):
        st.apply_round_file(got, now=1005.0)
    # The screen's read is the club hit: GSPro's (the club selected at rest) doesn't replace it.
    assert [_row(st, n)["club"] for n in (1, 2, 3)] == ["7I", "7I", "7I"]
    assert "from GSPro's round file" not in caplog.text
    for sid in (_row(st, n)["id"] for n in (1, 2, 3)):
        st._release_track(sid)
    st.db.execute("UPDATE shots SET club = NULL")
    with caplog.at_level("INFO"):
        st.apply_round_file(got, now=1005.0)
    # One the screen read none for is GSPro's, but never the holed putt's (a hole-out selects the putter).
    assert [_row(st, n)["club"] for n in (1, 2, 3)] == ["DR", "7I", None]
    assert "club unread -> DR from GSPro's round file" in caplog.text
    sid = _row(st, 1)["id"]
    st._release_track(sid)
    assert _row(st, 1)["club"] is None
    # A putt the booth took for a full shot (no club read): GSPro's putter makes it one.
    lag = {**putt, "ShotID": "lag", "HoleResult": 3, "activeShot": {**putt["activeShot"],
                                                                     "sd": {**putt["activeShot"]["sd"], "isHoled": False}}}
    missed = round_file.tracks([drive, approach, lag])
    pid = _row(st, 3)["id"]
    st._release_track(pid)
    st.db.execute("UPDATE shots SET is_putt = 0 WHERE id = ?", (pid,))
    st.apply_round_file(missed, now=1005.0)
    assert (_row(st, 3)["club"], _row(st, 3)["is_putt"]) == ("PT", 1)
    st._release_track(pid)
    assert (_row(st, 3)["club"], _row(st, 3)["is_putt"]) == (None, 0)
    # An approach the booth took for a putt (no club read): a full shot, its finish kept.
    aid = _row(st, 2)["id"]
    st._release_track(aid)
    st.db.execute("UPDATE shots SET is_putt = 1, finish_yards = NULL WHERE id = ?", (aid,))
    st.apply_round_file(got, now=1005.0)
    assert (_row(st, 2)["club"], _row(st, 2)["is_putt"]) == ("7I", 0) and _row(st, 2)["finish_yards"] is not None
    # A shot recorded blind takes GSPro's club's word on a putt too, where the screen read no club.
    assert Store._blind_named({"hole": None, "is_putt": 1}, got[1], lambda n: None)["is_putt"] == 0
    st.apply_round_file(got[1:], now=1005.0, gone={drive["ShotID"]})   # taken back (a mulligan): the read returns
    assert _row(st, 1)["club"] is None
    # GSPro's driver off the deck is the booth's 3-wood (hud.deck_club), as the screen's is.
    deck = round_file.tracks([{**APPROACH, "ClubIndex": 0}])[0]
    assert round_file.club_fix(deck, None) == "3W" and round_file.club_fix(deck, "5W") is None
    gap = round_file.tracks([{**APPROACH, "ClubIndex": 23}])[0]
    assert round_file.club_fix(gap, "AW") is None and round_file.club_fix(gap, None) == "GW"
    assert round_file.club_fix(round_file.tracks([{**APPROACH, "ClubIndex": 27}])[0], None) is None


# Two real rounds GSPro 4.x wrote on 2026-11-18 (Bethpage Black, Josh Merritt and Corey Martin): a
# single-player stroke round (RoundID 1696) and a two-man scramble of the front nine (1697), with the
# scramble's GSPro log (its LaunchExt Vars and score lines) and the booth's shots for both, the club as
# the screen read it (booth log `impact HUD ... club`).
FIXTURES = Path(__file__).parent / "fixtures" / "round_files"
BOOTH = json.loads((FIXTURES / "booth_shots_2026-11-18.json").read_text())
SINGLE_HOLES = {1: 4, 2: 3, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4, 8: 2, 9: 3}
# Each team stroke is both players' ball, but for the hole-ending putt only one may play it.
SCRAMBLE_HOLES = {1: 6, 2: 5, 3: 4, 4: 8, 5: 5, 6: 6, 7: 8, 8: 4, 9: 6}
# GSPro's team score (its log's score lines), by hole.
SCRAMBLE_SCORES = {1: 3, 2: 3, 3: 2, 4: 4, 5: 3, 6: 4, 7: 5, 8: 3, 9: 4}


def _real(name):
    return json.loads(gzip.decompress((FIXTURES / f"{name}.dat.gz").read_bytes()).decode("utf-8-sig"))


def _scramble_fires():
    """Each LaunchExt-logged shot of the scramble, as GsproState has it once the round is over."""
    st, got = GsproState(), {}
    for ev in parse_events(gzip.decompress((FIXTURES / "scramble_1697_gspro.log.gz").read_bytes()).decode()):
        st.apply(ev)
        for n in range(max(1, st.fires - 3), st.fires + 1):   # (it keeps only the latest fires' info)
            if info := st.fire_info(n):
                got[n] = dict(info)
    return st, got


def _booth_tracks(name):
    """(booth shot, the one stroke of GSPro's file it is): tied by ball speed, start direction, launch
    angle (none read on a putt) and player."""
    entries = _real(name)
    by_id = {str(e["ShotID"]): e for e in entries}
    got = round_file.tracks(entries)
    out = []
    for b in BOOTH[name]:
        hits = [t for t in got if t.player == b["player"].split()[0] and (v := round_file.launch_of(by_id[t.shot_id]))
                and abs(v["sp"] - b["ball"]) <= 0.06 and abs(v["az"] - b["start"]) <= 0.06
                and (b["launch"] is None or abs(v["el"] - b["launch"]) <= 0.06)]
        assert len(hits) == 1, (b, [(t.hole, t.player, t.shot_number) for t in hits])
        out.append((b, hits[0]))
    assert len({t.shot_id for _, t in out}) == len(out) == len(got)
    return out


def test_a_real_single_players_round_file_has_each_stroke_once():
    got = round_file.tracks(_real("single_1696"))   # 37 entries: 9 the copies of a hole's last stroke
    assert len(got) == 28 and Counter(t.hole for t in got) == SINGLE_HOLES
    for h in SINGLE_HOLES:
        strokes = [t for t in got if t.hole == h]
        assert [t.shot_number for t in strokes] == list(range(1, len(strokes) + 1))
        assert [t.dropped for t in strokes] == [False] * (len(strokes) - 1) + [True]
    _booth_tracks("single_1696")


def test_a_scramble_holing_out_copies_its_ball_onto_every_teammate_not_a_stroke():
    entries = _real("scramble_1697")
    got = round_file.tracks(entries)   # 68 entries: 16 copies, one per teammate per hole
    assert len(got) == 52 and Counter(t.hole for t in got) == SCRAMBLE_HOLES
    for h in SCRAMBLE_HOLES:
        for p in ("Josh", "Corey"):
            assert [t.shot_number for t in got if t.hole == h and t.player == p] == \
                list(range(1, (SCRAMBLE_HOLES[h] + (p == "Josh")) // 2 + 1))
    # The stroke that ended the hole still did: holed (in the cup), or in the gimme circle (a stroke
    # more); the team's score is GSPro's.
    last = {h: max((t for t in got if t.hole == h), key=lambda t: (t.shot_number, t.dropped)) for h in SCRAMBLE_HOLES}
    assert all(t.dropped for t in last.values()) and sum(t.dropped for t in got) == 9
    assert {h: t.shot_number + (not t.in_cup) for h, t in last.items()} == SCRAMBLE_SCORES
    st, fires = _scramble_fires()
    assert {h + 1: n for (_, h), n in st.hole_scores.items()} == SCRAMBLE_SCORES
    # Each shot GSPro logged is one stroke's ball (its LaunchExt line), the player it credits.
    assert len(fires) == 52
    roster = round_file.slot_players(((f["player"], f["launch"]) for f in fires.values()), entries)
    assert roster == {0: "Josh", 1: "Corey"}
    for f in fires.values():
        balls = round_file.ball_entries(f["launch"], entries)
        assert [(e["PlayerName"], e["Hole"]) for e in balls] == [(roster[f["player"]], f["hole"])]
    assert {str(e["ShotID"]) for f in fires.values() for e in round_file.ball_entries(f["launch"], entries)} \
        == {t.shot_id for t in got}
    # And each the booth recorded.
    assert len(_booth_tracks("scramble_1697")) == 52
    assert round_file._of_round(entries, ["Josh", "Corey"], [b["ball"] for b in BOOTH["scramble_1697"]],
                                "Bethpage Black")


def test_every_surface_code_in_the_real_round_files_names_a_lie():
    for name in ("single_1696", "scramble_1697"):
        for e in _real(name):
            for k in ("StartingSurface", "EndingSurface"):
                assert round_file.SURFACES.get(e[k]), (name, k, e[k])
    assert sorted(round_file.SURFACES) == list(range(23))   # GSPro's Trajectory.PhyMatList
    assert round_file.SURFACES[0] is None and round_file.SURFACES[19] is None   # no surface: the screen's read
    got = round_file.tracks([{**APPROACH, "StartingSurface": 12, "EndingSurface": 16}])[0]
    assert (got.lie_before, got.lie_after) == ("semi rough", "pine straw")


def _scramble_store(tmp_path, club=lambda b: b["club"], entries=None):
    """The scramble's 52 booth shots stored (the club as `club` has it) and GSPro's file applied to them:
    (the store, booth shot -> its club and putt or not). `entries`: the file as it was part way through."""
    st = Store(str(tmp_path / "season.db"))
    pairs = _booth_tracks("scramble_1697")
    for i, (b, t) in enumerate(pairs):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt,"
                      " ball_speed, raw) VALUES (?, ?, ?, 'Bethpage Black', ?, ?, ?, ?, ?, '{}')",
                      (st.session_id, 1000.0 + i, b["player"], f"Hole {t.hole}", t.shot_number, club(b),
                       int(b["putt"]), b["ball"]))
    st.db.commit()
    got = round_file.tracks(_real("scramble_1697") if entries is None else entries)
    written, pending = st.apply_round_file(got, now=1000.0 + len(pairs))
    assert (written, pending) == (len(got), [])
    return st, _clubs(st, pairs)


def _clubs(st, pairs):
    return {b["shot"]: tuple(st.db.execute("SELECT club, is_putt FROM shots WHERE ts = ?", (1000.0 + i,)).fetchone())
            for i, (b, _) in enumerate(pairs)}


def test_the_screens_club_stands_over_the_one_gspro_selected_at_rest(tmp_path):
    # Booth shot 51 is Corey's lob-wedge chip-in on hole 4: GSPro's file has the putter (a hole-out
    # selects it). Shots 65 and 66, hole 7's second shots off the fairway, are 3-woods it has as drivers.
    entries = _real("scramble_1697")
    chip = next(t for t in round_file.tracks(entries) if (t.hole, t.player, t.shot_number) == (4, "Corey", 4))
    assert (chip.club, chip.in_cup, chip.putt) == ("PT", True, False)
    assert round_file.club_fix(chip, "LW") is None and round_file.club_fix(chip, None) is None
    assert {t.club for t in round_file.tracks(entries) if (t.hole, t.shot_number) == (7, 2)} == {"DR"}
    st, rows = _scramble_store(tmp_path)
    assert rows[51] == ("LW", 0)
    assert rows[65] == rows[66] == ("3W", 0)
    assert all(rows[b["shot"]][0] == b["club"] for b in BOOTH["scramble_1697"])


def test_gspros_club_fills_a_club_the_screen_missed_but_not_on_the_stroke_that_ended_the_hole(tmp_path):
    # Booth shot 29 is Josh's drive on hole 1; 51 Corey's chip-in (the file's putter).
    st, rows = _scramble_store(tmp_path, club=lambda b: None if b["shot"] in (29, 51) else b["club"])
    assert rows[29] == ("DR", 0)
    assert rows[51] == (None, 0)


def test_a_club_gspros_file_filled_goes_back_once_the_hole_ends_on_that_stroke(tmp_path):
    # Corey's second stroke on hole 8, a lag the team took the gimme on: read before GSPro's hole-out
    # copies of it, it has a club the file can give; once they're in, it hasn't.
    entries = _real("scramble_1697")
    lag = next(i for i, e in enumerate(entries) if (e["Hole"], e["PlayerName"], e["HoleShot"]) == (7, "Corey", 2))
    shot = next(b["shot"] for b, t in _booth_tracks("scramble_1697") if (t.hole, t.player, t.shot_number) == (8, "Corey", 2))
    st, rows = _scramble_store(tmp_path, club=lambda b: None if b["shot"] == shot else b["club"], entries=entries[:lag + 1])
    assert rows[shot] == ("PT", 1)
    st.apply_round_file(round_file.tracks(entries), now=2000.0)
    assert _clubs(st, _booth_tracks("scramble_1697"))[shot] == (None, 1)
    # (with no club to say, GSPro's own call at impact: a putt, here, as the fill had it)
    st.apply_round_file([replace(t, putt=False) if (t.hole, t.player, t.shot_number) == (8, "Corey", 2) else t
                         for t in round_file.tracks(entries)], now=2001.0)
    assert _clubs(st, _booth_tracks("scramble_1697"))[shot] == (None, 0)


def test_a_club_gspros_file_set_over_the_screens_goes_back_when_the_file_is_read_again(tmp_path):
    # Corey's lob-wedge chip-in on hole 4 (booth shot 51), as the booth's earlier versions stored it:
    # GSPro's putter over the screen's lob wedge, and a putt, with a snapshot of the screen's club but
    # not the putt (they kept that only when the file's club changed it).
    st, rows = _scramble_store(tmp_path)
    sid = st.db.execute("SELECT id FROM shots WHERE club = 'LW' AND ball_speed = 19.5").fetchone()[0]
    was = json.loads(st.db.execute("SELECT was FROM shot_tracks WHERE shot_id = ?", (sid,)).fetchone()[0])
    assert was["club"] == "LW"
    was.pop("is_putt", None)
    st.db.execute("UPDATE shot_tracks SET was = ? WHERE shot_id = ?", (json.dumps(was), sid))
    st.db.execute("UPDATE shots SET club = 'PT', is_putt = 1 WHERE id = ?", (sid,))
    st.apply_round_file(round_file.tracks(_real("scramble_1697")), now=2000.0)
    assert tuple(st.db.execute("SELECT club, is_putt FROM shots WHERE id = ?", (sid,)).fetchone()) == ("LW", 0)


def test_a_club_gspros_file_fills_drops_the_zeros_its_monitor_writes_for_fields_that_club_doesnt_measure(tmp_path):
    # Josh's drive on hole 1 (booth shot 29), read with no club: his monitor measures club speed and
    # path with his driver, not face angle, which it sends as 0. (Each scramble swing is its player's:
    # only the team's ball is shared.)
    shot = 29
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, club, is_putt, club_speed, club_path, face_to_target, raw)"
                  " VALUES (?, 1.0, 'Josh', 'DR', 0, 110.0, 2.0, 0.0, '{}')", (st.session_id,))
    pairs = _booth_tracks("scramble_1697")
    for i, (b, t) in enumerate(pairs):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt,"
                      " ball_speed, club_speed, club_path, face_to_target, fmt, shared_ball, raw)"
                      " VALUES (?, ?, ?, 'Bethpage Black', ?, ?, ?, ?, ?, ?, ?, ?, 'scramble', 1, '{}')",
                      (st.session_id, 1000.0 + i, b["player"], f"Hole {t.hole}", t.shot_number,
                       None if b["shot"] == shot else b["club"], int(b["putt"]), b["ball"],
                       *((112.0, 1.5, 0.0) if b["shot"] == shot else (None, None, None))))
    st.db.commit()
    i = next(i for i, (b, _) in enumerate(pairs) if b["shot"] == shot)
    fields = lambda: tuple(st.db.execute("SELECT club, club_speed, club_path, face_to_target FROM shots WHERE ts = ?",
                                         (1000.0 + i,)).fetchone())
    tracks = round_file.tracks(_real("scramble_1697"))
    st.apply_round_file(tracks, now=2000.0)
    assert fields() == ("DR", 112.0, 1.5, None)
    drive = next(t for t in tracks if (t.hole, t.player, t.shot_number) == (1, "Josh", 1))
    st.apply_round_file([replace(t, dropped=True) if t is drive else t for t in tracks], now=2001.0)
    assert fields() == (None, 112.0, 1.5, 0.0)


def test_the_store_drops_a_filled_clubs_fields_as_the_live_path_does(tmp_path):
    # Josh's driver measures club speed only: a shot with none (a misread) teaches nothing, a blind
    # shot's are checked against the player the file names it for, and a field his driver doesn't
    # measure that reads 0 goes.
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, club, is_putt, club_speed, raw)"
                  " VALUES (?, 1.0, 'Josh', 'DR', 0, 110.0, '{}')", (st.session_id,))
    row = lambda **cols: {"player": None, "shared_ball": 0, "fmt": None, "club_speed": None,
                          **{c: None for c in CLUB_COLS}, **cols}
    assert st._unmeasured(row(club_path=3.0), "DR", "Josh") == ["club_path"]
    assert st._unmeasured(row(club_speed=105.0, face_to_target=0.0), "DR", "Josh") == ["face_to_target"]
    assert st._unmeasured(row(club_speed=105.0, face_to_target=0.0), "DR", None) == []


def test_gspros_full_shot_call_names_a_blind_slow_chip_a_full_shot():
    t = round_file.Track("s1", None, None, "Josh", 4, 4, 19.5, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                         club="PT", in_cup=True, dropped=True)
    row = {"hole": None, "is_putt": 1}
    assert Store._blind_named(row, t, lambda n: n, None)["is_putt"] == 0
    assert Store._blind_named(row, replace(t, putt=None), lambda n: n, None)["is_putt"] == 1


@pytest.mark.parametrize("at_rest", ["LW", "PT"])
def test_a_blind_chip_named_and_filled_then_holed_keeps_gspros_full_shot_call(tmp_path, at_rest):
    # Recorded blind with no club, the chip's slow speed guessed a putt: GSPro's file names it Josh's
    # chip (not a putt, its isPutt says) with its club at rest, then the hole ends on it, which takes
    # GSPro's club back. A putter at rest makes it a putt while it stands, not once it's taken back.
    st = Store(str(tmp_path / "season.db"))
    for n, sp in enumerate([151.0, 100.2, 12.0]):
        st.db.execute("INSERT INTO shots (session_id, ts, is_putt, ball_speed, attributed, raw)"
                      " VALUES (?, ?, ?, ?, 0, '{}')", (st.session_id, 1000.0 + n, int(sp < 20), sp))
    st.db.commit()
    mk = lambda n, sp, lie, **kw: round_file.Track(f"s{n}", 1, None, "Josh", 1, n, sp, False, (0.0, 0.0, 0.0),
                                                   (0.0, 0.0, 1.0), None, lie_before=lie, lie_after="green",
                                                   club={1: "DR", 2: "8I", 3: at_rest}[n], **kw)
    tracks = [mk(1, 151.0, "tee"), mk(2, 100.2, "fairway"), mk(3, 12.0, "rough")]
    st.apply_round_file(tracks, now=1005.0, name_of=str)
    chip = lambda: tuple(st.db.execute("SELECT player, club, is_putt FROM shots WHERE ball_speed = 12.0").fetchone())
    assert chip() == ("Josh", at_rest, int(at_rest == "PT"))
    sid = st.db.execute("SELECT id FROM shots WHERE ball_speed = 12.0").fetchone()[0]
    assert st.screen_putt(sid) is False   # (how a hole-out on it settles: a full shot's)
    st.apply_round_file([*tracks[:2], replace(tracks[2], holed=True, dropped=True, in_cup=True)], now=1006.0,
                        name_of=str)
    assert chip() == ("Josh", None, 0)


@pytest.mark.parametrize("fmt", ["scramble", "alternate"])
def test_a_blind_shot_the_file_names_drops_its_zeros_only_when_the_swing_is_the_named_players(tmp_path, fmt):
    # A scramble's swing is its player's; an alternate shot's ball is the side's, whichever partner hit it.
    st = Store(str(tmp_path / "season.db"))
    for n, sp in enumerate([151.0, 100.2, 60.0]):
        st.db.execute("INSERT INTO shots (session_id, ts, is_putt, ball_speed, club_speed, club_path, fmt, shared_ball,"
                      " attributed, raw) VALUES (?, ?, 0, ?, 90.0, 0.0, ?, 1, 0, '{}')", (st.session_id, 1000.0 + n, sp, fmt))
    st.db.commit()
    tracks = [round_file.Track(f"s{n}", 1, None, "Josh", 1, n, sp, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                               lie_before="fairway", lie_after="rough", club="7I")
              for n, sp in enumerate([151.0, 100.2, 60.0], start=1)]
    st.apply_round_file(tracks, now=1005.0, name_of=str)
    paths = [r[0] for r in st.db.execute("SELECT club_path FROM shots ORDER BY ts")]
    assert paths == ([None] * 3 if fmt == "scramble" else [0.0] * 3)


def test_blind_shots_the_file_names_learn_their_club_fields_in_the_order_they_were_hit(tmp_path):
    # Josh's driver, new to the booth: two real club speeds, then a misread with a speed of 0.
    st = Store(str(tmp_path / "season.db"))
    for n, (sp, cs, path) in enumerate([(151.0, 100.0, 0.0), (149.0, 97.0, 0.0), (150.0, 0.0, 2.0)]):
        st.db.execute("INSERT INTO shots (session_id, ts, is_putt, ball_speed, club_speed, club_path, attributed, raw)"
                      " VALUES (?, ?, 0, ?, ?, ?, 0, '{}')", (st.session_id, 1000.0 + n, sp, cs, path))
    st.db.commit()
    tracks = [round_file.Track(f"s{n}", 1, None, "Josh", n, 1, sp, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                               lie_before="tee", lie_after="fairway", club="DR")
              for n, sp in enumerate([151.0, 149.0, 150.0], start=1)]
    st.apply_round_file(tracks, now=1005.0, name_of=str)
    assert [tuple(r) for r in st.db.execute("SELECT club_speed, club_path FROM shots ORDER BY ts")] == \
        [(100.0, None), (97.0, None), (None, None)]


def test_gspros_putt_call_decides_a_shot_with_no_club_and_goes_with_its_stroke(tmp_path):
    # A 21 mph lag the screen read no club for, its launch too quick for a putt: GSPro's file says putt
    # (holed, so its club at rest is no help), until the stroke turns out not to be this shot's.
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, hole, shot_number, is_putt, ball_speed, raw)"
                  " VALUES (?, 1000.0, 'Josh', 'Hole 1', 3, 0, 21.0, '{}')", (st.session_id,))
    st.db.commit()
    t = round_file.Track("s3", 1, None, "Josh", 1, 3, 21.0, True, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                         lie_before="green", lie_after="green", club="PT", holed=True, dropped=True, in_cup=True)
    row = lambda: tuple(st.db.execute("SELECT club, is_putt FROM shots").fetchone())
    st.apply_round_file([t], now=1005.0)
    assert row() == (None, 1)
    st.db.execute("UPDATE shots SET outcome = 'missed'")   # (graded a putt meanwhile)
    st.apply_round_file([], now=1005.0, gone={"s3"})
    assert row() == (None, 0)
    assert st.db.execute("SELECT outcome FROM shots").fetchone()[0] is None


def test_gspros_full_shot_call_on_a_guessed_putt_takes_its_miss_until_released(tmp_path):
    # A 12 mph chip the booth guessed a putt and graded a miss, its club unread (an empty box).
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, hole, shot_number, club, is_putt, outcome, ball_speed,"
                  " raw) VALUES (?, 1000.0, 'Josh', 'Hole 1', 3, '', 1, 'missed', 12.0, '{}')", (st.session_id,))
    st.db.commit()
    t = round_file.Track("s3", 1, None, "Josh", 1, 3, 12.0, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                         lie_before="rough", lie_after="rough")
    row = lambda: tuple(st.db.execute("SELECT is_putt, outcome FROM shots").fetchone())
    st.apply_round_file([t], now=1005.0)
    assert row() == (0, None)
    st.apply_round_file([], now=1005.0, gone={"s3"})
    assert row() == (1, "missed")


def test_gspros_putt_call_on_a_full_shot_that_left_a_gimme_makes_it_a_conceded_putt_until_released(tmp_path):
    # A 21 mph roll from 45 feet the booth took for a chip (its club unread), inside the gimme circle.
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, hole, shot_number, club, is_putt, ball_speed, raw)"
                  " VALUES (?, 1000.0, 'Josh', 'Hole 1', 3, NULL, 0, 21.0, '{}')", (st.session_id,))
    st.db.commit()
    sid = st.db.execute("SELECT id FROM shots").fetchone()[0]
    st.record_gimme(sid, 0.5)
    t = round_file.Track("s3", 1, None, "Josh", 1, 3, 21.0, True, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                         lie_before="green", lie_after="green", dropped=True)
    row = lambda: tuple(st.db.execute("SELECT is_putt, outcome FROM shots WHERE id = ?", (sid,)).fetchone())
    st.apply_round_file([t], now=1005.0)
    assert row() == (1, "conceded")
    st.apply_round_file([], now=1005.0, gone={"s3"})
    assert row() == (0, None)


def test_gspros_full_shot_call_on_a_conceded_guessed_putt_leaves_the_concession_to_its_gimme(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, hole, shot_number, club, is_putt, outcome, ball_speed,"
                  " raw) VALUES (?, 1000.0, 'Josh', 'Hole 1', 3, '', 1, 'conceded', 12.0, '{}')", (st.session_id,))
    st.db.commit()
    t = round_file.Track("s3", 1, None, "Josh", 1, 3, 12.0, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                         lie_before="rough", lie_after="green", dropped=True)
    row = lambda: tuple(st.db.execute("SELECT is_putt, outcome FROM shots").fetchone())
    st.apply_round_file([t], now=1005.0)
    assert row() == (0, None)
    st.apply_round_file([], now=1005.0, gone={"s3"})
    assert row() == (1, "conceded")


def test_a_concession_graded_under_gspros_putt_call_follows_the_putt_or_not_put_back(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    for n, putt in ((3, 0), (5, 1)):   # a chip the booth guessed (club unread), and a guessed putt
        st.db.execute("INSERT INTO shots (session_id, ts, player, hole, shot_number, club, is_putt, ball_speed, raw)"
                      " VALUES (?, ?, 'Josh', 'Hole 1', ?, NULL, ?, 12.0, '{}')", (st.session_id, 1000.0 + n, n, putt))
    st.db.commit()
    chip, putt = (i for (i,) in st.db.execute("SELECT id FROM shots ORDER BY id"))
    ts = [round_file.Track(f"s{n}", 1, None, "Josh", 1, n, 12.0, not p, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                           lie_before="green", lie_after="green") for n, p in ((3, 0), (5, 1))]
    st.apply_round_file(ts, now=1010.0)
    st.set_shot_result(chip, outcome="conceded")   # (graded meanwhile: the chip as GSPro's putt, the putt a chip)
    st.record_gimme(chip)
    st.record_gimme(putt)
    st.apply_round_file([], now=1010.0, gone={"s3", "s5"})
    row = lambda i: tuple(st.db.execute("SELECT is_putt, outcome FROM shots WHERE id = ?", (i,)).fetchone())
    assert (row(chip), row(putt)) == ((0, None), (1, "conceded"))


def test_a_concession_graded_under_gspros_full_shot_call_beats_the_miss_it_took_off(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    st.db.execute("INSERT INTO shots (session_id, ts, player, hole, shot_number, club, is_putt, outcome, ball_speed,"
                  " raw) VALUES (?, 1000.0, 'Josh', 'Hole 1', 3, '', 1, 'missed', 12.0, '{}')", (st.session_id,))
    st.db.commit()
    sid = st.db.execute("SELECT id FROM shots").fetchone()[0]
    t = round_file.Track("s3", 1, None, "Josh", 1, 3, 12.0, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                         lie_before="green", lie_after="green")
    st.apply_round_file([t], now=1005.0)
    st.record_gimme(sid)   # (the team picked the ball for a gimme meanwhile)
    st.apply_round_file([], now=1005.0, gone={"s3"})
    assert tuple(st.db.execute("SELECT is_putt, outcome FROM shots WHERE id = ?", (sid,)).fetchone()) \
        == (1, "conceded")


def test_gspros_full_shot_call_naming_a_blind_guessed_putt_takes_its_miss(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    for n, (sp, putt, out) in enumerate([(151.0, 0, None), (100.2, 0, None), (12.0, 1, "missed")]):
        st.db.execute("INSERT INTO shots (session_id, ts, is_putt, outcome, ball_speed, attributed, raw)"
                      " VALUES (?, ?, ?, ?, ?, 0, '{}')", (st.session_id, 1000.0 + n, putt, out, sp))
    st.db.commit()
    tracks = [round_file.Track(f"s{n}", 1, None, "Josh", 1, n, sp, False, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), None,
                               lie_before="fairway", lie_after="rough")
              for n, sp in enumerate([151.0, 100.2, 12.0], start=1)]
    st.apply_round_file(tracks, now=1005.0, name_of=str)
    chip = lambda: tuple(st.db.execute("SELECT player, is_putt, outcome FROM shots WHERE ball_speed = 12.0").fetchone())
    assert chip() == ("Josh", 0, None)
    st.apply_round_file(tracks[:2], now=1005.0, gone={"s3"}, name_of=str)
    assert chip() == (None, 1, "missed")

