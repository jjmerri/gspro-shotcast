import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from commentator import stats_web
from commentator.models import Line
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


def test_a_player_needs_a_recorded_round_not_just_stray_shots(tmp_path):
    path, db = _db(tmp_path)
    _shot(db, "Kroll", ts=100, course=None)              # a session whose round was never recorded
    _shot(db, "Pat", ts=900, course="Pebble")            # alternate shot: shots in a round, no card of her own
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, is_league)"
               " VALUES (1, 1000, '2026-10-01', 'Pebble', 'Pat & Lou', 9, 0)")
    db.commit()
    with stats_web.connect(path) as ro:
        assert [p["name"] for p in stats_web.players(ro)] == ["Pat"]


def test_viewer_opens_the_database_read_only(tmp_path):
    path, _ = _db(tmp_path)
    with stats_web.connect(path) as ro, pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO sessions (started_at, date) VALUES (0, 'x')")


def test_a_page_being_built_doesnt_lock_the_booth_out_of_the_database(tmp_path):
    path, db = _db(tmp_path)
    for i in range(3):
        _shot(db, "Ann", ts=i)
    db.commit()
    with stats_web.connect(path) as ro:
        reading = ro.execute("SELECT id FROM shots")
        reading.fetchone()                      # a long build, part way through a read
        with sqlite3.connect(path, timeout=0.2) as booth:
            booth.execute("INSERT INTO sessions (started_at, date) VALUES (0, 'x')")
        assert len(reading.fetchall()) == 2     # it reads what it started with

def test_server_serves_the_page_and_summary(tmp_path):
    path, db = _db(tmp_path)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, gross, scores, hole_pars, is_league)"
               " VALUES (1, 0, '2026-10-16', 'Pebble', 'Ann', 9, 37, ?, ?, 0)",
               (json.dumps({"1": 5, "2": 4}), json.dumps(PARS)))
    db.commit()
    brand = {"site_name": "Ann & Bo's", "more_tab": "The Shed"}
    server = stats_web.make_server(path, port=0, site=tmp_path / "site",   # season.db is next to site/
                                   brand=lambda: brand)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read()
        assert b"<title>Ann &amp; Bo&#x27;s</title>" in page and b'content="The Shed"' in page
        assert b'<span id="sitename">Ann &amp; Bo&#x27;s</span>' in page
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
        brand = {**brand, "season_start": "10-01"}     # saved in Settings: the next visit has it
        version = json.loads(urllib.request.urlopen(base + "/latest.json").read())["version"]
        body = json.loads(urllib.request.urlopen(f"{base}/v/{version}/summary-practice.json").read())
        assert body["season_from"]
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


def test_tournament_rounds_are_their_own_context(tmp_path):
    from contextlib import closing

    from commentator import season_analytics as A, stats_site

    path, db = _db(tmp_path)
    _shot(db, "Ann")
    _shot(db, "Bob", is_tournament=1)
    db.commit()
    with stats_web.connect(path) as ro:
        s = stats_web.summary(ro)
        assert (s["has_league"], s["has_tournament"]) == (False, True)   # the filter shows, without League
        assert [r["player"] for r in A.shot_log(ro, "tournament")] == ["Bob"]
        assert [r["player"] for r in A.shot_log(ro, "practice")] == ["Ann"]
    out = tmp_path / "data.db"
    with stats_web.connect(path) as ro:
        stats_site.build_data_db(ro, out)
    sql, args = stats_site.shot_query("tournament", {})
    with closing(sqlite3.connect(out)) as pub:
        assert [json.loads(j)["player"] for (j,) in pub.execute(sql, args)] == ["Bob"]


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


def test_a_round_counts_each_players_mulligans_but_a_misread_putt(tmp_path):
    path, db = _db(tmp_path)
    for ts, player, putt, mph in ((10, "Ann", 0, 150.0), (11, "Ann", 1, 1.2), (12, "Ann", 1, 3.5),
                                  (13, "Ann", 1, None), (14, "Bob", 1, 0.9)):
        _shot(db, player, ts=ts, course="Pebble", mulligan=1, is_putt=putt, ball_speed=mph)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, players, holes, gross, scores,"
               " hole_pars, complete) VALUES (1, 100, '2026-10-01', 'Pebble', 'Team', '[\"Ann\", \"Bob\"]', 18, 8,"
               " '{\"1\": 4, \"2\": 4}', '{\"1\": 4, \"2\": 4}', 0)")
    db.commit()
    with stats_web.connect(path) as ro:
        # the drive, the 3.5 mph putt and a putt with no speed read count; the putts under 2 mph don't
        assert stats_web.rounds(ro)[0]["mulligans"] == {"Ann": 3, "Bob": 0}


def test_a_one_player_round_splits_its_holes_by_putts_when_its_shots_add_up(tmp_path):
    path, db = _db(tmp_path)
    for ts, hole, lie in ((10, 1, "green"), (11, 2, "green"), (12, 2, "green"), (13, 3, "green"), (14, 3, "green"),
                          (15, 3, "green"), (16, 3, "green"), (17, 4, "fairway")):   # a chip-in on 4
        _shot(db, "Ann", ts=ts, course="Pebble", hole=f"Hole {hole}, par 4", lie_before=lie, is_putt=lie == "green")
    _shot(db, "Ann", ts=18, course="Pebble", hole="Hole 1, par 4", lie_before="green", is_putt=1, mulligan=1)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, players, holes, gross, putts, scores,"
               " hole_pars, complete) VALUES (1, 100, '2026-10-01', 'Pebble', 'Ann', '[\"Ann\"]', 4, 16, 7,"
               " '{\"1\": 4, \"2\": 4, \"3\": 5, \"4\": 3}', '{\"1\": 4, \"2\": 4, \"3\": 4, \"4\": 4}', 1)")
    db.commit()

    def split():
        with stats_web.connect(path) as ro:
            return stats_web.rounds(ro)[0]["putt_split"]
    assert split() == [1, 1, 1]
    # A putt short of the round's count (a shot not read) leaves the split out rather than show it wrong.
    db.execute("UPDATE rounds SET putts = 8")
    db.commit()
    assert split() is None


