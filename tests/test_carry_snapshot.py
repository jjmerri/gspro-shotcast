"""Store.carry_model, which starts from the carry_snapshot, learns what a CarryModel built from every shot
learns, through the booth's writes, another connection's, a new day and a change of altitudes."""
import random
import sqlite3
import time

from commentator.carry_model import CarryModel
from commentator.store import Store

DEFAULTS = {"rough": 0.9}
ALT = {"c1": 5000}
DAY = 86400.0


def midnight(now):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def add_shots(db, rnd, sessions, times, n):
    """About n strokes, a hole at a time (each stroke's next says where it came to rest), from the given times."""
    for _ in range(max(1, n // 3)):
        sid, t, player, course = rnd.choice(sessions), rnd.choice(times), rnd.choice(("Ann", "Bo")), rnd.choice(("c1", "c2"))
        hole, fmt = rnd.choice((1, 2, 3)), rnd.choice((None, None, None, "scramble"))
        strokes = [(k, t + 10.0 * (k - 1)) for k in (1, 2, 3)]
        if rnd.random() < 0.2:
            strokes.reverse()   # written out of order
        for k, ts in strokes:
            tile = rnd.random() < 0.5
            db.execute(
                """INSERT INTO shots (session_id, ts, uid, player, course, hole, shot_number, is_putt, mulligan, fmt,
                       lie_before, carry_raw, units, carry_raw_tile_yards, carry_game_yards, elevation_yards, to_pin,
                       launch_angle, finish_yards, attributed, shared_ball, left_out, tree_hits, excluded)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, ts, "%016x" % rnd.getrandbits(64), player, course, hole, k, int(rnd.random() < 0.05),
                 int(rnd.random() < 0.05), fmt, rnd.choice(("tee", "fairway", "rough", "rough", "sand", "deep rough")),
                 None if tile else rnd.uniform(30, 280), rnd.choice(("yards", "yards", "meters")),
                 rnd.uniform(30, 280) if tile else None, rnd.uniform(25, 290) if rnd.random() < 0.95 else None,
                 rnd.uniform(-15, 15) if rnd.random() < 0.95 else None,
                 rnd.choice(("150 yards", "40 yards", "5 yards")), rnd.choice((None, 9.0, 16.0, 25.0)),
                 rnd.choice((None, None, 0.0, 12.0)), int(rnd.random() < 0.95), int(rnd.random() < 0.05),
                 int(rnd.random() < 0.03), None if rnd.random() < 0.97 else 1, 0 if rnd.random() < 0.9 else 1))


def norm(state):
    return {k: sorted(v) for k, v in state.items()}


def check(st, now, altitudes=ALT):
    """What carry_model learns, and the snapshot it leaves, against a CarryModel of every shot."""
    full = norm(CarryModel(DEFAULTS, st.carry_pairs(), altitudes).state())
    assert norm(st.carry_model(DEFAULTS, altitudes, now).state()) == full
    snap = st.db.execute("SELECT before_ts, state FROM carry_snapshot").fetchone()
    assert snap is not None and snap["before_ts"] == midnight(now)
    before = [p for ts, p in st._carry_pairs() if ts < midnight(now)]
    assert norm(CarryModel(DEFAULTS, None, altitudes).state()) != norm(CarryModel(DEFAULTS, before, altitudes).state())
    from json import loads
    assert norm(loads(snap["state"])) == norm(CarryModel(DEFAULTS, before, altitudes).state())


def snapshot(st):
    return st.db.execute("SELECT COUNT(*) FROM carry_snapshot").fetchone()[0]


def test_the_carry_model_from_its_snapshot_matches_one_from_every_shot(tmp_path):
    path = str(tmp_path / "season.db")
    rnd = random.Random(3)
    now = time.time()
    m = midnight(now)
    past_times = [m - d * DAY + h * 3600 for d in (3, 2, 1) for h in (19, 20, 21)] + [m - 60.0]
    today = [m, m + 60.0, m + 3600.0, m + 7200.0]
    setup = Store(path, new_session=False)
    past = [setup.db.execute("INSERT INTO sessions (started_at, date) VALUES (?, ?)", (m - d * DAY, f"d{d}")).lastrowid
            for d in (3, 2, 1)]
    add_shots(setup.db, rnd, past, past_times, 500)
    late = setup.db.execute("INSERT INTO sessions (started_at, date) VALUES (?, 'late')", (m - 60,)).lastrowid
    add_shots(setup.db, rnd, [late], [m - 60.0, m + 30.0], 20)   # a round that ran past midnight
    setup.db.commit()
    setup.db.close()

    st = Store(path)
    assert snapshot(st) == 0
    check(st, now)   # the first start saves it
    seen = []
    pairs = st._carry_pairs
    st._carry_pairs = lambda since=None: seen.append(since) or pairs(since)
    check(st, now)   # the next uses it
    assert m in seen
    del st._carry_pairs

    add_shots(st.db, rnd, [st.session_id], today, 40)   # tonight's shots: it still holds
    st.db.commit()
    assert snapshot(st) == 1
    check(st, now)

    def dropped_by(sql, params=(), db=None):
        db = db or st.db
        assert snapshot(st) == 1
        db.execute(sql, params)
        db.commit()
        assert snapshot(st) == 0, sql
        check(st, now)

    old = lambda: st.db.execute("SELECT id, uid FROM shots WHERE session_id = ? AND excluded = 0 AND is_putt = 0 "
                                "ORDER BY random() LIMIT 1", (past[1],)).fetchone()
    st.db.execute("UPDATE shots SET picked = 1, record_uuid = 'r', outcome = 'holed' WHERE session_id = ?", (past[1],))
    st.db.commit()
    assert snapshot(st) == 1   # nothing it learns from
    check(st, now)
    other = sqlite3.connect(path)   # the stats page
    dropped_by("UPDATE shots SET carry_game_yards = carry_game_yards + 7 WHERE id = ?", (old()["id"],), other)
    dropped_by("UPDATE shots SET excluded = 1 WHERE session_id = ?", (past[2],), other)
    dropped_by("DELETE FROM shots WHERE id = ?", (old()["id"],))
    dropped_by("UPDATE shots SET rowid = (SELECT MAX(id) + 7 FROM shots) WHERE id = ?", (old()["id"],))   # renumbered
    dropped_by("UPDATE shots SET ts = ? WHERE id = (SELECT MAX(id) FROM shots WHERE session_id = ?)",
               (m - 5.0, st.session_id))   # tonight's shot moved before midnight
    dropped_by("UPDATE shots SET ts = ? WHERE id = ?", (m + 5.0, old()["id"]))   # an old one moved after
    add_shots(st.db, rnd, [st.session_id], [m - 1.0], 1)   # a shot stored late, timed before midnight
    st.db.commit()
    assert snapshot(st) == 0
    check(st, now)
    add_shots(st.db, rnd, [late], [m + 90.0], 1)   # the late round's next stroke: its last before midnight's
    st.db.commit()
    assert snapshot(st) == 0
    check(st, now)

    def fresh():   # a session of tonight's shots only, so only the trigger under test can see an old shot
        sid = st.db.execute("INSERT INTO sessions (started_at, date) VALUES (?, 'fresh')", (m + 1,)).lastrowid
        add_shots(st.db, rnd, [sid], today, 9)
        st.db.commit()
        check(st, now)
        return sid, st.db.execute("SELECT * FROM shots WHERE session_id = ? AND is_putt = 0 ORDER BY id DESC",
                                  (sid,)).fetchone()

    sid, t = fresh()
    dropped_by("UPDATE shots SET session_id = ?, ts = ? WHERE id = ?", (sid, m + 500.0, old()["id"]))   # moved out
    sid, t = fresh()
    dropped_by("UPDATE shots SET session_id = ? WHERE id = ?", (past[0], t["id"]))   # moved into an old round
    sid, t = fresh()
    cols = [k for k in t.keys() if k != "id"]
    dropped_by(f"INSERT OR REPLACE INTO shots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
               [old()["uid"] if k == "uid" else t[k] for k in cols])   # tonight's shot in an old one's place
    sid, t = fresh()
    dropped_by("UPDATE OR REPLACE shots SET uid = ? WHERE id = ?", (old()["uid"], t["id"]))
    sid, t = fresh()
    dropped_by("UPDATE shots SET ts = ? WHERE id = ?", (m - 30.0, t["id"]))
    dropped_by("DELETE FROM shots WHERE id = ?", (t["id"],))   # its round's only shot before midnight

    st.db.execute("UPDATE shots SET carry_game_yards = 1 WHERE id = ?", (old()["id"],))   # rolled back
    st.db.rollback()
    assert snapshot(st) == 1
    check(st, now)

    st.db.execute("UPDATE shots SET carry_game_yards = 100 WHERE id = ?", (old()["id"],))
    assert norm(st.carry_model(DEFAULTS, ALT, now).state()) == norm(   # inside a transaction: not saved
        CarryModel(DEFAULTS, st.carry_pairs(), ALT).state())
    assert st.db.in_transaction and snapshot(st) == 0
    st.db.commit()

    check(st, now, {"c1": 5000, "c2": 7000})   # other altitudes: learned again
    check(st, now)
    check(st, now + DAY)   # a new day: tonight's shots go in the snapshot
    add_shots(st.db, rnd, [st.session_id], [m + DAY + 10], 1)   # tonight's session has rows before it now
    st.db.commit()
    assert snapshot(st) == 0
    check(st, now + DAY)
