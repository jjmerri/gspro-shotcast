import asyncio
import json

import numpy as np

from commentator import recap
from commentator.board import Board, Entry
from commentator.league import League
from commentator.tts.base import Audio
from tests.test_board import LEAGUE_DATA, POINTS, _engine
from tests.test_record_clips import FakeS3

ROOT = "live/"
FOUR = {**LEAGUE_DATA, "teams": LEAGUE_DATA["teams"] + [
    {"name": "Yips", "players": [{"name": "Gus", "squabbit_id": "g"}, {"name": "Bea", "squabbit_id": "h"}]}]}


def _board(nets=None, before=None):
    league = League(FOUR)
    nets = nets or {"Aces": -5, "Duffers": -2, "Hackers": 1, "Yips": 3}
    weeks = {6: [Entry(t, n) for t, n in nets.items()]}
    if before:
        weeks[5] = [Entry(t, n) for t, n in before.items()]
    return Board(weeks, league, POINTS + [40.0])


def _moments(*specs):
    """(player, at, level, bad) each."""
    return [{"id": i + 1, "player": p, "at": at, "level": lv, "bad": bad, "what": f"shot {i + 1}", "hole": i + 1}
            for i, (p, at, lv, bad) in enumerate(specs)]


def test_every_team_with_footage_gets_a_moment_and_the_leaders_more_within_the_budget():
    facts = recap.week_facts(_board(before={"Aces": 4, "Duffers": -3, "Hackers": 0, "Yips": 2}), 6, "2026-12-07")
    assert [(r["team"], r["place"], r["points"]) for r in facts["rows"]] == \
        [("Aces", 1, 70.0), ("Duffers", 2, 60.0), ("Hackers", 3, 50.0), ("Yips", 4, 40.0)]
    # Aces were last after week 5: a big mover.
    assert {s["team"]: s["was"] for s in facts["standings"]}["Aces"] == 4
    found = {
        "Aces": {"key": "1", "moments": _moments(("Mike", 0, 2, False), ("Sarah", 30, 4, False), ("Mike", 60, 3, False),
                                                 ("Sarah", 90, 2, False)), "ats": [0, 30, 60, 90]},
        "Duffers": {"key": "2", "moments": _moments(("Tony", 0, 2, False), ("Priya", 30, 3, False), ("Tony", 60, 4, False)),
                    "ats": [0, 30, 60]},
        "Hackers": {"key": "3", "moments": _moments(("Lou", 0, 3, True), ("Dee", 30, 2, False), ("Dee", 60, 2, False)),
                    "ats": [0, 30, 60]},
        "Yips": {"key": "4", "moments": _moments(("Gus", 0, 3, True)), "ats": [0]},
    }
    clips = recap.choose(facts, found)
    by = {t: [c["at"] for c in clips if c["team"] == t] for t in found}
    # Last place to the winner, each team's in the order hit; the best first in, a bad one only
    # for a team with nothing else; three for the leaders, two for the rest.
    assert [c["team"] for c in clips] == ["Yips"] + ["Hackers"] * 2 + ["Duffers"] * 3 + ["Aces"] * 3
    assert by == {"Aces": [0, 30, 60], "Duffers": [0, 30, 60], "Hackers": [30, 60], "Yips": [0]}
    assert 9 * recap.CLIP_SECONDS <= recap.RECAP_SECONDS - recap.INTRO_SECONDS - recap.OUTRO_SECONDS
    # A shorter budget: one each first, then the leaders.
    small = recap.choose(facts, found, budget=recap.INTRO_SECONDS + recap.OUTRO_SECONDS + 5 * recap.CLIP_SECONDS)
    assert sorted(c["team"] for c in small) == ["Aces", "Aces", "Duffers", "Hackers", "Yips"]


def test_a_reel_two_teams_share_gives_each_its_own_players_shots():
    reels = {"7": {"moments": _moments(("Mike", 0, 2, False), ("tony", 20, 2, False), ("Ghost", 40, 2, False))},
             "8": {"moments": _moments(("Lou B", 0, 2, False), ("Mike", 20, 2, False))}}
    found = recap.footage(reels, {"Aces": "7", "Duffers": "7", "Hackers": "8", "Yips": "9"},
                          League(FOUR).team_players)
    # Hackers' reel is theirs alone, but Mike's shot in it an Ace's (a round shared, the Aces since on another).
    assert {t: [m["player"] for m in f["moments"]] for t, f in found.items()} == \
        {"Aces": ["Mike"], "Duffers": ["tony"], "Hackers": ["Lou B"]}
    assert found["Aces"]["ats"] == [0, 20, 40]


