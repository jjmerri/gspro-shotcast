import json
import sqlite3

import pytest

from commentator import round_record as R
from commentator.store import CLOSER_SECONDS, FORMAT, Store

T0 = 1_700_000_000.0


def _raw(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _season(path):
    """A season written as an older version of the app wrote it (no records): two sessions, the second
    a booth restart that finished a round begun in the first; an excluded and a deleted round; practice
    before, between and after; a putt conceded after its round was recorded; a round quit early."""
    Store(str(path)).close()
    db = _raw(path)
    db.execute("DELETE FROM round_records")
    db.execute("UPDATE shots SET record_uuid = NULL")
    db.execute("PRAGMA user_version = 5")
    db.execute("INSERT INTO sessions (id, started_at, date) VALUES (2, ?, '2025-01-01')", (T0 + 5000,))
    db.execute("INSERT INTO sessions (id, started_at, date) VALUES (3, ?, '2025-01-02')", (T0 + 90000,))

    def shot(sess, ts, player="Ann", course="Pebble", outcome=None, excluded=0):
        return db.execute("INSERT INTO shots (session_id, ts, player, course, hole, outcome, excluded, is_putt)"
                          " VALUES (?, ?, ?, ?, 'Hole 1', ?, ?, 0)", (sess, T0 + ts, player, course, outcome, excluded)).lastrowid

    def line(sess, ts, shot_id=None, excluded=0):
        return db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text, excluded)"
                          " VALUES (?, ?, ?, 'shot', 'pbp', 'x', ?)", (sess, shot_id, T0 + ts, excluded)).lastrowid

    def side(sess, end, side, course="Pebble", complete=1, excluded=0, players=None):
        return db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, complete, excluded, players)"
                          " VALUES (?, ?, '2025-01-01', ?, ?, 18, ?, ?, ?)",
                          (sess, T0 + end, course, side, complete, excluded, players)).lastrowid

    def card(sess, end, player, course="Pebble", excluded=0):
        db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, excluded) VALUES (?, ?, ?, ?, ?)",
                   (sess, T0 + end, player, course, excluded))

    shot(1, -7200, course="Range")                    # practice, hours before
    line(1, -7100)
    # Round 1 (Pebble, session 1): two players, two sides; a late gimme and a closer after it's recorded.
    a = shot(1, 0)
    line(1, 1, a)
    shot(1, 10, "Bo")
    side(1, 100, "Ann", players='["Ann"]')
    side(1, 110, "Team B", players='["Bo"]')
    card(1, 100, "Ann")
    card(1, 110, "Bo")
    shot(1, 100 + CLOSER_SECONDS / 2, outcome="gimme")
    line(1, 100 + CLOSER_SECONDS / 2)
    # Practice on another course between rounds.
    shot(1, 2000, course="Range")
    # Round 2 begun in session 1, finished after a restart in session 2 (excluded from the stats).
    shot(1, 4000, course="Augusta", excluded=1)
    shot(2, 6000, course="Augusta", excluded=1)
    line(2, 6001, excluded=1)
    side(2, 6100, "Ann", course="AUGUSTA", excluded=1)
    # Round 3, deleted, quit early.
    shot(3, 90010, excluded=2)
    side(3, 90100, "Ann", complete=0, excluded=2)
    card(3, 90100, "Ann", excluded=2)
    # Practice after, in two stretches (an idle gap).
    shot(3, 95000, course="Range")
    shot(3, 95000 + R.PRACTICE_IDLE_SECONDS + 1, course="Range")
    db.commit()
    db.close()


def _grouping(db):
    """Each row's record, by table."""
    return {t: dict(db.execute(f"SELECT id, {col} FROM {t}").fetchall())
            for t, col in (("shots", "record_uuid"), ("lines", "record_uuid"), ("rounds", "round_uuid"),
                           ("player_rounds", "round_uuid"))}


def test_the_migration_stores_the_rounds_the_time_rules_give(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    Store(str(path), new_session=False).close()
    db = _raw(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == FORMAT
    got = _grouping(db)
    assert all(u for rows in got.values() for u in rows.values())   # every row has a record
    groups = R.membership(db)
    assert len(groups) == 3
    seen = set()
    for g in groups:
        (u,) = {got["rounds"][i] for i in g["sides"]} | {got["player_rounds"][i] for i in g["cards"]} \
            | {got["shots"][i] for i in g["shots"]} | {got["lines"][i] for i in g["lines"]}
        assert u not in seen
        seen.add(u)
        rec = db.execute("SELECT * FROM round_records WHERE uuid = ?", (u,)).fetchone()
        assert (rec["kind"], rec["assigned"], rec["ended_at"]) == ("round", "inferred", g["end"])
    states = [r[0] for r in db.execute("SELECT state FROM round_records WHERE kind = 'round' ORDER BY ended_at")]
    assert states == ["finished", "finished", "abandoned"]
    # The late gimme and the closer are round 1's; round 2 spans the restart.
    r1, r2, _ = groups
    # (and so is the session's line before it: no round came before it to end the stretch)
    assert len(r1["shots"]) == 3 and len(r1["lines"]) == 3 and len(r1["sides"]) == 2 and len(r1["cards"]) == 2
    assert {db.execute("SELECT session_id FROM shots WHERE id = ?", (i,)).fetchone()[0] for i in r2["shots"]} == {1, 2}
    # Everything else is practice: before, between, and two stretches after.
    in_round = {i for g in groups for i in g["shots"]}
    practice = {u for i, u in got["shots"].items() if i not in in_round}
    assert len(practice) == 4
    assert {r[0] for r in db.execute(f"SELECT kind FROM round_records WHERE uuid IN ({','.join('?' * 4)})",
                                     tuple(practice))} == {"practice"}
    # Slots: one per player, on the side they played on.
    slots = db.execute("SELECT name, side, matched_by FROM round_slots WHERE round_uuid = ? ORDER BY name",
                       (got["rounds"][r1["sides"][0]],)).fetchall()
    assert [tuple(s) for s in slots] == [("Ann", "Ann", "legacy"), ("Bo", "Team B", "legacy")]
    assert db.execute("SELECT COUNT(*) FROM shots WHERE player IS NOT NULL AND slot_id IS NULL").fetchone()[0] == 0
    # Nothing a record shows changed, so no counter moved; every shot and line has its own uid.
    assert db.execute("SELECT MAX(gen) FROM round_records").fetchone()[0] == 0
    for t in ("shots", "lines"):
        assert db.execute(f"SELECT COUNT(DISTINCT uid) = COUNT(*) AND SUM(uid IS NULL) = 0 FROM {t}").fetchone()[0] == 1


def test_an_older_versions_rows_join_their_records_at_the_next_start(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    Store(str(path), new_session=False).close()
    db = _raw(path)
    before = _grouping(db)
    gens = dict(db.execute("SELECT uuid, gen FROM round_records"))
    r1 = before["rounds"][1]
    # An older version (no records, no quiet) adds a line just after round 1 and a practice shot.
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text) VALUES (1, ?, 'shot', 'pbp', 'late')",
               (T0 + 100 + CLOSER_SECONDS - 1,))
    shot = db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt) VALUES (3, ?, 'Cy', 'Range', 0)",
                      (T0 + 99000,)).lastrowid
    db.commit()
    assert db.execute("SELECT uid FROM shots WHERE id = ?", (shot,)).fetchone()[0]   # (the trigger gave it one)
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    after = _grouping(db)
    assert all(u for rows in after.values() for u in rows.values())
    # Records keep their UUIDs; the line joined round 1, whose counter moved.
    for t, rows in before.items():
        assert {i: u for i, u in after[t].items() if i in rows} == rows
    assert after["lines"][max(after["lines"])] == r1
    assert db.execute("SELECT gen FROM round_records WHERE uuid = ?", (r1,)).fetchone()[0] > 0
    assert db.execute("SELECT kind FROM round_records WHERE uuid = ?", (after["shots"][shot],)).fetchone()[0] == "practice"
    # The rest kept theirs: their kept results (stats_cache) stand.
    moved = {u for u, g in db.execute("SELECT uuid, gen FROM round_records") if gens.get(u, g) != g}
    assert moved <= {r1, after["shots"][shot]} and len(gens) > 2