def test_a_round_s_length_from_its_tees_sums_its_holes_labels_when_all_are_read(tmp_path):
    path, db = _db(tmp_path)
    for who, lengths in (("Ann", (410, 160, 530)), ("Bob", (350, 140, 450))):   # Bob off the forward tees
        for h, yards in enumerate(lengths, 1):
            _shot(db, who, ts=10 + h, course="Pebble", hole=f"Hole {h}, par 4, {yards} yards", shot_number=1)
    _shot(db, "Ann", ts=14, course="Pebble", hole="Hole 2, par 4, 524 yards", shot_number=2)   # a misread
    _shot(db, "Ann", ts=15, course="Pebble", hole="Hole 2, par 4, 160 yards", shot_number=3)
    for side in ("Ann", "Bob"):
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, complete, scores, hole_pars)"
                   " VALUES (1, 100, '2026-10-01', 'Pebble', ?, 3, 1, '{\"1\": 4, \"2\": 3, \"3\": 5}',"
                   " '{\"1\": 4, \"2\": 3, \"3\": 5}')", (side,))
    db.commit()

    def lengths():
        with stats_web.connect(path) as ro:
            return {r["side"]: r["tee_yards"] for r in stats_web.rounds(ro)}
    assert lengths() == {"Ann": 1100, "Bob": 940}
    # A hole of the card without its label leaves the length unknown, whatever other hole was read.
    db.execute("UPDATE shots SET hole = 'Hole 9, par 4, 400 yards' WHERE player = 'Bob' AND hole LIKE 'Hole 3,%'")
    db.commit()
    assert lengths() == {"Ann": 1100, "Bob": None}


def test_a_round_has_its_tee_boxs_whole_course_length_from_the_course_file(tmp_path, monkeypatch):
    from commentator import hole_replay

    path, db = _db(tmp_path)
    _shot(db, "Ann", ts=10, course="Pebble", hole="Hole 1, par 4", shot_number=1)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, complete, scores, hole_pars)"
               " VALUES (1, 100, '2026-10-01', 'Pebble', 'Ann', 9, 1, '{\"1\": 4}', '{\"1\": 4}')")
    db.execute("INSERT INTO tee_lengths VALUES ('PEBBLE', 'Blue', 6115, 18), ('Pebble', 'Red', 5000, 18)")
    db.commit()
    monkeypatch.setattr(hole_replay, "round_tees", lambda db, of_shot: dict.fromkeys(of_shot.values(), "Blue"))
    with stats_web.connect(path) as ro:
        r = stats_web.rounds(ro)[0]
    assert (r["tee"], r["course_yards"], r["course_holes"]) == ("Blue", 6115, 18)


def test_a_rounds_replay_hides_with_it_and_goes_when_every_round_on_it_is_deleted(tmp_path):
    import re
    import shutil
    import subprocess
    from pathlib import Path

    path, db = _db(tmp_path)
    # (first shot, end): A alone on one video, B and C on another, D on a third; a fourth has no round.
    for first, end in ((1000, 2000), (4000, 5000), (7000, 8000), (10000, 11000)):
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, holes_played, gross, scores,"
                   " hole_pars, is_league) VALUES (1, ?, '2026-10-16', 'Pebble', 'Ann', 9, 9, 36, '{}', ?, 0)",
                   (end, json.dumps(PARS)))
        _shot(db, "Ann", ts=first, course="Pebble", club="DR", shot_number=1, lie_before="tee")
        _shot(db, "Ann", ts=end - 60, course="Pebble", club="PT", is_putt=1)
    _shot(db, "Ann", ts=2010, course="Pebble", club="PT", is_putt=1, outcome="gimme")   # A's, conceded late
    db.commit()
    videos = tmp_path / "recordings"
    videos.mkdir()
    spans = {"20261016-190000-aaaaaa": (900, 2100), "20261016-200000-bbbbbb": (3900, 8200),
             "20261016-210000-cccccc": (9900, 11200), "20261016-220000-dddddd": (20000, 21000),
             "20261016-230000-eeeeee": (2095, 3800)}   # starts in A's closer, a few buffered seconds in
    for session, (since, ended) in spans.items():
        (videos / f"{session}.ts").write_bytes(b"x")
        (videos / f"{session}.json").write_text(json.dumps({"session": session, "since": since, "ended": ended,
                                                            "duration": ended - since, "shots": True}))
    bucket = [{"session": "20261016-190001-111111", "since": 905, "ended": 2100},
              {"session": "20261016-200001-222222", "since": 3905, "ended": 8200},
              {"session": "20261016-210001-333333", "since": 9905, "ended": None}]   # still going
    asked = []
    server = stats_web.make_server(path, port=0, site=tmp_path / "site", videos=lambda: (videos, 14),
                                   video_deleted=lambda r: asked.append(r["session"]), bucket_videos=lambda: bucket)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(url, body):
        return json.loads(urllib.request.urlopen(urllib.request.Request(
            base + url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})).read())

    def kept():
        return sorted(p.stem for p in videos.glob("*.ts"))
    try:
        post("/api/rounds/1/delete", {"ended": 2000, "confirm": True})
        assert kept() == sorted(set(spans) - {"20261016-190000-aaaaaa"})
        assert asked == ["20261016-190001-111111"]          # the bucket's copy, by its own name
        post("/api/rounds/2/delete", {"ended": 5000, "confirm": True})   # C is still on that video
        post("/api/rounds/4/excluded", {"excluded": True, "ended": 11000})   # hidden, not deleted
        # (asked again each time till the booth's mirror deletes it: the same request file)
        assert kept() == sorted(set(spans) - {"20261016-190000-aaaaaa"}) and set(asked) == {"20261016-190001-111111"}
        version = json.loads(urllib.request.urlopen(base + "/latest.json").read())["version"]
        out = json.loads(urllib.request.urlopen(f"{base}/v/{version}/summary-all.json").read())["vod_rounds"]
        assert out == [[1000, 2000, 1], [4000, 5000, 1], [7000, 8000, 0], [10000, 11000, 1]]
        post("/api/rounds/4/delete", {"ended": 11000, "confirm": True})
        assert "20261016-210000-cccccc" not in kept()
        assert set(asked) == {"20261016-190001-111111"}     # the bucket's is still on air
    finally:
        server.shutdown()
        server.server_close()

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = re.search(r"\nconst vodOut = .*?\nconst shownVods = .*?\n", page, re.S).group(0)
    recs = [{"session": s, "since": a, "ended": b} for s, (a, b) in spans.items()]
    recs.append({"session": "live", "since": 9900, "ended": None})
    script = (f"const state = {{ data: {{ vod_rounds: {json.dumps(out)} }}, vodsAll: {json.dumps(recs)} }};" + fns
              + "console.log(JSON.stringify(shownVods().map(v => v.session)))")
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    assert got == ["20261016-200000-bbbbbb", "20261016-220000-dddddd", "20261016-230000-eeeeee", "live"]


