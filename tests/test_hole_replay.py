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


def test_fixing_starts_corrects_positions_taken_from_another_players_ball(tmp_path):
    path, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 50, start_z = 180 WHERE shot_number IN (1, 2)")   # the old shift
    st.db.commit()
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    assert backfill(path, [str(log)])["fixed"] == 0                  # left alone unless asked
    got = backfill(path, [str(log)], fix_starts=True)
    assert got["fixed"] == 2
    rows = st.db.execute("SELECT start_x, start_z FROM shots ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(None, None), (10.0, 200.0), (2.0, 295.0)]
    # And a tee shot is drawn from the tee whatever position was stored for it.
    st.db.execute("UPDATE shots SET start_x = 50, start_z = 180 WHERE shot_number = 1")
    st.db.commit()
    assert hole_replays(st.db)[(1, 1)]["players"][0]["shots"][0]["from"] == [0.0, 0.0]


def test_a_replay_keeps_its_own_rounds_tee_when_a_later_round_played_other_tees(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (50.0, 2.0, 50.0),
                                          "pin": (0.0, 1.0, 300.0)}], session_id=st.session_id + 1, ts=9e9)
    assert hole_replays(st.db)[(1, 1)]["tee"] == [0.0, 0.0]


def test_backfill_keeps_a_later_unrecorded_rounds_layout_off_an_earlier_round(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    # The recorded round is quit before its layout is logged; a round never recorded follows, to its end.
    other = LAYOUT.replace('"z":300.0', '"z":350.0')
    log.write_text(_hole_log().replace("Round Is Complete\n" + LAYOUT, "End Round Button Click\n")
                   + "Selected Course: test_gsp\nLoading course Test Links\n" + SHOT + _state(0, 1, 0)
                   + "Round Is Complete\n" + other)
    backfill(path, [str(log)])
    assert [tuple(r) for r in st.db.execute("SELECT session_id, pin_z FROM hole_layouts")] == [(None, 350.0)]


def test_a_round_quit_before_its_layout_doesnt_take_the_next_rounds_pin(tmp_path):
    path, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    # The same session plays the hole again later, to the end, and that round logs a different pin.
    for n, to_pin in enumerate([300.0, 100.0], 1):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt,"
                      " to_pin_yards, raw) VALUES (?, ?, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', ?, '7I', 0, ?, '{}')",
                      (st.session_id, 5000.0 + n, n, to_pin))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, 6000, '2025-10-20',"
                  " 'Test Links', 'Dan', 1)", (st.session_id,))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (0.0, 1.0, 350.0)}],
                          session_id=st.session_id, ts=5003.0)
    rp = hole_replays(st.db)[(1, 1)]
    assert rp["pin_estimated"] and math.dist(rp["pin"], (0, 300)) < 2.0


def test_a_hole_gspro_capped_at_its_maximum_shows_the_card_score(tmp_path):
    # DPC Sodgrass, 11-06: Evan hit his 8th stroke after three balls in the water, but the par 3's
    # maximum put 7 on the card. The replay gives the card's score, marked; an uncapped hole is unchanged.
    _, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295, outcome = NULL WHERE shot_number = 3")
    st.db.commit()
    assert hole_replays(st.db)[(1, 1)]["players"][0]["strokes"] == 3
    st.db.execute("UPDATE rounds SET scores = '{\"1\": 2}'")
    st.db.commit()
    dan = hole_replays(st.db)[(1, 1)]["players"][0]
    assert (dan["strokes"], dan.get("capped")) == (2, True)
    st.db.execute("UPDATE rounds SET scores = '{\"1\": 4}'")
    st.db.commit()
    dan = hole_replays(st.db)[(1, 1)]["players"][0]
    assert (dan["strokes"], dan.get("capped")) == (3, None)


def test_the_same_pins_from_new_tees_later_in_the_session_are_kept_and_repeats_are_not(tmp_path):
    _, st = _season(tmp_path)
    hole = {"par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (0.0, 1.0, 300.0)}
    add = lambda h, ts: st.record_hole_layout("Test Links", [h], session_id=st.session_id, ts=ts)  # noqa: E731
    assert (add(hole, 1500.0), add(hole, 1500.0), add(hole, 1600.0)) == (1, 0, 0)   # one round's repeats
    assert add({**hole, "tee": (50.0, 2.0, 50.0)}, 1700.0) == 1                      # other tees, same pin
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, raw)"
                  " VALUES (?, 1800, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 1, 0, '{}')", (st.session_id,))
    assert add(hole, 1900.0) == 1                                                    # played again since


def test_an_ace_with_no_stored_position_is_replayed_from_the_tee_into_the_pin(tmp_path):
    _, st = _season(tmp_path)
    st.db.execute("DELETE FROM shots WHERE shot_number > 1")
    st.db.execute("UPDATE shots SET outcome = 'holed'")
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id, ts=1500.0)
    shots = hole_replays(st.db)[(1, 1)]["players"][0]["shots"]
    assert [(s["from"], s["to"]) for s in shots] == [([0.0, 0.0], [0.0, 300.0])]


