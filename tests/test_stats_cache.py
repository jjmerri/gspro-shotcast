import sqlite3
from contextlib import closing

from commentator import highlights, stats_cache, stats_web
from commentator import night_totals as N
from commentator import season_analytics as A
from commentator.hole_replay import hole_replays
from commentator.store import Store

T0 = 1_700_000_000.0


def _hit(store, player, at, hole="Hole 1, par 4, 400 yards", putt=False, outcome=None):
    from commentator.hud import Hud
    from commentator.models import Shot
    sid = store.record_shot(player=player, hole=hole, shot=Shot(club="PT" if putt else "Dr", timestamp=at),
                            before=Hud(), after=Hud(), tags=[], carry_game_yards=None, total_yards=250.0,
                            offline_yards=None, course="Pebble")
    store.db.execute("UPDATE shots SET is_putt = ?, outcome = ?, to_pin_yards = ?, lie_before = ?, start_x = 0,"
                     " start_z = ? WHERE id = ?",
                     (int(putt), outcome, 3.0 if putt else 400.0, "Green" if putt else "Tee", 360.0 if putt else 0.0, sid))
    return sid


def _season(path):
    """Two recorded rounds, each in its own session: a drive and a holed putt per player; practice after
    the second."""
    shots = []
    for n, at in enumerate((T0, T0 + 86400)):
        s = Store(str(path))
        u = s.begin_round(f"r{n}", course="Pebble", players={"Ann": None, "Bo": None})
        for i, p in enumerate(("Ann", "Bo")):
            shots.append(_hit(s, p, at + i))
            shots.append(_hit(s, p, at + 10 + i, putt=True, outcome="holed"))
        s.end_round(u)
        s.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, round_uuid)"
                     " VALUES (?, ?, '2023-11-14', 'Pebble', 'Ann', 1, ?)", (s.session_id, at + 100, u))
        if n:
            _hit(s, "Ann", at + 200)
        s.db.commit()
        s.close()
    return shots


def _build(path, cached):
    """hole_replays and highlights.rounds, with the cache or without, and the cache's counts."""
    with closing(stats_web.connect(path)) as db:
        if not cached:
            return hole_replays(db), highlights.rounds(db), None
        with stats_cache.opened(db, path) as c:
            assert c is not None
            got = hole_replays(db), highlights.rounds(db)
            return (*got, (c.hits, c.misses))


def _every_shot(shots, sides=None, before=None, talk=None):
    """(pick, standing in: a moment per shot counted, saying all it was given, so a reel shows it)"""
    said = repr((sorted((sides or {}).items()), sorted((before or {}).items()),
                 [sorted(i for i in t.items() if i[0] in {s["session_id"] for s in shots}) for t in talk or ()]))
    return [{"shot": s, "what": said, "bad": False, "level": 1, "post": 1.0, "stop": 2.0, "must": False}
            for s in shots if not s["left_out"]]


def test_a_warm_build_reuses_every_round_and_an_edit_works_out_only_its_round_again(tmp_path, monkeypatch):
    monkeypatch.setattr(highlights, "pick", _every_shot)
    path = tmp_path / "season.db"
    shots = _season(path)
    cold = _build(path, True)
    assert len(cold[0]) == 2 and len(cold[1]) == 2
    assert cold[:2] == _build(path, False)[:2] and cold[2] == (0, 4)   # 2 rounds x (replays, reels)
    assert _build(path, True) == (*cold[:2], (4, 0))
    with closing(sqlite3.connect(path)) as db, db:   # a shot of the second round left out
        db.execute("UPDATE shots SET left_out = 1 WHERE id = ?", (shots[-1],))
    after = _build(path, True)
    assert after[:2] == _build(path, False)[:2] and after[:2] != cold[:2]
    assert after[2] == (2, 2)


def test_a_change_to_the_code_or_the_file_throws_the_cache_away(tmp_path, monkeypatch):
    path = tmp_path / "season.db"
    _season(path)
    _build(path, True)
    monkeypatch.setattr(stats_cache, "_code", "other code")
    assert _build(path, True)[2] == (0, 4)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE meta SET dataset_id = 'restored'")
    assert _build(path, True)[2] == (0, 4)
    stats_cache.clear(path)
    assert _build(path, True)[2] == (0, 4)