def test_a_live_record_is_never_regrouped(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    Store(str(path), new_session=False).close()
    db = _raw(path)
    # The booth's own record of a practice shot that the time rules would put in round 1.
    db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, ended_at, state, assigned)"
               " VALUES ('live-1', 'practice', 1, ?, ?, 'finished', 'live')", (T0 + 50, T0 + 50))
    sid = db.execute("INSERT INTO shots (session_id, ts, player, course, record_uuid, is_putt) VALUES (1, ?, 'Ann', 'Pebble', 'live-1', 0)",
                     (T0 + 50,)).lastrowid
    lid = db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (1, ?, ?, 'shot', 'pbp', 'x')",
                     (sid, T0 + 51)).lastrowid   # (an older version's line on it: it follows its shot)
    db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt) VALUES (3, ?, 'Cy', 'Range', 0)", (T0 + 99000,))
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    assert db.execute("SELECT record_uuid FROM shots WHERE id = ?", (sid,)).fetchone()[0] == "live-1"
    assert db.execute("SELECT record_uuid FROM lines WHERE id = ?", (lid,)).fetchone()[0] == "live-1"
    # ...and round 1, which the time rules would give it to, stays its own.
    assert {r[0] for r in db.execute("SELECT round_uuid FROM rounds WHERE id IN (1, 2)")} == {_grouping(db)["rounds"][1]} != {"live-1"}


def test_a_card_with_no_side_is_a_round_of_its_own_and_settles(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    db = _raw(path)
    db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, complete) VALUES (3, ?, 'Cy', 'Sawgrass', 0)",
               (T0 + 97000,))
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    u = db.execute("SELECT round_uuid FROM player_rounds WHERE player = 'Cy'").fetchone()[0]
    assert tuple(db.execute("SELECT kind, course, state FROM round_records WHERE uuid = ?", (u,)).fetchone()) \
        == ("round", "Sawgrass", "abandoned")
    assert not R.unassigned(db)
    gens = db.execute("SELECT uuid, gen FROM round_records").fetchall()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    assert db.execute("SELECT uuid, gen FROM round_records").fetchall() == gens
    # Its side turns up after all: the card joins it, and Cy's slot learns the side.
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, complete, players)"
               " VALUES (3, ?, 'd', 'Sawgrass', 'Team C', 18, 0, '[\"Cy\"]')", (T0 + 97001,))
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    u = db.execute("SELECT round_uuid FROM player_rounds WHERE player = 'Cy'").fetchone()[0]
    assert db.execute("SELECT round_uuid FROM rounds WHERE side = 'Team C'").fetchone()[0] == u
    assert db.execute("SELECT side FROM round_slots WHERE round_uuid = ? AND name = 'Cy'", (u,)).fetchone()[0] == "Team C"


def test_a_sides_players_get_slots_without_shots_or_cards(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    db = _raw(path)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, players)"
               " VALUES (3, ?, 'd', 'Sawgrass', 'Team D', 18, '[\"Di\", \"Ed\"]')", (T0 + 130000,))
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    u = db.execute("SELECT round_uuid FROM rounds WHERE side = 'Team D'").fetchone()[0]
    assert {tuple(r) for r in db.execute("SELECT name, side FROM round_slots WHERE round_uuid = ?", (u,))} \
        == {("Di", "Team D"), ("Ed", "Team D")}


def test_a_round_with_no_shots_still_splits_practice(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    db = _raw(path)
    before = db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt) VALUES (3, ?, 'Bo', 'Range', 0)",
                        (T0 + 120000,)).lastrowid
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, complete)"
               " VALUES (3, ?, 'd', 'Pebble', 'Ann', 18, 1)", (T0 + 120100,))
    after = db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt) VALUES (3, ?, 'Bo', 'Range', 0)",
                       (T0 + 120200,)).lastrowid
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    g = _grouping(db)["shots"]
    assert g[before] != g[after]


def test_a_player_who_leaves_a_record_leaves_its_slots(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    Store(str(path), new_session=False).close()
    db = _raw(path)
    # Bo practises with Ann; then a side is recorded for Ann's shot, taking it into a round.
    bo = db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt) VALUES (3, ?, 'Bo', 'Range', 0)",
                    (T0 + 120000,)).lastrowid
    db.execute("INSERT INTO shots (session_id, ts, player, course, is_putt) VALUES (3, ?, 'Ann', 'Pebble', 0)", (T0 + 120010,))
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    practice = db.execute("SELECT record_uuid FROM shots WHERE id = ?", (bo,)).fetchone()[0]
    slots = lambda: {r[0] for r in db.execute("SELECT name FROM round_slots WHERE round_uuid = ?", (practice,))}  # noqa: E731
    assert slots() == {"Ann", "Bo"}
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, complete)"
               " VALUES (3, ?, 'd', 'Pebble', 'Ann', 18, 1)", (T0 + 120100,))
    db.commit()
    db.close()
    Store(str(path), new_session=False).close()
    db = _raw(path)
    assert db.execute("SELECT record_uuid FROM shots WHERE id = ?", (bo,)).fetchone()[0] == practice
    assert slots() == {"Bo"}


def test_counters_move_with_what_a_record_shows(tmp_path):
    st = Store(str(tmp_path / "season.db"))
    db = st.db
    db.execute("INSERT INTO round_records (uuid, kind, state, assigned) VALUES ('a', 'round', 'in_progress', 'live'),"
               " ('b', 'round', 'in_progress', 'live')")
    gen = lambda u: db.execute("SELECT gen FROM round_records WHERE uuid = ?", (u,)).fetchone()[0]  # noqa: E731
    sid = db.execute("INSERT INTO shots (session_id, ts, player, record_uuid, is_putt) VALUES (?, 1, 'Ann', 'a', 0)",
                     (st.session_id,)).lastrowid
    a = gen("a")
    assert a > 0
    db.execute("UPDATE shots SET record_uuid = 'b' WHERE id = ?", (sid,))   # moving a shot changes both
    assert gen("a") > a and gen("b") > 0
    b = gen("b")
    rid = db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, round_uuid)"
                     " VALUES (?, 2, 'd', 'Ann', 18, 'b')", (st.session_id,)).lastrowid
    db.execute("INSERT INTO week_results (ended_at, week, team, net, round_id) VALUES (3, 1, 'T', 0, ?)", (rid,))
    db.execute("UPDATE round_records SET state = 'finished' WHERE uuid = 'b'")
    assert gen("b") >= b + 3
    b = gen("b")
    R.set_quiet(db, "bookkeeping")
    db.execute("DELETE FROM shots WHERE id = ?", (sid,))
    R.set_quiet(db, "0")
    assert gen("b") == b
    st.close()


def test_a_copy_of_the_season_is_recognisable(tmp_path):
    one, two = (Store(str(tmp_path / f"{n}.db"), new_session=False) for n in ("a", "b"))
    ids = [s.db.execute("SELECT dataset_id FROM meta").fetchone()[0] for s in (one, two)]
    assert ids[0] != ids[1]
    one.close()
    assert Store(str(tmp_path / "a.db"), new_session=False).db.execute("SELECT dataset_id FROM meta").fetchone()[0] == ids[0]
    two.close()