def test_a_booth_restarted_mid_round_still_finds_the_rounds_layout_for_earlier_holes(tmp_path):
    _, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    # Hole 2 is played after a restart, in a new session, which records the round and its layout.
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, start_x, start_z,"
                  " outcome, raw) VALUES (?, 1500, 'Dan', 'Test Links', 'Hole 2, par 3, 150 yards', 2, 1, 0, 140,"
                  " 'holed', '{}')", (st.session_id + 1,))
    st.db.execute("UPDATE rounds SET session_id = ?, holes = 2", (st.session_id + 1,))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id + 1, ts=1900.0)
    rp = hole_replays(st.db)[(1, 1)]
    assert (rp["pin"], rp["pin_estimated"]) == ([0.0, 300.0], False)


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
    assert rp["ts"] == st.db.execute("SELECT MIN(ts) FROM shots").fetchone()[0]   # the page's video link
    shots = rp["players"][0]["shots"]
    assert [(s["from"], s["to"]) for s in shots] == [([0.0, 0.0], [10.0, 200.0]), ([10.0, 200.0], [2.0, 295.0]),
                                                     ([2.0, 295.0], [0.0, 300.0])]
    # The approach aimed at the pin's distance, 40/677 of it to the left.
    d = math.hypot(10, 100)
    f = (-10 / d, 100 / d)
    want = (10.0 + f[0] * d - f[1] * 40 / 677 * d, 200.0 + f[1] * d + f[0] * 40 / 677 * d)
    assert math.dist(shots[1]["aim"], want) < 0.1