def test_a_moment_moved_or_rescored_in_its_reel_rebuilds_the_recap():
    facts = recap.week_facts(_board(), 6, "2026-12-07")
    reels = {"7": {"cuts": [[1, 12.0, None, None]], "moments": _moments(("Mike", 0, 2, False))}}
    one = recap.digest(facts, recap.footage(reels, {"Aces": "7"}, {}))
    reels["7"]["moments"][0]["at"] = 3.0
    two = recap.digest(facts, recap.footage(reels, {"Aces": "7"}, {}))
    reels["7"]["moments"][0]["hole"] = 9
    assert len({one, two, recap.digest(facts, recap.footage(reels, {"Aces": "7"}, {}))}) == 3


def test_a_clip_runs_for_its_lines_but_not_into_the_next_moment_or_past_its_part():
    reel = [[(f"s1/a{i}.ts", 4.0) for i in range(10)], [(f"s2/b{i}.ts", 4.0) for i in range(10)]]
    # 1 s in the first part: from 0 (a second before, clamped to the part) for its 6 s of lines.
    segs, skew = recap.cut({"at": 1.0}, reel, [1.0, 50.0], 6.0)
    assert [u for u, _ in segs] == ["s1/a0.ts", "s1/a1.ts", "s1/a2.ts"] and skew == 0.0
    # Long lines: stopped short of the next moment a few seconds on.
    segs, skew = recap.cut({"at": 9.0}, reel, [9.0, 21.0], 30.0)
    assert [u for u, _ in segs] == ["s1/a2.ts", "s1/a3.ts", "s1/a4.ts"] and skew == 0.0
    # The end of its part.
    segs, _ = recap.cut({"at": 33.0}, reel, [33.0], 30.0)
    assert segs[-1][0] == "s1/a9.ts"
    assert recap.cut({"at": 500.0}, reel, [500.0], 5.0) is None


def test_the_script_says_what_is_missing():
    clips = [{"team": "Aces"}, {"team": "Yips"}]
    raw = json.dumps({"intro": [{"speaker": "pbp", "text": "Week six is in."}],
                      "clips": [{"clip": 0, "lines": [{"speaker": "color", "text": "Mike stuck it."}]},
                                {"clip": 5, "lines": [{"speaker": "pbp", "text": "No such clip."}]}],
                      "outro": []})
    lines, problems = recap.script(raw, clips, ["pbp", "color"])
    assert lines["intro"] == [("pbp", "Week six is in.")] and lines["clips"] == {0: [("color", "Mike stuck it.")]}
    assert problems == ["write the outro", "write lines for clip 1"]


def _reel_objects(key, session, n):
    names = [f"seg{i}.ts" for i in range(n)]
    pl = "\n".join(["#EXTM3U"] + [f"#EXTINF:4.000,\n{session}/{x}" for x in names] + ["#EXT-X-ENDLIST"])
    objs = {f"{ROOT}reels/{key}/{session}/{x}": x.encode() for x in names}
    objs[f"{ROOT}reels/{key}/reel.m3u8"] = pl.encode()
    return objs