@pytest.mark.parametrize("excluded", [0, 1])
def test_round_rows_reads_the_same_membership(tmp_path, excluded):
    from commentator import stats_web

    path = tmp_path / "season.db"
    _season(path)
    db = _raw(path)
    groups = R.membership(db)
    g = groups[excluded]
    assert stats_web._round_rows(db, g["round"], g["end"]) == [g["sides"], g["cards"], g["shots"], g["lines"]]
    assert stats_web._round_rows(db, groups[2]["round"], groups[2]["end"]) is None   # deleted


# ---- written as the booth plays (Live) -----------------------------------------------------------------

def _hit(store, player, at, course="Pebble"):
    from commentator.hud import Hud
    from commentator.models import Shot
    return store.record_shot(player=player, hole="Hole 1", shot=Shot(club="Dr", timestamp=at), before=Hud(),
                             after=Hud(), tags=[], carry_game_yards=None, total_yards=None, offline_yards=None,
                             course=course)


def _said(store, shot_id=None, text="x"):
    from commentator.models import Line
    store.record_lines("shot", [Line("pbp", text)], shot_id=shot_id)
    return store.db.execute("SELECT record_uuid FROM lines ORDER BY id DESC LIMIT 1").fetchone()[0]


def _rec(store, shot_id):
    return tuple(store.db.execute("SELECT record_uuid, slot_id FROM shots WHERE id = ?", (shot_id,)).fetchone())


def _record(store, u):
    return store.db.execute("SELECT kind, state, ended_at FROM round_records WHERE uuid = ?", (u,)).fetchone()


def _slots(store, u):
    return {r[0]: (r[1], r[2], r[3]) for r in store.db.execute(
        "SELECT name, player, seat, matched_by FROM round_slots WHERE round_uuid = ?", (u,))}


