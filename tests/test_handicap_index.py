import json

from commentator import handicap_index as HI
from commentator.course_card import CourseCard
from commentator.store import Store

PARS = {h: 4 for h in range(1, 19)}


def test_index_follows_the_whs_table():
    assert HI.index_from([10.0, 12.0]) is None
    assert HI.index_from([10.0, 12.0, 14.0]) == 8.0                       # lowest less 2
    assert HI.index_from([9.0, 11.0, 13.0, 15.0, 17.0, 19.0]) == 9.0      # lowest 2 averaged, less 1
    twenty = [float(d) for d in range(30, 10, -1)]                        # 30 .. 11
    assert HI.index_from(twenty) == 14.5                                  # lowest 8: 11..18
    assert HI.index_from([5.0] * 30 + twenty) == 14.5                     # only the latest 20 count
    assert HI.index_from([70.0, 70.0, 70.0]) == 54.0


def test_differential_caps_each_hole_and_scales_by_slope():
    tee = {"tee": "Blue", "rating": 70.0, "slope": 130, "par": PARS, "index": {h: h for h in PARS}}
    scores = {h: 5 for h in PARS} | {1: 12}
    # No index yet: a hole is capped at par + 5 (12 counts as 9). 94 - 70 = 24, x 113/130.
    assert HI._differential(scores, PARS, tee, None) == (20.9, 18)
    # Index 0: the cap is net double bogey, par + 2 (12 counts as 6). 91 - 70 = 21.
    assert HI._differential(scores, PARS, tee, 0.0) == (18.3, 18)


def test_short_rounds_scale_up_without_an_index_and_add_the_expected_holes_with_one():
    tee = {"tee": "Blue", "rating": 72.0, "slope": 113, "par": PARS, "index": {}}
    nine = {h: 5 for h in range(1, 10)}                    # 45 against a 36.0 rating
    assert HI._differential(nine, PARS, tee, None) == (18.0, 9)
    assert HI._differential(nine, PARS, tee, 10.0) == (15.4, 9)   # 9 + 0.52 x 10 + 1.2
    # No rated tee: par is the rating, on a 113 slope.
    assert HI._differential(nine, PARS, None, None) == (18.0, 9)
    # A round quit after 17 keeps all 17 holes and adds the expected score for the one not played.
    seventeen = {h: 4 for h in range(1, 18)}
    assert HI._differential(seventeen, PARS, tee, 10.0) == (0.7, 17)              # 0 + 6.4 / 9
    assert HI._differential(seventeen | {17: 8}, PARS, tee, 10.0) == (2.7, 17)    # capped at 6: no stroke there
    # The strokes fall as over the full 18 (index 10: holes 1-10), so hole 10 keeps its stroke.
    tee = {**tee, "index": {h: h for h in PARS}}
    assert HI._differential(seventeen | {10: 7}, PARS, tee, 10.0) == (3.7, 17)


def test_the_course_handicap_rounds_half_up():
    tee = {"tee": "Blue", "rating": 72.0, "slope": 113, "par": PARS, "index": {h: h for h in PARS}}
    # Index 2.5 gives 3 strokes, so the third-hardest hole's cap is 4 + 2 + 1 = 7.
    assert HI._differential(PARS | {3: 7}, PARS, tee, 2.5) == (3.0, 18)


def test_the_tee_is_the_one_whose_yardages_match_the_logged_holes():
    tees = [{"tee": t, "rating": r, "slope": 120, "yards": {1: y, 2: y + 100}}
            for t, r, y in (("Black", 74.0, 450), ("Green", 70.0, 400), ("Junior", 72.0, 300))]
    assert HI.pick_tee(tees, {1: [395.0, 405.0], 2: [498.0]}, [1, 2])["tee"] == "Green"
    # Without yardages, the length played says it, never by a placeholder-rated junior tee.
    bare = [{**t, "yards": {}} for t in tees]
    assert HI.pick_tee(bare, {1: [450.0], 2: [450.0]}, [1, 2])["tee"] == "Black"   # 8100 yd -> 77.7
    assert HI.pick_tee(bare, {1: [250.0], 2: [250.0]}, [1, 2])["tee"] == "Green"   # 4500 yd -> 61.4


