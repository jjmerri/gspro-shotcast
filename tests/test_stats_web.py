import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from commentator import stats_web
from commentator.store import Store

PARS = {str(h): p for h, p in zip(range(1, 10), [4, 4, 3, 5, 4, 3, 4, 5, 4])}   # par 36


def _db(tmp_path):
    path = tmp_path / "season.db"
    Store(str(path)).close()   # the booth's schema (and one session row)
    db = sqlite3.connect(path)
    return path, db


def _player_round(db, player, scores, fmt=None, complete=1, played=None, league=0, **counts):
    cols = {"fir_hit": 0, "fir_tries": 0, "gir_hit": 0, "gir_tries": 0, "scramble_hit": 0, "scramble_tries": 0,
            "sand_hit": 0, "sand_tries": 0, "putts": 0, "putt_holes": 0, **counts}
    db.execute(
        f"INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, {', '.join(cols)}, scores, hole_pars,"
        f" is_league, complete, holes_played) VALUES (1, 0, ?, 'Pebble', ?, {', '.join('?' * len(cols))}, ?, ?, ?, ?, ?)",
        (player, fmt, *cols.values(), json.dumps({str(h): s for h, s in scores.items()}), json.dumps(PARS),
         league, complete, played if played is not None else len(scores)))


def _shot(db, player, **kw):
    row = {"session_id": 1, "ts": 0, "player": player, "is_putt": 0, "attributed": 1, "mulligan": 0, "is_league": 0, **kw}
    db.execute(f"INSERT INTO shots ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def test_player_scoring_leaves_out_shared_ball_rounds_and_short_quits(tmp_path):
    path, db = _db(tmp_path)
    nine = {h: PARS[str(h)] for h in range(1, 10)}
    _player_round(db, "Ann", {**nine, 1: 3, 2: 6}, gir_hit=5, gir_tries=9, putts=15, putt_holes=9, fir_hit=4, fir_tries=7)
    _player_round(db, "Ann", {h: 2 for h in range(1, 10)}, fmt="scramble", gir_hit=9, gir_tries=9, fir_hit=3, fir_tries=7)
    _player_round(db, "Ann", {1: 9}, complete=0, played=1, gir_hit=0, gir_tries=1)
    db.commit()
    with stats_web.connect(path) as ro:
        p = stats_web.player(ro, "Ann")
    assert p["rounds"] == 1 and p["avg_to_par_9"] == 1.0      # birdie + double; the quit isn't a round
    assert p["gir"] == [5, 10]                                  # the quit's hole counts, the scramble doesn't
    assert p["fir"] == [7, 14]                                  # own tee shots count in a scramble too
    assert p["putts_per_hole"] == pytest.approx(15 / 9, abs=0.01)
    assert p["holes"] == 10 and p["birdie_pct"] == 10.0


def test_shot_stats_skip_mulligans_and_long_tee_shots_aren_t_approaches(tmp_path):
    path, db = _db(tmp_path)
    _shot(db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=250)
    _shot(db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=330, mulligan=1)
    _shot(db, "Ann", club="3W", shot_number=1, lie_before="tee", to_pin_yards=480, finish_yards=240)
    _shot(db, "Ann", club="3W", shot_number=2, lie_before="fairway", to_pin_yards=230, finish_yards=20)
    _shot(db, "Ann", club="PT", is_putt=1, to_pin_yards=2.5, outcome="holed")    # 7.5 ft
    _shot(db, "Ann", club="PT", is_putt=1, to_pin_yards=2.0, outcome="missed")   # 6 ft
    _shot(db, "Ann", club="PT", is_putt=1, to_pin_yards=0.5, outcome="gimme")
    _shot(db, "Ann", club="PT", is_putt=1, to_pin_yards=1.0, outcome="holed")    # 3 ft, gimmes off
    db.commit()
    with stats_web.connect(path) as ro:
        p = stats_web.player(ro, "Ann", gimme_ft=4.0)
        rec = stats_web.records(ro)
        six = stats_web.player(ro, "Ann", gimme_ft=6.0)["putting"][0]
    assert (p["avg_drive"], p["longest_drive"]) == (250, 250)
    assert p["clubs"] == [{"club": "3W", "n": 1, "avg_finish": 20.0, "best_finish": 20.0, "avg_from": 230.0}]
    six_ten = next(b for b in p["putting"] if b["range"] == "6-10 ft")
    assert (six_ten["made"], six_ten["attempts"]) == (1, 2)
    assert p["putting"][0]["range"] == "4-6 ft" and (p["putting"][0]["made"], p["putting"][0]["attempts"]) == (1, 1)
    assert (six["range"], six["made"], six["attempts"]) == ("6-10 ft", 2, 3)   # buckets start at the usual circle
    assert rec["longest_drive"]["v"] == 250 and rec["longest_putt"]["v"] == 2.5


def test_the_usual_gimme_is_the_one_most_league_rounds_used(tmp_path):
    path, db = _db(tmp_path)
    # Three league rounds (two at 6 ft, one at 4 ft) and a practice round at 4 ft with many more shots.
    for i, (league, ft, n) in enumerate(((1, 6, 2), (1, 6, 2), (1, 4, 2), (0, 4, 30))):
        end = 1000 * (i + 1)
        for k in range(n):
            _shot(db, "Ann", ts=end - 100 + k, course="Pebble", gimme_yards=ft / 3, is_league=league)
        _shot(db, "Ann", ts=end - 1, course="Pebble", is_league=league)   # an opening tee shot logs none
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, gross, scores, hole_pars,"
                   " is_league) VALUES (1, ?, '2026-10-01', 'Pebble', 'Ann', 9, 36, '{}', '{}', ?)", (end, league))
    db.commit()
    with stats_web.connect(path) as ro:
        s = stats_web.summary(ro)
    assert s["gimme_ft"] == 6.0
    assert sorted(r["gimme_ft"] for r in s["rounds"]) == [4.0, 4.0, 6.0, 6.0]