def test_the_pages_tee_line_gives_the_whole_course_and_the_nine_played():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = re.search(r"\nconst teeName = .*?\nfunction teeText\(.*?\n}\n", page, re.S).group(0)
    card = lambda holes: [{"hole": h} for h in holes]
    cases = [
        ({"tee": "Blue", "holes": 9, "card": card(range(1, 10)), "tee_yards": 3170, "course_yards": 6115,
          "course_holes": 18}, False),
        ({"tee": "Blue", "holes": 9, "card": card(range(10, 19)), "tee_yards": 3170, "course_yards": 6115,
          "course_holes": 18}, True),
        ({"tee": "Blue", "holes": 18, "card": card(range(1, 19)), "tee_yards": 6100, "course_yards": 6115,
          "course_holes": 18}, False),
        ({"tee": "Blue", "holes": 9, "card": card(range(10, 19)), "tee_yards": 3170, "course_yards": None,
          "course_holes": None}, False),
        ({"tee": "Blue", "holes": 9, "card": card(range(1, 10)), "tee_yards": 3100, "course_yards": 3105,
          "course_holes": 9}, False),   # a nine-hole course, played through
        ({"tee": "Par3", "holes": 1, "card": card([1]), "tee_yards": 116, "course_yards": None,
          "course_holes": None}, False),
    ]
    script = fns + "console.log(JSON.stringify(%s.map(([r, s]) => teeText(r, s))))" % json.dumps(cases)
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    assert got == ["Blue tees · 6,115 yds · front 9: 3,170", "Blue tees · 6,115 yds",
                   "Blue tees · 6,115 yds", "Blue tees · back 9: 3,170 yds", "Blue tees · 3,105 yds",
                   "Par 3 tees · 1 hole: 116 yds"]


def test_the_pages_handicap_toggle_swaps_in_the_index_without_mulligans():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = re.search(r"\nfunction useHcp\(.*?\n}\n", page, re.S).group(0) \
        + re.search(r"\nconst leftForMulligans = .*?\n", page).group(0) \
        + re.search(r"\nfunction mulliganTotal\(.*?\n}\n", page, re.S).group(0) \
        + re.search(r"\nfunction mulliganText\(.*?\n}\n", page, re.S).group(0) \
        + re.search(r"\nfunction puttText\(.*?\n}\n", page, re.S).group(0)
    script = "const esc = s => s, state = {hcpClean: false, data: {players: [{index: 7.0, index_history: [1, 2, 3, 4],"\
        " index_clean: 5.0, index_history_clean: [1, 2, 3]}]}};" + fns + """
const p = state.data.players[0], seen = [];
useHcp(); seen.push([p.index, p.index_history.length, leftForMulligans(p)]);
state.hcpClean = true; useHcp(); seen.push([p.index, p.index_history.length, leftForMulligans(p)]);
state.hcpClean = false; useHcp(); seen.push([p.index, p.index_history.length, leftForMulligans(p)]);
const one = {mulligans: {Ann: 2}}, two = {mulligans: {Ann: 2, Bob: 1}}, none = {mulligans: {Ann: 0, Bob: 0}};
seen.push([mulliganTotal(one), mulliganText(one)], [mulliganTotal(two), mulliganText(two)],
          [mulliganTotal(none), mulliganText(none)], [mulliganTotal({}), mulliganText({})],
          puttText({putt_split: [4, 12, 2]}), puttText({putt_split: null}));
console.log(JSON.stringify(seen));"""
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    assert got == [[7.0, 4, 0], [5.0, 3, 1], [7.0, 4, 0], [2, ""], [3, "Ann 2, Bob 1"], [0, ""], ["—", ""],
                   "1-putts 4 · 2-putts 12 · 3-putts 2", ""]


def test_the_live_player_catches_up_a_viewer_who_fell_behind():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = re.search(r"\nconst LIVE_SLACK = .*?\nfunction liveCatchUp\(.*?\n}\n", page, re.S).group(0) \
        + re.search(r"\nfunction chatBehind\(.*?\nfunction gateChat\(.*?\n}\n", page, re.S).group(0)
    script = "let player; const state = {tab: 'live'};" + fns + """
const behind = {style: {display: "none"}}, send = {disabled: false};
const chat = {el: {querySelector: q => q === ".behind" ? behind : send}};
const seen = [], hls = {latency: 12, targetLatency: 10, liveSyncPosition: 500};
const p = player = {hls, golive: {hidden: true, textContent: ""}, edge: null,
                    video: {paused: false, playbackRate: 1, currentTime: 490, play: () => Promise.resolve()}};
const look = () => seen.push([p.video.playbackRate, p.golive.hidden, p.golive.hidden ? "" : p.golive.textContent]);
liveCatchUp(p); look();                                   // 2 s over: left alone
hls.latency = 25; liveCatchUp(p); look();                 // 15 s behind: plays fast
p.video.paused = true; liveCatchUp(p); look();            // paused: normal speed
p.video.paused = false; hls.latency = 90; liveCatchUp(p); look();   // 80 s behind: offered a jump
seen.push([send.disabled, behind.style.display]);         // and can't send to the chat
liveCatchUp(p, true); seen.push(p.video.currentTime); look();       // jumped to the usual delay
seen.push([send.disabled, behind.style.display]);
hls.latency = 10.2; liveCatchUp(p); look();               // caught up
// An iPhone's own player: behind measured against where it started.
const v = {paused: false, playbackRate: 1, currentTime: 100, seekable: {length: 1, end: () => 120}, play: () => Promise.resolve()};
const q = player = {hls: null, golive: {hidden: true}, edge: null, video: v};
const look2 = () => seen.push([v.playbackRate, q.golive.hidden]);
liveCatchUp(q); look2();
v.currentTime = 90; liveCatchUp(q); look2();
console.log(JSON.stringify(seen));"""
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    assert got == [[1, True, ""], [1.25, True, ""], [1, True, ""], [1.25, False, "1:20 behind live · Jump to live"],
                   [True, ""], 500, [1, True, ""], [False, "none"], [1, True, ""], [1, True], [1.25, True]]