def test_indexes_from_the_rounds_on_record():
    store = Store(None)
    card = CourseCard(name="Test Links", par=PARS, index={h: h for h in PARS},
                      yards={"Green": {h: 400 for h in PARS}, "Black": {h: 450 for h in PARS}},
                      ratings={"Green": (70.0, 113), "Black": (74.0, 130)})
    store.record_course_tees("Test Links", card)
    sid = store.db.execute("SELECT max(id) FROM sessions").fetchone()[0]
    if sid is None:
        sid = store.db.execute("INSERT INTO sessions (started_at, date) VALUES (0, '2026-10-01')").lastrowid
    # Green tees (400 yards to the pin), logged at the round.
    store.db.executemany("INSERT INTO hole_layouts (session_id, course, hole, tee_x, tee_z, pin_x, pin_z)"
                         " VALUES (?, 'Test Links', ?, 0, 0, 0, ?)", [(sid, h, 400 / 1.0936133) for h in PARS])
    for i, (gross, fmt) in enumerate(((80, None), (82, "best_ball"), (84, None), (60, "scramble"))):
        scores = {h: 4 for h in PARS} | {h: 5 for h in range(1, gross - 72 + 1)}
        store.db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, scores, hole_pars)"
                         " VALUES (?, ?, 'Ann', 'Test Links', ?, ?, ?)",
                         (sid, i, fmt, json.dumps(scores), json.dumps(PARS)))
    ann = HI.indexes(store.db)["Ann"]
    assert ann["rounds"] == 3                       # not the scramble
    assert [h["tee"] for h in ann["history"]] == ["Green"] * 3
    assert [h["differential"] for h in ann["history"]] == [10.0, 12.0, 14.0]
    assert ann["index"] == 8.0 and ann["history"][1]["index"] is None


def _rounds(store, rounds, layouts=()):
    sid = store.db.execute("INSERT INTO sessions (started_at, date) VALUES (0, '2026-10-01')").lastrowid
    for ts, yards in layouts:
        store.db.executemany("INSERT INTO hole_layouts (session_id, ts, course, hole, tee_x, tee_z, pin_x, pin_z)"
                             " VALUES (?, ?, 'Test Links', ?, 0, 0, 0, ?)",
                             [(sid, ts, h, yards / 1.0936133) for h in PARS])
    for ended, scores in rounds:
        store.db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, scores, hole_pars)"
                         " VALUES (?, ?, 'Ann', 'Test Links', ?, ?)", (sid, ended, json.dumps(scores), json.dumps(PARS)))
    return HI.indexes(store.db)["Ann"]


def test_the_first_index_waits_for_54_holes():
    nine = {h: 5 for h in range(1, 10)}
    ann = _rounds(Store(None), [(i, nine) for i in range(6)])
    assert [h["index"] is None for h in ann["history"]] == [True] * 5 + [False]


def test_each_round_takes_the_tee_from_its_own_layouts():
    store = Store(None)
    card = CourseCard(name="Test Links", par=PARS, index={h: h for h in PARS},
                      yards={"White": {h: 400 for h in PARS}, "Black": {h: 450 for h in PARS}},
                      ratings={"White": (70.0, 113), "Black": (74.0, 113)})
    store.record_course_tees("Test Links", card)
    ann = _rounds(store, [(1000.0, PARS), (5000.0, PARS)], layouts=[(1001.0, 400), (5002.0, 450)])
    assert [h["tee"] for h in ann["history"]] == ["White", "Black"]


def test_the_league_handicap_comes_first_and_our_index_stands_in():
    from commentator.league import League

    lg = League({"teams": [{"name": "A", "players": [{"name": "Ann", "handicap": 3.2}, {"name": "Bo"}]}]})
    lg.own_indexes = {"Ann": 9.9, "Bo": 12.4}
    assert lg.handicap("Ann") == 3.2 and lg.handicap("Bo") == 12.4 and lg.handicap("Cy") is None