def test_a_round_played_gets_its_record_as_it_goes(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    before = _hit(s, "Ann", T0)                        # practice
    opener = _hit(s, "Ann", T0 + 60)                   # an opening drive, struck before the round started
    on_opener = _said(s, opener)
    u = s.begin_round("r1", course="Pebble", players={"Ann": "Ann Smith", "Bo": None}, seats=["Bo", "Ann"],
                      since_id=before)
    assert u == "r1"
    assert _slots(s, u) == {"Ann": ("Ann Smith", 1, "booth"), "Bo": (None, 0, "booth")}
    practice = _rec(s, before)[0]
    assert _record(s, practice)[:2] == ("practice", "finished")
    assert _rec(s, opener)[0] == u and s.db.execute("SELECT record_uuid FROM lines WHERE shot_id = ?",
                                                     (opener,)).fetchone()[0] == u != on_opener
    a = _hit(s, "Ann", T0 + 100)
    b = _hit(s, "Bo", T0 + 110)
    assert _rec(s, a) == (u, _rec(s, opener)[1]) and _rec(s, b)[0] == u and _rec(s, b)[1] != _rec(s, a)[1]
    assert _said(s, a) == u and _said(s) == u          # a line on a shot, and one on none, mid-round
    g = s.record_gimme(b)
    assert _rec(s, g) == _rec(s, b)
    assert s.db.execute("SELECT derived_from FROM shots WHERE id = ?", (g,)).fetchone()[0] == \
        s.db.execute("SELECT uid FROM shots WHERE id = ?", (b,)).fetchone()[0]
    s.end_round(u, complete=True)
    assert _record(s, u)[:2] == ("round", "finished")
    assert _said(s) == u                               # the closer
    late = _rec(s, s.record_gimme(b))                   # a putt conceded after it's recorded
    assert late == _rec(s, b)
    s.db.execute("UPDATE round_records SET state = state")
    R.reconcile(s.db)                                  # the live rows stay put
    assert _rec(s, a)[0] == u and _rec(s, before)[0] == practice and not R.unassigned(s.db)


def test_a_line_long_after_a_round_is_practices(tmp_path, monkeypatch):
    import time as _time
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    s.end_round(u)
    clock = _time.time() + CLOSER_SECONDS + 5
    monkeypatch.setattr(_time, "time", lambda: clock)
    assert _record(s, _said(s))[0] == "practice"


def test_practice_splits_when_the_bay_sits_idle_or_the_booth_restarts(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    a, b = _hit(s, "Ann", T0), _hit(s, "Ann", T0 + R.PRACTICE_IDLE_SECONDS - 1)
    c = _hit(s, "Ann", T0 + 2 * R.PRACTICE_IDLE_SECONDS + 5)
    assert _rec(s, a)[0] == _rec(s, b)[0] != _rec(s, c)[0]
    assert _record(s, _rec(s, a)[0])[1:] == ("finished", T0 + R.PRACTICE_IDLE_SECONDS - 1)
    s.close()
    s = Store(str(tmp_path / "s.db"))                  # a restart ends the open stretch
    assert _record(s, _rec(s, c)[0])[1] == "finished"
    assert _rec(s, _hit(s, "Ann", T0 + 2 * R.PRACTICE_IDLE_SECONDS + 10))[0] != _rec(s, c)[0]


def test_a_restart_mid_round_continues_it_on_the_same_course_with_the_same_players(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Bo": None})
    a = _hit(s, "Ann", T0)
    s.close()
    s = Store(str(tmp_path / "s.db"))                  # restarted before ROUND_SPAN_SECONDS: left waiting
    assert _record(s, u)[1] == "in_progress"
    assert s.begin_round("r2", course="Pebble", players={"bo": None, "ann": None}) == u
    assert _hit(s, "Ann", T0 + 50) and _rec(s, a)[0] == u
    s.end_round(u, complete=False)
    assert _record(s, u)[1] == "abandoned"


def test_a_continued_round_keeps_its_first_start_and_strokes_for_its_round_file(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    first, _ = s.round_start_and_speeds(u)
    s.db.execute("UPDATE shots SET ball_speed = 150.5 WHERE id = ?", (_hit(s, "Ann", T0),))
    s.db.execute("UPDATE shots SET ball_speed = 90, mulligan = 1 WHERE id = ?", (_hit(s, "Ann", T0 + 30),))
    s.close()
    s = Store(str(tmp_path / "s.db"))
    assert s.begin_round("r2", course="Pebble", players={"Ann": None}) == u
    assert s.round_start_and_speeds(u) == (first, [150.5]) and first
    assert s.round_start_and_speeds("nope") == (None, [])


def test_a_restart_into_another_round_abandons_the_old_one(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    import time as _time
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Bo": None})
    last = _time.time() + 5
    _hit(s, "Ann", last)
    s.close()
    s = Store(str(tmp_path / "s.db"))
    assert s.begin_round("r2", course="Pebble", players={"Ann": None}) == "r2"
    assert _record(s, u)[1:] == ("abandoned", last)   # ended at its last shot


def test_a_round_begun_by_this_booth_is_never_continued(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    assert s.begin_round("r2", course="Pebble", players={"Ann": None}) == "r2"   # (never ended)
    assert _record(s, u)[1] == "abandoned"


def test_a_round_begun_again_keeps_its_record(tmp_path):
    # The engine starting the same round again (a missed start joined after it was let go).
    s = Store(str(tmp_path / "s.db"))
    other = s.begin_round("r0", course="Pebble", players={"Bo": None})
    assert s.begin_round("r1", course="Pebble", players={"Ann": None}) == "r1"
    a = _hit(s, "Ann", T0)
    assert s.begin_round("r1", course="Pebble", players={"Ann": None}) == "r1"   # still in progress
    s.end_round("r1", complete=False)
    assert s.begin_round("r1", course="Pebble", players={"Ann": None}) == "r1"   # ended, and taken up again
    assert _record(s, "r1")[1:] == ("in_progress", None) and _rec(s, a)[0] == "r1"
    assert _record(s, other)[1] == "abandoned"


def test_a_booth_starting_ends_a_round_too_old_to_continue(tmp_path, monkeypatch):
    import time as _time
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    s.close()
    later = _time.time() + R.ROUND_SPAN_SECONDS + 60
    monkeypatch.setattr(_time, "time", lambda: later)
    s = Store(str(tmp_path / "s.db"))
    assert _record(s, u)[1] == "abandoned"


def test_marking_and_renaming_a_round_touch_only_its_record(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    floor = s.last_shot_id()
    u = s.begin_round("r1", course="Pebble", players={"Anne": None})
    a = _hit(s, "Anne", T0)
    slot = _rec(s, a)[1]
    s.end_round(u)
    p = _hit(s, "Anne", T0 + 600)                       # practice after it
    assert s.mark_tournament(floor, record=u) == 1 and s.mark_league(floor, record=u) == 1
    assert s.players_after(floor, record=u) == ["Anne"]
    assert s.rename_shots("Anne", "Ann", after_id=floor, record=u) == 1
    s.db.execute("UPDATE shots SET is_league = 0 WHERE id = ?", (p,))
    assert s.mark_league(floor, record=u) == 1
    assert tuple(s.db.execute("SELECT player, is_league, is_tournament FROM shots WHERE id = ?", (p,)).fetchone()) \
        == ("Anne", 0, 0)
    assert _rec(s, a)[1] == slot and _slots(s, u) == {"Ann": (None, None, "booth")}   # the slot keeps its ID


def test_a_shot_given_to_someone_else_moves_slot(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Bo": None})
    a, b = _hit(s, "Ann", T0), _hit(s, "Bo", T0 + 1)
    g = s.record_gimme(a)
    s.move_shot_player(a, "Bo", fmt=None, shared_ball=False, gimme_id=g)
    assert _rec(s, a) == _rec(s, g) == _rec(s, b) == (u, _rec(s, b)[1])
    n = _hit(s, None, T0 + 2)
    assert _rec(s, n) == (u, None)
    s.set_shot_player(n, "Ann", attributed=True, fmt=None, shared_ball=False)
    assert _rec(s, n)[1] == _slots_id(s, u, "Ann")


def _slots_id(store, u, name):
    return store.db.execute("SELECT slot_id FROM round_slots WHERE round_uuid = ? AND name = ?", (u, name)).fetchone()[0]


def test_an_empty_practice_stretch_is_dropped(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    s.records._practice(s.session_id, T0, None)
    s.db.commit()
    s.begin_round("r1", course="Pebble", players={"Ann": None})
    assert s.db.execute("SELECT COUNT(*) FROM round_records WHERE kind = 'practice'").fetchone()[0] == 0


def test_a_league_players_shots_go_to_the_slot_made_with_their_gspro_name(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"MikeD": "Mike"})
    a = _hit(s, "Mike", T0)                           # the booth names him by his roster name
    assert _rec(s, a)[1] == _slots_id(s, u, "MikeD") and len(_slots(s, u)) == 1
    s.close()
    s = Store(str(tmp_path / "s.db"))                  # so a restart still sees the same players
    assert s.begin_round("r2", course="Pebble", players={"MikeD": "Mike"}) == u


def test_a_shot_stored_late_stays_in_the_round_it_was_struck_in(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    s = Store(str(tmp_path / "s.db"))
    a = s.begin_round("r1", course="Pebble", players={"Ann": None})
    s.end_round(a)
    s.begin_round("r2", course="Pebble", players={"Ann": None})
    late = s.record_shot(player="Ann", hole="Hole 18", shot=Shot(club="P"), before=Hud(), after=Hud(), tags=[],
                         carry_game_yards=None, total_yards=None, offline_yards=None, course="Pebble", record=a)
    assert _rec(s, late) == (a, _slots_id(s, a, "Ann"))


def test_a_restart_that_reads_a_name_in_another_case_keeps_the_players_slot(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    a = _hit(s, "Ann", T0)
    s.close()
    s = Store(str(tmp_path / "s.db"))
    assert s.begin_round("r2", course="Pebble", players={"ann": None}) == u
    assert _rec(s, _hit(s, "ann", T0 + 50)) == _rec(s, a) and list(_slots(s, u)) == ["ann"]
    s.end_round(u)
    v = s.begin_round("r3", course="Pebble", players={"José": None})
    j = _hit(s, "José", T0 + 100)
    s.close()
    s = Store(str(tmp_path / "s.db"))
    assert s.begin_round("r4", course="Pebble", players={"JOSÉ": None}) == v
    assert _rec(s, _hit(s, "JOSÉ", T0 + 150)) == _rec(s, j)


def test_a_players_slots_follow_them_joining_and_leaving_mid_round(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Ghost": None}, seats=["Ann", "Ghost"])
    m = _hit(s, "Mike", T0)                           # before the booth added him to the round
    s.round_players(u, {"Ann": None, "MikeD": "Mike"}, ["Ann", "MikeD"])   # Ghost dropped, MikeD added
    assert _slots(s, u) == {"Ann": (None, 0, "booth"), "MikeD": ("Mike", 1, "booth")}
    assert _rec(s, m)[1] == _slots_id(s, u, "MikeD")
    s.close()
    s = Store(str(tmp_path / "s.db"))
    assert s.begin_round("r2", course="Pebble", players={"Ann": None, "MikeD": "Mike"}) == u


def test_a_round_left_too_long_is_not_continued_even_by_a_booth_running_since(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    s.close()
    s = Store(str(tmp_path / "s.db"))
    import time as _time
    s.records.mine.clear()
    assert s.records.begin("r2", session_id=s.session_id, course="Pebble", players={"Ann": None}, seats=None,
                           since_id=None, now=_time.time() + R.ROUND_SPAN_SECONDS + 60) == "r2"
    assert _record(s, u)[1] == "abandoned"


def test_a_misread_name_corrected_leaves_no_slot_behind(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Bo": None})
    a = _hit(s, "Anne", T0)
    assert _slots(s, u)["Anne"] == (None, None, "shot")
    s.set_shot_player(a, "Ann", attributed=True, fmt=None, shared_ball=False)
    assert set(_slots(s, u)) == {"Ann", "Bo"}
    b = _hit(s, "Cy", T0 + 1)                          # a name on a shot, kept till the restart
    s.close()
    s = Store(str(tmp_path / "s.db"))
    assert s.begin_round("r2", course="Pebble", players={"Ann": None, "Bo": None}) == u
    assert _rec(s, b)[0] == u


def test_a_rename_onto_a_misread_shots_name_keeps_the_players_slot(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    floor = s.last_shot_id()
    u = s.begin_round("r1", course="Pebble", players={"Anne": None}, seats=["Anne"])
    a, b = _hit(s, "Ann", T0), _hit(s, "Anne", T0 + 1)
    s.rename_shots("Anne", "Ann", after_id=floor, record=u)
    assert _slots(s, u) == {"Ann": (None, 0, "booth")} and _rec(s, a) == _rec(s, b)
    c = _hit(s, "BO", T0 + 2)                           # and in another case
    s.round_players(u, {"Ann": None, "Bob": None}, ["Ann", "Bob"])
    s.rename_shots("Bob", "Bo", after_id=floor, record=u)
    assert set(_slots(s, u)) == {"Ann", "Bo"} and _rec(s, c)[1] == _slots_id(s, u, "Bo")
    assert _slots(s, u)["Bo"] == (None, 1, "booth")
    assert s.db.execute("SELECT player FROM shots WHERE id = ?", (c,)).fetchone()[0] == "Bo"


def test_the_opener_aired_over_the_load_is_the_rounds(tmp_path, monkeypatch):
    s = Store(str(tmp_path / "s.db"))
    import time
    p = _hit(s, "Ann", time.time())                    # practice, then the opener over the course load
    warmup = _said(s, p)
    opener = _said(s, text="Welcome to Pebble")
    practice = _rec(s, p)[0]
    assert opener == practice
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    got = dict(s.db.execute("SELECT text, record_uuid FROM lines").fetchall())
    assert got == {"x": practice, "Welcome to Pebble": u} and warmup == practice
    s2 = Store(str(tmp_path / "t.db"))                 # an opener with no practice before it: dropped stretch
    _said(s2, text="Welcome")
    v = s2.begin_round("r1", course="Pebble", players={"Ann": None})
    assert s2.db.execute("SELECT record_uuid FROM lines").fetchone()[0] == v
    assert s2.db.execute("SELECT COUNT(*) FROM round_records WHERE kind = 'practice'").fetchone()[0] == 0
    s3 = Store(str(tmp_path / "u.db"))                 # yesterday's practice talk, the booth left running
    _hit(s3, "Ann", time.time())
    old = _said(s3, text="Nice one")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 86400)
    assert s3.roll_session_if_idle()
    w = s3.begin_round("r1", course="Pebble", players={"Ann": None})
    assert s3.db.execute("SELECT record_uuid FROM lines").fetchone()[0] == old != w
    monkeypatch.setattr(time, "time", real)
    s4 = Store(str(tmp_path / "v.db"))                 # the next morning's opener, aired before the round
    _hit(s4, "Ann", time.time())                       # last night's practice
    _said(s4, text="Nice one")
    monkeypatch.setattr(time, "time", lambda: real() + 86400)
    _said(s4, text="Good morning from Pebble")
    old_session = s4.session_id
    assert s4.roll_session_if_idle()
    w = s4.begin_round("r1", course="Pebble", players={"Ann": None})
    got = {t: (u, sid) for t, u, sid in s4.db.execute("SELECT text, record_uuid, session_id FROM lines")}
    assert got["Good morning from Pebble"] == (w, s4.session_id) != (w, old_session)
    assert got["Nice one"][0] != w
    assert s4.db.execute("SELECT COUNT(*) FROM round_records WHERE kind = 'practice'").fetchone()[0] == 1


def test_an_opener_left_in_the_old_sessions_practice_moves_to_its_round(tmp_path):
    path = str(tmp_path / "s.db")
    s = Store(path)
    old = s.session_id
    now = __import__("time").time()
    db = s.db
    db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, ended_at, state, assigned)"
               " VALUES ('p', 'practice', ?, ?, ?, 'finished', 'live')", (old, now - 31, now - 31))
    db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, ended_at, state, assigned)"
               " VALUES ('lone', 'practice', ?, ?, ?, 'finished', 'live')", (old, now - 7200, now - 7200))
    new = db.execute("INSERT INTO sessions (started_at, date) VALUES (?, '2026-11-16')", (now,)).lastrowid
    db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, state, assigned)"
               " VALUES ('r', 'round', ?, ?, 'finished', 'live')", (new, now - 24))
    db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, ended_at, state, assigned)"
               " VALUES ('talk', 'practice', ?, ?, ?, 'finished', 'live')", (old, now - 60, now - 60))
    for text, rec, at, kind in (("Good morning", "p", now - 31, "opener"), ("Hole 1", "p", now - 31, "opener"),
                                ("Earlier", "lone", now - 7200, "opener"), ("Idle", "talk", now - 60, "segment")):
        db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text, record_uuid) VALUES (?, ?, ?, 'pbp', ?, ?)",
                   (old, at, kind, text, rec))
    db.execute("PRAGMA user_version = 6")
    db.commit()
    s.close()
    s = Store(path, new_session=False)
    got = {t: (u, sid) for t, u, sid in s.db.execute("SELECT text, record_uuid, session_id FROM lines")}
    assert got == {"Good morning": ("r", new), "Hole 1": ("r", new), "Earlier": ("lone", old), "Idle": ("talk", old)}
    assert {u for (u,) in s.db.execute("SELECT uuid FROM round_records")} == {"lone", "talk", "r"}
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == FORMAT


def test_a_picked_up_holes_last_stroke_stored_as_holed_is_left_where_it_stopped(tmp_path):
    path = str(tmp_path / "s.db")
    s = Store(path)
    db, sid = s.db, s.session_id
    db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, ended_at, state, assigned)"
               " VALUES ('r', 'round', ?, ?, ?, 'finished', 'live')", (sid, T0, T0 + 600))
    # This round's pin, logged after the hole; and a round before's (another pin), logged before it.
    db.execute("INSERT INTO hole_layouts (session_id, ts, course, hole, pin_x, pin_y, pin_z)"
               " VALUES (?, ?, 'DINO', 11, 100.0, 5.0, 200.0), (?, ?, 'DINO', 11, 100.0, 5.0, 151.3)",
               (sid, T0 + 120, sid, T0 - 3600))
    # Evan's iron 48.7 m short, a putt holed at a cup 0.26 m off the logged pin, and a putt picked up 3 m off.
    for n, (end, gid) in enumerate((((100.0, 5.0, 151.3), "iron"), ((100.26, 5.0, 200.0), "putt"),
                                    ((103.0, 5.0, 200.0), "lag")), 1):
        shot = db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt,"
                          " outcome, finish_yards, raw, record_uuid) VALUES (?, ?, 'Evan', 'Dino', 'Hole 11, Par 4',"
                          " ?, ?, 'holed', 0.0, '{}', 'r')", (sid, T0 + n, n, int(n > 1))).lastrowid
        db.execute("INSERT INTO shot_tracks (shot_id, gspro_shot_id, end_x, end_y, end_z, finish_yards)"
                   " VALUES (?, ?, ?, ?, ?, 0.0)", (shot, gid, *end))
    db.execute("PRAGMA user_version = 7")
    db.commit()
    s.close()
    s = Store(path, new_session=False)
    got = [tuple(r) for r in s.db.execute("SELECT s.outcome, s.finish_yards, t.finish_yards FROM shots s"
                                          " JOIN shot_tracks t ON t.shot_id = s.id ORDER BY s.shot_number")]
    assert got == [(None, 53.3, 53.3), ("holed", 0.0, 0.0), ("missed", None, None)]
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == FORMAT