def test_the_league_cards_place_ties_mark_the_sim_and_the_viewers_team():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = "\n".join(re.search(rf"\nconst {n} = .*?\n", page).group(0) for n in ("esc", "toPar", "weekPanel", "myTeam")) \
        + "".join(re.search(rf"\nfunction {n}\(.*?\n}}\n", page, re.S).group(0) for n in ("weekCard", "standingsCard"))
    script = "const state = {}, when = () => 'Sun 9:00 AM';" + fns + """
const text = h => h.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim();
const out = [weekPanel()];
state.week = {week: 6, course: "Test Links", deadline: "2026-12-06", holes: 9, waiting: ["Hackers"],
              rows: [{team: "Duffers", net: -3}, {team: "Birdies", net: 1}, {team: "Aces", net: -3, thru: 7, on: true}]};
out.push(text(weekPanel()));
state.week.rows[2] = {team: "Aces", net: null, thru: 0, on: true};
state.week.waiting = [];
out.push(text(weekPanel()));
// Home's cards: a final week names who never posted; the viewer's team is bold; tied places shared.
state.me = "Mike";
out.push(weekCard({week: 5, course: "Old", final: true, waiting: ["Hackers"],
                   rows: [{team: "Aces", players: ["Mike", "Sarah"], net: -2}, {team: "Birdies", net: 0, sim: true}]}));
out.push(text(standingsCard({weeks: 1, rows: [{team: "Aces", players: ["Mike"], points: 65, place: 1},
  {team: "Duffers", players: [], points: 65, place: 1}, {team: "Hackers", players: [], points: 50, place: 3}],
  provisional: true})));
out.push(standingsCard(null) + weekCard(null));
console.log(JSON.stringify(out));"""
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    assert got[0] == ""
    assert got[1].startswith("Week 6 Test Links · due ") and got[1].endswith(
        "Team Net T1 Duffers -3 T1 Aces -3 thru 7 · on now 3 Birdies +1 Yet to play: Hackers")
    assert got[2].endswith("Team Net 1 Duffers -3 2 Birdies +1 Aces – on now")
    assert '<tr class="mine">' in got[3] and "Week 5 <span class=\"sub\">Old · final</span>" in got[3]
    assert "Didn't post: Hackers" in got[3] and "on now" not in got[3]
    assert "<td>E*</td>" in got[3] and "* the sim's count: not on Squabbit yet" in got[3]
    assert got[4] == ("Standings after 1 week Team Points T1 Aces 65 T1 Duffers 65 3 Hackers 50 "
                      "Counts the sim's score for a team not on Squabbit yet.")
    assert got[5] == ""


def test_a_rounds_highlights_list_its_moments_to_jump_to():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = "\n".join(re.search(rf"\nconst {n} = .*?\n", page).group(0) for n in ("esc", "hm")) \
        + "".join(re.search(rf"\nfunction {n}\(.*?\n}}\n", page, re.S).group(0) for n in ("reelBtn", "reelPart"))
    script = "const state = {}, hashFor = (t, o) => t + JSON.stringify(o), shareBtn = (h, t) => `[share ${h}]`;" + fns + """
const out = [reelPart(), reelBtn(7)];
state.reels = {"7": {label: "Pebble · Ann & <Bob>", date: "2026-11-01", ended: 2, league: true, duration: 95,
                     moments: [{at: 3, what: "Ann holes a 30 ft putt"}, {at: 41.5, what: "Bob finds the water", bad: true}]},
               "9": {label: "Riviera · Cal", date: "2026-11-02", ended: 3, moments: [{at: 0, what: "x"}, {at: 9, what: "y"}]}};
out.push(reelBtn(7), reelBtn(8));
state.vod = {reel: "7", at: 0};
out.push(...reelPart());
console.log(JSON.stringify(out));"""
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    assert got[:2] == [["", ""], ""]
    assert got[2] == '<button class="link" data-reel="7" data-vodat="0" title="The round\'s best moments, 2 of them">▶ Highlights</button>'
    assert got[3] == ""
    head, rows = got[4], got[5]
    assert "Pebble · Ann &amp; &lt;Bob&gt;" in head and 'id="liveslot"' in head
    assert 'data-reel="7" data-vodat="41.5">Bob finds the water</button> <span class="sub">(oops)</span>' in head
    assert '[share vods{"reel":"7","player":""}]' in head and 'round{&quot;r&quot;:&quot;7&quot;}' in head
    assert rows.index("Riviera") < rows.index("Pebble") and '<tr class="sel">' in rows and "2 min" in rows
    assert "league rounds' kept all season" in rows


def test_a_round_card_takes_its_longest_drive_from_its_shots(tmp_path):
    """Stored as none at the round's end (the club wasn't read), the drive counts once a rule does."""
    path, db = _db(tmp_path)
    _shot(db, "Win", ts=10, course="Pebble", hole="Hole 1, par 4", club=None, shot_number=1, lie_before="tee",
          to_pin_yards=365, total_yards=319.5)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, gross, scores, hole_pars,"
               " longest_drive, complete) VALUES (1, 100, '2026-10-01', 'Pebble', 'Win', 18, 4,"
               " '{\"1\": 4}', '{\"1\": 4}', NULL, 0)")
    db.commit()
    with stats_web.connect(path) as ro:
        assert [r["longest_drive"] for r in stats_web.rounds(ro)] == [319.5]


