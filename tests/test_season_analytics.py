import json
import sqlite3

import pytest

from commentator import season_analytics as A
from commentator import stats_web
from commentator.store import Store

PARS = {str(h): p for h, p in zip(range(1, 10), [4, 4, 3, 5, 4, 3, 4, 5, 4])}


class _Conn(sqlite3.Connection):
    path = None


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "season.db"
    Store(str(path)).close()
    conn = sqlite3.connect(path, factory=_Conn)
    conn.row_factory = sqlite3.Row
    conn.path = path
    yield conn
    conn.close()


_ts = iter(range(1, 10_000))


def shot(db, player="Ann", **kw):
    row = {"session_id": 1, "ts": next(_ts), "player": player, "course": "Pebble", "is_putt": 0, "attributed": 1,
           "mulligan": 0, "is_league": 0, **kw}
    return db.execute(f"INSERT INTO shots ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                      tuple(row.values())).lastrowid


def player_round(db, player, scores, fmt=None, league=0):
    db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, scores, hole_pars, is_league,"
               " complete, holes_played) VALUES (1, 0, ?, 'Pebble', ?, ?, ?, ?, 1, ?)",
               (player, fmt, json.dumps({str(h): s for h, s in scores.items()}), json.dumps(PARS), league, len(scores)))


def par4(db, player="Ann", hole="Hole 1, par 4, 400 yards", **last):
    """Drive to the fairway, approach to 20 ft, two-putt: a par with the second putt conceded."""
    shot(db, player, hole=hole, shot_number=1, club="DR", lie_before="tee", to_pin_yards=400, lie_after="fairway")
    shot(db, player, hole=hole, shot_number=2, club="8I", lie_before="fairway", to_pin_yards=150, finish_yards=20 / 3)
    shot(db, player, hole=hole, shot_number=3, club="PT", is_putt=1, lie_before="green", to_pin_yards=20 / 3,
         outcome="conceded")
    shot(db, player, hole=hole, shot_number=4, is_putt=1, lie_before="green", outcome="gimme")


def test_baseline_interpolates_and_reads_feet_on_the_green():
    assert A.expected("fairway", 100) == pytest.approx(2.80)
    assert A.expected("fairway", 110) == pytest.approx(2.825)
    assert A.expected("green", 10 / 3) == pytest.approx(1.61)              # 10 ft
    assert A.expected("deep rough", 100) == pytest.approx(3.02 + A.DEEP_ROUGH_EXTRA)
    assert A.expected("tee", 700) > A.expected("tee", 600)                  # extrapolates past the table
    assert A.expected("fairway", None) is None