def test_a_chip_in_finishes_at_0_whatever_the_log_still_has_at_the_next_tee(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    db, sid = s.db, s.session_id

    def stroke(player, n, ts, finish, outcome="holed"):
        return db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome,"
                          " finish_yards, raw) VALUES (?, ?, ?, 'Bethpage', 'Hole 4, par 5, 447 yards', ?, 0, ?, ?,"
                          " '{}')", (sid, ts, player, n, outcome, finish)).lastrowid

    # From now on: holing a full shot finishes it at 0, and the next tee's stale reading doesn't move it.
    db = s.db
    later = stroke("Corey", 2, T0 + 900, None, None)
    s.set_shot_result(later, outcome="holed")
    s.set_shot_result(later, finish_yards=11.3)
    assert s.db.execute("SELECT finish_yards FROM shots WHERE id = ?", (later,)).fetchone()[0] == 0.0
    # A hole-out read late off a stroke its player has since played on from isn't one: its finish stands.
    stale = stroke("Josh", 2, T0 + 1000, 5.0, None)
    stroke("Josh", 3, T0 + 1030, None, None)
    s.set_shot_result(stale, outcome="holed")
    assert tuple(s.db.execute("SELECT outcome, finish_yards FROM shots WHERE id = ?", (stale,)).fetchone()) == (None, 5.0)


