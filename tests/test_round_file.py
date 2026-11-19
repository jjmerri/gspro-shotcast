"""GSPro's round file (currentRound.dat, 3.59+) applied to the stored shots and drawn in hole replays."""
import json
import math
from pathlib import Path

from commentator import round_file
from commentator.hole_replay import hole_replays
from commentator.sources.gspro_log import M_TO_YD
from commentator.store import Store

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
            [(150.2, 0, None, "semi rough", 210.0), (99.8, 0, None, "fairway", 90.0), (8.0, 1, "holed", None, 2.0)], 1):
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
    # The HUD's finer lie stays; a plain one gives way to GSPro's.
    assert (drive["lie_after"], approach["lie_before"], approach["lie_after"]) == ("semi rough", "fairway", "green")
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
    assert st.set_lie_after(approach["id"], "pine straw") == "green"   # not a finer word for GSPro's green
    # A finer read that isn't a word for GSPro's lie (the screen's fringe for a ball in the sand) goes.
    st.db.execute("UPDATE shots SET lie_after = 'pine straw' WHERE id = ?", (approach["id"],))
    st.apply_round_file(round_file.tracks([DRIVE, APPROACH, PUTT]), now=1005.0)
    assert _row(st, 2)["lie_after"] == "green"
    st.set_lie_after(approach["id"], "fringe")   # finer than GSPro's code
    assert _row(st, 2)["lie_after"] == "fringe"


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
    al = _entry(1, (0, 0), (5, 200), sp=149.8, name="Al", flight=[(0, 0), (4, 180)], land=(4, 180), to_pin=99.0)
    files = [{**DRIVE, "GhostData": {**DRIVE["GhostData"], "sp": 150.9}, "HolePar": 4}, al, APPROACH, PUTT]
    written, pending = st.apply_round_file(round_file.tracks(files), now=1005.0, name_of={"Dan": "Dan Smith"}.get)
    assert (written, pending) == (4, [])
    rows = st.db.execute("SELECT player, hole, shot_number, is_putt, attributed, finish_yards FROM shots ORDER BY ts").fetchall()
    assert [tuple(r)[:5] for r in rows] == [("Dan Smith", "Hole 1, par 4", 1, 0, 1), ("Al", "Hole 1", 1, 0, 1),
                                            ("Dan Smith", "Hole 1", 2, 0, 1), ("Dan Smith", "Hole 1", 3, 1, 1)]
    assert rows[3]["finish_yards"] is None   # a putt keeps no finish
    # Applied again, by name now; a mulligan taking a stroke back gives its shot back its blind read.
    assert st.apply_round_file(round_file.tracks(files), now=1005.0, name_of={"Dan": "Dan Smith"}.get)[1] == []
    st.apply_round_file(round_file.tracks(files[:3]), now=1005.0, gone={PUTT["ShotID"]})
    assert tuple(st.db.execute("SELECT player, hole, shot_number, attributed FROM shots WHERE ball_speed = 8.0")
                 .fetchone()) == (None, "Hole 18, par 4, 404 yards", None, 0)


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


def test_a_finer_hud_lie_is_kept_only_inside_gspros():
    assert round_file.finer_lie("rough", "semi rough") == "semi rough"
    assert round_file.finer_lie("green", "fringe") == "fringe"
    assert round_file.finer_lie("sand", "fringe") == "sand"         # it rolled on from the fringe
    assert round_file.finer_lie("fairway", "rough") == "fairway"    # not finer: GSPro's stands
    assert round_file.finer_lie("fairway", None) == "fairway"
    assert round_file.finer_lie(None, "structure") == "structure"   # a surface GSPro's code isn't known for


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


def test_gspros_club_index_corrects_the_club_read_off_the_screen(tmp_path, caplog):
    st = _season(tmp_path)   # every shot read as a 7-iron
    drive, approach, putt = ({**e, "ClubIndex": i} for e, i in ((DRIVE, 0), (APPROACH, 19), (PUTT, 26)))
    got = round_file.tracks([drive, approach, putt])
    assert [t.club for t in got] == ["DR", "7I", "PT"]
    with caplog.at_level("INFO"):
        st.apply_round_file(got, now=1005.0)
    assert [_row(st, n)["club"] for n in (1, 2, 3)] == ["DR", "7I", "PT"]
    assert "club 7I -> DR from GSPro's round file" in caplog.text
    # A match stored before clubs were tracked keeps no club to go back to: the one it replaces is added.
    sid = _row(st, 1)["id"]
    st.db.execute("UPDATE shots SET club = '7I' WHERE id = ?", (sid,))
    was = json.loads(st.db.execute("SELECT was FROM shot_tracks WHERE shot_id = ?", (sid,)).fetchone()[0])
    was.pop("club")
    st.db.execute("UPDATE shot_tracks SET was = ? WHERE shot_id = ?", (json.dumps(was), sid))
    st.apply_round_file(got, now=1005.0)
    assert _row(st, 1)["club"] == "DR"
    st._release_track(sid)
    assert _row(st, 1)["club"] == "7I"
    # A putt the booth took for a full shot (no putter on the screen): GSPro's putter makes it one.
    pid = _row(st, 3)["id"]
    st._release_track(pid)
    st.db.execute("UPDATE shots SET club = '7I', is_putt = 0 WHERE id = ?", (pid,))
    st.apply_round_file(got, now=1005.0)
    assert (_row(st, 3)["club"], _row(st, 3)["is_putt"]) == ("PT", 1)
    st._release_track(pid)
    assert (_row(st, 3)["club"], _row(st, 3)["is_putt"]) == ("7I", 0)
    st.apply_round_file(got[1:], now=1005.0, gone={drive["ShotID"]})   # taken back (a mulligan): the read returns
    assert _row(st, 1)["club"] == "7I"
    # The screen's 3-wood for GSPro's driver off the deck (hud.deck_club), and its approach wedge for the
    # gap wedge, aren't disagreements.
    deck = round_file.tracks([{**APPROACH, "ClubIndex": 0}])[0]
    assert round_file.club_fix(deck, "3W") is None and round_file.club_fix(deck, "5W") == "3W"
    gap = round_file.tracks([{**APPROACH, "ClubIndex": 23}])[0]
    assert round_file.club_fix(gap, "AW") is None and round_file.club_fix(gap, None) == "GW"
    assert round_file.club_fix(round_file.tracks([{**APPROACH, "ClubIndex": 27}])[0], "7I") is None
