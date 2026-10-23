"""Hole replays from logged ball positions, and filling older shots' positions from a saved log."""
import math

from commentator.hole_replay import estimate_pin, hole_image_sources, hole_replays
from commentator.log_backfill import backfill
from commentator.sources.gspro_log import M_TO_YD
from commentator.store import Store

SHOT = "* ------- Shot Fired! ------- *\n"
LAYOUT = ('[{"active":true,"played":true,"par":4,"pinPos":{"x":0.0,"y":1.0,"z":300.0},'
          '"teePos":{"x":0.0,"y":2.0,"z":0.0},"ShotWPPos0":{"x":0.0,"y":0.0,"z":200.0}}]\n')


def _state(hole, strokes, prev):
    return f"ActivePlayer: 0  currentHole: {hole}  strokes: {strokes}  Previous Score: {prev}\n"


def _setup(x, z, to_pin_m):
    return (f"Selecting shot for player: 0\nData for selected player: X {x} Y 1.0 Z {z}\n"
            f"Logging tdist: {to_pin_m} and ActiveGameGimmieDistance: 1.22\n")


def _hole_log(course="Test Links"):
    """A par 4 played tee (0,0) -> (10,200) -> (2,295) -> holed, with an aim click before the approach."""
    return (f"Selected Course: test_gsp\nLoading course {course}\n" + SHOT + _state(0, 1, 0)
            + _setup(10.0, 200.0, round(math.hypot(10, 100), 4)) + "(-40.0, 339.0, 0.0)\n" + SHOT + _state(0, 1, 1)
            + _setup(2.0, 295.0, round(math.hypot(2, 5), 4)) + SHOT + "Hole out !!! \n" + _state(0, 1, 2)
            + "Round Is Complete\n" + LAYOUT)


def _season(tmp_path, course="Test Links"):
    """A season file holding the hole's three shots with no positions, and its round."""
    path = str(tmp_path / "season.db")
    st = Store(path)
    for n, (to_pin, putt, outcome) in enumerate([(300 * M_TO_YD, 0, None), (math.hypot(10, 100) * M_TO_YD, 0, None),
                                                  (math.hypot(2, 5) * M_TO_YD, 1, "holed")], 1):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt,"
                      " to_pin_yards, outcome, raw) VALUES (?, ?, 'Dan', ?, 'Hole 1, par 4, 328 yards', ?, ?, ?, ?, ?, '{}')",
                      (st.session_id, 1000.0 + n, course, n, "PT" if putt else "7I", putt, round(to_pin, 1), outcome))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, 2000, '2025-10-20', ?, 'Dan', 1)",
                  (st.session_id, course))
    st.db.commit()
    return path, st


