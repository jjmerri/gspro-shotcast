"""A paired booth's players' histories from Shotcast Cloud (commentator/cloud_history.py): the answer
loaded into the history file, the booth's past read from it and the season file together, the calls
made as players set up, and nothing of it self-hosted."""
import asyncio
import gzip
import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
from datetime import date, timedelta

import httpx
import pytest

from commentator import cloud, cloud_history, cloud_rounds
from commentator.store import Store
from tests.test_cloud import FakeAPI, mock_http, paired_cfg

TODAY = date.today()

# The answer's columns as the cloud's api/shotcast_cloud/history.py has them (its PLAYER_COLUMNS,
# SIDE_COLUMNS and SHOT_COLUMNS): the fake cloud below answers in exactly this shape.
_COUNTS = ("fir_hit", "fir_tries", "gir_hit", "gir_tries", "scramble_hit", "scramble_tries", "sand_hit",
           "sand_tries", "putts", "putt_holes")
PLAYER_COLUMNS = ("ended_at", "course", "fmt", *_COUNTS, "scores", "hole_pars", "complete", "holes_played",
                  "nine_to_par")
SIDE_COLUMNS = ("ended_at", "date", "course", "format", "side", "team", "holes", "gross", "par", "strokes",
                "net_to_par", *_COUNTS, "penalties", "longest_drive", "scores", "hole_pars", "source", "complete",
                "holes_played", "nine_to_par", "nine_net_to_par", "green_firmness", "fairway_firmness",
                "course_type", "launch_monitor")
SHOT_COLUMNS = ("uid", "ts", "course", "hole", "shot_number", "club", "is_putt", "lie_before", "lie_after",
                "to_pin_yards", "finish_yards", "total_yards", "carry_game_yards", "carry_raw", "ball_speed",
                "club_speed", "launch_angle", "total_spin", "spin_axis", "start_direction", "outcome", "mulligan",
                "attributed")


def answer(rounds=(), shots=(), index=None):
    """The cloud's answer, columnar, from rows: each round {id, day, is_league, is_tournament, player: {..},
    side: {.., side_id, players}}, each shot {round: its place, ..}. Columns a row doesn't give are null."""
    rounds, shots = list(rounds), list(shots)
    return {
        "v": 1,
        "rounds": {
            **{k: [r.get(k, 0 if k.startswith("is_") else None) for r in rounds]
               for k in ("id", "day", "is_league", "is_tournament")},
            "player": {k: [r.get("player", {}).get(k) for r in rounds] for k in PLAYER_COLUMNS},
            "side": {k: [r.get("side", {}).get(k, [] if k == "players" else None) for r in rounds]
                     for k in (*SIDE_COLUMNS, "side_id", "players")},
        },
        "shots": {k: [s.get(k) for s in shots] for k in ("round", *SHOT_COLUMNS)},
        "index": index,
    }


def _holes(text):
    return {k: v for k, v in json.loads(text).items()} if text else None


def answer_from(db, player, index=None):
    """What the cloud answers for `player` when it has the rounds a season file holds (as the PC sent
    them): the player's card, their side's, and their shots not left out, newest round first."""
    rounds, shots = [], []
    for pr in db.execute("""SELECT p.*, s.date AS day FROM player_rounds p JOIN sessions s ON s.id = p.session_id
                            WHERE p.player = ? AND p.round_uuid IS NOT NULL ORDER BY p.ended_at DESC""",
                         (player,)).fetchall():
        side = db.execute("""SELECT * FROM rounds WHERE round_uuid = ? AND (side = ? OR EXISTS
                             (SELECT 1 FROM json_each(rounds.players) WHERE value = ?))""",
                          (pr["round_uuid"], player, player)).fetchone()
        place = len(rounds)
        rounds.append({
            "id": pr["round_uuid"], "day": pr["day"], "is_league": pr["is_league"], "is_tournament": pr["is_tournament"],
            "player": {k: (_holes(pr[k]) if k in ("scores", "hole_pars") else bool(pr[k]) if k == "complete"
                           else pr[k]) for k in PLAYER_COLUMNS},
            "side": {**{k: (_holes(side[k]) if k in ("scores", "hole_pars") else bool(side[k]) if k == "complete"
                            else side[k]) for k in SIDE_COLUMNS},
                     "side_id": hashlib.sha256(side["side"].encode()).hexdigest()[:16],
                     "players": json.loads(side["players"])}})
        for s in db.execute("SELECT * FROM shots WHERE record_uuid = ? AND player = ? AND left_out = 0"
                            " ORDER BY ts, id", (pr["round_uuid"], player)):
            shots.append({"round": place, **{k: (bool(s[k]) if k in ("is_putt", "mulligan", "attributed") else s[k])
                                             for k in SHOT_COLUMNS}})
    return answer(rounds, shots, index)


# ---- a season of rounds, as a PC records them ----------------------------------------------------

def rows(a):
    """An answer's tables as rows again (answer's own form)."""
    rs = a["rounds"]
    rounds = [{**{k: rs[k][i] for k in ("id", "day", "is_league", "is_tournament")},
               "player": {k: v[i] for k, v in rs["player"].items()}, "side": {k: v[i] for k, v in rs["side"].items()}}
              for i in range(len(rs["id"]))]
    return rounds, [{k: v[j] for k, v in a["shots"].items()} for j in range(len(a["shots"]["uid"]))]


def _day(n):
    return (TODAY - timedelta(days=n)).isoformat()


def play(db, rnd, day, uuid, side, players, n_shots=14, session=None):
    """One round on `day` in its own session (one a day; else `session`): each player's shots and card,
    and the side's card."""
    session = session or db.execute("INSERT INTO sessions (started_at, date) VALUES (?, ?)",
                                    (time.mktime(date.fromisoformat(day).timetuple()), day)).lastrowid
    t0 = time.mktime(date.fromisoformat(day).timetuple()) + 3600
    holes = rnd.choice((9, 18))
    pars = {str(h): rnd.choice((3, 4, 4, 5)) for h in range(1, holes + 1)}
    for i, p in enumerate(players):
        for k in range(n_shots):
            putt = rnd.random() < 0.35
            db.execute(
                """INSERT INTO shots (session_id, ts, player, course, hole, shot_number, club, is_putt, start_direction,
                       lie_before, lie_after, to_pin_yards, finish_yards, total_yards, carry_game_yards, carry_raw,
                       outcome, attributed, is_league, mulligan, left_out, excluded, record_uuid, uid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session, t0 + i * 1000 + k * 7.5, p, "c1", rnd.choice(("1", "2")), rnd.choice((1, 1, 2, 3)),
                 "PT" if putt else rnd.choice(("DR", "DR", "7I", "PW", None)), int(putt),
                 rnd.choice((None, -2.5, 0.0, 1.5)), "green" if putt else rnd.choice(("tee", "fairway", "rough", "sand")),
                 rnd.choice(("fairway", "green", "rough", None)),
                 rnd.choice((2.0, 5.0, 10.0)) if putt else rnd.choice((40.0, 100.0, 150.0, 260.0, 300.0)),
                 rnd.choice((None, 0.0, 2.5, 8.0, 15.5)), rnd.choice((None, 120.0, 250.5, 280.0, 301.0)),
                 rnd.choice((None, 230.0, 275.5)), rnd.choice((None, 240.0)),
                 rnd.choice(("holed", "missed", "gimme", None)) if putt else None,
                 int(rnd.random() < 0.9), 0, int(rnd.random() < 0.05), int(rnd.random() < 0.05), 0, uuid,
                 f"{uuid}-{p}-{k}"))
        scores = {h: pars[h] + rnd.choice((-1, 0, 0, 1, 2)) for h in pars}
        db.execute(
            """INSERT INTO player_rounds (session_id, ended_at, player, course, fmt, fir_hit, fir_tries, gir_hit, gir_tries,
                   putts, putt_holes, scores, hole_pars, is_league, complete, holes_played, round_uuid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?)""",
            (session, t0 + 5000, p, "c1", "stroke" if len(players) == 1 else "best_ball", rnd.randint(2, 7), 7,
             rnd.randint(2, 9), holes, rnd.randint(14, 34), holes, json.dumps(scores), json.dumps(pars), holes, uuid))
    gross = sum(pars.values()) + rnd.randint(-2, 12)
    db.execute(
        """INSERT INTO rounds (session_id, ended_at, date, course, format, side, holes, gross, par, net_to_par,
               fir_hit, fir_tries, gir_hit, gir_tries, putts, putt_holes, penalties, scores, hole_pars, source,
               is_league, complete, holes_played, players, round_uuid)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scorecard',0,1,?,?,?)""",
        (session, t0 + 5000, day, "c1", "stroke", side, holes, gross, sum(pars.values()), rnd.randint(-3, 9),
         rnd.randint(2, 7), 7, rnd.randint(2, 9), holes, rnd.randint(14, 34), holes, rnd.randint(0, 3),
         json.dumps({h: pars[h] + 1 for h in pars}), json.dumps(pars), holes, json.dumps(players), uuid))