def test_a_scramble_draws_only_the_ball_the_team_played_on(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    # Dan's ball was picked off the tee and for the approach; Derek hit from the same spots.
    st.db.execute("UPDATE shots SET shared_ball = 1, total_yards = ?, lie_after = 'fairway' WHERE shot_number = 1",
                  (round(math.hypot(10, 200) * M_TO_YD, 1),))
    st.db.execute("UPDATE shots SET shared_ball = 1, total_yards = ? WHERE shot_number = 2",
                  (round(math.hypot(8, 95) * M_TO_YD, 1),))
    st.db.execute("UPDATE shots SET shared_ball = 1 WHERE shot_number = 3")
    for n, ts, total, x, z, lie in ((1, 1001.5, 150.0, None, None, "rough"), (2, 1002.5, 30.0, 10.0, 200.0, None)):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                      " total_yards, lie_after, shared_ball, start_x, start_z, raw) VALUES (?, ?, 'Derek', 'Test Links',"
                      " 'Hole 1, par 4, 328 yards', ?, '7I', 0, 100, ?, ?, 1, ?, ?, '{}')",
                      (st.session_id, ts, n, total, lie, x, z))
    st.db.commit()
    players = {p["name"]: p["shots"] for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert [(s["from"], s["to"]) for s in players["Dan"]] == [([0.0, 0.0], [10.0, 200.0]), ([10.0, 200.0], [2.0, 295.0]),
                                                             ([2.0, 295.0], [0.0, 300.0])]
    assert [(s["from"], s["to"]) for s in players["Derek"]] == [([0.0, 0.0], None), ([10.0, 200.0], None)]


def test_alternate_shot_runs_each_stroke_to_the_partners_next_one(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    # Wentworth, 11-08: Dan and Derek take turns on one ball (Dan 1 and 3, Derek 2), and Derek's missed
    # putt is left a gimme Dan's stroke number shares.
    st.db.execute("UPDATE shots SET fmt = 'alternate', shared_ball = 1, outcome = NULL")
    st.db.execute("UPDATE shots SET player = 'Derek', outcome = 'missed' WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET outcome = 'missed' WHERE shot_number = 3")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, outcome, fmt,"
                  " shared_ball, raw) VALUES (?, 1004, 'Derek', 'Test Links', 'Hole 1, par 4, 328 yards', 3, 'PT', 1,"
                  " 'gimme', 'alternate', 1, '{}')", (st.session_id,))
    # Another team's stroke 4 isn't where Dan's last putt went.
    st.db.execute("UPDATE rounds SET players = '[\"Dan\", \"Derek\"]'")
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, players) VALUES"
                  " (?, 2000, '2025-10-20', 'Test Links', 'Kyle', 1, '[\"Kyle\"]')", (st.session_id,))
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, fmt,"
                  " shared_ball, start_x, start_z, raw) VALUES (?, 1005, 'Kyle', 'Test Links', 'Hole 1, par 4,"
                  " 328 yards', 4, 'PT', 1, 'alternate', 1, 50.0, 50.0, '{}')", (st.session_id,))
    st.db.commit()
    players = {p["name"]: p["shots"] for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert [(s["from"], s["to"]) for s in players["Dan"]] == [([0.0, 0.0], [10.0, 200.0]), ([2.0, 295.0], [0.0, 300.0])]
    assert [(s["from"], s["to"]) for s in players["Derek"]] == [([10.0, 200.0], [2.0, 295.0])]
    # A gimme recorded under the shooter's own name, at the next stroke, also ends the hole.
    st.db.execute("UPDATE shots SET player = 'Dan', shot_number = 4 WHERE outcome = 'gimme'")
    st.db.commit()
    players = {p["name"]: p["shots"] for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert players["Dan"][-1]["to"] == [0.0, 300.0]


def test_a_scramble_draws_the_ball_gspro_picked_and_counts_each_partners_used_shots(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    # No lengths to go by: the log's pick says Dan's drive and Derek's approach; Dan holed, and the
    # gimme row GSPro gave Dan is no one's shot.
    st.db.execute("UPDATE shots SET shared_ball = 1, picked = shot_number IN (1, 3)")
    for n, x, z, picked, outcome in ((1, None, None, 0, None), (2, 10.0, 200.0, 1, None),
                                     (3, 2.0, 295.0, None, "missed")):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                      " shared_ball, start_x, start_z, picked, outcome, raw) VALUES (?, ?, 'Derek', 'Test Links',"
                      " 'Hole 1, par 4, 328 yards', ?, '7I', 0, 100, 1, ?, ?, ?, ?, '{}')",
                      (st.session_id, 1000.5 + n, n, x, z, picked, outcome))
    st.db.execute("UPDATE shots SET picked = 0 WHERE player = 'Dan' AND shot_number = 2")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, outcome,"
                  " shared_ball, raw) VALUES (?, 1004, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 4, 'PT', 1,"
                  " 'gimme', 0, '{}')", (st.session_id,))
    st.db.commit()
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert [s["to"] for s in players["Dan"]["shots"]] == [[10.0, 200.0], None, [0.0, 300.0]]
    assert [s["to"] for s in players["Derek"]["shots"]] == [None, [2.0, 295.0], None]
    assert (players["Dan"]["used"], players["Derek"]["used"]) == (2, 1)
    assert [(players[n]["used_gimmes"], players[n]["used_pens"]) for n in ("Dan", "Derek")] == [(1, 0), (0, 0)]
    # The log's pick holds when its ball has no stored spot: Dan's lifted approach isn't drawn instead.
    st.db.execute("UPDATE shots SET start_x = NULL, start_z = NULL WHERE player = 'Derek' AND shot_number = 2")
    st.db.commit()
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert players["Dan"]["shots"][1]["to"] is None
    assert (players["Dan"]["used"], players["Derek"]["used"]) == (2, 1)
    # Tee shots recorded before the log revealed the scramble carry only its format, not the flag.
    st.db.execute("UPDATE shots SET fmt = 'scramble'")
    st.db.execute("UPDATE shots SET shared_ball = 0 WHERE shot_number = 1")
    st.db.commit()
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert players["Dan"]["shots"][0]["to"] == [10.0, 200.0] and players["Derek"]["shots"][0]["to"] is None
    assert (players["Dan"]["used"], players["Derek"]["used"]) == (2, 1)
    # Dan's picked drive cost a penalty stroke: the team's next stroke is its third. The partners'
    # counts still add up to the score (5).
    st.db.execute("UPDATE shots SET shot_number = shot_number + 1 WHERE shot_number >= 2")
    st.db.commit()
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    got = [(players[n]["used"], players[n]["used_gimmes"], players[n]["used_pens"]) for n in ("Dan", "Derek")]
    assert got == [(2, 1, 1), (1, 0, 0)] and sum(map(sum, got)) == 5


def test_scramble_teams_sharing_a_tee_are_drawn_apart_and_a_penalty_gap_is_not_run_to_the_pin(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    st.db.execute("UPDATE shots SET shared_ball = 1, total_yards = ? WHERE shot_number = 1",
                  (round(math.hypot(10, 200) * M_TO_YD, 1),))
    st.db.execute("UPDATE shots SET shared_ball = 1 WHERE shot_number > 1")
    # Carl & Ed: Carl's 180 off the tee was picked and both played on from it. Fay & Gus: both drives
    # went wet, and after the penalty Gus holed stroke 3 from the drop.
    for who, n, ts, total, x, z, outcome, *hole in (
            ("Carl", 1, 1001.1, 180, None, None, None), ("Ed", 1, 1001.2, 150, None, None, None),
            ("Carl", 2, 1002.1, 110, 0.0, 180.0, None), ("Ed", 2, 1002.2, 130, 0.0, 180.0, None),
            ("Fay", 1, 1001.3, 260, None, None, None), ("Gus", 1, 1001.4, 250, None, None, None),
            ("Fay", 3, 1003.1, 40, 5.0, 150.0, None), ("Gus", 3, 1003.2, 165, 5.0, 150.0, "holed"),
            # Hal & Ivy: Ivy's drive was picked and Hal holed from it before Ivy hit again; they
            # played on together the next hole, which says they're partners.
            ("Hal", 1, 1001.5, 220, None, None, None), ("Ivy", 1, 1001.6, 240, None, None, None),
            ("Hal", 2, 1002.5, 61, -20.0, 240.0, "holed"),
            ("Hal", 2, 1100.0, 50, 7.0, 7.0, None, "Hole 2, par 4, 300 yards"),
            ("Ivy", 2, 1100.1, 50, 7.0, 7.0, None, "Hole 2, par 4, 300 yards"),
            # Opponents putting from beside each other aren't partners.
            ("Carl", 2, 1100.2, 3, 0.0, 298.0, None, "Hole 2, par 4, 300 yards"),
            ("Fay", 2, 1100.3, 3, 0.5, 298.0, None, "Hole 2, par 4, 300 yards"),
            (None, 2, 1100.4, 3, 0.5, 298.0, None, "Hole 2, par 4, 300 yards")):   # the screen missed who
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                      " total_yards, shared_ball, start_x, start_z, outcome, raw) VALUES (?, ?, ?, 'Test Links',"
                      " ?, ?, '7I', 0, 100, ?, 1, ?, ?, ?, '{}')",
                      (st.session_id, ts, who, *(hole or ["Hole 1, par 4, 328 yards"]), n,
                       round(total * M_TO_YD, 1), x, z, outcome))
    st.db.commit()
    players = {p["name"]: p["shots"] for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert [s["to"] for s in players["Carl"]][0] == [0.0, 180.0] and players["Ed"][0]["to"] is None
    assert players["Dan"][0]["to"] == [10.0, 200.0]
    assert players["Fay"][0]["to"] is None and players["Gus"][0]["to"] is None
    assert players["Gus"][1]["to"] == [0.0, 300.0]
    assert players["Ivy"][0]["to"] == [-20.0, 240.0] and players["Hal"][0]["to"] is None


def test_a_full_shot_bows_against_its_curve_and_a_putt_is_straight(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    # A pushed slice off the tee, a pulled draw in, a putt that read a spin axis anyway.
    for n, start, axis, carry in [(1, 3.0, 12.0, 230.0), (2, -3.0, -5.0, 100.0), (3, 0.0, 8.0, None)]:
        st.db.execute("UPDATE shots SET start_direction = ?, spin_axis = ?, carry_game_yards = ? WHERE shot_number = ?",
                      (start, axis, carry, n))
    st.db.commit()
    shots = hole_replays(st.db)[(1, 1)]["players"][0]["shots"]
    assert [s.get("shape") for s in shots] == ["Push slice", "Pull draw", None]
    # Curving right, the drive starts left of where it ends up, so it bows left (negative), and the
    # draw the other way; about a quarter of 0.6 x carry x sin(axis), in metres.
    assert shots[0]["bend"] == round(-0.6 * 230 * math.sin(math.radians(12)) / 4 / M_TO_YD, 2)
    assert shots[1]["bend"] > 0
    assert "bend" not in shots[2]


def test_a_ball_rolled_into_a_penalty_area_runs_to_where_it_went_in_then_the_drop(tmp_path):
    # Tee shot rolls in (no splash line, only the entry points: a placeholder first), a penalty stroke,
    # then the drop holed out from 150 m: Josh's 16th at Paynes Valley.
    path = str(tmp_path / "season.db")
    st = Store(path)
    drop_m = math.hypot(6, 150)
    for n, to_pin, outcome in [(1, 300 * M_TO_YD, None), (3, drop_m * M_TO_YD, "holed")]:
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt,"
                      " to_pin_yards, outcome, tags, raw) VALUES (?, ?, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards',"
                      " ?, '7I', 0, ?, ?, '[\"in_rough\"]', '{}')", (st.session_id, 1000.0 + n, n, round(to_pin, 1), outcome))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, 2000, '2025-10-20', 'Test Links', 'Dan', 1)",
                  (st.session_id,))
    st.db.commit()
    log = tmp_path / "output_log.txt"
    log.write_text("Selected Course: test_gsp\nLoading course Test Links\n" + SHOT
                   + "Haz entry Point (0.0, 0.0, 0.0), Haz ball pos (5.0, 1.0, 152.0), 0\n"
                   + "Haz entry Point (4.0, 1.0, 150.0), Haz ball pos (5.0, 1.0, 152.0), 0\n"
                   + "Haz entry Point (4.0, 1.0, 150.0), Haz ball pos (3.0, 9.0, 140.0), 0\n"
                   + _state(0, 1, 0) + _state(0, 1, 1) + _setup(6.0, 150.0, round(drop_m, 4))
                   + SHOT + "Hole out !!! \n" + _state(0, 1, 2) + "Round Is Complete\n" + LAYOUT)
    assert backfill(path, [str(log)])["hazards"] == 1
    shots = hole_replays(st.db)[(1, 1)]["players"][0]["shots"]
    assert [(s["from"], s["to"], s.get("penalty")) for s in shots] == [
        ([0.0, 0.0], [4.0, 150.0], True), ([6.0, 150.0], [0.0, 300.0], None)]
    # An entry point with no penalty after it (GSPro logged one for a ball that finished dry) is ignored.
    st.db.execute("UPDATE shots SET shot_number = 2 WHERE shot_number = 3")
    st.db.commit()
    shots = hole_replays(st.db)[(1, 1)]["players"][0]["shots"]
    assert (shots[0]["to"], shots[0].get("penalty")) == ([6.0, 150.0], None)


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