def test_a_rounds_side_players_come_from_the_record_else_its_shots(tmp_path):
    path, db = _db(tmp_path)
    for player in ("Tony", "Priya"):
        _shot(db, player, ts=900, course="Pebble")
    # An older row (no players kept) for a practice team stored under its league team's name.
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, team, holes, is_league)"
               " VALUES (1, 1000, '2026-10-01', 'Pebble', 'Duffers', 'Duffers', 9, 0)")
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, team, holes, is_league, players)"
               " VALUES (1, 5000, '2026-10-02', 'Pebble', 'Aces', 'Aces', 9, 1, '[\"Josh\", \"Sam\"]')")
    db.commit()
    with stats_web.connect(path) as ro:
        got = {r["side"]: r["side_players"] for r in stats_web.rounds(ro)}
    assert got == {"Duffers": ["Priya", "Tony"], "Aces": ["Josh", "Sam"]}


def test_a_players_history_links_each_round_it_was_part_of(tmp_path):
    path, db = _db(tmp_path)
    for end, side in ((0, "Ann"), (10, "Bob"), (3600, "Ann")):   # two sides of one round, then another round
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, is_league)"
                   " VALUES (1, ?, '2026-10-01', 'Pebble', ?, 9, 0)", (end, side))
    nine = {h: PARS[str(h)] for h in range(1, 10)}
    _player_round(db, "Ann", nine)
    db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, scores, hole_pars, is_league,"
               " complete, holes_played) VALUES (1, 3600, 'Ann', 'Pebble', ?, ?, 0, 1, 9)",
               (json.dumps(nine), json.dumps(PARS)))
    db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, scores, hole_pars, is_league,"
               " complete, holes_played) VALUES (1, 9999, 'Ann', 'Elsewhere', ?, ?, 0, 1, 9)",
               (json.dumps(nine), json.dumps(PARS)))
    db.commit()
    with stats_web.connect(path) as ro:
        ids = [r["id"] for r in ro.execute("SELECT id FROM rounds ORDER BY id")]
        assert [h["round"] for h in stats_web.player(ro, "Ann")["history"]] == [ids[0], ids[2], None]
        assert {r["session"] for r in stats_web.rounds(ro)} == {1}