def season(path, rnd, rounds):
    """A season file with `rounds` [(days ago, uuid, side, players)] played before today."""
    s = Store(path, new_session=False)
    for ago, uuid, side, players in rounds:
        play(s.db, rnd, _day(ago), uuid, side, players)
    s.db.commit()
    s.db.close()


def paired_store(path):
    """A paired booth's season file (today's session) with its history file attached."""
    s = Store(str(path))
    s.attach_history(str(cloud_history.path_for(str(path))))
    return s


def reads(s, player, sides):
    """Every routed read of a player and their sides, with the rows' local ids left out."""
    out = {}
    for ctx in (None, "practice", "league"):
        out[("broadcast", ctx)] = s.player_broadcast_stats(player, ctx)
        out[("holes", ctx)] = s.player_hole_stats(player, ctx)
        out[("season_stats", ctx)] = s.season_stats(player, ctx)
        out[("profile", ctx)] = s.player_profile(player, ctx)
        out[("fairways", ctx)] = s.fairways_hit(player, ctx)
        for y in (40.0, 150.0, 300.0):
            out[("approach", y, ctx)] = s.approach_proximity(player, y, 15.0, ctx)
        for feet in (6.0, 15.0, 30.0):
            out[("make", feet, ctx)] = s.putting_make_rate(player, feet, 2.5, ctx)
            out[("three", feet, ctx)] = s.three_putt_rate(player, feet, 10.0, ctx)
        out[("recovery", ctx)] = s.recovery_proximity(player, ("rough", "sand"), None, ctx)
        for side in sides:
            out[("season_rounds", side, ctx)] = s.season_rounds(side, ctx)
            out[("team_broadcast", side, ctx)] = s.team_broadcast_stats(side, ctx)
            out[("team_holes", side, ctx)] = s.team_hole_stats(side, ctx)
            out[("course", side, ctx)] = s.team_course_history(side, "c1", ctx)
    for side in sides:
        out[("side_rounds", side)] = [{k: v for k, v in r.items() if k != "id"} for r in s.side_rounds(side)]
        out[("skip", side)] = s.season_rounds(side, "practice", skip_rows=[])
    out["team_rounds"] = [{k: v for k, v in r.items() if k != "id"} for r in s.player_team_rounds(player)]
    out["band"] = s.putt_band_rate(player, 3, 15)
    out["leave"] = s.approach_leave(player, 100, 150)
    for club in ("DR", "7I", None):
        out[("length", club)] = s.length_norm(player, club)
    return out


# ---- the load ----------------------------------------------------------------------------------------

def test_each_routed_read_gives_what_it_gives_with_the_same_rounds_local(tmp_path):
    rnd = random.Random(11)
    played = [(3, "r1", "Ann", ["Ann"]), (5, "r2", "Ann & Bo", ["Ann", "Bo"]), (9, "r3", "Ann", ["Ann"]),
              (12, "r4", "Ann", ["Ann"]), (20, "r5", "Ann & Bo", ["Ann", "Bo"]), (30, "r6", "Ann", ["Ann"])]
    season(str(tmp_path / "local.db"), rnd, played)
    local = Store(str(tmp_path / "local.db"))
    got = answer_from(local.db, "Ann")
    assert len(got["rounds"]["id"]) == 6 and got["shots"]["uid"]

    cloudy = paired_store(tmp_path / "season.db")   # (a new bay: nothing local before today)
    cloud_history.load(cloud_history.path_for(str(tmp_path / "season.db")), "Ann", got)
    want = reads(local, "Ann", ["Ann", "Ann & Bo"])
    assert want[("season_rounds", "Ann", "practice")]["rounds"] == 4 and want[("season_stats", None)]["nights"] == 6
    assert reads(cloudy, "Ann", ["Ann", "Ann & Bo"]) == want


def test_the_answer_is_renamed_to_tonights_name_and_is_practice(tmp_path):
    s = paired_store(tmp_path / "season.db")
    got = answer([{"id": "r1", "day": _day(2), "is_league": 1,
                   "player": {"ended_at": 100.0, "course": "c1", "scores": {"1": 4}, "hole_pars": {"1": 4}},
                   "side": {"ended_at": 100.0, "date": _day(2), "course": "c1", "side": "pat smith", "holes": 9,
                            "gross": 40, "par": 36, "side_id": "s1", "players": ["pat smith"], "complete": True}}],
                 [{"round": 0, "uid": "u1", "ts": 50.0, "is_putt": False, "club": "DR", "mulligan": False,
                   "attributed": True}])
    cloud_history.load(cloud_history.path_for(str(tmp_path / "season.db")), "Pat Smith", got)
    h = s.db
    assert [tuple(r) for r in h.execute("SELECT player, is_league, record_uuid, session_id < 0 FROM hist.shots")] == \
        [("Pat Smith", 0, "r1", 1)]
    assert [tuple(r) for r in h.execute("SELECT player, is_league, scores FROM hist.player_rounds")] == \
        [("Pat Smith", 0, '{"1": 4}')]
    assert [tuple(r) for r in h.execute("SELECT side, players, is_league, complete FROM hist.rounds")] == \
        [("Pat Smith", '["Pat Smith"]', 0, 1)]
    assert [tuple(r) for r in h.execute("SELECT date FROM hist.sessions")] == [(_day(2),)]
    # The league round counts in the practice slice, as every round of a paired booth does.
    assert s.season_rounds("Pat Smith", "practice")["rounds"] == 1 and s.season_rounds("Pat Smith", "league")["rounds"] == 0
    assert s.season_stats("Pat Smith", "practice")["shots"] == 1


def test_columns_the_season_file_needs_are_filled_or_the_row_is_left_out(tmp_path):
    s = paired_store(tmp_path / "season.db")
    path = cloud_history.path_for(str(tmp_path / "season.db"))
    got = answer([
        # no times nor date: the round's day, at midday
        {"id": "r1", "day": _day(1), "player": {"course": "c1", "holes_played": 9},
         "side": {"side": "Ann", "holes": None, "holes_played": 9, "gross": 40, "side_id": "a", "players": ["Ann"]}},
        # no holes at all, and a team side with no label: the side's row can't be kept; no card for the player
        {"id": "r2", "day": _day(2), "side": {"side": None, "holes": None, "gross": 40, "side_id": "b",
                                               "players": ["Ann", "Bo"]}},
    ], [{"round": 0, "uid": "u1", "ts": None, "is_putt": False}, {"round": 0, "uid": "u2", "ts": 5.0, "is_putt": None},
        {"round": 1, "uid": "u3", "ts": 6.0, "is_putt": True}])
    cloud_history.load(path, "Ann", got)
    noon = time.mktime((TODAY - timedelta(days=1)).timetuple()) + 12 * 3600
    assert [tuple(r) for r in s.db.execute("SELECT ended_at, date, side, holes FROM hist.rounds")] == [(noon, _day(1), "Ann", 9)]
    assert [tuple(r) for r in s.db.execute("SELECT ended_at, complete FROM hist.player_rounds")] == [(noon, 1)]
    assert [r[0] for r in s.db.execute("SELECT uid FROM hist.shots")] == ["u3"]
    assert {r[0] for r in s.db.execute("SELECT round_uuid FROM hist.held")} == {"r1", "r2"}
    # (and every NOT NULL column of the season file's shapes is filled on what was kept)
    for t in ("sessions", "shots", "player_rounds", "rounds"):
        for name in [c[1] for c in s.db.execute(f"PRAGMA main.table_info({t})") if c[3]]:
            assert s.db.execute(f"SELECT COUNT(*) FROM hist.{t} WHERE {name} IS NULL").fetchone()[0] == 0, (t, name)


def _team(day, players=("Ann", "Bo")):
    return {"id": "t1", "day": day, "player": {"course": "c1", "ended_at": 10.0},
            "side": {"side": "Ann & Bo", "holes": 9, "gross": 38, "ended_at": 10.0, "date": day,
                     "side_id": "team", "players": list(players)}}