def _numbers(db, name="Dan"):
    pl = next(p for p in hole_replays(db)[(1, 1)]["players"] if p["name"] == name)
    return [s["n"] for s in pl["shots"]], [s["to"] for s in pl["shots"]], pl["strokes"]


def test_a_replay_numbers_a_misread_stroke_when_the_hole_leaves_no_doubt(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    st.db.execute("UPDATE shots SET shot_number = NULL, player = NULL WHERE shot_number = 2")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, is_putt, raw)"   # a swing after holing
                  " VALUES (?, 1010, NULL, 'Test Links', 'Hole 1, par 4, 328 yards', 0, '{}')", (st.session_id,))
    st.db.commit()
    assert _numbers(st.db) == ([1, 2, 3], [[10.0, 200.0], [2.0, 295.0], [0.0, 300.0]], 3)
    assert _numbers(st.db, "unknown")[0] == [None]
    # The last putt conceded and both it and the gimme unnumbered: they follow on from stroke 2.
    st.db.execute("DELETE FROM shots WHERE ts = 1010")
    st.db.execute("UPDATE shots SET player = 'Dan', shot_number = 2 WHERE ts = 1002")
    st.db.execute("UPDATE shots SET outcome = 'conceded', shot_number = NULL WHERE ts = 1003")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, is_putt, outcome, raw)"
                  " VALUES (?, 1004, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 1, 'gimme', '{}')", (st.session_id,))
    st.db.commit()
    assert _numbers(st.db) == ([1, 2, 3], [[10.0, 200.0], [2.0, 295.0], [0.0, 300.0]], 4)
    # A stroke that may have cost a penalty leaves the next number unknown.
    st.db.execute("UPDATE shots SET hazard_x = 5.0, hazard_z = 290.0 WHERE ts = 1002")
    st.db.commit()
    assert _numbers(st.db)[0] == [1, 2, None]
    # Nor is a run numbered on when a shot's length doesn't reach where the next was played from.
    st.db.execute("UPDATE shots SET hazard_x = NULL, hazard_z = NULL, shot_number = NULL, total_yards = 30 WHERE ts = 1002")
    st.db.execute("UPDATE shots SET outcome = 'holed' WHERE ts = 1003")
    st.db.execute("DELETE FROM shots WHERE ts = 1004")
    st.db.commit()
    assert _numbers(st.db)[0] == [1, None, None]


