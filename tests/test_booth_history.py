"""The booth's history reads (commentator/booth_history.py) give what the queries they replaced gave, kept
up to date through the booth's writes, another connection's and a new day."""
import random
import sqlite3
import time

from commentator.store import DRIVE_SQL, UNKNOWN_CLUB_DRIVE_SQL, Store, _ctx_sql, _DRIVE, _median

OK = "attributed = 1 AND mulligan = 0 AND left_out = 0 AND excluded = 0"
BEFORE_TODAY = "session_id NOT IN (SELECT id FROM sessions WHERE date = (SELECT date FROM sessions WHERE id = ?))"


def one(db, sql, params):
    r = db.execute(sql, params).fetchone()
    return {k: r[k] for k in r.keys()}


# The queries Store ran before booth_history, as they were.
def ref_season_stats(db, sid, player, ctx):
    return one(db, f"""SELECT COUNT(*) AS shots, MAX(CASE WHEN {DRIVE_SQL} THEN {_DRIVE} END) AS longest_drive,
        AVG(CASE WHEN {DRIVE_SQL} THEN {_DRIVE} END) AS avg_drive, COUNT(DISTINCT session_id) AS nights
        FROM shots WHERE player = ? AND {BEFORE_TODAY} AND mulligan = 0 AND left_out = 0 AND excluded = 0"""
               + _ctx_sql(ctx), (player, sid))


def ref_season_longest_drive(db, sid, ctx):
    r = db.execute(f"""SELECT player, {_DRIVE} AS carry FROM shots WHERE {DRIVE_SQL} AND player IS NOT NULL
        AND player != 'unknown' AND {BEFORE_TODAY} AND mulligan = 0 AND left_out = 0 AND excluded = 0"""
                   + _ctx_sql(ctx) + " ORDER BY carry DESC, id LIMIT 1", (sid,)).fetchone()
    return (r["player"], r["carry"]) if r else None


def ref_player_profile(db, player, ctx):
    d = one(db, f"""SELECT COUNT(*) AS shots, COUNT(DISTINCT session_id) AS nights,
        AVG(CASE WHEN is_putt = 0 THEN start_direction END) AS avg_start_dir,
        COUNT(CASE WHEN is_putt = 0 AND start_direction IS NOT NULL THEN 1 END) AS aimed
        FROM shots WHERE player = ? AND {OK}""" + _ctx_sql(ctx), (player,))
    fav = db.execute(f"""SELECT club, COUNT(*) AS n FROM shots WHERE player = ? AND {OK} AND club IS NOT NULL
        AND club NOT IN ('PT')""" + _ctx_sql(ctx) + " GROUP BY club ORDER BY n DESC, club LIMIT 1",
                     (player,)).fetchone()
    d["favorite_club"], d["favorite_club_n"] = (fav["club"], fav["n"]) if fav else (None, 0)
    return d


def ref_approach_proximity(db, player, yards, band, ctx):
    return one(db, f"""SELECT COUNT(*) AS n, ROUND(AVG(finish_yards), 1) AS avg_finish_yards FROM shots
        WHERE player = ? AND {OK} AND is_putt = 0 AND finish_yards IS NOT NULL AND to_pin_yards BETWEEN ? AND ?"""
               + _ctx_sql(ctx), (player, yards - band, yards + band))


def ref_putting_make_rate(db, player, feet, band, ctx):
    lo, hi = max(0.0, feet - band) / 3.0, (feet + band) / 3.0
    return one(db, f"""SELECT COUNT(*) AS attempts, SUM(CASE WHEN outcome = 'holed' THEN 1 ELSE 0 END) AS made
        FROM shots WHERE player = ? AND {OK} AND is_putt = 1 AND outcome IS NOT NULL AND outcome != 'gimme'
        AND to_pin_yards BETWEEN ? AND ?""" + _ctx_sql(ctx), (player, lo, hi))