def test_a_team_round_from_two_teammates_answers_is_held_once(tmp_path):
    s = paired_store(tmp_path / "season.db")
    path = cloud_history.path_for(str(tmp_path / "season.db"))
    cloud_history.load(path, "Ann", answer([_team(_day(3), ("ann", "Player"))]))   # (a venue: Bo is "Player")
    cloud_history.load(path, "Bo", answer([_team(_day(3), ("Ann", "bo"))]))
    assert [tuple(r) for r in s.db.execute("SELECT side, players FROM hist.rounds")] == [("Ann & Bo", '["Ann", "Bo"]')]
    assert len(s.player_team_rounds("Ann")) == len(s.player_team_rounds("Bo")) == 1
    assert s.season_rounds("Ann & Bo")["rounds"] == 1
    # Ann's answer again, without the round: Bo's still holds it; then Bo's too, and it's gone.
    cloud_history.load(path, "Ann", answer())
    assert s.db.execute("SELECT COUNT(*) FROM hist.rounds").fetchone()[0] == 1
    assert s.player_team_rounds("Bo") and not s.db.execute("SELECT 1 FROM hist.player_rounds WHERE player = 'Ann'").fetchone()
    cloud_history.load(path, "Bo", answer())
    assert s.db.execute("SELECT COUNT(*) FROM hist.rounds").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [
    lambda a: a["rounds"]["day"].pop(),                         # a column short
    lambda a: a["rounds"]["side"].pop("gross"),                 # a column missing
    lambda a: a["shots"]["round"].__setitem__(0, 5),            # a shot of no round in the answer
    lambda a: a["rounds"]["day"].__setitem__(0, "yesterday"),   # not a date
    lambda a: a["rounds"]["side"]["scores"].__setitem__(0, {"1": [4]}) or a["shots"]["club"].__setitem__(0, {"x": 1}),
    lambda a: a.__setitem__("v", 2),                            # a format this app doesn't read
    lambda a: a.__setitem__("index", float("nan")),             # (review 147, 7) not a number
    lambda a: a.__setitem__("index", float("inf")),
])
def test_a_bad_answer_is_dropped_whole_and_what_was_there_is_kept(tmp_path, bad):
    s = paired_store(tmp_path / "season.db")
    path = cloud_history.path_for(str(tmp_path / "season.db"))
    good = answer([_team(_day(3))], [{"round": 0, "uid": "u1", "ts": 1.0, "is_putt": False}])
    cloud_history.load(path, "Ann", good)
    before = s.season_rounds("Ann & Bo"), s.season_stats("Ann", None)
    worse = answer([_team(_day(4)) | {"id": "t2"}, _team(_day(5)) | {"id": "t3"}],
                   [{"round": 0, "uid": "u2", "ts": 1.0, "is_putt": False, "club": "DR"}])
    bad(worse)
    with pytest.raises((cloud_history.Bad, sqlite3.Error)):
        cloud_history.load(path, "Ann", worse)
    assert (s.season_rounds("Ann & Bo"), s.season_stats("Ann", None)) == before
    assert [r[0] for r in s.db.execute("SELECT round_uuid FROM hist.held")] == ["t1"]


# ---- the union -------------------------------------------------------------------------------------

def test_the_season_files_own_rows_count_unless_the_answer_holds_their_round(tmp_path):
    rnd = random.Random(3)
    path = str(tmp_path / "season.db")
    # On this PC: a round the cloud has (r1), one it hasn't counted or been sent yet (r2), and a practice
    # stretch (its shots in no round), all before today.
    season(path, rnd, [(2, "r1", "Ann", ["Ann"]), (4, "r2", "Ann", ["Ann"])])
    db = sqlite3.connect(path)
    sid = db.execute("SELECT id FROM sessions ORDER BY id LIMIT 1").fetchone()[0]
    for k in range(5):
        db.execute("INSERT INTO shots (session_id, ts, player, is_putt, club, lie_before, total_yards, to_pin_yards,"
                   " record_uuid, uid, is_league) VALUES (?, ?, 'Ann', 0, 'DR', 'tee', 250, 400, 'stretch', ?, 0)",
                   (sid, 10.0 + k, f"p{k}"))
    db.commit()
    db.close()
    s = paired_store(path)
    local = {(p, r): n for p, r, n in s.db.execute(
        "SELECT player, record_uuid, COUNT(*) FROM shots WHERE left_out = 0 AND mulligan = 0 GROUP BY 1, 2")}
    # The cloud's answer: r1, less a shot its admins left out there, and r0, a round at another bay.
    rounds, shots = rows(answer_from(s.db, "Ann"))
    r1 = next(i for i, r in enumerate(rounds) if r["id"] == "r1")
    gone, *kept = [sh for sh in shots if sh["round"] == r1]
    other = {"id": "r0", "day": _day(8), "player": {"course": "c9", "ended_at": 1.0},
             "side": {"side": "Ann", "course": "c9", "holes": 18, "gross": 80, "par": 72, "ended_at": 1.0,
                      "date": _day(8), "side_id": "x", "players": ["Ann"], "complete": True}}
    cloud_history.load(cloud_history.path_for(path), "Ann", answer(
        [rounds[r1], other], [sh | {"round": 0} for sh in kept]
        + [{"round": 1, "uid": "o1", "ts": 1.0, "is_putt": False, "club": "DR", "lie_before": "tee",
            "total_yards": 999.0, "to_pin_yards": 400.0, "attributed": True}]))
    mull = int(gone["mulligan"])
    stats = s.season_stats("Ann", None)
    # r1's shots once (less the one the cloud left out), r2's and the stretch's from here, r0's from the cloud.
    assert stats["shots"] == local[("Ann", "r1")] - (1 - mull) + local[("Ann", "r2")] + 5 + 1
    assert stats["longest_drive"] == 999.0 and stats["nights"] == 3   # (r1's day once: its stretch and its round)
    assert s.season_rounds("Ann")["rounds"] == 3   # r0, r1 (the cloud's) and r2 (this PC's)
    assert [r["course"] for r in s.side_rounds("Ann")] == ["c1", "c1", "c9"]
    assert s.db.execute("SELECT COUNT(*) FROM temp.history_player_rounds WHERE player = 'Ann'").fetchone()[0] == 3


def test_tonights_earlier_rounds_are_today_and_the_next_answer_is_read_again_not_each_shot(tmp_path):
    from tests.test_round_record import _hit

    path = str(tmp_path / "season.db")
    s = paired_store(path)
    hist = cloud_history.path_for(path)
    cloud_history.load(hist, "Ann", answer(
        [{"id": "r1", "day": _day(1), "side": {"side": "Ann", "holes": 9, "gross": 40, "par": 36, "side_id": "a",
                                                "players": ["Ann"], "complete": True}}],
        [{"round": 0, "uid": "u1", "ts": 1.0, "is_putt": False}]))
    # A round earlier tonight, in this file: it's today's (not the season's), and it's in side_rounds.
    play(s.db, random.Random(1), TODAY.isoformat(), "tonight", "Ann", ["Ann"], session=s.session_id)
    s.db.commit()
    assert s.season_rounds("Ann")["rounds"] == 1 and len(s.side_rounds("Ann")) == 2
    just = s.db.execute("SELECT id FROM rounds WHERE round_uuid = 'tonight'").fetchone()[0]
    assert s.season_rounds("Ann", skip_rows=[])["rounds"] == 2 and s.season_rounds("Ann", skip_rows=[just])["rounds"] == 1

    past_reads = []
    s.db.set_trace_callback(lambda sql: past_reads.append(sql) if "FROM temp.history_shots" in sql else None)
    assert s.season_stats("Ann", None)["shots"] == 1
    _hit(s, "Ann", time.time())
    s.db.commit()
    profile = s.player_profile("Ann")["shots"]
    _hit(s, "Ann", time.time() + 1)
    s.db.commit()
    assert s.season_stats("Ann", None)["shots"] == 1 and s.player_profile("Ann")["shots"] == profile + 1
    assert len(past_reads) == 1   # (a shot tonight doesn't read the past again)
    cloud_history.load(hist, "Ann", answer(
        [{"id": "r1", "day": _day(1), "side": {"side": "Ann", "holes": 9, "side_id": "a", "players": ["Ann"]}}],
        [{"round": 0, "uid": u, "ts": 1.0, "is_putt": False} for u in ("u1", "u2")]))
    assert s.season_stats("Ann", None)["shots"] == 2 and len(past_reads) == 2


# ---- the calls ---------------------------------------------------------------------------------------

class Calls:
    """cloud_history.fetch, faked: each call's name, answered once `go` is set (or at once)."""

    def __init__(self, monkeypatch, answers=None, wait=False):
        self.asked, self.running, self.most = [], 0, 0
        self.go = threading.Event()
        if not wait:
            self.go.set()
        self.answers = answers or {}
        self.lock = threading.Lock()
        monkeypatch.setattr(cloud_history, "fetch", self)

    def __call__(self, cl, name, before):
        with self.lock:
            self.asked.append((name, before))
            self.running += 1
            self.most = max(self.most, self.running)
        try:
            self.go.wait(10)
            got = self.answers.get(name, answer())
            if isinstance(got, Exception):
                raise got
            return got
        finally:
            with self.lock:
                self.running -= 1


