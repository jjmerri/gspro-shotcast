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
            return mine
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
    for start in (None, 0, 5, 12, 30, 120, 160):
        for lie in (None, "green", "fringe", "fairway", "rough", "bunker", "deep rough"):
            for finish in (None, 1.0, 3.0):
                assert E.short_game_gimme_level(start, lie, finish) == I.short_game_gimme_level(start, lie, finish)
        if start:
            assert E.approach_gimme_level(start, 1.0) == I.approach_gimme_level(start, 1.0)
            assert E.holed_out_level(start, 2, 4, 100.0) == I.holed_out_level(start, 2, 4, 100.0)
    for feet in (1, 4.4, 5.6, 14):
        assert E.stuffed_level(feet) == I.stuffed_level(feet)
    assert E.putt_to_par(3, 4) == I.putt_to_par(3, 4) == -1
    assert S.APP.shot_shows == H.SHOT_SHOWS and S.APP.talk_lag == H.TALK_LAG and S.APP.pre == H.PRE