def test_a_replay_gives_a_stroke_with_no_player_only_to_the_player_whose_ball_it_was(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    # Eve's second ran 142 m from (-30, 150) to (-5, 290); the next stroke, read without a player or
    # number, was played from there between Dan's second and third, so it's Eve's third, not Dan's.
    for who, n, ts, x, z, total_m in (("Eve", 1, 1001.5, None, None, 150.0),
                                      ("Eve", 2, 1002.3, -30.0, 150.0, math.hypot(25, 140)),
                                      (None, None, 1002.6, -5.0, 290.0, 9.0)):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, start_x, start_z,"
                      " total_yards, raw) VALUES (?, ?, ?, 'Test Links', 'Hole 1, par 4, 328 yards', ?, 0, ?, ?, ?, '{}')",
                      (st.session_id, ts, who, n, x, z, round(total_m * M_TO_YD, 1)))
    st.db.commit()
    assert _numbers(st.db, "Eve")[:2] == ([1, 2, 3], [[-30.0, 150.0], [-5.0, 290.0], None])
    assert _numbers(st.db)[0] == [1, 2, 3]
    # With two players and no position to tell whose ball it was, it stays unattributed.
    st.db.execute("UPDATE shots SET start_x = NULL, start_z = NULL WHERE ts = 1002.6")
    st.db.commit()
    assert _numbers(st.db, "Eve")[0] == [1, 2]
    # ...and as it may be Dan's, his holing stroke after it can't be numbered on from his second.
    cols = "session_id, ts, player, course, hole, club, is_putt, to_pin_yards, outcome, start_x, start_z, raw"
    st.db.execute(f"INSERT INTO shots ({cols}) SELECT {cols} FROM shots WHERE ts = 1003")   # unnumbered, in play order
    st.db.execute("DELETE FROM shots WHERE ts = 1003 AND shot_number = 3")
    st.db.commit()
    assert _numbers(st.db)[0] == [1, 2, None]


def test_a_stroke_with_no_player_is_left_alone_when_another_reading_of_the_hole_fits(tmp_path):
    path, st = _season(tmp_path)

    def hole(shots):
        st.db.execute("DELETE FROM shots WHERE hole LIKE 'Hole 2,%'")
        for i, (who, n, z, total, outcome) in enumerate(s + (10, None)[len(s) - 3:] for s in shots):
            st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, start_x, start_z,"
                          " total_yards, outcome, raw) VALUES (?, ?, ?, 'Test Links', 'Hole 2, par 4, 300 yards', ?, 0, 0.0, ?, ?, ?, '{}')",
                          (st.session_id, 1100.0 + i, who, n, z, round(total * M_TO_YD, 1), outcome))
        st.db.commit()
        return {p["name"]: [s["n"] for s in p["shots"]] for p in hole_replays(st.db)[(1, 2)]["players"]}
    # Two unattributed strokes could be Eve's next two or the two Dan's numbers leave room for: neither is given.
    assert hole([("Dan", 1, 0.0), ("Eve", 1, 0.0), (None, None, 10.0), (None, None, 20.0), ("Dan", 4, 30.0)]) == {
        "Dan": [1, 4], "Eve": [1], "unknown": [None, None]}
    # Stroke 2 after Dan's unnumbered second can only be Eve's, though either ball could have been there.
    assert hole([("Dan", 1, 0.0), ("Eve", 1, 0.0, 20), ("Dan", None, 10.0), (None, 2, 20.0)]) == {
        "Dan": [1, 2], "Eve": [1, 2]}
    # A swing after Dan holed isn't his, even with a numbered stroke after it making it look like a gap.
    got = hole([("Dan", 1, 0.0, 10, "holed"), ("Eve", 1, 0.0, 20), (None, None, 10.0), (None, 3, 20.0)])
    assert (got["Dan"], got["unknown"]) == ([1], [None])


def test_a_round_quit_early_estimates_its_pin_from_the_shots_distances(tmp_path):
    path, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    st.db.commit()
    rp = hole_replays(st.db)[(1, 1)]
    # Two balls nearly in line with the pin, with distances rounded to 0.1 yd: close, not exact.
    assert rp["pin_estimated"] and math.dist(rp["pin"], (0, 300)) < 2.0


def test_a_round_quit_early_takes_its_tee_and_pin_from_the_course_file(tmp_path):
    from commentator.course_layout import fill_missing
    path, st = _season(tmp_path)
    # A dogleg: from the tee GSPro measures through the aim point (60, 150), 323.1 m, not the 300 straight.
    st.db.execute("UPDATE shots SET to_pin_yards = ?, start_x = 50, start_z = 100 WHERE shot_number = 1",
                  (round(2 * math.hypot(60, 150) * M_TO_YD, 1),))   # a tee shot's stored start isn't the tee
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    st.db.commit()
    _course_file(tmp_path)
    assert hole_replays(st.db)[(1, 1)]["tee"] is None
    assert fill_missing(st.db, [str(tmp_path / "Courses")]) == 1
    rp = hole_replays(st.db)[(1, 1)]
    assert rp["tee"] == [0, 0] and rp["pin"] == [0, 300] and not rp["pin_estimated"]
    assert st.db.execute("SELECT tee_box FROM hole_layouts").fetchone()[0] == "White"
    assert fill_missing(st.db, [str(tmp_path / "Courses")]) == 0   # once