def _asks(tmp_path, indexes=None):
    s = paired_store(tmp_path / "season.db")
    seen = {} if indexes is None else indexes
    return s, cloud_history.Asks(cloud_history.path_for(str(tmp_path / "season.db")), lambda: None,
                                 lambda: TODAY.isoformat(), lambda n, i: seen.__setitem__(n, i))


def _one(name, day=1, index=None):
    return answer([{"id": f"r-{name}", "day": _day(day), "side": {"side": name, "holes": 9, "gross": 40, "par": 36,
                                                                   "side_id": name, "players": [name]}}], index=index)


async def test_the_setup_screens_names_are_asked_for_once_they_stand_and_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_history, "DEBOUNCE_S", 0.05)
    calls = Calls(monkeypatch, {"Ann": _one("Ann", index=12.5), "Bo": _one("Bo")})
    indexes = {}
    s, asks = _asks(tmp_path, indexes)
    asks.want(["Ann", "Bob"])      # (a misread on the way to Bo)
    asks.want(["Ann", "Bo"])
    asks.want(["Ann", "Bo"])       # (the same names on the next poll: nothing more)
    await asyncio.sleep(0.02)
    assert calls.asked == []
    await asks.ready(["Ann", "Bo"])
    assert sorted(calls.asked) == [("Ann", TODAY.isoformat()), ("Bo", TODAY.isoformat())]
    assert s.season_rounds("Ann")["rounds"] == s.season_rounds("Bo")["rounds"] == 1
    assert indexes == {"Ann": 12.5, "Bo": None}
    asks.want(["Bo", "Ann"], now=True)   # answered already today: not asked again
    await asks.ready(["Ann", "Bo"])
    assert len(calls.asked) == 2


async def test_at_most_two_calls_at_once_and_a_superseded_one_is_dropped(tmp_path, monkeypatch):
    calls = Calls(monkeypatch, {n: _one(n) for n in ("Ann", "Bo", "Cy", "Di")}, wait=True)
    s, asks = _asks(tmp_path)
    asks.want(["Ann", "Bo", "Cy"], now=True)
    await asyncio.sleep(0.1)
    assert calls.running == 2 and sorted(n for n, _ in calls.asked) == ["Ann", "Bo"]
    asks.want(["Di"], now=True)   # another roster: Cy's call (not started) is dropped, Ann's and Bo's not loaded
    calls.go.set()
    await asks.ready(["Di"])
    await asyncio.sleep(0.1)
    assert sorted(n for n, _ in calls.asked) == ["Ann", "Bo", "Di"] and calls.most == 2
    assert [r[0] for r in s.db.execute("SELECT DISTINCT player FROM hist.held")] == ["Di"]


async def test_the_opener_waits_for_its_players_calls_up_to_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_history, "BOUND_S", 0.3)
    calls = Calls(monkeypatch, {"Ann": _one("Ann")}, wait=True)
    s, asks = _asks(tmp_path)
    asks.want(["Ann"], now=True)
    t0 = time.monotonic()
    await asks.ready(["Ann"])   # the call never comes back in time: the opener goes on without it
    assert 0.25 <= time.monotonic() - t0 < 1.0 and s.season_rounds("Ann")["rounds"] == 0
    calls.go.set()
    await asks.ready(["Ann"])
    assert s.season_rounds("Ann")["rounds"] == 1


async def test_a_failed_call_keeps_the_last_answer_and_says_so_once(tmp_path, monkeypatch, caplog):
    calls = Calls(monkeypatch, {"Ann": _one("Ann")})
    s, asks = _asks(tmp_path)
    asks.want(["Ann"], now=True)
    await asks.ready(["Ann"])
    assert s.season_rounds("Ann")["rounds"] == 1
    asks.answered.clear()   # (another day)
    calls.answers["Ann"] = cloud.Unreachable("couldn't reach Shotcast Cloud: offline")
    with caplog.at_level(logging.WARNING, logger="commentator.cloud_history"):
        for _ in range(3):
            asks.want(["Ann"], now=True)
            await asks.ready(["Ann"])
    assert s.season_rounds("Ann")["rounds"] == 1 and len(calls.asked) == 4
    assert len([r for r in caplog.records if "offline" in r.getMessage()]) == 1


# ---- with the API (the fake) ---------------------------------------------------------------------------

def test_a_paired_booth_asks_the_api_and_a_429_holds_its_calls_quietly(tmp_path, monkeypatch, caplog):
    api = FakeAPI()
    mock_http(monkeypatch, api)
    cfg = paired_cfg(tmp_path, api)
    monkeypatch.setattr(cloud, "startup", cloud.locate())
    asked, indexes = [], {}

    def hook(req):
        if req.url.path != cloud_history.PATH:
            return None
        if req.headers.get("X-Shotcast-Device") not in api.access:
            return httpx.Response(401, json={"error": "access_token_invalid", "action": "retry", "message": "no"})
        asked.append(json.loads(req.content))
        if len(asked) > 1:
            return httpx.Response(429, headers={"Retry-After": "60"},
                                  json={"error": "rate_limited", "action": "retry", "message": "too many"})
        return httpx.Response(200, content=gzip.compress(json.dumps(_one("Ann", index=8.1)).encode()),
                              headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})

    api.hook = hook
    s = Store(cfg.db_path)
    asks = cloud_history.start(cfg, s, lambda n, i: indexes.__setitem__(n, i))
    assert [r[1] for r in s.db.execute("PRAGMA database_list")] == ["main", "temp", "hist"]

    async def go():
        asks.want(["Ann"], now=True)
        await asks.ready(["Ann"])
        with caplog.at_level(logging.WARNING, logger="commentator.cloud_history"):
            for name in ("Bo", "Cy", "Di"):
                asks.want([name], now=True)
                await asks.ready([name])

    asyncio.run(go())
    assert asked[0] == {"name": "Ann", "before": TODAY.isoformat()} and indexes == {"Ann": 8.1}
    assert s.season_rounds("Ann")["rounds"] == 1
    assert len(asked) == 2   # (Bo's 429 holds Cy's and Di's)
    assert len([r for r in caplog.records if "allowance" in r.getMessage()]) == 1


def test_the_answers_index_is_the_booths_own_index_for_that_player(tmp_path):
    from tests.test_round import _engine

    e = _engine(tmp_path)
    e._set_own_handicaps(lambda: {"Ann": 20.0, "Bo": 11.0})
    e._history_loaded("Ann", 12.5)
    assert e.league.own_indexes == {"Ann": 12.5, "Bo": 11.0}
    e._set_own_handicaps(lambda: {"Ann": 20.0, "Bo": 11.0})   # (worked out again after a round)
    assert e.league.own_indexes == {"Ann": 12.5, "Bo": 11.0}
    e._history_loaded("Ann", None)   # (no index now: the cloud's goes, ours stands at the next working out)
    assert e.league.own_indexes == {"Bo": 11.0}
    e._history_loaded("Bo", None)   # (none of the cloud's for Bo: ours is kept)
    assert e.league.own_indexes == {"Bo": 11.0}
    e._set_own_handicaps(lambda: {"Ann": 20.0})
    assert e.league.own_indexes == {"Ann": 20.0}


async def test_the_opener_off_the_setup_screen_talks_from_the_loaded_history(tmp_path, monkeypatch):
    from tests.test_round import _engine

    monkeypatch.setattr(cloud_history, "DEBOUNCE_S", 0.05)
    calls = Calls(monkeypatch, {"Walker": _one("Walker")}, wait=True)
    e = _engine(tmp_path)
    e.store = paired_store(tmp_path / "season.db")
    e._history_asks = cloud_history.Asks(cloud_history.path_for(str(tmp_path / "season.db")), lambda: None,
                                         lambda: TODAY.isoformat(), e._history_loaded)
    seen = []
    io = e._opener_io
    monkeypatch.setattr(e, "_opener_io", lambda r, **kw: seen.append(e.store.season_rounds("Walker")["rounds"]) or io(r, **kw))
    e._ask_history(["Walker"])
    threading.Timer(0.2, calls.go.set).start()
    await e._prepare_speculative_round("Pebble Beach", ["Walker"], air=False, gen=e._opener_gen)
    assert seen == [1] and calls.asked == [("Walker", TODAY.isoformat())]


# ---- self-hosted: nothing of it ------------------------------------------------------------------------