def test_a_tee_shot_with_no_club_read_counts_as_a_drive_off_a_long_hole(tmp_path):
    """A booth that never read the club box: its 320-yard drive was missing from the stats. A par 3's
    tee shot (the pin under 250) and a second shot with no club still aren't drives."""
    from commentator import season_analytics as A

    path, db = _db(tmp_path)
    _shot(db, "Win", ts=1, club=None, shot_number=1, lie_before="tee", to_pin_yards=365, total_yards=319.5)
    _shot(db, "Win", ts=2, club=None, shot_number=1, lie_before="tee", to_pin_yards=180, total_yards=400)
    _shot(db, "Win", ts=3, club=None, shot_number=2, lie_before="fairway", to_pin_yards=300, total_yards=450)
    _shot(db, "Win", ts=4, club="3W", shot_number=1, lie_before="tee", to_pin_yards=380, total_yards=500)
    db.commit()
    with stats_web.connect(path) as ro:
        p = stats_web.player(ro, "Win")
        assert (p["longest_drive"], p["drives"]) == (320, 1)
        assert [d["v"] for d in A.highlights(ro, A.holes_played(ro))["drives"]] == [320]
    store = Store(None)
    _shot(store.db, "Win", club=None, shot_number=1, lie_before="tee", to_pin_yards=365, total_yards=319.5)
    assert store.leaderboard()[0]["longest_drive"] == 320


def test_the_booth_round_sheet_drops_a_left_out_drive():
    store = Store(None)
    for yards, out in ((999, 1), (250, 0)):
        _shot(store.db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=yards, left_out=out)
    assert list(store.shot_lengths([1, 2]).values()) == [250]


def test_the_local_page_can_leave_a_shot_out(tmp_path):
    path, db = _db(tmp_path)
    _shot(db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=330)
    _shot(db, "Ann", club="DR", shot_number=1, lie_before="tee", total_yards=250)
    db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, is_league) VALUES (1, 10, '2026-10-01', 'Ann', 9, 0)")
    db.commit()
    changes = []
    server = stats_web.make_server(path, port=0, site=tmp_path / "site", on_change=lambda: changes.append(1),
                                   publish_status=lambda: {"enabled": True, "due": 5})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(body, kind="application/json", url="/api/shots/1/left-out", **headers):
        return urllib.request.urlopen(urllib.request.Request(base + url, data=json.dumps(body).encode(),
                                                             headers={"Content-Type": kind, **headers}))
    try:
        assert json.loads(urllib.request.urlopen(base + "/latest.json").read())["local"] is True
        with pytest.raises(urllib.error.HTTPError) as e:   # not the shot the page showed
            post({"left_out": True, "ts": 5})
        assert e.value.code == 404
        assert json.loads(post({"left_out": True, "ts": 0}).read()) == {"id": 1, "left_out": True}
        assert db.execute("SELECT left_out FROM shots WHERE id = 1").fetchone()[0] == 1
        version = json.loads(urllib.request.urlopen(base + "/latest.json").read())["version"]
        summary = json.loads(urllib.request.urlopen(f"{base}/v/{version}/summary-all.json").read())
        assert summary["players"][0]["longest_drive"] == 250
        for bad, code in (({"kind": "text/plain"}, 403), ({"Origin": "http://evil.example"}, 403),
                          ({"url": "/api/shots/99/left-out"}, 404)):
            with pytest.raises(urllib.error.HTTPError) as e:
                post({"left_out": False, "ts": 0}, **bad)
            assert e.value.code == code
        assert db.execute("SELECT left_out FROM shots WHERE id = 1").fetchone()[0] == 1
        assert changes == [1]   # only the edit that saved: the tray publishes after it
        status = json.loads(urllib.request.urlopen(base + "/api/publish-status").read())
        assert status == {"enabled": True, "due": 5}
    finally:
        server.shutdown()
        server.server_close()