def test_the_readers_read_the_stored_record_once_every_row_has_one(tmp_path):
    from commentator import stats_web

    s = Store(str(tmp_path / "s.db"))
    p = _hit(s, "Ann", T0)                             # practice on the round's course, just before it
    u = s.begin_round("r1", course="Pebble", players={"Ann": None}, since_id=p)
    a = _hit(s, "Ann", T0 + 100)
    s.end_round(u)
    sess = s.db.execute("SELECT session_id FROM shots WHERE id = ?", (a,)).fetchone()[0]
    rid = s.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, round_uuid)"
                       " VALUES (?, ?, '2023-11-14', 'Pebble', 'Ann', 18, ?)", (sess, T0 + 200, u)).lastrowid
    assert R.recorded(s.db)
    assert R.shot_rounds(s.db) == {a: rid} and R._timed_shot_rounds(s.db) == {p: rid, a: rid}
    assert stats_web._round_rows(s.db, rid, T0 + 200)[2] == [a]
    assert R.round_groups(s.db) == R._timed_round_groups(s.db)
    s.db.execute("UPDATE shots SET record_uuid = NULL WHERE id = ?", (p,))   # as an older version writes
    assert not R.recorded(s.db)
    assert R.shot_rounds(s.db) == {p: rid, a: rid} and stats_web._round_rows(s.db, rid, T0 + 200)[2] == [p, a]


def test_a_round_is_edited_only_once_the_booth_is_done_with_it(tmp_path, monkeypatch):
    import time
    from commentator import stats_web

    path = str(tmp_path / "s.db")
    s = Store(path)
    now = time.time()

    def play(rid, course, end):
        u = s.begin_round(rid, course=course, players={"Ann": None})
        _hit(s, "Ann", end, course=course)
        return u, s.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, round_uuid)"
                               " VALUES (?, ?, '2026-11-13', ?, 'Ann', 18, ?)", (s.session_id, end, course, u)).lastrowid
    u, done = play("r1", "Pebble", now)
    s.end_round(u)
    s.db.commit()
    shot_of = lambda course: s.db.execute("SELECT id FROM shots WHERE course = ?", (course,)).fetchone()[0]
    with pytest.raises(stats_web.RoundInPlay):         # just ended: a late putt or the closer may follow
        stats_web.set_round_excluded(path, done, now, True)
    with pytest.raises(stats_web.RoundInPlay):
        stats_web.set_left_out(path, shot_of("Pebble"), True)
    rounds, shots = R.unsettled(s.db, now)
    assert rounds == {done: now + R.CLOSER_SECONDS}
    assert shots == {shot_of("Pebble"): pytest.approx(now + R.CLOSER_SECONDS, abs=1)}   # by its record's end
    _, open_ = play("r2", "Augusta", now + 60)         # its record still open (the booth stopped before closing it)
    s.db.commit()
    later = now + 60 + R.CLOSER_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    with pytest.raises(stats_web.RoundInPlay):
        stats_web.set_round_league(path, open_, now + 60, False)
    with pytest.raises(stats_web.RoundInPlay):
        stats_web.set_left_out(path, shot_of("Augusta"), True)
    assert R.unsettled(s.db, later) == ({open_: None}, {shot_of("Augusta"): None})
    assert stats_web.set_round_excluded(path, done, now, True)
    assert stats_web.set_left_out(path, shot_of("Pebble"), True)


def test_frozen_reuses_answers_until_its_outermost_scope_ends():
    db = sqlite3.connect(":memory:")
    calls = []
    ask = lambda: R.reuse(db, ("k",), lambda: calls.append(1) or len(calls))
    assert ask() == 1 and ask() == 2                     # not frozen: computed every time
    with R.frozen(db):
        assert ask() == 3
        with R.frozen(db):
            assert ask() == 3
        assert ask() == 3                                # the inner scope kept the outer's answers
    assert ask() == 4


def test_round_stats_recorded_from_the_screens_reads_are_worked_out_again_from_the_stored_shots(tmp_path):
    path = str(tmp_path / "s.db")
    s = Store(path)
    db, sid = s.db, s.session_id

    def round_(uuid, hole_text, fir_hit, drive_text=None, card_scores="'{\"1\": 4}'"):
        db.execute("INSERT INTO round_records (uuid, kind, session_id, started_at, ended_at, state, assigned)"
                   " VALUES (?, 'round', ?, ?, ?, 'finished', 'live')", (uuid, sid, T0, T0 + 600))
        # Ann's drive the screen read in the rough, GSPro's round file has in the fairway; on in two, two putts.
        for n, (lie_before, lie_after, to_pin, putt) in enumerate(
                (("tee", "fairway", "400 yards", 0), ("fairway", "green", "150 yards", 0),
                 ("green", "green", "20 feet", 1), ("green", None, "3 feet", 1)), 1):
            db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, lie_before,"
                       " lie_after, to_pin, outcome, total_yards, raw, record_uuid) VALUES (?, ?, 'Ann', 'Pebble',"
                       " ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
                       (sid, T0 + n, drive_text if n == 1 and drive_text else hole_text, n, putt, lie_before, lie_after, to_pin,
                        "holed" if n == 4 else None, 250.0 if n == 1 else None, uuid))
        side = db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, format, holes, complete,"
                          " players, scores, hole_pars, round_uuid, fir_hit, fir_tries, gir_hit, gir_tries,"
                          " scramble_hit, scramble_tries, sand_hit, sand_tries, putts, putt_holes, penalties,"
                          " longest_drive) VALUES (?, ?, '2025-01-01', 'Pebble', 'Ann', 'stroke', 1, 1, '[\"Ann\"]',"
                          " '{\"1\": 4}', '{\"1\": 4}', ?, ?, 1, 1, 1, 0, 0, 0, 0, 2, 1, 0, 250.0)",
                          (sid, T0 + 600, uuid, fir_hit)).lastrowid
        card = db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, scores, hole_pars,"
                          " round_uuid, fir_hit, fir_tries, gir_hit, gir_tries, scramble_hit, scramble_tries,"
                          " sand_hit, sand_tries, putts, putt_holes) VALUES (?, ?, 'Ann', 'Pebble', 'stroke',"
                          f" {card_scores}, '{{\"1\": 4}}', ?, ?, 1, 1, 1, 0, 0, 0, 0, 2, 1)",
                          (sid, T0 + 600, uuid, fir_hit)).lastrowid
        return side, card

    wrong = round_("r", "Hole 1, par 4, 400 yards", 0)
    right = round_("ok", "Hole 1, par 4, 400 yards", 1)
    unplaced = round_("misread", "par 4, 400 yards", 0)   # (no hole number read: which hole isn't known)
    # The drive read on a hole the card hasn't, before the strokes on its own: a misread, not a hole
    # left unfinished at the end. And an older card with no scores kept: its own stats can't be told.
    lost = round_("lost", "Hole 1, par 4, 400 yards", 0, drive_text="Hole 2, par 4, 400 yards")
    unscored = round_("unscored", "Hole 1, par 4, 400 yards", 0, card_scores="NULL")
    db.execute("PRAGMA user_version = 8")
    db.commit()
    s.close()
    s = Store(path, new_session=False)

    def fir(rows):
        return [tuple(s.db.execute(f"SELECT fir_hit, fir_tries, gir_hit, putts FROM {t} WHERE id = ?", (i,)).fetchone())
                for t, i in zip(("rounds", "player_rounds"), rows)]
    assert fir(wrong) == fir(right) == [(1, 1, 1, 2), (1, 1, 1, 2)]
    assert fir(unplaced) == fir(lost) == [(0, 1, 1, 2), (0, 1, 1, 2)]
    assert fir(unscored) == [(1, 1, 1, 2), (0, 1, 1, 2)]
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == FORMAT
    assert s.refresh_round_stats("r") == s.refresh_round_stats("ok") == 0