def test_a_pin_gspro_logged_for_the_hole_is_used_over_the_course_files(tmp_path):
    from commentator.course_layout import fill_missing
    path, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET to_pin_yards = ?, start_x = 50, start_z = 100 WHERE shot_number = 1",
                  (round(2 * math.hypot(60, 150) * M_TO_YD, 1),))
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    st.db.commit()
    st.record_logged_pin((40.0, 1.0, 250.0), ts=990.0)    # another hole's: the shots don't fit it
    st.record_logged_pin((0.3, 1.0, 300.2), ts=999.0)     # this hole's, a little off the file's Thursday
    st.record_logged_pin((0.0, 1.0, 300.0), ts=1200.0)    # too long after the hole to be its
    # No course file: the logged pin alone fills it (the tee stays unknown).
    assert fill_missing(st.db, [str(tmp_path / "Courses")]) == 1
    rp = hole_replays(st.db)[(1, 1)]
    assert rp["pin"] == [0.3, 300.2] and rp["tee"] is None and not rp["pin_estimated"]
    # With the course file, the logged pin still wins, and the tee comes from the file.
    st.db.execute("DELETE FROM hole_layouts")
    st.db.commit()
    _course_file(tmp_path)
    assert fill_missing(st.db, [str(tmp_path / "Courses")]) == 1
    rp = hole_replays(st.db)[(1, 1)]
    assert rp["pin"] == [0.3, 300.2] and rp["tee"] == [0, 0]
    # A logged pin the shots don't fit is passed over for the file's.
    st.db.execute("DELETE FROM hole_layouts")
    st.db.execute("UPDATE logged_pins SET x = 20 WHERE ts = 999")
    st.db.commit()
    assert fill_missing(st.db, [str(tmp_path / "Courses")]) == 1
    assert hole_replays(st.db)[(1, 1)]["pin"] == [0, 300]


def _course_file(tmp_path):
    """Test Links' course file: hole 1's Black tee at (0, -40), White at (0, 0), a dogleg aim point and
    two days' pins."""
    import json

    folder = tmp_path / "Courses" / "test_gsp"
    folder.mkdir(parents=True)
    (folder / "coursedetails.txt").write_text("Test Links|Somewhere\n")
    pos = lambda x, z: {"x": x, "y": 1.0, "z": z}   # noqa: E731
    (folder / "test_gsp.GKD").write_text(json.dumps({"CourseName": "Test Links", "Holes": [{
        "HoleNumber": 1, "Par": 4,
        "Tees": [{"TeeType": "Black", "Position": pos(0, -40)}, {"TeeType": "White", "Position": pos(0, 0)},
                 {"TeeType": "AimPoint1", "Enabled": True, "Position": pos(60, 150)}],
        "Pins": [{"Day": "Thursday", "Position": pos(0, 300)}, {"Day": "Friday", "Position": pos(8, 290)}]}]}))


def test_a_rounds_tees_are_the_course_files_box_its_logged_tee_sits_on(tmp_path):
    from commentator import stats_web
    from commentator.course_layout import name_tee_boxes
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())    # its logged tee is (0, 0): the White box
    backfill(path, [str(log)])
    _course_file(tmp_path)
    # A later round on the hole from the Black box, and one from a tee no box is at.
    for n, tee in enumerate([(0.0, 1.0, -40.0), (0.0, 1.0, -20.0)], 1):
        st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": tee, "pin": (0.0, 1.0, 300.0)}],
                              session_id=st.session_id + n, ts=9e9)
    assert stats_web.rounds(st.db)[0]["tee"] is None
    assert name_tee_boxes(st.db, [str(tmp_path / "Courses")]) == 2
    assert [r[0] for r in st.db.execute("SELECT tee_box FROM hole_layouts ORDER BY id")] == ["White", "Black", None]
    assert stats_web.rounds(st.db)[0]["tee"] == "White"
    assert name_tee_boxes(st.db, [str(tmp_path / "Courses")]) == 0


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


def test_backfill_reads_each_scramble_strokes_pick_from_the_log(tmp_path):
    def state(p, score):
        return f"ActivePlayer: {p}  currentHole: 0  strokes: 1  Previous Score: {score}\n"
    log = tmp_path / "output_log.txt"
    log.write_text("Selected Course: test_gsp\nLoading course Test Links\n" + SHOT + state(0, 0) + SHOT + state(1, 0)
                   + "Selecting shot for player: 1\nData for selected player: X 0.0 Y 1.0 Z 200.0\n"
                   "Logging tdist: 100.0 and ActiveGameGimmieDistance: 1.22\n"
                   "1503 SelectShot Updating teammate score: player 0\n"
                   + SHOT + state(0, 1) + SHOT + "Hole out !!! \n" + state(1, 1))
    path = str(tmp_path / "season.db")
    st = Store(path)
    for ts, who, n, to_pin in ((1, "Dan", 1, 300), (2, "Derek", 1, 300), (3, "Dan", 2, 100), (4, "Derek", 2, 100)):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                      " shared_ball, fmt, raw) VALUES (?, ?, ?, 'Test Links', 'Hole 1, par 4, 328 yards', ?, '7I', 0, ?, 1,"
                      " 'scramble', '{}')",
                      (st.session_id, ts, who, n, round(to_pin * M_TO_YD, 1)))
    st.db.execute("UPDATE shots SET fmt = 'shamble'")
    st.db.commit()
    assert backfill(path, [str(log)])["picks"] == 0     # a shamble's partners play their own balls on
    st.db.execute("UPDATE shots SET fmt = 'scramble'")
    st.db.commit()
    assert backfill(path, [str(log)])["picks"] == 4
    assert [r[0] for r in st.db.execute("SELECT picked FROM shots ORDER BY id")] == [0, 1, 0, 1]
    # With Dan's second stroke never recorded, his lifted ball pairs with Derek's row: both indices
    # then read as Derek, so no pick is trusted rather than Derek's holed putt marked as lifted.
    st.db.execute("DELETE FROM shots WHERE player = 'Dan' AND shot_number = 2")
    st.db.execute("UPDATE shots SET picked = NULL")
    st.db.execute("UPDATE shots SET player = 'Derek' WHERE player = 'Dan'")
    st.db.commit()
    backfill(path, [str(log)])
    assert [r[0] for r in st.db.execute("SELECT picked FROM shots ORDER BY id")] == [None, None, None]
    # With Dan's drive back, each index has its own name again: Derek's drive was the pick, and his
    # second row, paired with Dan's lifted putt, is left alone.
    st.db.execute("UPDATE shots SET player = 'Dan' WHERE id = (SELECT MIN(id) FROM shots)")
    st.db.commit()
    backfill(path, [str(log)])
    assert [r[0] for r in st.db.execute("SELECT picked FROM shots ORDER BY id")] == [None, 1, None]