def test_the_local_page_can_exclude_a_round_and_delete_one(tmp_path):
    path, db = _db(tmp_path)
    nine = {h: PARS[str(h)] for h in range(1, 10)}
    for end, gross, course in ((100, 36, "Pebble"), (5000, 50, "Pebble")):   # the second is a test round
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, holes_played, gross, scores,"
                   " hole_pars, is_league) VALUES (1, ?, '2026-10-16', ?, 'Ann', 9, 9, ?, ?, ?, 0)",
                   (end, course, gross, json.dumps({str(h): s for h, s in nine.items()}), json.dumps(PARS)))
        db.execute("INSERT INTO player_rounds (session_id, ended_at, player, course, scores, hole_pars, is_league,"
                   " holes_played) VALUES (1, ?, 'Ann', ?, ?, ?, 0, 9)",
                   (end + 1, course, json.dumps({str(h): s for h, s in nine.items()}), json.dumps(PARS)))
        _shot(db, "Ann", ts=end - 10, course=course, club="DR", shot_number=1, lie_before="tee",
              total_yards=250 if end == 100 else 330)
    # A putt conceded after the first round was recorded: the first round's, not the next one's on the course.
    _shot(db, "Ann", ts=110, course="Pebble", club="PT", is_putt=1, outcome="gimme")
    db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (1, 2, 4990, 'shot', 'a', 'Big one')")
    db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text) VALUES (1, 1, 90, 'shot', 'a', 'Nice')")
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text) VALUES (1, 4985, 'preshot', 'a', 'Here we go')")
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text) VALUES (1, 5030, 'segment', 'a', 'What a round')")
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text) VALUES (1, 130, 'segment', 'a', 'Nice round')")
    db.commit()
    server = stats_web.make_server(path, port=0, site=tmp_path / "site")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(url, body):
        return json.loads(urllib.request.urlopen(urllib.request.Request(
            base + url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})).read())

    def summary():
        version = json.loads(urllib.request.urlopen(base + "/latest.json").read())["version"]
        return json.loads(urllib.request.urlopen(f"{base}/v/{version}/summary-all.json").read())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:   # not the round the page showed: left alone
            post("/api/rounds/2/excluded", {"excluded": True, "ended": 100})
        assert e.value.code == 404
        assert post("/api/rounds/2/excluded", {"excluded": True, "ended": 5000}) == {"id": 2, "excluded": True}
        assert db.execute("SELECT SUM(excluded) FROM rounds").fetchone()[0] == 1
        assert [r[0] for r in db.execute("SELECT excluded FROM shots ORDER BY id")] == [0, 1, 0]
        assert db.execute("SELECT excluded FROM player_rounds WHERE id = 2").fetchone()[0] == 1
        s = summary()
        assert [r["round"] for r in s["rounds"]] == [1] and s["players"][0]["longest_drive"] == 250
        assert [(x["round"], x["sides"]) for x in s["excluded_rounds"]] == [(2, [{"side": "Ann", "gross": 50}])]
        # The booth's own season stats (from earlier days) leave it out too.
        db.execute("UPDATE sessions SET started_at = 0, date = '2026-10-16' WHERE id = 1")
        db.commit()
        store = Store(str(path))
        try:
            assert store.season_longest_drive() == ("Ann", 250)
            assert store.leaderboard()[0]["longest_drive"] == 250
        finally:
            store.close()
        with stats_web.connect(path) as ro:   # a published site doesn't list it, or what was said on it
            assert "excluded_rounds" not in stats_web.summary(ro)
            assert sorted(ln[0] for ln in ro.execute("SELECT text FROM lines")) == ["Nice", "Nice round"]
        assert post("/api/rounds/2/excluded", {"excluded": False, "ended": 5000}) == {"id": 2, "excluded": False}
        # A practice round made a league one: every side, card and shot of it moves to the League stats.
        assert post("/api/rounds/2/league", {"league": True, "ended": 5000}) == {"id": 2, "league": True}
        assert [r[0] for r in db.execute("SELECT is_league FROM rounds ORDER BY id")] == [0, 1]
        assert [r[0] for r in db.execute("SELECT is_league FROM player_rounds ORDER BY id")] == [0, 1]
        assert [r[0] for r in db.execute("SELECT is_league FROM shots ORDER BY id")] == [0, 1, 0]
        s = summary()
        assert s["has_league"] and [(r["round"], r["league"]) for r in s["rounds"]] == [(2, True), (1, False)]
        with pytest.raises(urllib.error.HTTPError) as e:   # not the round the page showed
            post("/api/rounds/2/league", {"league": False, "ended": 100})
        assert e.value.code == 404
        assert post("/api/rounds/2/league", {"league": False, "ended": 5000}) == {"id": 2, "league": False}
        assert db.execute("SELECT SUM(is_league) FROM shots").fetchone()[0] == 0
        assert [r["round"] for r in summary()["rounds"]] == [2, 1] and summary()["excluded_rounds"] == []
        # A tournament one, and back: a league round is never a tournament one.
        assert post("/api/rounds/2/league", {"league": False, "tournament": True, "ended": 5000}) == {"id": 2, "league": False}
        assert [r[0] for r in db.execute("SELECT is_tournament FROM rounds ORDER BY id")] == [0, 1]
        assert [(r["round"], r["tournament"]) for r in summary()["rounds"]] == [(2, True), (1, False)]
        post("/api/rounds/2/league", {"league": True, "tournament": True, "ended": 5000})
        assert db.execute("SELECT SUM(is_tournament), SUM(is_league) FROM shots").fetchone() == (0, 1)
        post("/api/rounds/2/league", {"league": False, "ended": 5000})
        with pytest.raises(urllib.error.HTTPError) as e:   # no confirmation: nothing deleted
            post("/api/rounds/2/delete", {"ended": 5000})
        assert e.value.code == 400
        assert post("/api/rounds/2/delete", {"confirm": True, "ended": 5000}) == {"id": 2, "deleted": True}
        s = summary()
        assert [r["round"] for r in s["rounds"]] == [1] and s["excluded_rounds"] == []
        with stats_web.connect(path) as ro:
            assert [r[0] for r in ro.execute("SELECT id FROM shots")] == [1, 3]
            assert sorted(ln[0] for ln in ro.execute("SELECT text FROM lines")) == ["Nice", "Nice round"]
        # The booth's closer, said after the round was deleted, goes with it.
        db.execute("UPDATE rounds SET ended_at = ? WHERE id = 2", (time.time() - 5,))
        db.commit()
        store = Store(str(path))
        try:
            store.session_id = 1
            store.record_lines("segment", [Line("a", "Late closer")])
        finally:
            store.close()
        assert db.execute("SELECT excluded FROM lines WHERE text = 'Late closer'").fetchone()[0] == stats_web.DELETED
        # Gone for good: it can't be put back, and its ids aren't freed for the next round's to take.
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/rounds/2/excluded", {"excluded": False, "ended": 5000})
        assert e.value.code == 404
        _shot(db, "Bo", ts=9000)
        assert db.execute("SELECT MAX(id) FROM shots").fetchone()[0] == 4
    finally:
        server.shutdown()
        server.server_close()


def test_a_rounds_rows_take_its_own_late_gimme_and_its_lines_from_before_a_restart(tmp_path):
    path, db = _db(tmp_path)
    db.execute("INSERT INTO sessions (id, started_at, date) VALUES (2, 0, '2026-10-16')")
    for sess, end, course in ((1, 100, "Pebble"), (2, 5000, "Augusta")):
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes) VALUES (?, ?, '2026-10-16', ?, 'Ann', 9)",
                   (sess, end, course))
    _shot(db, "Ann", ts=50, course="Pebble")
    _shot(db, "Ann", ts=200, course="Augusta", outcome="gimme")   # Augusta's first hole, in Pebble's session
    _shot(db, "Ann", ts=4000, session_id=2, course="Augusta")
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text) VALUES (1, 400, 'preshot', 'a', 'Augusta')")
    db.commit()
    db.row_factory = sqlite3.Row
    assert stats_web._round_rows(db, 1, 100)[2:] == [[1], []]
    assert stats_web._round_rows(db, 2, 5000)[2:] == [[2, 3], [1]]


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