def test_self_hosted_nothing_is_attached_and_every_read_is_the_season_files(tmp_path):
    from tests.test_cloud import base_cfg

    cfg = base_cfg(tmp_path)
    s = Store(str(tmp_path / "season.db"))
    assert cloud_history.start(cfg, s, lambda n, i: None) is None   # (cloud.startup is None: tests.cloud_isolation)
    assert [r[1] for r in s.db.execute("PRAGMA database_list")] == ["main", "temp"]
    assert not cloud_history.path_for(cfg.db_path).exists() and not (tmp_path / "history.db").exists()
    sql = []
    s.db.set_trace_callback(sql.append)
    reads(s, "Ann", ["Ann"])
    assert sql and not [q for q in sql if "temp.history_" in q or "hist." in q]
    assert s.past_rounds == "rounds" and s.past_player_rounds == "player_rounds" and s.history.past_shots == "main.shots"
    assert s.history.attached is None


def test_a_replay_or_a_self_hosted_booth_starts_no_history(tmp_path, monkeypatch):
    from tests.test_round import _engine

    e = _engine(tmp_path)   # (self-hosted)
    e._start_cloud_history()
    assert e._history_asks is None and [r[1] for r in e.store.db.execute("PRAGMA database_list")] == ["main", "temp"]
    api = FakeAPI()
    mock_http(monkeypatch, api)
    cfg = paired_cfg(tmp_path / "p", api)
    monkeypatch.setattr(cloud, "startup", cloud.locate())
    cfg.source.type = "replay"
    s = Store(None)
    assert cloud_history.start(cfg, s, lambda n, i: None) is None
    assert [r[1] for r in s.db.execute("PRAGMA database_list")] == ["main", "temp"]


def test_a_history_file_made_in_an_older_shape_is_emptied_and_asked_for_again(tmp_path):
    path = tmp_path / "season.db"
    s = paired_store(path)
    hist = cloud_history.path_for(str(path))
    cloud_history.load(hist, "Ann", _one("Ann"))
    s.db.close()
    old = sqlite3.connect(hist)
    old.executescript("CREATE TABLE s2 AS SELECT * FROM shots; DROP TABLE shots;"
                      "CREATE TABLE shots AS SELECT id, session_id, ts, player FROM s2; DROP TABLE s2;")
    old.close()
    s = paired_store(path)
    assert s.season_rounds("Ann")["rounds"] == 0 and s.season_stats("Ann", None)["shots"] == 0
    cloud_history.load(hist, "Ann", _one("Ann"))
    assert s.season_rounds("Ann")["rounds"] == 1


# ---- review pass 147 -----------------------------------------------------------------------------------

def _solo(name, rid="r1", day=2):
    return {"id": rid, "day": _day(day), "player": {"course": "c1", "ended_at": 5.0},
            "side": {"side": name, "holes": 9, "gross": 40, "par": 36, "ended_at": 5.0, "date": _day(day),
                     "side_id": "s1", "players": [name], "complete": True}}


def test_1_a_solo_side_asked_again_under_another_spelling_is_tonights_name(tmp_path):
    s = paired_store(tmp_path / "season.db")
    path = cloud_history.path_for(str(tmp_path / "season.db"))
    cloud_history.load(path, "pat", answer([_solo("pat")]))
    cloud_history.load(path, "Pat", answer([_solo("Pat Smith")]))   # (the roster's full name, say)
    assert s.season_rounds("Pat")["rounds"] == 1 and len(s.side_rounds("Pat")) == 1
    assert [tuple(r) for r in s.db.execute("SELECT side, players FROM hist.rounds")] == [("Pat", '["Pat"]')]


async def test_2_a_call_dropped_before_it_started_is_asked_again(tmp_path, monkeypatch):
    calls = Calls(monkeypatch, {"Ann": _one("Ann"), "Bo": _one("Bo")})
    s, asks = _asks(tmp_path)
    asks.want(["Ann"], now=True)
    asks.want(["Bo"], now=True)    # (Ann's call cancelled before its first step)
    await asks.ready(["Bo"])
    await asyncio.sleep(0)
    asks.want(["Ann"], now=True)
    await asks.ready(["Ann"])
    assert ("Ann", TODAY.isoformat()) in calls.asked and s.season_rounds("Ann")["rounds"] == 1


def test_3_an_answer_landing_mid_round_is_read_by_the_rounds_recall(tmp_path):
    from tests.test_round import _engine

    e = _engine(tmp_path)
    e._recall = {"round": 7, "rows": {"Ann": []}, "holes": {}, "moment": {}, "moment_told": {"x"}}
    e._history_loaded("Ann", None)
    assert e._recall["rows"] == {} and e._recall["moment_told"] == {"x"}   # (what was told stays told)
    e._recall = {"round": None}
    e._history_loaded("Ann", None)   # (no round yet: nothing kept)


def test_4_a_round_held_without_a_card_keeps_this_pcs_cards(tmp_path):
    path = str(tmp_path / "season.db")
    season(path, random.Random(5), [(2, "r1", "Ann", ["Ann"])])
    s = paired_store(path)
    want = s.season_rounds("Ann"), s.player_broadcast_stats("Ann")
    cloud_history.load(cloud_history.path_for(path), "Ann", answer(
        [{"id": "r1", "day": _day(2), "side": {"side_id": "s1", "players": ["Ann"]}}]))   # (no cards, no shots)
    assert s.db.execute("SELECT COUNT(*) FROM hist.held").fetchone()[0] == 1
    assert (s.season_rounds("Ann"), s.player_broadcast_stats("Ann")) == want
    assert s.season_stats("Ann", None)["shots"] == 0   # (the round's shots are the cloud's: none)


def test_5_a_shot_moved_here_into_a_round_not_sent_counts_once(tmp_path):
    path = str(tmp_path / "season.db")
    season(path, random.Random(6), [(2, "r2", "Ann", ["Ann"])])   # (r2: not sent yet)
    s = paired_store(path)
    uid = s.db.execute("SELECT uid FROM shots WHERE record_uuid = 'r2' AND mulligan = 0 AND left_out = 0"
                       " LIMIT 1").fetchone()[0]
    before = s.season_stats("Ann", None)["shots"]
    # The cloud has it in the practice stretch it was hit in (sent as a round before it moved here).
    cloud_history.load(cloud_history.path_for(path), "Ann", answer(
        [_solo("Ann", rid="stretch", day=2)], [{"round": 0, "uid": uid, "ts": 1.0, "is_putt": False}]))
    assert s.season_stats("Ann", None)["shots"] == before


def test_6_a_name_not_asked_for_in_a_month_is_dropped(tmp_path):
    path = tmp_path / "season.db"
    s = paired_store(path)
    hist = cloud_history.path_for(str(path))
    cloud_history.load(hist, "Ann", _one("Ann"))
    cloud_history.load(hist, "Bo", _one("Bo"))
    s.db.close()
    old = sqlite3.connect(hist)
    old.execute("UPDATE asked SET at = ? WHERE player = 'Ann'", (time.time() - 31 * 86400,))
    old.commit()
    old.close()
    s = paired_store(path)
    assert s.season_rounds("Ann")["rounds"] == 0 and s.season_rounds("Bo")["rounds"] == 1
    assert {r[0] for r in s.db.execute("SELECT player FROM hist.held UNION SELECT player FROM hist.asked")} == {"Bo"}
    assert s.db.execute("SELECT COUNT(*) FROM hist.rounds").fetchone()[0] == 1


async def test_7_asking_never_breaks_a_round_start_and_self_hosted_never_loads_the_module(tmp_path, caplog):
    import sys

    from tests.test_round import _engine

    e = _engine(tmp_path)

    class Broken:
        def want(self, names, now=False):
            raise sqlite3.OperationalError("database is locked")

        async def ready(self, names):
            raise sqlite3.OperationalError("database is locked")

    e._history_asks = Broken()
    with caplog.at_level(logging.ERROR, logger="commentator.engine"):
        for _ in range(2):
            e._ask_history(["Ann"], now=True)
            await e._history_ready(["Ann"])
    assert len([r for r in caplog.records if "histories failed" in r.getMessage()]) == 1
    e2 = _engine(tmp_path / "b")
    sys.modules.pop("commentator.cloud_history", None)
    e2._start_cloud_history()   # (self-hosted)
    assert "commentator.cloud_history" not in sys.modules and e2._history_asks is None


async def test_8_a_slow_answer_still_lands_after_the_openers_wait_on_a_daemon_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_history, "BOUND_S", 0.2)
    calls = Calls(monkeypatch, {"Ann": _one("Ann")}, wait=True)
    s, asks = _asks(tmp_path)
    asks.want(["Ann"], now=True)
    await asks.ready(["Ann"])   # (the opener goes on)
    assert s.season_rounds("Ann")["rounds"] == 0
    assert all(t.daemon for t in threading.enumerate() if t.name == "cloud-history")
    assert any(t.name == "cloud-history" for t in threading.enumerate())
    threading.Timer(0.3, calls.go.set).start()
    await asyncio.wait(set(asks.calls.values()), timeout=5)
    assert s.season_rounds("Ann")["rounds"] == 1