def test_context_filter_splits_league_and_practice(tmp_path):
    path, db = _db(tmp_path)
    nine = {h: PARS[str(h)] for h in range(1, 10)}
    _player_round(db, "Ann", nine, league=1)
    _player_round(db, "Bob", nine, league=0)
    db.commit()
    with stats_web.connect(path) as ro:
        assert [p["name"] for p in stats_web.players(ro, "league")] == ["Ann"]
        assert [p["name"] for p in stats_web.players(ro, "practice")] == ["Bob"]
        assert [p["name"] for p in stats_web.players(ro)] == ["Ann", "Bob"]


def test_viewer_opens_the_database_read_only(tmp_path):
    path, _ = _db(tmp_path)
    with stats_web.connect(path) as ro, pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO sessions (started_at, date) VALUES (0, 'x')")


def test_server_serves_the_page_and_summary(tmp_path):
    path, db = _db(tmp_path)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, gross, scores, hole_pars, is_league)"
               " VALUES (1, 0, '2026-10-16', 'Pebble', 'Ann', 9, 37, ?, ?, 0)",
               (json.dumps({"1": 5, "2": 4}), json.dumps(PARS)))
    db.commit()
    server = stats_web.make_server(path, port=0, site=tmp_path / "site")   # season.db is next to site/
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert b"Season stats" in urllib.request.urlopen(base + "/").read()
        version = json.loads(urllib.request.urlopen(base + "/latest.json").read())["version"]
        body = json.loads(urllib.request.urlopen(f"{base}/v/{version}/summary-practice.json").read())
        assert body["context"] == "practice"
        r = body["rounds"][0]
        assert (r["gross"], r["to_par"], r["played"]) == (9, 1, 9)
        assert [h["hole"] for h in r["card"]] == [1, 2]
        # The browser's SQLite reads stats.db in pieces: HEAD for the size, then byte ranges.
        url = f"{base}/v/{version}/stats.db"
        head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"))
        size = int(head.headers["Content-Length"])
        assert head.headers["Accept-Ranges"] == "bytes" and size > 1024
        part = urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-99"}))
        assert part.status == 206 and part.headers["Content-Range"] == f"bytes 0-99/{size}"
        assert part.read().startswith(b"SQLite format 3\x00")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/../season.db")
        assert e.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_predictions_show_the_commentators_on_air_names(tmp_path):
    path, db = _db(tmp_path)
    for who, ok in (("pbp", 1), ("pbp", 0), ("color", 1)):
        db.execute("INSERT INTO predictions (session_id, ts, commentator, kind, predicted, actual, correct)"
                   " VALUES (1, 0, ?, 'putt', 'make', 'made', ?)", (who, ok))
    db.commit()
    with stats_web.connect(path) as ro:
        rows = stats_web.predictions(ro, {"pbp": "Nigel", "color": "Duke"})
    assert [(r["commentator"], r["role"], r["calls"], r["right"]) for r in rows] == [
        ("Duke", "color analyst", 1, 1), ("Nigel", "play-by-play", 2, 1)]


def test_summary_says_whether_there_are_league_rounds(tmp_path):
    path, db = _db(tmp_path)
    _shot(db, "Ann")
    db.commit()
    with stats_web.connect(path) as ro:
        assert stats_web.summary(ro)["has_league"] is False   # practice only: the page hides the filter
    _shot(db, "Ann", is_league=1)
    db.commit()
    with stats_web.connect(path) as ro:
        assert stats_web.summary(ro)["has_league"] is True


