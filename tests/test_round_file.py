"""GSPro's round file (currentRound.dat, 3.59+) applied to the stored shots and drawn in hole replays."""
import json
import math

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


def test_a_penalty_that_follows_a_stroke_takes_back_where_the_file_had_it_stop(tmp_path):
    st = _season(tmp_path)
    st.apply_round_file(round_file.tracks([APPROACH]), now=1005.0)
    assert _row(st, 2)["lie_after"] == "green"
    drop = _entry(3, (2, 295), (2, 295), sp=0.0, result=2)
    st.apply_round_file(round_file.tracks([APPROACH, drop]), now=1005.0)
    approach = _row(st, 2)
    assert (approach["total_yards"], approach["finish_yards"], approach["lie_after"]) == (None, None, None)
    assert approach["carry_game_yards"] == round(math.hypot(2.5, 90) * M_TO_YD, 1)


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