def test_backfill_trusts_no_pick_for_an_index_tied_between_names(tmp_path):
    def state(p, score):
        return f"ActivePlayer: {p}  currentHole: 0  strokes: 1  Previous Score: {score}\n"
    log = tmp_path / "output_log.txt"
    log.write_text("Selected Course: test_gsp\nLoading course Test Links\n" + SHOT + state(0, 0) + SHOT + state(1, 0)
                   + "Selecting shot for player: 1\nData for selected player: X 0.0 Y 1.0 Z 200.0\n"
                   "Logging tdist: 100.0 and ActiveGameGimmieDistance: 1.22\n"
                   "1503 SelectShot Updating teammate score: player 0\n"
                   + SHOT + state(0, 1) + SHOT + "Hole out !!! \n" + state(1, 1))
    path = str(tmp_path / "season.db")
    st = Store(path)
    # Index 0's second stroke is stored as Derek's, so index 0 pairs once with each name.
    for ts, who, n, to_pin in ((1, "Dan", 1, 300), (2, "Derek", 1, 300), (3, "Derek", 2, 100), (4, "Derek", 2, 100)):
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                      " shared_ball, fmt, raw) VALUES (?, ?, ?, 'Test Links', 'Hole 1, par 4, 328 yards', ?, '7I', 0, ?, 1,"
                      " 'scramble', '{}')",
                      (st.session_id, ts, who, n, round(to_pin * M_TO_YD, 1)))
    st.db.commit()
    backfill(path, [str(log)])
    assert [r[0] for r in st.db.execute("SELECT picked FROM shots ORDER BY id")] == [None, 1, None, 1]


def test_a_scramble_hole_without_shared_spots_still_counts_shots_used(tmp_path):
    path, st = _season(tmp_path)
    log = tmp_path / "output_log.txt"
    log.write_text(_hole_log())
    backfill(path, [str(log)])
    # Dan's drive was picked and finished inside the gimme circle: no second stroke shares a spot,
    # so position can't tie the partners together, but the format and the log's pick can.
    st.db.execute("DELETE FROM shots WHERE shot_number > 1")
    st.db.execute("UPDATE shots SET fmt = 'scramble', shared_ball = 1, picked = 1")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, to_pin_yards,"
                  " shared_ball, fmt, start_x, start_z, picked, raw) VALUES (?, 1001.5, 'Derek', 'Test Links',"
                  " 'Hole 1, par 4, 328 yards', 1, 'DR', 0, 300, 1, 'scramble', 0.0, 0.0, 0, '{}')", (st.session_id,))
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, outcome,"
                  " shared_ball, fmt, raw) VALUES (?, 1004, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 2, 'PT', 1,"
                  " 'gimme', 1, 'scramble', '{}')", (st.session_id,))
    st.db.commit()
    players = {p["name"]: p for p in hole_replays(st.db)[(1, 1)]["players"]}
    assert (players["Dan"]["used"], players["Derek"]["used"]) == (1, 0)


def test_a_total_the_hidden_hud_never_showed_comes_from_the_logged_positions(tmp_path):
    _, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200, carry_raw = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    st.db.execute("UPDATE shots SET carry_raw = 200 WHERE shot_number = 1")
    st.db.commit()
    total = lambda n: st.db.execute("SELECT total_yards FROM shots WHERE shot_number = ?", (n,)).fetchone()[0]  # noqa: E731
    # The approach is filled at once: its next stroke's start is its rest. It didn't fit the carry.
    assert st.fill_log_totals() == 0 and total(2) is None
    st.db.execute("UPDATE shots SET carry_raw = 100 WHERE shot_number = 2")
    assert st.fill_log_totals() == 1 and total(2) == round(math.hypot(8, 95) * M_TO_YD, 1)
    # The drive waits for the round's layout, which logs the tee it was hit from.
    assert total(1) is None
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id, ts=1500.0)
    assert st.fill_log_totals() == 1 and total(1) == round(math.hypot(10, 200) * M_TO_YD, 1)
    assert total(3) is None   # a putt: none


def test_a_logged_total_pairs_strokes_by_time_and_never_across_a_round_end(tmp_path):
    _, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200, carry_raw = 100 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    # The approach's handler finished after the putt's, so it was saved after it.
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, start_x, start_z,"
                  " carry_raw, raw) SELECT session_id, ts, player, course, hole, shot_number, club, is_putt, start_x,"
                  " start_z, carry_raw, raw FROM shots WHERE shot_number = 2")
    st.db.execute("DELETE FROM shots WHERE id = (SELECT MIN(id) FROM shots WHERE shot_number = 2)")
    ended = st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes)"
                          " VALUES (?, 1002.5, '2025-10-20', 'Test Links', 'Dan', 1)", (st.session_id,)).lastrowid
    st.db.commit()
    assert st.fill_log_totals() == 0   # a round ended between the approach and the putt: another ball
    st.db.execute("DELETE FROM rounds WHERE id = ?", (ended,))
    assert st.fill_log_totals() == 1
    assert st.db.execute("SELECT total_yards FROM shots WHERE shot_number = 2").fetchone()[0] == round(
        math.hypot(8, 95) * M_TO_YD, 1)


def test_a_drive_doesnt_take_the_tee_of_a_later_round_on_the_hole(tmp_path):
    _, st = _season(tmp_path)   # its round ended without logging the layout
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET carry_raw = 200 WHERE shot_number = 1")
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, raw)"
                  " VALUES (?, 2500, 'Dan', 'Test Links', 'Hole 1', 1, 'DR', 0, '{}')", (st.session_id,))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 2.0, 20.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id, ts=3000.0)
    st.fill_log_totals()
    assert st.db.execute("SELECT total_yards FROM shots WHERE ts = 1001").fetchone()[0] is None