async def test_8_a_call_with_no_answer_in_its_time_is_given_up(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cloud_history, "CALL_S", 0.2)
    calls = Calls(monkeypatch, {"Ann": _one("Ann")}, wait=True)
    s, asks = _asks(tmp_path)
    with caplog.at_level(logging.WARNING, logger="commentator.cloud_history"):
        asks.want(["Ann"], now=True)
        await asyncio.wait(set(asks.calls.values()), timeout=5)
    calls.go.set()
    assert not asks.calls and "Ann" not in asks.answered
    assert any("no answer in" in r.getMessage() for r in caplog.records)


# ---- step 3: the season file stops keeping what the cloud gives back -------------------------------------

def _paired(tmp_path, monkeypatch):
    """A paired booth's config, its profile (as Shotcast started in it) and the fake cloud."""
    api = FakeAPI()
    mock_http(monkeypatch, api)
    cfg = paired_cfg(tmp_path, api)
    prof = cloud.locate()
    monkeypatch.setattr(cloud, "startup", prof)
    return cfg, prof, api


def _session(db, day):
    return db.execute("INSERT INTO sessions (started_at, date) VALUES (?, ?)",
                      (time.mktime(date.fromisoformat(day).timetuple()), day)).lastrowid


def _record(db, uuid, kind, session, state="finished", revision=None, error=None, names=("Ann",)):
    db.execute("INSERT INTO round_records (uuid, kind, session_id, course, started_at, ended_at, state, assigned)"
               " VALUES (?, ?, ?, 'c1', 1.0, 2.0, ?, 'live')", (uuid, kind, session, state))
    for n in names:
        db.execute("INSERT INTO round_slots (slot_id, round_uuid, name, matched_by) VALUES (?, ?, ?, 'booth')",
                   (f"{uuid}-{n}", uuid, n))
    if revision is not None:   # (sent: its rows as they were sent)
        db.execute("INSERT INTO cloud_uploads (uuid, revision, error, sent_rows) VALUES (?, ?, ?, ?)",
                   (uuid, revision, error, cloud_rounds.rows_of(db, uuid) if revision else None))


def _round_on(db, rnd, ago, uuid, side="Ann", players=("Ann",), **record):
    """A round `ago` days ago (its own session), with its record, a line on its first shot, a shot track
    and a week's result."""
    day = _day(ago)
    session = _session(db, day)
    play(db, rnd, day, uuid, side, list(players), n_shots=4, session=session)
    _record(db, uuid, "round", session, names=players, **record)
    shot = db.execute("SELECT MIN(id) FROM shots WHERE record_uuid = ?", (uuid,)).fetchone()[0]
    db.execute("INSERT INTO lines (session_id, shot_id, ts, kind, speaker, text, record_uuid)"
               " VALUES (?, ?, 1.0, 'shot', 'pbp', ?, ?)", (session, shot, f"said of {uuid}", uuid))
    db.execute("INSERT INTO shot_tracks (shot_id, gspro_shot_id) VALUES (?, ?)", (shot, f"g-{uuid}"))
    side_row = db.execute("SELECT id FROM rounds WHERE round_uuid = ?", (uuid,)).fetchone()[0]
    db.execute("INSERT INTO week_results (ended_at, week, team, net, round_id) VALUES (1.0, 1, ?, 0, ?)",
               (side, side_row))
    db.execute("INSERT INTO round_folders VALUES (?, 'c1_gsp')", (uuid,))
    return session


def _practice_on(db, ago, uuid):
    session = _session(db, _day(ago))
    _record(db, uuid, "practice", session)
    for k in range(3):
        db.execute("INSERT INTO shots (session_id, ts, player, is_putt, club, record_uuid, uid, is_league)"
                   " VALUES (?, ?, 'Ann', 0, 'DR', ?, ?, 0)", (session, 10.0 + k, uuid, f"{uuid}-{k}"))
    db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text, record_uuid) VALUES (?, 1.0, 'segment', 'pbp',"
               " ?, ?)", (session, f"said in {uuid}", uuid))
    return session


def _memory(db, session):
    """The booth's memory and the course caches, in a session whose rounds go."""
    db.execute("INSERT INTO form_mentions (session_id, ts, player, topic) VALUES (?, 1.0, 'Ann', 'putting')", (session,))
    db.execute("INSERT INTO asides (session_id, ts, kind, topic, text) VALUES (?, 1.0, 'trivia', 'c1', 'x')", (session,))
    db.execute("INSERT INTO news_heard (session_id, ts, player, title) VALUES (?, 1.0, 'Ann', 'story')", (session,))
    db.execute("INSERT INTO predictions (session_id, ts, commentator, kind, predicted) VALUES (?, 1.0, 'a', 'putt', 'make')",
               (session,))
    db.execute("INSERT INTO hole_layouts (session_id, ts, course, hole, par) VALUES (?, 1.0, 'c1', 1, 4)", (session,))
    db.execute("INSERT INTO logged_pins (session_id, ts, x, y, z) VALUES (?, 1.0, 1, 2, 3)", (session,))
    db.execute("INSERT INTO hole_images (session_id, ts, course, hole, image, ball_x, pin_x) VALUES (?, 1.0, 'c1', 1,"
               " x'00', 0.5, 0.5)", (session,))
    db.execute("INSERT INTO course_tees (course, tee, rating, slope) VALUES ('c1', 'Blue', 71.2, 128)")
    db.execute("INSERT INTO tee_lengths (course, tee, yards, holes) VALUES ('c1', 'Blue', 6800, 18)")


_KEPT_TABLES = ("sessions", "lines", "round_records", "round_slots", "cloud_uploads", "form_mentions", "asides",
                "news_heard", "predictions", "hole_layouts", "logged_pins", "hole_images", "course_tees", "tee_lengths")


def _count(db, table, where="1"):
    return db.execute(f"SELECT COUNT(*) FROM main.{table} WHERE {where}").fetchone()[0]


def _has(db, uuid):
    return {t: _count(db, t, f"{c} = '{uuid}'") for t, c in (("shots", "record_uuid"), ("rounds", "round_uuid"),
                                                              ("player_rounds", "round_uuid"))}


def _leaving_out(a, ids):
    """An answer without these rounds (and their shots)."""
    rounds, shots = rows(a)
    keep = [i for i, r in enumerate(rounds) if r["id"] not in ids]
    at = {old: new for new, old in enumerate(keep)}
    return answer([rounds[i] for i in keep], [s | {"round": at[s["round"]]} for s in shots if s["round"] in at],
                  a["index"])


def _answered(cfg, answers):
    """The cloud's answers, as the booth's calls loaded them before this start: {name: answer, or the
    season file's rounds of that name less these round ids}."""
    s = Store(cfg.db_path, new_session=False)
    hist = cloud_history.path_for(cfg.db_path)
    s.attach_history(str(hist))
    for name, got in answers.items():
        cloud_history.load(hist, name, got if isinstance(got, dict) else _leaving_out(answer_from(s.db, name), got))
    s.close()