def test_backfill_fills_positions_aims_and_layout_once(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    assert backfill(path, [str(log)], dry_run=True)["starts"] == 2
    assert st.db.execute("SELECT COUNT(*) FROM shots WHERE start_x IS NOT NULL").fetchone()[0] == 0
    got = backfill(path, [str(log)])
    assert (got["matched"], got["starts"], got["aims"], got["layouts"]) == (3, 2, 1, 1)
    rows = st.db.execute("SELECT start_x, start_z, aim_map_x FROM shots ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(None, None, None), (10.0, 200.0, -40.0), (2.0, 295.0, None)]
    lo = st.db.execute("SELECT session_id, hole, tee_z, pin_z FROM hole_layouts").fetchall()
    assert [tuple(r) for r in lo] == [(st.session_id, 1, 0.0, 300.0)]
    assert backfill(path, [str(log)]) == {**got, "starts": 0, "aims": 0, "layouts": 0}


def test_backfill_ignores_a_log_of_rounds_the_season_never_recorded(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log().replace(f"tdist: {round(math.hypot(10, 100), 4)}", "tdist: 40.0")
                   .replace(f"tdist: {round(math.hypot(2, 5), 4)}", "tdist: 3.0"))
    got = backfill(path, [str(log)])
    assert got["starts"] == 0 and got["layouts"] == 1      # the hole's tee is still worth keeping
    assert st.db.execute("SELECT session_id FROM hole_layouts").fetchone()[0] is None


def test_replay_runs_each_shot_to_the_next_and_the_last_into_the_pin(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    rp = hole_replays(st.db)[(1, 1)]
    assert (rp["tee"], rp["pin"], rp["pin_estimated"], rp["par"]) == ([0.0, 0.0], [0.0, 300.0], False, 4)
    shots = rp["players"][0]["shots"]
    assert [(s["from"], s["to"]) for s in shots] == [([0.0, 0.0], [10.0, 200.0]), ([10.0, 200.0], [2.0, 295.0]),
                                                     ([2.0, 295.0], [0.0, 300.0])]
    # The approach aimed at the pin's distance, 40/677 of it to the left.
    d = math.hypot(10, 100)
    f = (-10 / d, 100 / d)
    want = (10.0 + f[0] * d - f[1] * 40 / 677 * d, 200.0 + f[1] * d + f[0] * 40 / 677 * d)
    assert math.dist(shots[1]["aim"], want) < 0.1


def test_a_replay_counts_the_gimme_it_cant_draw_in_the_score(tmp_path):
    path, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET outcome = 'conceded' WHERE shot_number = 3")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome, raw)"
                  " VALUES (?, 1004, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 4, 1, 'gimme', '{}')",
                  (st.session_id,))
    st.db.commit()
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    pl = hole_replays(st.db)[(1, 1)]["players"][0]
    assert (len(pl["shots"]), pl["strokes"], pl["gimmes"]) == (3, 4, 1)


def test_a_round_quit_early_estimates_its_pin_from_the_shots_distances(tmp_path):
    path, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    st.db.commit()
    rp = hole_replays(st.db)[(1, 1)]
    # Two balls nearly in line with the pin, with distances rounded to 0.1 yd: close, not exact.
    assert rp["pin_estimated"] and math.dist(rp["pin"], (0, 300)) < 2.0


def test_estimate_pin_is_none_when_the_distances_dont_agree():
    assert estimate_pin([((0.0, 0.0), 100.0), ((0.0, 50.0), 10.0)]) is None


def test_the_minimap_snapshot_finds_ball_and_pin_and_backs_this_sessions_replay(tmp_path):
    from PIL import Image

    from commentator.minimap import snapshot

    image, ball, pin = snapshot(Image.open("tests/fixtures/minimap_water_long.jpg"))
    # The red ball and the white pin on the fixture, below GSPro's trimmed title bar.
    assert math.dist(ball, (0.495, 0.755)) < 0.01 and math.dist(pin, (0.494, 0.238)) < 0.01
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    assert st.record_hole_image("Test Links", 1, b"x", None, None) is not None
    assert st.record_hole_image("Test Links", 1, image, ball, pin) is not None   # a found one replaces it
    # One map per course and hole serves every round there: a later session's isn't kept.
    assert st.record_hole_image("test links", 1, image, ball, pin, session_id=st.session_id + 1) is None
    m = hole_replays(st.db)[(1, 1)]["map"]
    assert not m["approx"] and m["ball"][1] > m["pin"][1]
    assert hole_image_sources(st.db, {m["id"]})[m["id"]].startswith("data:image/jpeg;base64,")


def test_opening_a_season_keeps_one_map_per_hole_preferring_one_with_the_ball_and_pin(tmp_path):
    path, st = _season(tmp_path)
    for sid, spots in ((1, (None, None)), (2, (0.5, 0.9)), (3, (0.5, 0.9))):
        st.db.execute("INSERT INTO hole_images (session_id, course, hole, image, ball_x, ball_y, pin_x, pin_y)"
                      " VALUES (?, 'Test Links', 1, x'00', ?, ?, ?, ?)", (sid, *spots, *spots))
    st.db.commit()
    db = Store(path, new_session=False).db
    assert [tuple(r) for r in db.execute("SELECT session_id FROM hole_images")] == [(2,)]