def test_a_logged_total_follows_later_corrections_and_a_read_total_is_kept(tmp_path):
    _, st = _season(tmp_path)
    st.db.execute("UPDATE shots SET start_x = 10, start_z = 200, carry_raw = 100 WHERE shot_number = 2")
    st.db.execute("UPDATE shots SET start_x = 2, start_z = 295 WHERE shot_number = 3")
    st.db.commit()
    total = lambda n: st.db.execute("SELECT total_yards FROM shots WHERE shot_number = ?", (n,)).fetchone()[0]  # noqa: E731
    assert st.fill_log_totals() == 1 and total(2) == round(math.hypot(8, 95) * M_TO_YD, 1)
    st.db.execute("UPDATE shots SET start_z = 290 WHERE shot_number = 3")   # its position corrected
    assert st.fill_log_totals() == 1 and total(2) == round(math.hypot(8, 90) * M_TO_YD, 1)
    st.db.execute("UPDATE shots SET mulligan = 1 WHERE shot_number = 3")   # no longer the next stroke
    assert st.fill_log_totals() == 1 and total(2) is None
    st.db.execute("UPDATE shots SET mulligan = 0, total_yards = 99 WHERE shot_number = 2")   # read off the HUD
    assert st.fill_log_totals() == 0 and total(2) == 99


def test_a_round_doesnt_take_the_tees_of_a_later_round_left_out_of_the_stats(tmp_path):
    from commentator import stats_web
    path, st = _season(tmp_path)     # no layout of its own
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, raw, excluded)"
                  " VALUES (?, 3000, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 1, 'DR', 0, '{}', 1)", (st.session_id,))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, excluded)"
                  " VALUES (?, 4000, '2025-10-20', 'Test Links', 'Dan', 1, 1)", (st.session_id,))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 1.0, -40.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id, ts=3500)
    st.db.execute("UPDATE hole_layouts SET tee_box = 'Black'")
    st.db.commit()
    [r] = stats_web.rounds(stats_web.connect(path))
    assert r["tee"] is None


def test_a_stray_shot_before_the_rounds_layout_is_logged_doesnt_lose_its_tees(tmp_path):
    from commentator import stats_web
    path, st = _season(tmp_path)     # the round ends at 2000
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, raw)"
                  " VALUES (?, 2500, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 1, 'DR', 0, '{}')", (st.session_id,))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 1.0, 0.0), "pin": (0.0, 1.0, 300.0)}],
                          session_id=st.session_id, ts=2600)
    st.db.execute("UPDATE hole_layouts SET tee_box = 'White'")
    # and the next round, which the stray shot is taken to have started
    st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, raw)"
                  " VALUES (?, 3000, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 1, 'DR', 0, '{}')", (st.session_id,))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes)"
                  " VALUES (?, 4000, '2025-10-20', 'Test Links', 'Dan', 1)", (st.session_id,))
    st.db.commit()
    st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 1.0, -40.0), "pin": (0.0, 1.0, 310.0)}],
                          session_id=st.session_id, ts=3100)
    st.db.execute("UPDATE hole_layouts SET tee_box = 'Black' WHERE tee_box IS NULL")
    st.db.commit()
    assert [r["tee"] for r in stats_web.rounds(stats_web.connect(path))] == ["Black", "White"]


def test_a_round_doesnt_take_the_tees_worked_out_from_the_next_rounds_shots(tmp_path):
    from commentator import stats_web
    path, st = _season(tmp_path)     # no layout of its own
    for ts, hole in [(3000, 1), (3200, 1), (3300, 2)]:
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, raw)"
                      " VALUES (?, ?, 'Dan', 'Test Links', ?, 1, 'DR', 0, '{}')",
                      (st.session_id, ts, f"Hole {hole}, par 4, 328 yards"))
    st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes)"
                  " VALUES (?, 4000, '2025-10-20', 'Test Links', 'Dan', 2)", (st.session_id,))
    # course_layout.fill_missing's for the next round's first hole, stamped with its last shot there
    st.db.execute("INSERT INTO hole_layouts (session_id, ts, course, hole, par, tee_x, tee_y, tee_z, pin_x, pin_y,"
                  " pin_z, tee_box) VALUES (?, 3200, 'Test Links', 1, 4, 0, 1, -40, 0, 1, 300, 'Black')", (st.session_id,))
    st.db.commit()
    assert [r["tee"] for r in stats_web.rounds(stats_web.connect(path))] == ["Black", None]


def test_a_round_started_just_after_another_doesnt_take_its_tees(tmp_path):
    from commentator import stats_web
    path = str(tmp_path / "season.db")
    st = Store(path)
    for shot, layout, end, box in [(1000, 1001, 1020, "White"), (1060, 1061, 1070, "Black")]:
        st.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, raw)"
                      " VALUES (?, ?, 'Dan', 'Test Links', 'Hole 1, par 4, 328 yards', 1, 'DR', 0, '{}')",
                      (st.session_id, shot))
        st.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes)"
                      " VALUES (?, ?, '2025-10-20', 'Test Links', 'Dan', 1)", (st.session_id, end))
        st.db.commit()
        st.record_hole_layout("Test Links", [{"par": 4, "played": True, "tee": (0.0, 1.0, float(-shot)),
                                              "pin": (0.0, 1.0, 300.0)}], session_id=st.session_id, ts=layout)
        st.db.execute("UPDATE hole_layouts SET tee_box = ? WHERE tee_box IS NULL", (box,))
        st.db.commit()
    assert [r["tee"] for r in stats_web.rounds(stats_web.connect(path))] == ["Black", "White"]