def test_holes_split_on_stroke_one_and_strokes_gained_add_up(db):
    par4(db)
    par4(db, hole="par 4, 380 yards")                                        # hole number not read yet
    shot(db, hole="Hole 3, par 3, 150 yards", shot_number=1, club="7I", lie_before="tee", to_pin_yards=150)
    db.commit()
    holes = A.holes_played(db)
    assert [(h["complete"], h["score"]) for h in holes] == [(True, 4), (True, 4), (False, None)]
    sg = [s for s in A.strokes_gained(holes) if s["shot_id"] <= 4]
    # Each stroke: expected before - expected after - 1; the conceded tap-in ends the hole at 1.0.
    assert [s["category"] for s in sg] == ["off_tee", "approach", "putting"]
    assert sum(s["sg"] for s in sg) == pytest.approx(A.expected("tee", 400) - 1.0 - 3)
    shot(db, hole="Hole 4, par 5, 500 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=500)
    shot(db, hole="Hole 4, par 5, 500 yards", shot_number=3, club="3W", lie_before="rough", to_pin_yards=250)
    db.commit()
    assert len(A.strokes_gained(A.holes_played(db))) == 6     # unfinished holes don't count at all
    assert A.sg_summary(A.holes_played(db))["Ann"]["holes"] == 2


def test_a_penalty_stroke_costs_a_shot_and_mulligans_and_scrambles_are_left_out(db):
    shot(db, hole="Hole 1, par 4, 400 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=400)
    shot(db, hole="Hole 1, par 4, 400 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=400, mulligan=1)
    shot(db, hole="Hole 1, par 4, 400 yards", shot_number=3, club="8I", lie_before="fairway", to_pin_yards=150)
    shot(db, hole="Hole 1, par 4, 400 yards", shot_number=4, club="PT", is_putt=1, lie_before="green",
         to_pin_yards=2, outcome="holed")
    shot(db, "Bob", hole="Hole 1, par 4, 400 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=400,
         fmt="scramble")
    db.commit()
    holes = A.holes_played(db)
    assert [(h["player"], len(h["strokes"]), h["score"]) for h in holes] == [("Ann", 3, 4)]
    tee = A.strokes_gained(holes)[0]
    assert tee["sg"] == pytest.approx(A.expected("tee", 400) - A.expected("fairway", 150) - 2)


def test_gapping_uses_full_swings_and_reads_the_flight(db):
    for carry, speed in ((100, 90), (104, 92), (98, 91), (25, 40), (15, 30)):
        shot(db, club="PW", carry_raw=carry, ball_speed=speed, club_speed=75, start_direction=0.5, spin_axis=-5)
    for carry in (230, 240, 250):
        shot(db, club="DR", carry_raw=carry, ball_speed=150, club_speed=100, start_direction=3, spin_axis=10)
    db.commit()
    dr, pw = A.gapping(db, "Ann")
    assert (pw["club"], pw["n"], pw["swings"], pw["carry"]) == ("PW", 3, 5, 100)
    assert dr["gap_to_next"] == 140 and pw["gap_to_next"] is None
    assert pw["miss"] == "starts straight, draw" and dr["miss"] == "starts 3° right, slice"


def test_a_lefty_s_shots_are_named_for_their_hand(db, tmp_path):
    from commentator import shot_shape
    from commentator.hud import Hud
    from commentator.models import Shot

    # Starting right and curving right: a right-hander's push slice is a left-hander's pull hook.
    assert shot_shape.shape(3, 10, 200, handed="RH") == shot_shape.shape(3, 10, 200) == "Push slice"
    assert shot_shape.shape(3, 10, 200, handed="LH") == "Pull hook"
    for carry in (230, 240, 250):
        shot(db, player="Dan", club="DR", carry_raw=carry, ball_speed=150, start_direction=3, spin_axis=10,
             handed="LH")
    db.commit()
    assert A.gapping(db, "Dan")[0]["miss"] == "starts 3° right, hook"
    # The booth stores the hand it read with each shot.
    st = Store(str(tmp_path / "hands.db"))
    sid = st.record_shot(player="Dan", hole="Hole 1", shot=Shot(club="DR", handed="RH"), before=Hud(handed="lh"),
                         after=Hud(), tags=[], carry_game_yards=None, total_yards=None, offline_yards=None)
    assert st.db.execute("SELECT handed FROM shots WHERE id = ?", (sid,)).fetchone()[0] == "LH"
    st.close()


def test_highlights_and_records_by_date_range(db):
    from datetime import date

    starts = stats_web.period_starts(date(2026, 5, 31))
    assert starts == {"3m": "2026-02-28", "6m": "2025-11-30", "9m": "2025-08-31", "1y": "2025-05-31", "all": None}
    db.execute("INSERT INTO sessions (id, date, started_at) VALUES (2, '2025-01-10', 0)")
    db.execute("UPDATE sessions SET date = '2026-05-20' WHERE id = 1")
    tee = {"club": "DR", "shot_number": 1, "lie_before": "tee", "hole": "Hole 1, par 4, 400 yards"}
    shot(db, session_id=2, total_yards=320, **tee)      # last year's bomb
    shot(db, total_yards=250, **tee)
    db.commit()
    got = stats_web.highlights_by_period(db, A.holes_played(db), today=date(2026, 5, 31))
    assert [d["v"] for d in got["highlights"]["1y"]["drives"]] == [250]
    assert [d["v"] for d in got["highlights"]["all"]["drives"]] == [320, 250]
    assert got["records"]["3m"]["longest_drive"]["v"] == 250 and got["records"]["all"]["longest_drive"]["v"] == 320
    assert got["records"]["all"] == stats_web.records(db) and got["highlights"]["all"] == A.highlights(db, A.holes_played(db))


def test_recovery_counts_greens_found_from_trouble(db):
    hole = "Hole 1, par 4, 400 yards"
    shot(db, hole=hole, shot_number=1, club="DR", lie_before="tee", to_pin_yards=400)
    shot(db, hole=hole, shot_number=2, club="7I", lie_before="rough", to_pin_yards=160, finish_yards=30)
    shot(db, hole=hole, shot_number=3, club="SW", lie_before="sand", to_pin_yards=30, finish_yards=3)
    shot(db, hole=hole, shot_number=4, club="PT", is_putt=1, lie_before="green", to_pin_yards=3, outcome="holed")
    db.commit()
    rec = {(r["lie"], r["band"]): r for r in A.recovery(A.holes_played(db))["Ann"]}
    assert rec[("rough", "long")]["green_pct"] == 0.0
    assert rec[("sand", "greenside")]["green_pct"] == 100.0 and rec[("sand", "greenside")]["avg_left"] == 3


def test_courses_rank_holes_and_highlights_find_streaks_bounce_backs_and_real_hole_outs(db):
    nine = {h: PARS[str(h)] for h in range(1, 10)}
    player_round(db, "Ann", {**nine, 1: 3, 2: 3, 4: 3, 6: 6})               # birdie-birdie, eagle, a +3
    player_round(db, "Bob", {**nine, 3: 1, 5: 5, 6: 2, 7: 5, 9: 5})          # an ace; bogey then birdie
    player_round(db, "Cat", {h: 1 for h in range(1, 10)}, fmt="scramble")    # shared ball: not counted
    hole = "Hole 7, par 4, 400 yards"
    shot(db, hole=hole, shot_number=1, club="DR", lie_before="tee", to_pin_yards=400)
    shot(db, hole=hole, shot_number=2, club="SW", lie_before="rough", to_pin_yards=40, outcome="holed")
    # The older rows' bug: a full shot marked holed with more strokes after it isn't a hole-out.
    shot(db, hole="Hole 8, par 3, 150 yards", shot_number=1, club="9I", lie_before="fairway", to_pin_yards=150,
         outcome="holed")
    shot(db, hole="Hole 8, par 3, 150 yards", shot_number=2, club="PT", is_putt=1, lie_before="green",
         to_pin_yards=2, outcome="holed")
    db.commit()
    (c,) = A.courses(db)
    assert c["rounds"] == 2 and c["hardest"] == 6 and c["players"] == ["Ann", "Bob"]
    assert c["best"]["player"] == "Ann" and c["best"]["to_par"] == -1
    h = A.highlights(db, A.holes_played(db))
    kinds = sorted((m["player"], m["kind"]) for m in h["moments"])
    assert kinds == [("Ann", "eagle"), ("Ann", "holed out"), ("Bob", "ace")]
    assert [(s["player"], s["holes"]) for s in h["streaks"]] == [("Ann", [1, 2])]
    assert [(b["player"], b["holes"], b["from"], b["to"]) for b in h["bounce_backs"]] == [("Bob", [5, 6], "bogey", "birdie")]
    assert h["blowups"][0]["hole"] == 6 and h["blowups"][0]["over"] == 3


def test_booth_phrases_shot_log_and_transcript(db):
    sid = shot(db, hole="Hole 1, par 4, 400 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=400)
    lines = [("pbp", "shot", "Onto the green, Duke, onto the green.", sid),
             ("color", "shot", "What a strike, Nigel! What a strike.", sid),
             ("pbp", "opener", "Welcome to Pebble.", None)]
    for i, (who, kind, text, shot_id) in enumerate(lines):
        db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (1, ?, ?, ?, ?, ?)",
                   (shot_id, i if shot_id is None else 100 + i, kind, who, text))
    db.execute("INSERT INTO predictions (session_id, ts, commentator, kind, predicted, actual, correct)"
               " VALUES (1, 0, 'color', 'putt', 'make', 'made', 1)")
    db.commit()
    names = {"pbp": "Nigel", "color": "Duke"}
    b = A.booth(db, names)
    pbp = next(c for c in b["commentators"] if c["key"] == "pbp")
    assert pbp["name"] == "Nigel" and pbp["lines"] == 2 and pbp["kinds"] == {"shot": 1, "opener": 1}
    assert "onto the green" in pbp["phrases"] and not any("duke" in p for p in pbp["phrases"])
    assert b["calls"][0]["who"] == "Duke"
    (s,) = A.shot_log(db, player="Ann")
    assert [ln["speaker"] for ln in s["lines"]] == ["pbp", "color"]
    assert A.shot_log(db, club="PW") == []
    t = A.transcript(db, 1, names)
    assert [(x["name"], x["kind"]) for x in t] == [("Nigel", "opener"), ("Nigel", "shot"), ("Duke", "shot")]
    assert [x["id"] for x in A.broadcasts(db)] == [1]


def test_published_site_has_the_summary_shots_and_transcripts(db, tmp_path):
    from commentator import stats_site

    shot(db, hole="Hole 1, par 4, 400 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=400)
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text) VALUES (1, 0, 'opener', 'pbp', 'Hello.')")
    db.commit()
    vdir = tmp_path / "site" / "v" / stats_site.build(db.path, tmp_path / "site", {"pbp": "Nigel"})
    pub = sqlite3.connect(vdir / "stats.db")
    sql, args = stats_site.shot_query(None, {"player": "Ann", "club": "DR"})
    assert [json.loads(r[0])["club"] for r in pub.execute(sql, args)] == ["DR"]
    t = json.loads(pub.execute("SELECT json FROM pub_transcript WHERE session_id = 1").fetchone()[0])
    assert t[0]["name"] == "Nigel"
    pub.close()
    body = json.loads((vdir / "summary-all.json").read_text())
    assert {"courses", "highlights", "booth", "broadcasts", "filters"} <= set(body)
    assert body["players"][0]["gapping"] is not None and "trend" in body["players"][0]


def test_closest_approaches_leave_out_chips_and_par_4_tee_shots(db):
    shot(db, hole="Hole 1, par 4, 300 yards", shot_number=1, club="DR", lie_before="tee", to_pin_yards=300, finish_yards=1)
    shot(db, hole="Hole 2, par 3, 150 yards", shot_number=1, club="8I", lie_before="tee", to_pin_yards=150, finish_yards=3)
    shot(db, hole="Hole 3, par 4, 400 yards", shot_number=2, club="9I", lie_before="fairway", to_pin_yards=130,
         finish_yards=5)
    shot(db, hole="Hole 4, par 5, 500 yards", shot_number=2, club="3W", lie_before="fairway", to_pin_yards=260,
         finish_yards=0.5)                                                   # too far out to be an approach
    shot(db, hole="Hole 5, par 4, 400 yards", shot_number=3, club="LW", lie_before="rough", to_pin_yards=20,
         finish_yards=0.2)
    shot(db, hole="Hole 6, par 4, 400 yards", shot_number=3, club="SW", lie_before="sand", to_pin_yards=15,
         finish_yards=0, outcome="holed")
    db.commit()
    h = A.highlights(db, A.holes_played(db))
    assert [x["club"] for x in h["approaches"]] == ["8I", "9I"]
    assert [(x["club"], x["outcome"]) for x in h["chips"]] == [("SW", "holed"), ("LW", None)]
    assert stats_web.records(db)["closest_approach"]["club"] == "8I"


def test_a_season_db_from_before_the_stored_nine_still_opens(db):
    # Stats browsing and publishing open the database read-only and never migrate it, so a file
    # copied from the sim PC before the nine columns existed has to fall back to prorating rather
    # than fail to open at all.
    player_round(db, "Ann", {h: 4 for h in range(1, 10)})
    db.execute("ALTER TABLE player_rounds DROP COLUMN nine_to_par")
    db.commit()
    p = stats_web.player(db, "Ann")
    assert p["rounds"] == 1 and p["best_to_par_9"] == 0
    assert [c["rounds"] for c in A.courses(db)] == [1]
