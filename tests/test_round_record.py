import sqlite3

import pytest

from commentator import round_record as R
from commentator.store import CLOSER_SECONDS, Store

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
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
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