def _season_to_forget(cfg):
    """A paired season of every kind of record, before today; returns {name: session}."""
    rnd = random.Random(5)
    s = Store(cfg.db_path, new_session=False)
    db = s.db
    at = {"sent": _round_on(db, rnd, 3, "sent", revision=1)}
    _round_on(db, rnd, 4, "final", revision=1)                       # (409 round_final: revision 1, rows not known)
    db.execute("UPDATE cloud_uploads SET sent_rows = NULL WHERE uuid = 'final'")
    _round_on(db, rnd, 3, "unsent")                                  # never tried
    _round_on(db, rnd, 5, "refused", revision=0, error="HTTP 413")   # the API's no
    _round_on(db, rnd, 3, "unheld", revision=1)                      # the cloud's answer doesn't give it back
    _round_on(db, rnd, 3, "walkin", "Wally", ("Wally",), revision=1)   # a venue's walk-in: answered with nothing
    _round_on(db, rnd, 3, "team", "Ann & Bo", ("Ann", "Bo"), revision=1)   # Bo not asked yet
    _round_on(db, rnd, 3, "before", revision=1)                      # sent before its rows were noted
    db.execute("UPDATE cloud_uploads SET sent_rows = NULL WHERE uuid = 'before'")
    # Sent, then taken up again (GSPro's join after it was let go), last played an hour ago, yesterday
    # by the booth's date: in progress, it's a booth's to go on with.
    _round_on(db, rnd, 1, "open", state="in_progress", revision=1)
    db.execute("UPDATE shots SET ts = ? WHERE record_uuid = 'open'", (time.time() - 3600,))
    _round_on(db, rnd, 2, "played on", revision=1)                   # finished again since: a shot more
    db.execute("INSERT INTO shots (session_id, ts, player, is_putt, record_uuid, uid) SELECT session_id, ts + 1,"
               " player, 1, record_uuid, 'more' FROM shots WHERE record_uuid = 'played on' LIMIT 1")
    # Sent, its record yesterday's, with a row a booth that went on past midnight wrote today: a shot,
    # a line, a side row or a card.
    today = _session(db, TODAY.isoformat())
    for uuid, table, col in (("late shot", "shots", "record_uuid"), ("late line", "lines", "record_uuid"),
                             ("late side", "rounds", "round_uuid"), ("late card", "player_rounds", "round_uuid")):
        _round_on(db, rnd, 1, uuid, revision=1)
        db.execute(f"UPDATE {table} SET session_id = ? WHERE id = (SELECT MAX(id) FROM {table} WHERE {col} = ?)",
                   (today, uuid))
    _round_on(db, rnd, 0, "today", revision=1)                       # sent, played today
    at["young"] = _practice_on(db, cloud_history.PRACTICE_DAYS - 1, "young")
    _practice_on(db, cloud_history.PRACTICE_DAYS + 1, "old")
    _memory(db, at["sent"])
    db.commit()
    s.close()
    return at


KEPT = ("final", "unsent", "refused", "unheld", "walkin", "team", "before", "open", "played on", "late shot", "late line",
        "late side", "late card", "today")


def test_a_paired_booths_start_drops_the_rounds_the_history_file_holds_from_earlier_days(tmp_path, monkeypatch):
    cfg, prof, _ = _paired(tmp_path, monkeypatch)
    at = _season_to_forget(cfg)
    _answered(cfg, {"Ann": {"unheld"}, "Wally": answer()})
    s = Store(cfg.db_path)
    s.db.execute("INSERT INTO finish_recheck VALUES (?)", (at["young"],))   # (noted before: kept)
    s.db.commit()
    before = {t: _count(s.db, t) for t in _KEPT_TABLES}
    lines = sorted(r[0] for r in s.db.execute("SELECT text FROM lines"))
    assert cloud_history.start(cfg, s, lambda n, i: None) is not None
    for u in ("sent", "old"):
        assert set(_has(s.db, u).values()) == {0}, u
    for u in KEPT:
        assert all(_has(s.db, u).values()), u
    assert _has(s.db, "young")["shots"] == 3
    assert _count(s.db, "shot_tracks") == _count(s.db, "week_results") == len(KEPT)
    assert {r[0] for r in s.db.execute("SELECT record_uuid FROM round_folders")} == set(KEPT)
    # Kept: the lines (on no shot, where theirs went), every session and the memory in them, the course
    # caches, and the records with their slots and upload marks.
    assert {t: _count(s.db, t) for t in _KEPT_TABLES} == before
    assert sorted(r[0] for r in s.db.execute("SELECT text FROM lines")) == lines
    assert s.db.execute("SELECT shot_id FROM lines WHERE record_uuid = 'sent'").fetchone()[0] is None
    assert s.db.execute("SELECT shot_id FROM lines WHERE record_uuid = 'unsent'").fetchone()[0] is not None
    assert _count(s.db, "form_mentions", f"session_id = {at['sent']}") == 1
    assert s.recent_form_mentions("Ann") == ["putting"] and s.news_heard(["Ann"]) == {"story"}
    assert "said of sent" in s.spoken_lines()
    # (nothing noted for working out again but what was noted before)
    assert [r[0] for r in s.db.execute("SELECT session_id FROM finish_recheck")] == [at["young"]]
    assert _count(s.db, "round_stats_due") == 0
    # A second start drops nothing more.
    assert cloud_history.forget(s.db, prof, TODAY.isoformat()) == (0, 0)


def test_a_drop_that_fails_part_way_keeps_everything(tmp_path, monkeypatch, caplog):
    cfg, prof, _ = _paired(tmp_path, monkeypatch)
    _season_to_forget(cfg)
    _answered(cfg, {"Ann": set()})
    monkeypatch.setattr(cloud_history, "_DROP", cloud_history._DROP + ("DELETE FROM main.no_such_table WHERE ? = 1",))
    s = Store(cfg.db_path)
    every = ("shots", "rounds", "player_rounds", "shot_tracks", "week_results", "round_folders", "lines")
    before = {t: _count(s.db, t) for t in every}, s.db.execute("SELECT shot_id FROM lines ORDER BY id").fetchall()
    with caplog.at_level(logging.WARNING, logger="commentator.cloud_history"):
        assert cloud_history.start(cfg, s, lambda n, i: None) is not None
    assert ({t: _count(s.db, t) for t in every}, s.db.execute("SELECT shot_id FROM lines ORDER BY id").fetchall()) == before
    assert any("kept for now" in r.getMessage() for r in caplog.records) and not s.db.in_transaction


def _holding(db, player, uuids):
    """The cloud's answer holding these rounds of `player`'s (by their shots: the uploader's test rounds
    have no cards)."""
    rounds, shots = [], []
    for uuid in uuids:
        day = db.execute("SELECT s.date FROM round_records r JOIN sessions s ON s.id = r.session_id WHERE r.uuid = ?",
                         (uuid,)).fetchone()[0]
        for sh in db.execute("SELECT * FROM shots WHERE record_uuid = ? AND player = ?", (uuid, player)):
            shots.append({"round": len(rounds), **{k: (bool(sh[k]) if k in ("is_putt", "mulligan", "attributed")
                                                       else sh[k]) for k in SHOT_COLUMNS}})
        rounds.append({"id": uuid, "day": day, "side": {"side": player, "holes": 9, "side_id": player,
                                                        "players": [player]}})
    return answer(rounds, shots)


def test_after_the_drop_and_a_reopen_nothing_is_regrouped_repaired_or_sent_again(tmp_path, monkeypatch):
    from tests.test_cloud_rounds import _drain, _round
    from tests.test_round_record import _hit

    cfg, prof, api = _paired(tmp_path, monkeypatch)
    cl = cloud.Cloud(cloud.store(), clock=lambda: api.now, sleep=lambda s: None)
    s = Store(cfg.db_path)
    stored, final, failed, again = _round(s, 1), _round(s, 100), _round(s, 200), _round(s, 300)
    # `final`: the cloud stored it and its answer was lost (api.rounds), then it was taken up again and
    # played on; sent again, it's round_final, the cloud keeping the earlier build without that hole.
    api.rounds[final] = b'{"an": "earlier build"}'
    s.begin_round(final, course="Pebble", players={"Ann": None})
    _hit(s, "Ann", 150)
    s.end_round(final, complete=True)
    api.answers = [httpx.Response(201, json={"status": "stored"}),
                   httpx.Response(409, json={"error": "round_final", "action": "fix", "sha256": "0" * 64}),
                   httpx.Response(502)]
    assert _drain(cl, cfg).sent == [stored, final]
    assert _drain(cl, cfg).sent == [failed, again]
    # Taken up again (GSPro's join after it was let go) and played on: its later hole never went.
    s.begin_round(again, course="Pebble", players={"Ann": None})
    _hit(s, "Ann", 400)
    s.end_round(again, complete=True)
    s.db.execute("UPDATE sessions SET date = ?", (_day(1),))   # (all of it yesterday)
    s.db.commit()
    hold = _holding(s.db, "Ann", [stored, final, again])
    s.close()
    _answered(cfg, {"Ann": hold})
    s = Store(cfg.db_path)
    assert cloud_history.start(cfg, s, lambda n, i: None) is not None
    assert not any(_has(s.db, stored).values())
    assert _has(s.db, failed)["shots"] == 1 and _has(s.db, again)["shots"] == 2   # (not held; played on)
    assert _has(s.db, final)["shots"] == 2   # (round_final: what the cloud kept can't be told)
    records = s.db.execute("SELECT uuid, gen, state FROM round_records ORDER BY uuid").fetchall()
    s.close()
    s = Store(cfg.db_path)   # (migrate, reconcile, the repairs and the finishes at a start)
    assert s.db.execute("SELECT uuid, gen, state FROM round_records ORDER BY uuid").fetchall() == records
    assert not any(_has(s.db, stored).values()) and _count(s.db, "round_slots", f"round_uuid = '{stored}'") == 1
    puts = len(api.puts)
    assert _drain(cl, cfg).sent == [] and len(api.puts) == puts
    assert cloud_rounds.status(cfg.db_path)["sent"] == 4 and cloud_rounds.behind(cfg.db_path) == 0