def test_a_hole_picked_up_after_a_full_shot_isnt_worked_out_again_as_putts(tmp_path):
    # Four strokes into the trees, then picked up: GSPro scores it 8, and nobody putted.
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    db, sid = s.db, s.session_id
    for n in (1, 2, 3, 4):
        db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, lie_before,"
                   " lie_after, to_pin, raw, record_uuid) VALUES (?, ?, 'Ann', 'Pebble', 'Hole 1, par 4, 400 yards',"
                   " ?, 0, ?, ?, '100 yards', '{}', 'r')",
                   (sid, T0 + n, n, "tee" if n == 1 else "rough", "green" if n == 4 else "rough"))
    db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, scores, hole_pars, round_uuid)"
               " VALUES (?, ?, 'Ann', 'Pebble', 'stroke', '{\"1\": 8}', '{\"1\": 4}', 'r')", (sid, T0 + 600))
    (st,) = recompute(db, "r")[1].values()
    assert st.putts == 0 and st.putt_holes == 0 and st.penalties == 0


def _stored_hole(s, uuid, strokes, score, labels=None):
    """Ann's strokes (lie before, lie after, putt) on par-4 holes, as the store keeps them, and her card."""
    db, sid = s.db, s.session_id
    ids = []
    for i, (hole, before, after, putt) in enumerate(strokes):
        n = 1 + sum(1 for h, *_ in strokes[:i] if h == hole)
        ids.append(db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt,"
                              " lie_before, lie_after, to_pin, raw, record_uuid) VALUES (?, ?, 'Ann', 'Pebble', ?,"
                              " ?, ?, ?, ?, '100 yards', '{}', ?)",
                              (sid, T0 + i, (labels or {}).get(i, f"Hole {hole}, par 4, 400 yards"), n, int(putt),
                               before, after, uuid)).lastrowid)
    db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, scores, hole_pars, round_uuid)"
               " VALUES (?, ?, 'Ann', 'Pebble', 'stroke', ?, ?, ?)",
               (sid, T0 + 600, json.dumps(score), json.dumps({h: 4 for h in score}), uuid))
    return ids


def test_a_full_shot_that_left_a_tap_in_is_on_in_regulation_with_its_conceded_putt(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    # A birdie: the approach stopped on the fringe inside the gimme circle, its tap-in conceded.
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "fringe", False)], {1: 3})
    uid = s.db.execute("SELECT uid FROM shots WHERE id = ?", (ids[-1],)).fetchone()[0]
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome, raw,"
                 " record_uuid, derived_from) VALUES (?, ?, 'Ann', 'Pebble', 'Hole 1, par 4, 400 yards', 3, 1,"
                 " 'gimme', '{}', 'r', ?)", (s.session_id, T0 + 5, uid))
    (st,) = recompute(s.db, "r")[1].values()
    assert st.gir == (1, 1) and st.putts == 1
    # A gimme from before the store linked it: the stroke before it is its parent.
    _stored_hole(s, "old", [(1, "tee", "fairway", False), (1, "fairway", "fringe", False)], {1: 3})
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome, raw,"
                 " record_uuid) VALUES (?, ?, 'Ann', 'Pebble', 'Hole 1, par 4, 400 yards', 3, 1, 'gimme', '{}',"
                 " 'old')", (s.session_id, T0 + 5))
    (st,) = recompute(s.db, "old")[1].values()
    assert st.gir == (1, 1) and st.putts == 1
    # One whose stroke before it isn't stored: which shot it conceded can't be told.
    _stored_hole(s, "lost", [(1, "tee", "fairway", False), (1, "fairway", "fringe", False)], {1: 3})
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome, raw,"
                 " record_uuid) VALUES (?, ?, 'Ann', 'Pebble', 'Hole 1, par 4, 400 yards', NULL, 1, 'gimme', '{}',"
                 " 'lost')", (s.session_id, T0 + 5))
    assert recompute(s.db, "lost") is None
    # A linked one whose parent the round doesn't have: the same.
    _stored_hole(s, "gone", [(1, "tee", "fairway", False), (1, "fairway", "fringe", False)], {1: 3})
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome, raw,"
                 " record_uuid, derived_from) VALUES (?, ?, 'Ann', 'Pebble', 'Hole 1, par 4, 400 yards', 3, 1,"
                 " 'gimme', '{}', 'gone', 'no-such-shot')", (s.session_id, T0 + 5))
    assert recompute(s.db, "gone") is None


def test_a_row_with_a_hole_whose_par_isnt_kept_is_left_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False), (1, "green", None, True)],
                 {1: 3}, labels={0: "Hole 1", 1: "Hole 1", 2: "Hole 1"})
    s.db.execute("UPDATE player_rounds SET hole_pars = NULL")
    assert recompute(s.db, "r") == ({}, {})
    # Another player's shot on it says its par: the hole is scored with it.
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, lie_before,"
                 " lie_after, raw, record_uuid) VALUES (?, ?, 'Ben', 'Pebble', 'Hole 1, par 4, 400 yards', 1, 0,"
                 " 'tee', 'fairway', '{}', 'r')", (s.session_id, T0 + 9))
    (st,) = recompute(s.db, "r")[1].values()
    assert st.fir == (1, 1) and st.gir == (1, 1) and st.putts == 1


def test_a_gimme_noted_after_the_next_tee_shot_doesnt_stop_a_round_quit_early_being_worked_out(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "fringe", False),
                                (2, "tee", "fairway", False)], {1: 3})
    s.db.execute("UPDATE player_rounds SET complete = 0")
    uid = s.db.execute("SELECT uid FROM shots WHERE id = ?", (ids[1],)).fetchone()[0]
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, outcome, raw,"
                 " record_uuid, derived_from) VALUES (?, ?, 'Ann', 'Pebble', 'Hole 1, par 4, 400 yards', 3, 1,"
                 " 'gimme', '{}', 'r', ?)", (s.session_id, T0 + 5, uid))
    (st,) = recompute(s.db, "r")[1].values()
    assert st.fir == (1, 1) and st.putts == 1


def test_a_stroke_past_its_holes_score_is_a_misread_hole_and_the_round_is_left_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    holes = [(1, "tee", "fairway", False), (1, "fairway", "green", False), (1, "green", None, True)] \
        + [(2, "tee", "rough", False), (2, "rough", "rough", False), (2, "rough", "green", False),
           (2, "green", "green", True), (2, "green", None, True)]
    _stored_hole(s, "r", holes, {1: 3, 2: 5}, labels={7: "Hole 1, par 4, 400 yards"})   # hole 2's 5th, read as 1's
    assert recompute(s.db, "r") is None
    _stored_hole(s, "ok", holes, {1: 3, 2: 5})
    assert recompute(s.db, "ok") is not None


