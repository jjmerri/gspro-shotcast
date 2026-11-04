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


def test_the_week_is_told_as_one_tournament_hole_by_hole():
    facts = recap.week_facts(_board(nets={"Aces": -3, "Duffers": -2, "Hackers": 1, "Yips": 3}), 6, "2026-12-07")
    facts["holes"] = {"Aces": {1: 0, 2: 0, 3: -1, 4: -1, 5: -1}, "Duffers": {1: -1, 2: -1, 3: 0, 4: 0, 5: 0},
                      "Hackers": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
                      "Yips": {1: 1, 2: 1, 3: 1, 4: 0, 5: 0}}
    # A team's card that doesn't add up to its net (or is missing, or on other holes): no story, as
    # it might have led.
    assert recap.race(facts) is None
    facts["holes"]["Hackers"] = {1: 0, 2: 0, 3: 1, 4: 0, 5: 0}
    story = recap.race(facts)
    assert story["teams"] == ["Aces", "Duffers", "Hackers", "Yips"] and story["holes"] == [1, 2, 3, 4, 5]
    texts = [e["text"] for e in story["events"]]
    for want in ("Duffers lead after hole 1 at -1", "Aces were 2 back after hole 2 and came back to win",
                 "Tied for the lead after hole 4 at -2: Aces, Duffers",
                 "Aces pull clear alone after hole 5 at -3 (falling back: Duffers)", "Aces won it on the last hole"):
        assert want in texts
    assert story["key"]["Duffers"] >= {1, 4} and 5 in story["key"]["Aces"]
    assert recap.standing(story, "Duffers", 1) == "-2, leading by 2" and recap.standing(story, "Aces", 3) == "-2, tied for the lead"

    moments = {"Aces": [{"id": 1, "player": "Mike", "at": 0, "level": 4, "bad": False, "what": "a", "hole": 1},
                        {"id": 2, "player": "Mike", "at": 20, "level": 1, "bad": False, "what": "b", "hole": 5},
                        {"id": 3, "player": "Mike", "at": 40, "level": 4, "bad": False, "what": "c", "hole": 12}],
               "Duffers": [{"id": 1, "player": "Tony", "at": 0, "level": 4, "bad": False, "what": "d", "hole": 2},
                           {"id": 2, "player": "Tony", "at": 30, "level": 1, "bad": True, "what": "e", "hole": 4}],
               "Hackers": [{"id": 1, "player": "Lou", "at": 0, "level": 2, "bad": False, "what": "f", "hole": 3}]}
    found = {t: {"key": t, "moments": ms, "ats": [m["at"] for m in ms]} for t, ms in moments.items()}
    clips = recap.choose(facts, found)
    # The holes the story turned on first (Duffers' rough 4th, where the lead went), none off the
    # week's holes, and the whole field in hole order.
    assert [(c["team"], c["hole"]) for c in clips] == [("Aces", 1), ("Duffers", 2), ("Hackers", 3), ("Duffers", 4), ("Aces", 5)]
    # The winners' finish, cut on its own: last, its shot not shown again from their reel.
    found["Aces"]["finish"] = {"at": 2.0, "id": 2, "player": "Mike", "hole": 5, "what": "Mike holes a 6 ft putt on 5 to finish"}
    fin = recap.choose(facts, found)
    assert [(c["team"], c["hole"], bool(c.get("finish"))) for c in fin] == \
        [("Aces", 1, False), ("Duffers", 2, False), ("Hackers", 3, False), ("Duffers", 4, False), ("Aces", 5, True)]
    assert "THE WINNING FINISH" in recap.prompt(facts, fin, {"pbp": "Ann"})[1]
    user, facts_text = recap.prompt(facts, clips, {"pbp": "Ann"})
    assert "- after hole 4: Aces -2, Duffers -2, Hackers +1, Yips +3" in facts_text
    assert "Going to the hole: -2, leading by 1. How the hole ends (still to come as this shot's hit): even as a team, net, leaving them -2, tied for the lead" in facts_text
    assert "broadcast" in user
    # The net leaderboard going to a hole, the clip's team marked.
    assert recap.board(story, 3, "Yips", {r["team"]: r for r in facts["rows"]}) == {"thru": 4, "rows": [
        {"place": 1, "tied": True, "name": "Aces", "net": -2}, {"place": 1, "tied": True, "name": "Duffers", "net": -2},
        {"place": 3, "tied": False, "name": "Hackers", "net": 1}, {"place": 4, "tied": False, "name": "Yips", "net": 3, "on": True}]}


def test_a_finish_plays_from_its_own_playlist():
    s3 = FakeS3({ROOT + "reels/7/finish/finish.m3u8": b"#EXTM3U\n#EXTINF:4.000,\ns1/stream36.ts\n#EXT-X-ENDLIST\n"})
    assert recap.reel_playlist(s3, "b", "", "7", finish=True) == [[("finish/s1/stream36.ts", 4.0)]]


def test_a_reel_two_teams_share_gives_each_its_own_players_shots():
    reels = {"7": {"moments": _moments(("Mike", 0, 2, False), ("tony", 20, 2, False), ("Ghost", 40, 2, False))},
             "8": {"moments": _moments(("Lou B", 0, 2, False), ("Mike", 20, 2, False))}}
    found = recap.footage(reels, {"Aces": "7", "Duffers": "7", "Hackers": "8", "Yips": "9"},
                          League(FOUR).team_players)
    # Hackers' reel is theirs alone, but Mike's shot in it an Ace's (a round shared, the Aces since on another).
    assert {t: [m["player"] for m in f["moments"]] for t, f in found.items()} == \
        {"Aces": ["Mike"], "Duffers": ["tony"], "Hackers": ["Lou B"]}
    assert found["Aces"]["ats"] == [0, 20, 40]
    # The Duffers' finish in it, though none of its highlights are theirs, is still theirs to end on.
    reels["7"]["moments"] = reels["7"]["moments"][:1]
    reels["7"]["finish"] = {"at": 60.0, "id": 9, "player": "Tony", "hole": 9, "what": "Tony holes out to finish"}
    found = recap.footage(reels, {"Aces": "7", "Duffers": "7"}, League(FOUR).team_players)
    assert (found["Duffers"]["moments"], found["Duffers"]["finish"]["id"]) == ([], 9)
    assert "finish" not in found["Aces"]


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