def ref_three_putt_rate(db, player, feet, band, ctx):
    lo, hi = max(0.0, feet - band) / 3.0, (feet + band) / 3.0
    return one(db, f"""WITH hole_putts AS (
        SELECT to_pin_yards, ROW_NUMBER() OVER (PARTITION BY session_id, course, hole ORDER BY ts, id) AS putt_no,
               COUNT(*) OVER (PARTITION BY session_id, course, hole) AS putts
        FROM shots WHERE player = ? AND {OK} AND hole IS NOT NULL
              AND (lie_before = 'green' OR (is_putt = 1 AND lie_before IS NULL))""" + _ctx_sql(ctx) + """)
        SELECT COUNT(*) AS holes, SUM(CASE WHEN putts >= 3 THEN 1 ELSE 0 END) AS three_putts
        FROM hole_putts WHERE putt_no = 1 AND to_pin_yards BETWEEN ? AND ?""", (player, lo, hi))


def ref_approach_leave(db, sid, player, lo, hi):
    return one(db, f"""SELECT COUNT(*) AS n, AVG(CASE WHEN outcome = 'holed' THEN 0 ELSE finish_yards END)
        AS avg_finish_yards FROM shots WHERE player = ? AND {OK} AND is_putt = 0 AND to_pin_yards >= ?
        AND to_pin_yards < ? AND finish_yards IS NOT NULL AND (lie_after = 'green' OR outcome IN ('holed', 'conceded'))
        AND {BEFORE_TODAY}""", (player, lo, hi if hi is not None else 1e9, sid))


def ref_putt_band_rate(db, sid, player, lo, hi):
    return one(db, f"""SELECT COUNT(*) AS attempts, SUM(CASE WHEN outcome = 'holed' THEN 1 ELSE 0 END) AS made
        FROM shots WHERE player = ? AND {OK} AND is_putt = 1 AND outcome IS NOT NULL AND outcome != 'gimme'
        AND to_pin_yards > ? AND to_pin_yards <= ? AND {BEFORE_TODAY}""",
               (player, lo / 3.0, (hi if hi is not None else 1e9) / 3.0, sid))


def ref_length_norm(db, player, club):
    base = f"""SELECT total_yards, to_pin_yards FROM shots WHERE player = ? AND {OK} AND is_putt = 0
        AND total_yards IS NOT NULL AND total_yards > 0"""
    if club is None:
        totals = [r[0] for r in db.execute(base + " AND lie_before = 'tee' AND to_pin_yards >= 250", (player,))]
    else:
        rows = db.execute(base + " AND club = ? AND to_pin_yards >= 40 AND lie_before IN ('tee', 'fairway')",
                          (player, club)).fetchall()
        rough = _median([r[0] for r in rows])
        totals = [r[0] for r in rows if rough and r[1] > rough * 1.15]
    return {"n": len(totals), "avg_total": _median(totals)}


def ref_fairways_hit(db, player, ctx):
    return one(db, f"""SELECT COUNT(*) AS tries, SUM(CASE WHEN lie_after = 'fairway' THEN 1 ELSE 0 END) AS hit
        FROM shots WHERE player = ? AND {OK} AND is_putt = 0
        AND ((club = 'DR' AND shot_number = 1) OR {UNKNOWN_CLUB_DRIVE_SQL}) AND lie_after IS NOT NULL"""
               + _ctx_sql(ctx), (player,))


def ref_recovery_proximity(db, player, lies, max_to_pin, ctx):
    clause = " AND to_pin_yards <= ?" if max_to_pin is not None else ""
    return one(db, f"""SELECT COUNT(*) AS n, ROUND(AVG(finish_yards), 1) AS avg_finish_yards FROM shots
        WHERE player = ? AND {OK} AND is_putt = 0 AND finish_yards IS NOT NULL
        AND lie_before IN ({",".join("?" for _ in lies)}){clause}{_ctx_sql(ctx)}""",
               [player, *lies] + ([max_to_pin] if max_to_pin is not None else []))