def test_a_round_quit_early_leaves_only_a_hole_begun_after_the_rest_off_its_card(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    # Quit on hole 2 after its drive: that's the unfinished hole. A chip-in read on "Hole 9" isn't.
    _stored_hole(s, "quit", [(1, "tee", "fairway", False), (1, "fairway", "green", False), (1, "green", None, True),
                             (1, "green", None, True), (2, "tee", "rough", False)], {1: 4})
    _stored_hole(s, "misread", [(1, "tee", "fairway", False), (1, "fairway", "fringe", False),
                                (1, "fringe", None, False)], {1: 3}, labels={2: "Hole 9, par 4, 400 yards"})
    s.db.execute("UPDATE player_rounds SET complete = 0")
    # A mulligan with no hole read doesn't count anywhere, so it doesn't stop the rest being placed.
    s.db.execute("INSERT INTO shots (session_id, ts, player, course, shot_number, is_putt, mulligan, raw, record_uuid)"
                 " VALUES (?, ?, 'Ann', 'Pebble', 1, 0, 1, '{}', 'quit')", (s.session_id, T0 - 5))
    assert recompute(s.db, "quit") is not None
    assert recompute(s.db, "misread") is None


def test_a_round_whose_shots_another_connection_changed_is_worked_out_again_when_the_booth_opens(tmp_path):
    path = str(tmp_path / "s.db")
    s = Store(path)
    ids = _stored_hole(s, "r", [(1, "tee", "rough", False), (1, "rough", "green", False), (1, "green", None, True),
                                (1, "green", None, True)], {1: 4})
    s.db.execute("UPDATE player_rounds SET fir_hit = 0, fir_tries = 1")
    s.db.commit()
    s.close()
    other = sqlite3.connect(path)   # (a backfill, or the stats page)
    other.execute("UPDATE shots SET lie_after = 'fairway' WHERE id = ?", (ids[0],))
    other.execute("UPDATE shots SET lie_before = 'fairway' WHERE id = ?", (ids[1],))
    other.commit()
    other.close()
    s = Store(path, new_session=False)
    assert tuple(s.db.execute("SELECT fir_hit, fir_tries FROM player_rounds").fetchone()) == (1, 1)
    assert s.db.execute("SELECT COUNT(*) FROM round_stats_due").fetchone()[0] == 0


def test_a_round_noted_while_the_database_is_busy_stays_noted_for_the_next_time(tmp_path):
    path = str(tmp_path / "s.db")
    s = Store(path)
    _stored_hole(s, "r", [(1, "tee", "rough", False), (1, "rough", "green", False), (1, "green", None, True)], {1: 3})
    s.db.execute("INSERT OR IGNORE INTO round_stats_due VALUES ('r')")
    s.db.commit()
    s.db.execute("PRAGMA busy_timeout = 0")
    other = sqlite3.connect(path)   # (the stats page, writing)
    other.execute("BEGIN IMMEDIATE")
    assert s.refresh_changed_round_stats() == 0
    other.rollback()
    other.close()
    assert not s.db.in_transaction
    assert [tuple(r) for r in s.db.execute("SELECT uuid FROM round_stats_due")] == [("r",)]


def test_a_player_renamed_mid_round_keeps_the_strokes_under_their_old_name(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", None, True)], {1: 3})
    s.db.execute("UPDATE shots SET slot_id = 'ann'")
    s.db.execute("UPDATE player_rounds SET slot_id = 'ann'")
    s.db.execute("UPDATE shots SET player = 'ann' WHERE id = ?", (ids[0],))   # (her name, read before a restart)
    (st,) = recompute(s.db, "r")[1].values()
    assert st.fir == (1, 1) and st.gir == (1, 1) and st.putts == 1


def test_a_stroke_on_a_scored_hole_thats_nobodys_leaves_the_round_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "rough", False), (1, "rough", "fairway", False),
                                (1, "fairway", "green", False), (1, "green", None, True)], {1: 4})
    s.db.execute("UPDATE shots SET player = NULL WHERE id = ?", (ids[1],))   # (its match taken back)
    assert recompute(s.db, "r") is None


def test_a_drive_the_stats_page_left_out_isnt_the_longest(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", None, True)], {1: 3})
    s.db.execute("UPDATE shots SET club = 'DR', total_yards = 310 WHERE id = ?", (ids[0],))
    s.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, format, holes, players, scores, hole_pars,"
                 " round_uuid) VALUES (?, ?, '2026-11-01', 'Pebble', 'Ann', 'stroke', 1, '[\"Ann\"]', '{\"1\": 3}',"
                 " '{\"1\": 4}', 'r')", (s.session_id, T0 + 600))
    (st,) = recompute(s.db, "r")[0].values()
    assert st.longest_drive == 310
    s.db.execute("UPDATE shots SET left_out = 1 WHERE id = ?", (ids[0],))
    (st,) = recompute(s.db, "r")[0].values()
    assert st.longest_drive is None


def test_a_stroke_taken_back_off_its_player_and_hole_leaves_the_round_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", None, True), (1, "green", None, True)], {1: 4})
    s.db.execute("UPDATE shots SET player = NULL, hole = NULL WHERE id = ?", (ids[2],))
    assert recompute(s.db, "r") is None


def test_a_hole_missing_its_first_stroke_leaves_its_row_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", None, True)], {1: 3})
    s.db.execute("UPDATE shots SET record_uuid = NULL WHERE id = ?", (ids[0],))
    assert recompute(s.db, "r") == ({}, {})


def test_a_hole_missing_a_putt_isnt_a_penalty_and_its_row_is_left_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", "green", True), (1, "green", None, True)], {1: 4})
    s.db.execute("UPDATE shots SET record_uuid = NULL WHERE id = ?", (ids[2],))
    assert recompute(s.db, "r") == ({}, {})
    # Nor is one missing the approach that put it there.
    ids = _stored_hole(s, "a", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", "green", True), (1, "green", None, True)], {1: 4})
    s.db.execute("UPDATE shots SET record_uuid = NULL WHERE id = ?", (ids[1],))
    assert recompute(s.db, "a") == ({}, {})


def test_a_scramble_player_missing_their_own_drive_is_left_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    db, sid = s.db, s.session_id
    ids = {}
    for i, (who, n, before, after, putt) in enumerate([("Ann", 1, "tee", "fairway", 0), ("Ben", 1, "tee", "rough", 0),
                                                       ("Ann", 2, "fairway", "green", 0), ("Ben", 2, "fairway", "green", 0),
                                                       ("Ann", 3, "green", None, 1)]):
        ids[who, n] = db.execute(
            "INSERT INTO shots (session_id, ts, player, course, hole, shot_number, is_putt, lie_before, lie_after,"
            " to_pin, raw, record_uuid) VALUES (?, ?, ?, 'Pebble', 'Hole 1, par 4, 400 yards', ?, ?, ?, ?,"
            " '100 yards', '{}', 'r')", (sid, T0 + i, who, n, putt, before, after)).lastrowid
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, format, holes, players, scores,"
               " hole_pars, round_uuid) VALUES (?, ?, '2026-11-01', 'Pebble', 'AB', 'scramble', 1,"
               " '[\"Ann\", \"Ben\"]', '{\"1\": 3}', '{\"1\": 4}', 'r')", (sid, T0 + 600))
    for who in ("Ann", "Ben"):
        db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, scores, hole_pars,"
                   " round_uuid) VALUES (?, ?, ?, 'Pebble', 'scramble', '{\"1\": 3}', '{\"1\": 4}', 'r')",
                   (sid, T0 + 600, who))
    assert len(recompute(db, "r")[1]) == 2 and len(recompute(db, "r")[0]) == 1
    db.execute("UPDATE shots SET record_uuid = NULL WHERE id = ?", (ids["Ann", 1],))
    got = recompute(db, "r")
    assert got is not None and [s.fir for s in got[1].values()] == [(0, 1)]   # (Ben's)
    assert got[0] == {}   # (the team's longest drive may have been hers)
    db.execute("UPDATE shots SET record_uuid = NULL WHERE player = 'Ann'")   # (none of hers at all)
    assert recompute(db, "r")[0] == {}


def test_a_hole_with_a_stroke_whose_number_wasnt_read_leaves_its_row_as_stored(tmp_path):
    from commentator.stored_stats import recompute

    s = Store(str(tmp_path / "s.db"))
    ids = _stored_hole(s, "r", [(1, "tee", "fairway", False), (1, "fairway", "green", False),
                                (1, "green", None, True)], {1: 3})
    s.db.execute("UPDATE shots SET shot_number = NULL, outcome = 'holed' WHERE id = ?", (ids[2],))
    assert recompute(s.db, "r") == ({}, {})