def test_a_new_shot_after_a_drop_takes_a_used_id_and_no_old_line_and_the_course_is_still_played(tmp_path, monkeypatch):
    from tests.test_round_record import _hit

    cfg, prof, _ = _paired(tmp_path, monkeypatch)
    s = Store(cfg.db_path, new_session=False)
    _round_on(s.db, random.Random(2), 2, "sent", revision=1)
    ids = {r[0] for r in s.db.execute("SELECT id FROM shots")}
    s.db.commit()
    s.close()
    _answered(cfg, {"Ann": set()})
    s = Store(cfg.db_path)
    cloud_history.start(cfg, s, lambda n, i: None)
    assert _count(s.db, "shots") == 0
    new = _hit(s, "Ann", time.time())
    assert new in ids and _count(s.db, "lines", f"shot_id = {new}") == 0
    s.db.execute("DELETE FROM shots")
    assert "c1" in s.courses_played()   # (from its record: no shot is on it now)


def test_the_kept_answers_indexes_are_the_booths_from_its_start(tmp_path, monkeypatch):
    from tests.test_round import _engine

    e = _engine(tmp_path)
    hist = tmp_path / "history.db"
    e.store.attach_history(str(hist))
    cloud_history.load(hist, "Ann", _one("Ann", index=12.5))
    cloud_history.load(hist, "Bo", _one("Bo"))
    assert cloud_history.indexes(e.store.db) == {"Ann": 12.5}
    monkeypatch.setattr(cloud, "startup", object())
    monkeypatch.setattr(cloud_history, "start", lambda cfg, store, cb: store.attach_history(str(hist)))
    e2 = _engine(tmp_path / "b")
    e2._start_cloud_history()   # (the calls not answered yet: none tonight)
    assert e2.league.own_indexes.get("Ann") == 12.5
    e2._set_own_handicaps(lambda: {"Ann": 20.0, "Bo": 11.0})
    assert e2.league.own_indexes == {"Ann": 12.5, "Bo": 11.0}


def test_a_history_file_made_before_the_index_was_kept_gains_it(tmp_path):
    path = tmp_path / "season.db"
    s = paired_store(path)
    hist = cloud_history.path_for(str(path))
    cloud_history.load(hist, "Ann", _one("Ann", index=3.0))
    s.close()
    old = sqlite3.connect(hist)
    old.executescript("CREATE TABLE a2 AS SELECT player, at FROM asked; DROP TABLE asked; ALTER TABLE a2 RENAME TO asked;")
    old.close()
    s = paired_store(path)
    assert cloud_history.indexes(s.db) == {} and s.season_rounds("Ann")["rounds"] == 1
    cloud_history.load(hist, "Ann", _one("Ann", index=4.0))
    assert cloud_history.indexes(s.db) == {"Ann": 4.0}


def test_the_booths_history_reads_the_same_once_the_cloud_has_the_rounds_and_the_pc_doesnt(tmp_path, monkeypatch):
    cfg, prof, _ = _paired(tmp_path, monkeypatch)
    rnd = random.Random(11)
    s = Store(cfg.db_path, new_session=False)
    for ago, uuid, side, players in [(3, "r1", "Ann", ["Ann"]), (5, "r2", "Ann & Bo", ["Ann", "Bo"]),
                                     (9, "r3", "Ann", ["Ann"]), (20, "r4", "Ann & Bo", ["Ann", "Bo"])]:
        session = _session(s.db, _day(ago))
        play(s.db, rnd, _day(ago), uuid, side, players, session=session)
        _record(s.db, uuid, "round", session, revision=1, names=players)
    s.db.commit()
    hist = cloud_history.path_for(cfg.db_path)
    s.attach_history(str(hist))
    for name in ("Ann", "Bo"):
        cloud_history.load(hist, name, answer_from(s.db, name))
    want = reads(s, "Ann", ["Ann", "Ann & Bo"]), reads(s, "Bo", ["Ann & Bo"])
    s.close()
    s = Store(cfg.db_path)
    cloud_history.start(cfg, s, lambda n, i: None)
    assert _count(s.db, "shots") == _count(s.db, "rounds") == _count(s.db, "player_rounds") == 0
    assert (reads(s, "Ann", ["Ann", "Ann & Bo"]), reads(s, "Bo", ["Ann & Bo"])) == want
    assert want[0][("season_rounds", "Ann", None)]["rounds"] == 2 and want[0][("season_stats", None)]["nights"] == 4


def _untouched(cfg):
    s = Store(cfg.db_path, new_session=False)
    try:
        return {t: _count(s.db, t) for t in ("shots", "rounds", "player_rounds", "shot_tracks", "lines")}
    finally:
        s.close()


@pytest.mark.parametrize("how", ["self-hosted", "unpaired", "replay", "refused", "never answered",
                                 "answered long ago", "another file", "bound elsewhere", "no marks kept"])
def test_nothing_is_dropped_but_from_a_paired_profiles_own_bound_file_the_cloud_answers_for(tmp_path, monkeypatch, how):
    import dataclasses
    import shutil

    cfg, prof, _ = _paired(tmp_path, monkeypatch)
    _season_to_forget(cfg)
    if how != "never answered":
        _answered(cfg, {"Ann": set()})
    if how == "answered long ago":
        db = sqlite3.connect(cloud_history.path_for(cfg.db_path))
        db.execute("UPDATE asked SET at = at - ? * 86400 - 60", (cloud_history.AGE_DAYS,))
        db.commit()
        db.close()
    if how in ("self-hosted", "another file"):   # (a self-hosted booth's file, a copy of the profile's)
        (tmp_path / "own").mkdir()
        shutil.copy(cfg.db_path, tmp_path / "own" / "season.db")
        shutil.copy(cloud_history.path_for(cfg.db_path), tmp_path / "own" / "history.db")
        cfg.db_path = str(tmp_path / "own" / "season.db")
    if how in ("self-hosted", "unpaired"):
        monkeypatch.setattr(cloud, "startup", None)
    if how == "replay":
        cfg.source.type = "replay"
    if how == "refused":
        monkeypatch.setattr(cloud, "startup", dataclasses.replace(prof, problem=cloud.REPAIR))
    db = sqlite3.connect(cfg.db_path)
    if how == "bound elsewhere":
        db.execute("UPDATE cloud_pairing SET device_id = 'd2'")
    if how == "no marks kept":   # (an older Shotcast's queue)
        db.executescript("CREATE TABLE u2 AS SELECT uuid, revision, error, tries FROM cloud_uploads;"
                         " DROP TABLE cloud_uploads; ALTER TABLE u2 RENAME TO cloud_uploads;")
    db.commit()
    db.close()
    before = _untouched(cfg)
    s = Store(cfg.db_path)
    cloud_history.start(cfg, s, lambda n, i: None)
    s.close()
    assert _untouched(cfg) == before and before["shots"] > 0


def test_a_round_held_without_its_card_or_its_side_here_is_kept(tmp_path, monkeypatch):
    """The answer holds the round, but not this PC's card of the player (the slot had none in the cloud)
    or its side's row (one the history file can't keep): the booth still reads those here, so the round
    stays."""
    cfg, prof, _ = _paired(tmp_path, monkeypatch)
    s = Store(cfg.db_path, new_session=False)
    rnd = random.Random(4)
    for ago, uuid in ((2, "whole"), (3, "nocard"), (4, "noside")):
        _round_on(s.db, rnd, ago, uuid, revision=1)
    s.db.commit()
    rounds, shots = rows(answer_from(s.db, "Ann"))
    for r in rounds:
        if r["id"] == "nocard":
            r["player"] = dict.fromkeys(r["player"])
        if r["id"] == "noside":
            r["side"] |= {"holes": None, "holes_played": None}
    s.close()
    _answered(cfg, {"Ann": answer(rounds, shots)})
    s = Store(cfg.db_path)
    cloud_history.start(cfg, s, lambda n, i: None)
    assert not any(_has(s.db, "whole").values())
    assert all(_has(s.db, "nocard").values()) and all(_has(s.db, "noside").values())


def test_self_hosted_the_courses_played_are_the_shots_alone(tmp_path):
    from tests.test_round_record import _hit

    s = Store(str(tmp_path / "season.db"))
    _hit(s, "Ann", 1.0, course="Pebble")
    s.begin_round("quit", course="Riviera", players={"Ann": None})   # (quit before a stroke)
    s.end_round("quit", complete=False)
    assert s.courses_played() == ["Pebble"]
    s.attach_history(str(tmp_path / "history.db"))   # (paired: the records' too)
    assert s.courses_played() == ["Pebble", "Riviera"]