def test_a_shot_left_out_drops_out_of_the_stats_but_stays_in_its_hole(tmp_path):
    from commentator import season_analytics as A

    path, db = _db(tmp_path)
    _shot(db, "Ann", ts=1, hole="Hole 1, par 4", club="DR", shot_number=1, lie_before="tee", to_pin_yards=400,
          total_yards=250)
    _shot(db, "Ann", ts=2, hole="Hole 1, par 4", club="DR", shot_number=2, lie_before="fairway", to_pin_yards=150,
          total_yards=330, left_out=1)
    _shot(db, "Ann", ts=3, hole="Hole 1, par 4", club="PT", shot_number=3, is_putt=1, lie_before="green",
          to_pin_yards=3, outcome="holed")
    db.commit()
    with stats_web.connect(path) as ro:
        assert stats_web.player(ro, "Ann")["shots"] == 2
        holes = A.holes_played(ro)
        assert [s["shot_number"] for s in holes[0]["strokes"]] == [1, 2, 3]
        # The left-out stroke still ends the drive (no penalty jump), but gains nothing itself.
        assert [s["shot_id"] for s in A.strokes_gained(holes)] == [1, 3]
        assert [s["left_out"] for s in A.shot_log(ro)] == [0, 1, 0]
    # A database from before leaving shots out reads as none left out.
    db.execute("ALTER TABLE shots DROP COLUMN left_out")
    db.commit()
    with stats_web.connect(path) as ro:
        assert stats_web.player(ro, "Ann")["shots"] == 3
    # The local page's toggle adds the column to such a database.
    assert stats_web.set_left_out(path, 1, True)
    assert db.execute("SELECT left_out FROM shots WHERE id = 1").fetchone() == (1,)


def test_a_round_left_out_drive_is_not_its_longest(tmp_path):
    path, db = _db(tmp_path)
    for i, yards in enumerate((999, 250)):
        _shot(db, "Ann", ts=10 + i, course="Pebble", hole=f"Hole {i + 1}, par 4", club="DR", shot_number=1,
              lie_before="tee", total_yards=yards, left_out=int(yards == 999))
    _shot(db, "Bob", ts=12, course="Pebble", hole="Hole 1, par 4", club="DR", shot_number=1, lie_before="tee",
          total_yards=300)
    for side, drive in (("Ann", 999), ("Bob", 300)):
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, gross, scores, hole_pars,"
                   " longest_drive, complete) VALUES (1, 100, '2026-10-01', 'Pebble', ?, 18, 8,"
                   " '{\"1\": 4, \"2\": 4}', '{\"1\": 4, \"2\": 4}', ?, 0)", (side, drive))
    db.commit()
    with stats_web.connect(path) as ro:
        assert {r["side"]: r["longest_drive"] for r in stats_web.rounds(ro)} == {"Ann": 250, "Bob": 300}


def test_the_booth_round_sheet_drops_a_left_out_drive():
    store = Store(None)
    for yards, out in ((999, 1), (250, 0)):
        _shot(store.db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=yards, left_out=out)
    assert list(store.shot_lengths([1, 2]).values()) == [250]