def test_the_pages_league_average_rounds_like_pythons_own_numbers():
    """A pooled figure (the page's pyRound) and a player's own (Python's round) must agree, or a tie
    shows as a win: 49/20 is 2.45 as a float just under, 2.5 in Python; 26/32 -> 81.25% -> 81.2."""
    import random
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fn = re.search(r"const pyRound = .*?\n};\n", page, re.S).group(0)
    rng = random.Random(1)
    cases = [(49, 20, 1), (2600, 32, 1), (5, 2, 0), (7, 2, 0), (-5, 2, 0), (-1, 4, 1)] + [
        (rng.randint(-3000, 3000), rng.choice([2, 4, 8, 20, 40, rng.randint(1, 400)]), rng.choice([0, 1, 2]))
        for _ in range(3000)]
    script = fn + "console.log(JSON.stringify(%s.map(([a, b, d]) => pyRound(a / b, d))))" % json.dumps(cases)
    # on stdin: Windows caps a command line at 32k characters
    got = json.loads(subprocess.run([node], input=script, capture_output=True, text=True, check=True).stdout)
    assert got == [round(a / b, d) for a, b, d in cases]


def test_the_local_page_plays_the_round_videos_kept_on_the_pc(tmp_path):
    import time

    from commentator.local_video import LocalRecorder

    path, _ = _db(tmp_path)
    videos = tmp_path / "recordings"
    rec = LocalRecorder(videos, 14)
    since = round(time.time()) - 3600
    rec.begin("20261016-190000-abcdef", since)
    rec.rec["shots"] = True
    for i in range(16):
        rec.add(b"seg%02d" % i, 4.0)
    rec.end()
    rec.begin("20261016-210000-fedcba", since + 7200)              # still recording
    asked = []
    server = stats_web.make_server(path, port=0, site=tmp_path / "site", videos=lambda: (videos, 14),
                                   video_deleted=lambda r: asked.append((r["session"], r["since"], r["ended"])))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/live"

    def delete(session, body=b'{"confirm": true}'):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/recordings/{session}/delete",
                                     data=body, headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        index = json.loads(urllib.request.urlopen(base + "/recordings.json").read())
        assert index["keep_days"] == 14 and [r["session"] for r in index["recordings"]][1:] == ["20261016-190000-abcdef"]
        assert "segments" not in index["recordings"][1] and index["recordings"][1]["ended"] == since + 64
        full = urllib.request.urlopen(base + "/s/20261016-190000-abcdef/full.m3u8").read().decode()
        assert full.count("#EXTINF:4.000,") == 16 and "seg15.ts" in full and full.rstrip().endswith("#EXT-X-ENDLIST")
        assert urllib.request.urlopen(base + "/s/20261016-190000-abcdef/seg3.ts").read() == b"seg03"
        for bad in ("/s/20261016-190000-abcdef/seg16.ts", "/s/..%2F..%2Fseason.db/full.m3u8", "/clips.json"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404
        assert delete("20261016-190000-abcdef", b"{}") == 400                 # the page asks first
        assert delete("20261016-210000-fedcba") == 400                         # still recording
        assert delete("20261016-190000-000000") == 404 and delete("..%2F..%2Fseason") == 404 and not asked
        assert delete("20261016-190000-abcdef") == 200
        assert asked == [("20261016-190000-abcdef", since, since + 64)]        # the bucket's copy goes too
        assert not (videos / "20261016-190000-abcdef.ts").exists() and not (videos / "20261016-190000-abcdef.json").exists()
        index = json.loads(urllib.request.urlopen(base + "/recordings.json").read())
        assert [r["session"] for r in index["recordings"]] == ["20261016-210000-fedcba"]
    finally:
        server.shutdown()
        server.server_close()


def test_the_recap_player_times_its_lines_over_the_clips():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    page = (Path(__file__).parent.parent / "commentator" / "stats_web.html").read_text(encoding="utf-8")
    fns = re.search(r"\nfunction recapPlayer\(.*?\n}\n", page, re.S).group(0)
    body = {"week": 6, "course": "Test Links", "rows": [{"name": "Aces", "net": -5, "place": 1, "tied": False, "points": 70}],
            "standings": [{"name": "Aces", "points": 70, "place": 1, "was": 3}],
            "intro": [{"text": "i1", "audio": "a/0.wav"}, {"text": "i2", "audio": "a/1.wav"}],
            "clips": [{"at": 0, "lines": [{"text": "c1", "at": 1.5, "audio": "a/2.wav"}, {"text": "c2", "at": 4, "audio": "a/3.wav"},
                                          {"text": "c2b", "at": 10.5, "audio": "a/3b.wav"}]},
                      {"at": 10, "lines": [{"text": "c3", "at": 11.5, "audio": "a/4.wav"}, {"text": "c4", "at": 30, "audio": "a/5.wav"}]}],
            "outro": [{"text": "o1", "audio": "a/6.wav"}]}
    script = """
const esc = s => String(s), toPar = n => String(n), state = {};
const on = o => { o.ev = {}; o.addEventListener = (k, f) => (o.ev[k] ||= []).push(f); o.fire = k => (o.ev[k] || []).forEach(f => f({ target: o.t })); return o; };
const document = { createElement: () => on({}) };
const heard = [], log = [];
let audio;
class Audio { constructor() { audio = on(this); this.paused = true; }
  play() { this.paused = false; heard.push(this.src.split("/").pop()); return Promise.resolve(); }
  pause() { this.paused = true; } removeAttribute() {} }
const video = on({ currentTime: 0, paused: true, muted: true,
  play() { this.paused = false; log.push("play@" + this.currentTime); return Promise.resolve(); },
  pause() { this.paused = true; log.push("pause@" + this.currentTime); this.fire("pause"); } });
const BODY = %s;
const fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(BODY) });
const me = { video }; let player = me;
%s
recapPlayer(me, "2026-12-06-w6");
const at = t => { video.currentTime = t; video.fire("timeupdate"); };
setTimeout(() => {
  const r = me.recap, start = r.card.innerHTML.includes("Play recap");
  r.card.t = { dataset: { recapplay: "1" } }; r.card.fire("click");
  audio.fire("ended");             // i1 -> i2
  audio.fire("ended");             // i2 -> the clips
  const playing = r.phase;
  at(1); at(1.6);                  // c1 due
  at(4.2);                         // c2 due while c1 still going: the video holds
  const held = video.paused;
  audio.fire("ended");             // c1 done -> the video goes on, c2 plays
  at(10.1);                        // c2 still going into the next clip: the video holds
  audio.fire("ended");             // c2 done -> c2b, due later but its clip is over: still held
  const stillHeld = video.paused;
  audio.fire("ended");             // c2b done -> the next clip
  at(11.6);                        // c3, still going as the video ends (a pause, then the end)
  video.ended = true; video.pause(); video.fire("ended");
  const tail = [audio.paused, r.phase];
  audio.fire("ended");             // c3 done: c4, which the video ended before, still plays
  audio.fire("ended");             // then the outro
  const outro = r.card.innerHTML.includes("Standings");
  audio.fire("ended");
  console.log(JSON.stringify({ stillHeld, start, playing, held, heard, log, tail, outro, done: r.phase, again: r.card.innerHTML.includes("Watch again") }));
}, 0);
""" % (json.dumps(body), fns)
    got = json.loads(subprocess.run([node], input=script, capture_output=True, encoding="utf-8", check=True).stdout)
    # A seek into the next clip mid-line: the line's left behind, and the video goes on (its timeupdate
    # comes before the seeked, the line looking past its clip then).
    seek = script[:script.index("setTimeout(")] + """setTimeout(() => {
  const r = me.recap; r.card.t = { dataset: { recapplay: "1" } }; r.card.fire("click");
  audio.fire("ended"); audio.fire("ended");
  at(1.6);
  video.seeking = true; at(12); video.seeking = false; video.fire("seeked");
  const after = [video.paused, audio.paused, r.line];
  at(12.3);                        // c3
  video.ended = true; video.fire("ended");
  video.ended = false; video.seeking = true; at(1); video.seeking = false; video.fire("seeked");
  at(1.6); audio.fire("ended");    // back to c1, the video going on from there
  console.log(JSON.stringify({ after, heard, back: [r.phase, video.paused] }));
}, 0);"""
    sought = json.loads(subprocess.run([node], input=seek, capture_output=True, encoding="utf-8", check=True).stdout)
    # A line that won't load (its play rejected, and an error) is skipped once, not twice.
    bad = script[:script.index("setTimeout(")].replace("play() { this.paused = false;",
        "play() { if (this.src.endsWith('/0.wav')) { this.error = {}; queueMicrotask(() => this.fire('error')); return Promise.reject(new Error('x')); }"
        " this.error = null; this.paused = false;", 1) + """setTimeout(() => {
  const r = me.recap; r.card.t = { dataset: { recapplay: "1" } }; r.card.fire("click");
  setTimeout(() => console.log(JSON.stringify([r.phase, r.line && r.line.text])), 0);
}, 0);"""
    assert json.loads(subprocess.run([node], input=bad, capture_output=True, encoding="utf-8", check=True).stdout) == ["intro", "i2"]
    # A play the video's buffering cuts off (its pause aborting it): the line's still going, picked up after.
    cut = script[:script.index("setTimeout(")].replace("play() { this.paused = false;",
        "play() { if (this.src.endsWith('/2.wav') && !this.once) { this.once = true; const e = new Error('x'); e.name = 'AbortError';"
        " return Promise.reject(e); } this.paused = false;", 1) + """setTimeout(() => {
  const r = me.recap; r.card.t = { dataset: { recapplay: "1" } }; r.card.fire("click");
  audio.fire("ended"); audio.fire("ended");
  at(1.6);
  setTimeout(() => { video.fire("playing"); console.log(JSON.stringify([r.line && r.line.text, audio.paused])); }, 0);
}, 0);"""
    assert json.loads(subprocess.run([node], input=cut, capture_output=True, encoding="utf-8", check=True).stdout) == ["c1", False]
    # One that fails resumed at the video's end: skipped once, the next line still heard out.
    end = script[:script.index("setTimeout(")].replace("play() { this.paused = false;",
        "play() { if (this.src.endsWith('/4.wav') && this.again) { this.error = {}; queueMicrotask(() => this.fire('error'));"
        " return Promise.reject(new Error('x')); } if (this.src.endsWith('/4.wav')) this.again = true; this.error = null; this.paused = false;", 1) + """setTimeout(() => {
  const r = me.recap; r.card.t = { dataset: { recapplay: "1" } }; r.card.fire("click");
  audio.fire("ended"); audio.fire("ended");
  at(1.6); audio.fire("ended"); at(4.2); audio.fire("ended"); at(10.6); audio.fire("ended");
  at(11.6); audio.paused = true; video.ended = true; video.fire("ended");
  setTimeout(() => console.log(JSON.stringify([r.phase, r.line && r.line.text])), 0);
}, 0);"""
    assert json.loads(subprocess.run([node], input=end, capture_output=True, encoding="utf-8", check=True).stdout) == ["clips", "c4"]
    assert sought["after"] == [False, True, None] and sought["heard"] == ["0.wav", "1.wav", "2.wav", "4.wav", "2.wav"]
    assert sought["back"] == ["clips", False]
    assert got["start"] and got["playing"] == "clips" and got["held"] and got["outro"] and got["again"]
    assert got["heard"] == ["0.wav", "1.wav", "2.wav", "3.wav", "3b.wav", "4.wav", "5.wav", "6.wav"]
    assert got["log"] == ["play@0", "pause@4.2", "play@4.2", "pause@10.1", "play@10.1", "pause@11.6"] and got["done"] == "done"
    assert got["tail"] == [False, "clips"] and got["stillHeld"]
