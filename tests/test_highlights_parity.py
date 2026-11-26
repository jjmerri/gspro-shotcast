"""The shared highlight rules (shotcast_scoring.highlights, which Shotcast Cloud picks reels with) pick
as the app's own (commentator/highlights.py) do: every highlights test runs with the app's pick,
finish, talk and clip timing (_post) each checked against the shared ones on the same input."""
import sqlite3

import pytest
import test_highlights as T

from commentator import highlights as H
from shotcast_scoring import excitement as E
from shotcast_scoring import highlights as S
from commentator import interest as I

TESTS = [f for name, f in vars(T).items() if name.startswith("test_") and callable(f)]


@pytest.mark.parametrize("test", TESTS, ids=lambda f: f.__name__)
def test_the_shared_rules_pick_as_the_apps_do(test, monkeypatch, tmp_path):
    pick, finish, talk, post = H.pick, H.finish, H._talk, H._post

    def both_post(*args):
        mine = post(*args)
        assert S._post(*args) == mine
        return mine

    def both_pick(*args, **kw):
        mine = pick(*args, **kw)
        assert S.pick(*args, **kw) == mine
        return mine

    def both_finish(*args, **kw):
        mine = finish(*args, **kw)
        assert S.finish(*args, **kw) == mine
        return mine

    def both_talk(db, shots, sessions=None, ts_of=None):
        mine = talk(db, shots, sessions, ts_of)
        where, params = "", []
        if sessions is not None:
            where, params = f" WHERE session_id IN ({','.join('?' * len(sessions))})", list(sessions)
        try:
            rows = db.execute("SELECT session_id, shot_id, ts, kind, text, aired_at, aired_seconds FROM lines"
                              f"{where} ORDER BY ts, id", params).fetchall()
        except sqlite3.OperationalError:
            try:   # (a database from before the booth noted air times: the app's own fallback)
                rows = [(*r, None, None) for r in db.execute(
                    f"SELECT session_id, shot_id, ts, kind, text FROM lines{where} ORDER BY ts, id", params)]
            except sqlite3.OperationalError:
                return mine   # (no lines at all)
        assert S.talk(rows, shots, ts_of) == mine
        return mine

    monkeypatch.setattr(H, "pick", both_pick)
    monkeypatch.setattr(H, "finish", both_finish)
    monkeypatch.setattr(H, "_talk", both_talk)
    monkeypatch.setattr(H, "_post", both_post)
    test(tmp_path) if test.__code__.co_argcount else test()


def test_the_shared_excitement_levels_are_the_apps():
    for feet in (None, 0, 2, 5, 12, 13, 21, 41):
        for to_par in (None, 1, 0, -1, -2, -3):
            assert E.holed_putt_level(feet, to_par) == I.holed_putt_level(feet, to_par)
    lies = (None, "green", "fairway", *I._SHORT_GAME_LIE_BONUS)
    for start in (None, 0, 5, 12, 30, 120, 160):
        for lie in lies:
            for finish in (None, 1.0, 3.0):
                assert E.short_game_gimme_level(start, lie, finish) == I.short_game_gimme_level(start, lie, finish)
        if start:
            assert E.approach_gimme_level(start, 1.0) == I.approach_gimme_level(start, 1.0)
            for strokes, par in ((1, 3), (3, 4), (2, 4), (2, 5), (None, None)):
                assert E.holed_out_level(start, strokes, par, 100.0) == I.holed_out_level(start, strokes, par, 100.0)
    for feet in (1, 4.4, 5.6, 14):
        assert E.stuffed_level(feet) == I.stuffed_level(feet)
    assert E.putt_to_par(3, 4) == I.putt_to_par(3, 4) == -1
    # Every constant the same (the timing ones, as Timing's defaults too).
    for name in [n for n in vars(S) if n.isupper() and n not in ("APP", "I")]:
        assert getattr(S, name) == getattr(H, name, None) or name == "SHOT_SHOWS", name
    assert S.SHOT_SHOWS == H.SHOT_SHOWS
    for name in [n for n in vars(E) if n.isupper() or n == "_SHORT_GAME_LIE_BONUS"]:
        assert getattr(E, name) == getattr(I, name), name
    for f in S.Timing.__dataclass_fields__:
        assert getattr(S.APP, f) == getattr(H, f.upper()), f


def test_the_shared_talk_places_lines_as_the_apps_does_at_every_window():
    # (reactions and flight lines at each side of their windows, aired and not, an exchange waiting)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE lines (id INTEGER PRIMARY KEY, session_id, shot_id, ts, kind, text, aired_at, aired_seconds)")
    shots = [{"id": i, "session_id": 1, "ts": t, "outcome": o} for i, t, o in
             ((1, 100.0, None), (2, 160.0, None), (3, 170.0, "gimme"), (4, 260.0, None))]
    lines = [(1, 1, 101.0, "shot", "A long call " * 6, None, None), (1, None, 114.0, "flight", "Up it goes", None, None),
             (1, None, 120.0, "flight", "Still going", None, None), (1, None, 125.0, "reaction", "Wow", None, None),
             (1, None, 135.0, "reaction", "Late", None, None), (1, 2, 161.0, "shot", "Close", None, None),
             (1, None, 161.0, "banter", "Same breath " * 4, None, None), (2, None, 50.0, "banter", "Aired", 52.0, 3.0),
             (2, None, 60.0, "banter", "Never aired", None, None)]
    db.executemany("INSERT INTO lines (session_id, shot_id, ts, kind, text, aired_at, aired_seconds) VALUES (?,?,?,?,?,?,?)", lines)
    rows = db.execute("SELECT session_id, shot_id, ts, kind, text, aired_at, aired_seconds FROM lines ORDER BY ts, id").fetchall()
    assert S.talk(rows, shots) == H._talk(db, shots)