def test_the_local_page_can_leave_a_shot_out(tmp_path):
    path, db = _db(tmp_path)
    _shot(db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=330)
    _shot(db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=250)
    db.commit()
    server = stats_web.make_server(path, port=0, site=tmp_path / "site")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(body, kind="application/json", url="/api/shots/1/left-out", **headers):
        return urllib.request.urlopen(urllib.request.Request(base + url, data=json.dumps(body).encode(),
                                                             headers={"Content-Type": kind, **headers}))
    try:
        assert json.loads(urllib.request.urlopen(base + "/latest.json").read())["local"] is True
        assert json.loads(post({"left_out": True}).read()) == {"id": 1, "left_out": True}
        assert db.execute("SELECT left_out FROM shots WHERE id = 1").fetchone()[0] == 1
        version = json.loads(urllib.request.urlopen(base + "/latest.json").read())["version"]
        summary = json.loads(urllib.request.urlopen(f"{base}/v/{version}/summary-all.json").read())
        assert summary["players"][0]["longest_drive"] == 250
        for bad, code in (({"kind": "text/plain"}, 403), ({"Origin": "http://evil.example"}, 403),
                          ({"url": "/api/shots/99/left-out"}, 404)):
            with pytest.raises(urllib.error.HTTPError) as e:
                post({"left_out": False}, **bad)
            assert e.value.code == code
        assert db.execute("SELECT left_out FROM shots WHERE id = 1").fetchone()[0] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_a_players_round_is_summed_up_against_their_season(tmp_path):
    path, db = _db(tmp_path)
    scores = {h: PARS[str(h)] for h in range(1, 10)} | {1: 3, 2: 6}
    _player_round(db, "Ann", scores, gir_hit=5, gir_tries=9, putts=18, putt_holes=9)
    _player_round(db, "Ann", {h: PARS[str(h)] + 1 for h in range(1, 10)}, gir_hit=1, gir_tries=9, putts=18, putt_holes=9)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, holes_played, gross, scores,"
               " hole_pars, gir_hit, gir_tries, fir_hit, fir_tries, putts, putt_holes, is_league)"
               " VALUES (1, 100, '2026-10-16', 'Pebble', 'Ann', 9, 9, 37, ?, ?, 5, 9, 0, 0, 18, 9, 0)",
               (json.dumps({str(h): s for h, s in scores.items()}), json.dumps(PARS)))
    _shot(db, "Ann", ts=1, course="Pebble", hole="Hole 1, par 4", club="7I", shot_number=1, lie_before="tee",
          to_pin_yards=160, finish_yards=3)
    _shot(db, "Ann", ts=2, course="Pebble", hole="Hole 1, par 4", club="PT", shot_number=2, is_putt=1,
          lie_before="green", to_pin_yards=3, outcome="holed")
    _shot(db, "Ann", ts=3, course="Pebble", hole="Hole 2, par 4", club="PT", shot_number=1, is_putt=1,
          lie_before="green", to_pin_yards=20, outcome="holed", left_out=1)
    db.commit()
    with stats_web.connect(path) as ro:
        m = stats_web.summary(ro)["rounds"][0]["summary"]
    vs = {v["label"]: (v["round"], v["season"]) for v in m["vs"]}
    assert vs["Score / 9"] == (1.0, 5.0) and vs["Greens"] == (55.6, 33.3) and vs["Putts / hole"] == (2.0, 2.0)
    # The 9-footer gained most; the left-out 60-footer isn't counted; no shot lost strokes, so none cost any.
    assert m["best_shot"]["id"] == 2 and m["worst_shot"] is None
    assert m["sg"]["total"] == 0.99
    assert (m["best_hole"]["hole"], m["worst_hole"]["hole"]) == (1, 2)
    # A round quit part-way through scores by the nine it finished, as in the season average.
    db.execute("UPDATE rounds SET holes_played = 11, nine_to_par = -1")
    db.commit()
    with stats_web.connect(path) as ro:
        m = stats_web.summary(ro)["rounds"][0]["summary"]
    assert m["vs"][0]["round"] == -1


def test_an_eighteen_stopped_at_the_turn_reads_as_a_nine_and_a_later_quit_stays_a_quit(tmp_path):
    path, db = _db(tmp_path)
    for side, played in (("Ann", 9), ("Bob", 12)):
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, holes_played, complete)"
                   " VALUES (1, 100, '2026-10-01', 'Pebble', ?, 18, ?, 0)", (side, played))
    db.commit()
    with stats_web.connect(path) as ro:
        got = {r["side"]: (r["played"], r["holes"], r["complete"]) for r in stats_web.rounds(ro)}
    assert got == {"Ann": (9, 9, True), "Bob": (12, 18, False)}


def test_three_putts_by_first_putt_length_leave_out_scramble_holes(tmp_path):
    path, db = _db(tmp_path)
    ts = iter(range(1000))

    def hole(n, feet, putts, **kw):
        for i in range(putts):
            _shot(db, "Ann", ts=next(ts), course="Pebble", hole=f"Hole {n}, par 4", is_putt=1, lie_before="green",
                  to_pin_yards=(feet if i == 0 else 1) / 3, **kw)

    hole(1, 35, 3)
    hole(2, 33, 2)
    hole(3, 5, 1)
    hole(4, 35, 3, fmt="scramble", shared_ball=1)
    db.commit()
    with stats_web.connect(path) as ro:
        t = stats_web.three_putts(ro, "Ann")
    assert t["three_putts"] == [1, 3]
    bands = {b["range"]: (b["three_putts"], b["holes"]) for b in t["three_putting"]}
    assert bands["30-40 ft"] == (1, 2) and bands["0-10 ft"] == (0, 1)