PLAYERS = ("Ann", "Bo", "nobody")
CONTEXTS = (None, "league", "practice")
LIES = ("tee", "fairway", "rough", "sand", "green", None)


def add_shots(db, rnd, sessions, n):
    """n random shots, on a half-yard grid so the band edges and ties come up."""
    for _ in range(n):
        putt = rnd.random() < 0.35
        club = "PT" if putt and rnd.random() < 0.8 else rnd.choice(("DR", "DR", "7I", "PW", None))
        lie = "green" if putt and rnd.random() < 0.7 else rnd.choice(LIES)
        db.execute(
            """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, start_direction,
                   lie_before, lie_after, to_pin_yards, finish_yards, total_yards, carry_game_yards, outcome,
                   attributed, is_league, mulligan, left_out, excluded)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rnd.choice(sessions), rnd.choice((1.0, 2.0, 3.0, 4.0)), rnd.choice(("Ann", "Bo", "unknown", None)),
             rnd.choice(("c1", "c2")), rnd.choice((1, 2, None)), rnd.choice((1, 1, 2, 3, None)), club, int(putt),
             rnd.choice((None, -2.5, 0.0, 1.5)), lie, rnd.choice(("fairway", "green", "rough", None)),
             rnd.choice((None, 1.0, 2.5, 5.0, 40.0, 100.0, 137.5, 150.0, 260.0, 300.0)) if not putt
             else rnd.choice((None, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)),
             rnd.choice((None, 0.0, 2.5, 8.0, 15.5)), rnd.choice((None, 0.0, 120.0, 250.5, 280.0, 301.0)),
             rnd.choice((None, 230.0, 275.5)), rnd.choice(("holed", "conceded", "gimme", "missed", None)),
             int(rnd.random() < 0.85), int(rnd.random() < 0.6), int(rnd.random() < 0.05),
             int(rnd.random() < 0.05), rnd.choice((0, 0, 0, 0, 0, 0, 1, 2))))


def check(st):
    db, sid = st.db, st.session_id
    for ctx in CONTEXTS + ("tournament",):
        assert st.season_longest_drive(ctx) == ref_season_longest_drive(db, sid, ctx), ctx
    for p in PLAYERS:
        for ctx in CONTEXTS:
            assert st.season_stats(p, ctx) == ref_season_stats(db, sid, p, ctx)
            assert st.player_profile(p, ctx) == ref_player_profile(db, p, ctx)
            assert st.fairways_hit(p, ctx) == ref_fairways_hit(db, p, ctx)
            for y, band in ((5.0, 5.0), (100.0, 37.5), (150.0, 15.0), (275.0, 15.0)):
                assert st.approach_proximity(p, y, band, ctx) == ref_approach_proximity(db, p, y, band, ctx)
            for feet, band in ((1.5, 1.5), (3.0, 2.5), (6.0, 3.0), (15.0, 10.0), (30.0, 2.5)):
                assert st.putting_make_rate(p, feet, band, ctx) == ref_putting_make_rate(db, p, feet, band, ctx)
                assert st.three_putt_rate(p, feet, band, ctx) == ref_three_putt_rate(db, p, feet, band, ctx)
            for lies in (("rough",), ("sand", "rough"), ("tee", "fairway"), ("nowhere",)):
                for mx in (None, 2.5, 100.0):
                    assert st.recovery_proximity(p, lies, mx, ctx) == ref_recovery_proximity(db, p, lies, mx, ctx)
        for lo, hi in ((0, 3), (3, 6), (6, 15), (15, None), (3, 3)):
            assert st.putt_band_rate(p, lo, hi) == ref_putt_band_rate(db, sid, p, lo, hi)
        for lo, hi in ((0, 2.5), (2.5, 100), (100, 150), (150, None), (100, 100)):
            assert st.approach_leave(p, lo, hi) == ref_approach_leave(db, sid, p, lo, hi)
        for club in ("DR", "7I", "PW", None, "3W"):
            assert st.length_norm(p, club) == ref_length_norm(db, p, club)


def test_the_booth_history_reads_match_the_queries_through_every_kind_of_write(tmp_path):
    path = str(tmp_path / "season.db")
    rnd = random.Random(7)
    setup = Store(path, new_session=False)
    today = time.strftime("%Y-%m-%d")
    past = [setup.db.execute("INSERT INTO sessions (started_at, date) VALUES (?, ?)", (i, d)).lastrowid
            for i, d in enumerate(("2024-05-01", "2024-05-08", "2024-05-08", "2024-05-15"))]
    earlier_today = setup.db.execute("INSERT INTO sessions (started_at, date) VALUES (5, ?)", (today,)).lastrowid
    add_shots(setup.db, rnd, past + [earlier_today], 600)
    setup.db.commit()
    setup.db.close()

    st = Store(path)
    check(st)
    check(st)   # (from what's kept)

    add_shots(st.db, rnd, [st.session_id], 40)   # the booth's shots tonight
    st.db.commit()
    check(st)

    st.db.execute("UPDATE shots SET excluded = 1 WHERE session_id = ? AND player = 'Ann'", (past[1],))   # an old round
    st.db.commit()
    check(st)

    other = sqlite3.connect(path)   # the stats page's edits
    other.execute("UPDATE shots SET left_out = 1 WHERE id IN (SELECT id FROM shots WHERE session_id = ? LIMIT 30)",
                  (past[0],))
    other.execute("UPDATE shots SET to_pin_yards = 3.0, outcome = 'holed' WHERE session_id = ? AND is_putt = 1",
                  (earlier_today,))
    other.commit()
    check(st)

    st.db.execute("DELETE FROM shots WHERE session_id = ? AND id % 3 = 0", (past[3],))
    st.db.execute("UPDATE sessions SET date = '2024-05-08' WHERE id = ?", (past[3],))
    st.db.commit()
    check(st)

    for sid in (st.session_id, past[1]):   # a write rolled back, then one that isn't (read or not between)
        for read_between in (True, False):
            add_shots(st.db, rnd, [sid], 1)
            check(st)
            st.db.rollback()
            if read_between:
                check(st)
            add_shots(st.db, rnd, [sid], 1)
            st.db.commit()
            check(st)

    old = st.db.execute("SELECT * FROM shots WHERE session_id = ? AND player = 'Bo' LIMIT 1", (past[3],)).fetchone()
    cols = [k for k in old.keys() if k not in ("session_id", "player")]
    st.db.execute(f"INSERT OR REPLACE INTO shots (session_id, player, {','.join(cols)}) VALUES (?, 'Ann', "
                  f"{','.join('?' * len(cols))})", (st.session_id, *(old[k] for k in cols)))   # moved to tonight
    st.db.commit()
    check(st)
    old = st.db.execute(f"SELECT uid FROM shots WHERE session_id = ? AND player = 'Ann' AND {OK} LIMIT 1",
                        (past[2],)).fetchone()
    st.db.execute("UPDATE OR REPLACE shots SET uid = ? WHERE id = (SELECT MAX(id) FROM shots WHERE session_id = ?)",
                  (old[0], st.session_id))   # tonight's shot taking an old one's place
    st.db.commit()
    check(st)

    assert st.roll_session_if_idle(now=time.time() + 3 * 86400)   # a new day: tonight's shots are history
    check(st)
    add_shots(st.db, rnd, [st.session_id], 40)
    st.db.commit()
    check(st)

    newest = st.session_id   # a booth whose session isn't the newest (a maintenance open) still reads right
    st.session_id = past[2]
    add_shots(st.db, rnd, [past[2]], 20)
    st.db.commit()
    check(st)
    add_shots(st.db, rnd, [newest], 20)
    st.db.commit()
    check(st)
    st.session_id = None
    check(st)
    add_shots(st.db, rnd, [newest], 20)
    st.db.commit()
    check(st)