def test_per_key_keeps_a_result_while_its_signature_holds_and_a_missing_one_too(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    asked = []

    def compute(keys):
        asked.append(sorted(keys))
        return {k: {"v": (k, 1.5)} for k in keys if k != 2}

    with closing(stats_web.connect(path)) as db:
        with stats_cache.opened(db, path):
            assert stats_cache.per_key(db, "t", {1: "a", 2: "a", 3: "a"}, compute) == {1: {"v": (1, 1.5)}, 3: {"v": (3, 1.5)}}
        with stats_cache.opened(db, path):
            assert list(stats_cache.per_key(db, "t", {3: "a", 2: "a", 1: "b"}, compute)) == [3, 1]
        with stats_cache.opened(db, path):
            assert stats_cache.per_key(db, "t", {3: "a"}, compute) == {3: {"v": (3, 1.5)}}
            assert stats_cache.per_key(db, "t", {1: "b"}, compute) == {1: {"v": (1, 1.5)}}   # (1 was dropped)
    assert asked == [[1, 2, 3], [1], [1]]


def test_a_warm_build_after_each_kind_of_change_is_the_same_as_one_from_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(highlights, "pick", _every_shot)
    path = tmp_path / "season.db"
    shots = _season(path)
    _build(path, True)
    with closing(sqlite3.connect(path)) as db:
        first, second = (r[0] for r in db.execute("SELECT DISTINCT session_id FROM shots ORDER BY 1"))
        u1, u2 = (r[0] for r in db.execute("SELECT round_uuid FROM rounds ORDER BY id"))
        practice = db.execute("SELECT MAX(id) FROM shots").fetchone()[0]
    changes = [
        # an earlier round's drive left out: the later round's players' bests before it change
        ("UPDATE shots SET left_out = 1 WHERE id = ?", (shots[0],)),
        # the booth's words in the later round's session (where its clips end)
        ("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text, record_uuid)"
         " VALUES (?, ?, ?, 'shot', 'pbp', 'x', ?)", (second, shots[-1], T0 + 86400 + 12, u2)),
        # practice in the later round's session, moved up to just after its last shot (its clip ends sooner)
        ("UPDATE shots SET ts = ? WHERE id = ?", (T0 + 86400 + 12, practice)),
        # a second side for the first round, with no new shots
        ("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, round_uuid, players)"
         " VALUES (?, ?, '2023-11-14', 'Pebble', 'Bo', 1, ?, '[\"Bo\"]')", (first, T0 + 101, u1)),
        ("UPDATE rounds SET players = '[\"Ann\"]' WHERE side = 'Ann' AND round_uuid = ?", (u1,)),
        # a hole's layout learned for the course
        ("INSERT INTO hole_layouts (course, hole, tee_x, tee_z, pin_x, pin_z) VALUES ('Pebble', 1, 0, 0, 0, 380)", ()),
    ]
    for sql, args in changes:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(sql, args)
        warm, cold = _build(path, True), _build(path, False)
        assert warm[:2] == cold[:2], sql


def test_an_older_copy_put_back_by_hand_or_restored_doesnt_reuse_what_the_newer_one_kept(tmp_path, monkeypatch):
    import shutil

    from commentator import backup

    monkeypatch.setattr(highlights, "pick", _every_shot)
    path = tmp_path / "season.db"
    shots = _season(path)
    older = shutil.copy(path, tmp_path / "older.db")

    def left_out(shot):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE shots SET left_out = 1 WHERE id = ?", (shot,))

    left_out(shots[-1])
    _build(path, True)
    shutil.copy(older, path)       # by hand: a build keeps the round under the counter gone back...
    _build(path, True)
    left_out(shots[-2])            # ...before it reaches the count kept with other rows
    assert _build(path, True)[:2] == _build(path, False)[:2]
    left_out(shots[-3])
    _build(path, True)
    backup.restore(older, path, tmp_path / "backups")
    assert not stats_cache.path_for(path).exists()


def test_a_damaged_cache_file_or_entry_is_worked_around(tmp_path):
    path = tmp_path / "season.db"
    _season(path)
    stats_cache.path_for(path).write_bytes(b"not a database" * 100)
    with closing(stats_web.connect(path)) as db, stats_cache.opened(db, path) as c:
        assert c is None and len(hole_replays(db)) == 2
    stats_cache.path_for(path).unlink()
    cold = _build(path, True)
    with closing(sqlite3.connect(stats_cache.path_for(path))) as c, c:
        c.execute("UPDATE entries SET value = x'00'")
    assert _build(path, True) == (*cold[:2], (0, 4))


def test_player_stats_are_kept_per_night_and_an_edit_works_out_only_its_night_again(tmp_path):
    path = tmp_path / "season.db"
    shots = _season(path)
    with closing(sqlite3.connect(path)) as db, db:
        for n, (sid,) in enumerate(db.execute("SELECT id FROM sessions ORDER BY id").fetchall()):
            db.execute("UPDATE sessions SET date = ? WHERE id = ?", (f"2023-11-1{4 + n}", sid))

    def build(cached):
        with closing(stats_web.connect(path)) as db:
            if not cached:
                return stats_web.players(db, gimme_ft=3.0), None
            with stats_cache.opened(db, path) as c:
                return stats_web.players(db, gimme_ft=3.0), (c.hits, c.misses)

    cold = build(True)
    assert cold == (build(False)[0], (0, 2))
    assert [p["shots"] for p in cold[0]] == [5, 4]
    assert build(True) == (cold[0], (2, 0))
    with closing(sqlite3.connect(path)) as db, db:   # the first night's drive left out
        db.execute("UPDATE shots SET left_out = 1 WHERE id = ?", (shots[0],))
    after = build(True)
    assert after == (build(False)[0], (1, 1))
    assert [p["shots"] for p in after[0]] == [4, 4]


def test_hole_stats_are_kept_per_night_and_an_edit_works_out_only_its_night_again(tmp_path):
    path = tmp_path / "season.db"
    shots = _season(path)
    with closing(sqlite3.connect(path)) as db, db:
        for n, (sid,) in enumerate(db.execute("SELECT id FROM sessions ORDER BY id").fetchall()):
            db.execute("UPDATE sessions SET date = ? WHERE id = ?", (f"2023-11-1{4 + n}", sid))

    def fresh(db):
        holes = A.holes_played(db)
        usual = stats_web._usual(db)
        return {"sg": A.sg_summary(holes, usual), "recovery": A.recovery(holes),
                "trend": A.trends(db, holes, None, usual)}

    def build():
        with closing(stats_web.connect(path)) as db, stats_cache.opened(db, path) as c:
            got = stats_web._holes_merged(db, None)
            return {k: got[k] for k in ("sg", "recovery", "trend")}, fresh(db), (c.hits, c.misses)

    got, want, counts = build()
    assert got == want and counts == (0, 2) and got["sg"]
    assert build()[2] == (2, 0)
    with closing(sqlite3.connect(path)) as db, db:   # the first night's drive left out
        db.execute("UPDATE shots SET left_out = 1 WHERE id = ?", (shots[0],))
    got, want, counts = build()
    assert got == want and counts == (1, 1)


def test_a_round_resumed_after_midnight_is_in_both_its_nights_signatures(tmp_path):
    path = tmp_path / "season.db"
    shots = _season(path)
    with closing(sqlite3.connect(path)) as db, db:
        for n, (sid,) in enumerate(db.execute("SELECT id FROM sessions ORDER BY id").fetchall()):
            db.execute("UPDATE sessions SET date = ? WHERE id = ?", (f"2023-11-1{4 + n}", sid))
        first = db.execute("SELECT record_uuid FROM shots WHERE id = ?", (shots[0],)).fetchone()[0]
        db.execute("UPDATE shots SET record_uuid = ? WHERE id = ?", (first, shots[4]))   # the next night's drive

    def sigs():
        with closing(stats_web.connect(path)) as db, stats_cache.opened(db, path):
            return N.night_sigs(db)

    before = sigs()
    assert sigs() == before
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE shots SET left_out = 1 WHERE id = ?", (shots[4],))
    after = sigs()
    assert all(after[d] != before[d] for d in ("2023-11-14", "2023-11-15"))