def test_a_final_week_gets_its_recap_built_once(tmp_path):
    league = League(FOUR)
    e = _engine(tmp_path, league)
    p = e.cfg.publish
    p.enabled, p.bucket, p.season_start = True, "b", "10-01"
    e._league_page_on = True

    def row(players):
        return e.store.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, is_league, players)"
                                  " VALUES (?, ?, '2026-12-01', 'x', 9, 1, ?)",
                                  (e.store.session_id, 1000 + 3600 * row.n, json.dumps(players))).lastrowid
    row.n = 0
    aces = row(["Mike", "Sarah"])
    row.n = 1
    yips = row(["Gus", "Bea"])
    e.store.record_week_result(6, "2026-12-06", "Aces", -5, aces)
    e.store.record_week_result(6, "2026-12-06", "Yips", 3, yips)
    e.board = _board()
    old = {f"{ROOT}recaps/2025-11-01-w2/recap.json": b"{}"}
    s3 = FakeS3({**_reel_objects(str(aces), "s1", 20), **_reel_objects(str(yips), "s2", 10), **old,
                 f"{ROOT}recaps.json": json.dumps({"recaps": {"2025-11-01-w2": {"week": 2, "end_date": "2025-11-01"}}}).encode(),
                 f"{ROOT}reels.json": json.dumps({"reels": {
                     str(aces): {"moments": _moments(("Mike", 2.0, 3, False), ("Sarah", 40.0, 2, False))},
                     str(yips): {"moments": _moments(("Gus", 6.0, 2, True))}}}).encode()})
    e._s3 = lambda: s3
    calls = []

    async def complete(kind, llm, user, images, schema=None, system=None):
        calls.append(user)
        return json.dumps({"intro": [{"speaker": "pbp", "text": "The Aces won week six."},
                                     {"speaker": "color", "text": "And the Yips brought up the rear."}],
                           "clips": [{"clip": i, "lines": [{"speaker": "pbp", "text": "Here it comes."}]} for i in range(3)],
                           "outro": [{"speaker": "color", "text": "The Aces lead the standings."}]})
    e._complete = complete

    async def synth(line, **_):
        return True, Audio(np.zeros(24000 * 2, dtype="float32"), 24000)
    e.speech._synthesize = synth
    asyncio.run(e._build_recap())
    index = json.loads(s3.objects[ROOT + "recaps.json"])["recaps"]
    assert list(index) == ["2026-12-06-w6"]                       # last season's gone
    assert not any(k.startswith(f"{ROOT}recaps/2025-11-01-w2/") for k in s3.objects)
    assert index["2026-12-06-w6"]["winner"] == "Aces" and index["2026-12-06-w6"]["clips"] == 3
    built = index["2026-12-06-w6"]["built"]
    body = json.loads(s3.objects[f"{ROOT}recaps/2026-12-06-w6/recap-{built}.json"])
    assert [c["team"] for c in body["clips"]] == ["Yips", "Aces", "Aces"]
    # Each clip's line 1.5 s after a second before its moment: Gus's at 6 s (its segment from 4 s),
    # Mike's at 2 s (from 0), Sarah's at 40 s (from 36 s); each clip three segments, the 10 s it runs at least.
    assert [(c["at"], c["lines"][0]["at"]) for c in body["clips"]] == [(0.0, 2.5), (12.0, 14.5), (24.0, 28.5)]
    assert all(s3.objects[f"{ROOT}recaps/2026-12-06-w6/{l['audio']}"][:4] == b"RIFF"
               for part in [body["intro"], body["outro"]] + [c["lines"] for c in body["clips"]] for l in part)
    pl = s3.objects[f"{ROOT}recaps/2026-12-06-w6/recap-{built}.m3u8"].decode()
    assert pl.count("#EXT-X-DISCONTINUITY") == 2 and "s2/seg1.ts" in pl and f"{ROOT}recaps/2026-12-06-w6/s2/seg1.ts" in s3.objects
    # Nothing new: not built again; a changed net: built again.
    asyncio.run(e._build_recap())
    assert len(calls) == 1
    e.board = _board(nets={"Aces": -6, "Duffers": -2, "Hackers": 1, "Yips": 3})
    asyncio.run(e._build_recap())
    assert len(calls) == 2 and "-6" in calls[-1]


def test_a_rebuilds_leftovers_stay_an_hour_for_a_page_still_playing_the_last():
    s3 = FakeS3({})
    body = {"week": 6, "duration": 30.0, "clips": [], "rows": []}
    base = f"{ROOT}recaps/w6/a/"

    def build(name, at):
        recap.publish(s3, "b", "", "w6", body, [], [], [(f"a/{name}.wav", b"RIFF")], name, clock=lambda: at)
    build("one", 1000)
    build("two", 1060)                      # a minute on: "one" stays for a page still on it
    assert {base + "one.wav", base + "two.wav"} <= set(s3.objects)
    build("three", 1060 + recap.RETIRE_SECONDS)
    assert base + "one.wav" not in s3.objects and {base + "two.wav", base + "three.wav"} <= set(s3.objects)
    # Another week's build later on clears this one's too.
    recap.publish(s3, "b", "", "w7", {**body, "week": 7}, [], [], [], "w7", clock=lambda: 1060 + 3 * recap.RETIRE_SECONDS)
    assert base + "two.wav" not in s3.objects and base + "three.wav" in s3.objects
    assert "retired" not in json.loads(s3.objects[f"{ROOT}recaps.json"])["recaps"]["w6"]
