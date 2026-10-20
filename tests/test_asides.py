import json
import random
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from commentator.engine import PreparedNews
from commentator.models import Line
from commentator.scores import ScoreItem, parse_games, parse_tour
from commentator.trivia import _words, clean
from tests.test_round import _engine

NOW = datetime(2026, 10, 20, 18, 5, tzinfo=timezone(timedelta(hours=-4)))


def _player(name, score, order, holes, today):
    return {"order": order, "athlete": {"displayName": name}, "score": score,
            "linescores": [{"period": 1, "displayValue": "-2", "linescores": [{"value": 4}] * 18},
                           {"period": 2, "displayValue": today, "linescores": [{"value": 4}] * holes}]}


def test_a_live_tour_leaderboard_reads_positions_ties_and_thru():
    body = {"events": [{"id": "9", "name": "Zozo Championship", "status": {"type": {"state": "in"}},
                        "competitions": [{"status": {"period": 2, "type": {"state": "in",
                                                                           "detail": "Round 2 - In Progress"}},
                                          "competitors": [_player("Scottie Scheffler", "-12", 1, 14, "-5"),
                                                          _player("Rory McIlroy", "-10", 2, 18, "-4"),
                                                          _player("Xander Schauffele", "-10", 3, 16, "-3"),
                                                          _player("Tom Kim", "E", 4, 0, None)]}]}]}
    [item] = parse_tour(body, "pga", NOW, top=3)
    assert item.state == "in" and item.kind == "tour"
    assert item.fact == ("PGA Tour, live: the Zozo Championship, Round 2 - In Progress. Leaderboard as of "
                         "6:05 pm: 1. Scottie Scheffler -12 (thru 14, -5 today); T2. Rory McIlroy -10 "
                         "(-4 today); T2. Xander Schauffele -10 (thru 16, -3 today).")
    pre = {"events": [{"id": "1", "name": "Bermuda", "status": {"type": {"state": "pre"}}, "competitions": [{}]}]}
    assert parse_tour(pre, "pga", NOW) == []      # a tournament that hasn't started is no score


def _game(state, start, detail="", bos=0, other=0, recap=None):
    comp = {"competitors": [{"homeAway": "home", "score": str(other), "records": [{"summary": "4-3-1"}],
                             "team": {"abbreviation": "DAL", "displayName": "Dallas Stars", "shortDisplayName": "Stars"}},
                            {"homeAway": "away", "score": str(bos), "records": [{"summary": "7-2-0"}],
                             "team": {"abbreviation": "BOS", "displayName": "Boston Bruins",
                                      "shortDisplayName": "Bruins"}}]}
    if recap:
        comp["headlines"] = [{"shortLinkText": "Bruins win in Dallas", "description": recap}]
    return {"events": [{"id": "5", "date": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                        "status": {"type": {"state": state, "detail": detail}}, "competitions": [comp]}]}


def test_the_home_teams_games_live_finished_and_later_tonight():
    [live] = parse_games(_game("in", NOW - timedelta(hours=1), "8:14 - 2nd", 2, 1), "nhl", ["BOS"], NOW)
    assert live.fact == ("NHL, live as of 6:05 pm: Boston Bruins (7-2-0) at Dallas Stars (4-3-1). "
                         "Bruins 2, Stars 1, 8:14 - 2nd.")
    [later] = parse_games(_game("pre", NOW + timedelta(hours=2)), "nhl", ["BOS"], NOW)
    assert later.fact.endswith("starting at 8:05 pm.") and later.rank > live.rank
    assert parse_games(_game("pre", NOW + timedelta(hours=9)), "nhl", ["BOS"], NOW) == []      # not tonight
    assert parse_games(_game("in", NOW, "1st", 0, 0), "nhl", ["NYR"], NOW) == []               # not our team
    [final] = parse_games(_game("post", NOW - timedelta(hours=20), "Final", 3, 2,
                                recap="— Pastrnak scored twice. The Bruins held on late in the"),
                          "nhl", ["BOS"], NOW)
    assert final.fact.endswith("Bruins 3, Stars 2. Recap: Bruins win in Dallas. Pastrnak scored twice.")
    assert parse_games(_game("post", NOW - timedelta(days=2), "Final"), "nhl", ["BOS"], NOW) == []


def test_trivia_keeps_the_prose_and_matches_on_a_distinctive_word():
    text = "Bethpage Black is a golf course.\n\n\n== History ==\nOpened in 1936.\n\n== References ==\n1. A book"
    assert clean(text) == "Bethpage Black is a golf course.\n\n== History ==\nOpened in 1936."
    assert _words("Riviera") & _words("Riviera Country Club")
    assert not _words("The Ancient Course") & _words("Golf course")


def _bit(kind, hole=None, expires=None, trigger=None):
    return PreparedNews(kind, trigger, [Line("pbp", "a"), Line("color", "b")], [None, None], kind=kind,
                        hole=hole, expires=expires, topic=kind)


def test_news_trivia_and_scores_are_one_bucket_picked_at_random(tmp_path):
    e = _engine(tmp_path)
    e.rng = random.Random(0)
    e.cfg.news.banter_rate = 1.0
    e.round = SimpleNamespace(holes=9, seq=3, ended=False, is_league=False)
    routine = {"tags": [], "routine": True}
    news, trivia, score = _bit("news"), _bit("trivia"), _bit("score", expires=time.monotonic() + 60)
    e._news_bits = [news, trivia, score]
    picks = {e._news_pick(routine)[0].kind for _ in range(60)}
    assert picks == {"news", "trivia", "score"}
    assert e._news_pick({"tags": [], "routine": False}) is None    # never over a notable shot
    e.cfg.news.banter_rate = 0.0
    assert e._news_pick(routine) is None                           # the one dice roll covers them all
    e.cfg.news.banter_rate = 1.0
    e._news_bits = [_bit("score", expires=time.monotonic() - 1)]
    assert e._news_pick(routine) is None and e._news_bits == []    # gone stale: dropped


async def test_a_score_check_in_moves_past_a_recent_aside(tmp_path):
    e = _engine(tmp_path)
    e.scores = _Scores([])
    r = SimpleNamespace(holes=9, seq=4, ended=False, is_league=False)
    e.round, e._score_holes, e._news_aired = r, [4], [3]
    e._score_hole_started(r)
    assert e._score_holes == [6] and not e._aside_tasks           # written when it can actually play
    r.seq = 6
    e._score_hole_started(r)
    assert e._score_holes == [] and e._aside_tasks
    for t in list(e._aside_tasks):
        await t


def test_a_story_about_one_hole_plays_on_that_hole_even_late(tmp_path):
    e = _engine(tmp_path)
    e.cfg.news.banter_rate = 1.0
    e.round = SimpleNamespace(holes=18, seq=17, ended=False, is_league=False)
    seventeen = _bit("trivia", hole=17)
    e._news_bits = [seventeen]
    e.hole_number = 16
    routine = {"tags": [], "routine": True}
    assert e._news_pick(routine) is None
    e.hole_number = 17
    assert e._news_pick(routine) == (seventeen, False)             # one of the last two, but it's this hole
    e._news_aired = [16]
    assert e._news_pick(routine) is None                           # still spaced from the last aside


class _Scores:
    def __init__(self, items):
        self.items, self.calls = items, 0

    async def fetch(self):
        self.calls += 1
        return list(self.items)


class _AsideLLM:
    provider, model = "fake", "fake"

    def __init__(self, hole=0):
        self.prompts, self.hole = [], hole

    async def complete_json(self, system, user, schema, images=None):
        from commentator.llm.base import LLMResult
        self.prompts.append(user)
        lines = [{"speaker": "pbp", "text": "Had a look at my phone and the Bruins lead it two to one."},
                 {"speaker": "color", "text": "They always find a way in that building, so I'd take that."}]
        return LLMResult(text=json.dumps({"lines": lines, "hole": self.hole}), input_tokens=10, output_tokens=10)


async def test_scores_are_planned_at_round_start_but_fetched_and_written_as_their_hole_starts(tmp_path):
    e = _engine(tmp_path)
    e.closer_llm = llm = _AsideLLM()
    item = ScoreItem("nhl:5", "team", "in", "NHL, live as of 6:05 pm: Bruins 2, Stars 1, 8:14 - 2nd.", "2-1")
    e.scores = _Scores([item])
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = r
    await e._plan_scores(r)
    [hole] = e._score_holes
    assert 2 <= hole <= 7 and e.scores.calls == 1 and not llm.prompts     # planned, nothing written yet
    r.seq = hole
    e._score_hole_started(r)
    assert e._score_holes == []
    for t in list(e._aside_tasks):
        await t
    assert e.scores.calls == 2 and "Bruins 2, Stars 1" in llm.prompts[0]
    [bit] = e._news_bits
    assert bit.kind == "score" and bit.expires > time.monotonic()
    await e._prepare_score(r)                       # the same score again: nothing new to say
    assert len(e._news_bits) == 1 and len(llm.prompts) == 1


async def test_nothing_on_tonight_means_no_score_holes(tmp_path):
    e = _engine(tmp_path)
    e.scores = _Scores([])
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = r
    await e._plan_scores(r)
    assert e._score_holes == []


async def test_course_trivia_is_written_from_the_article_and_remembers_what_aired(tmp_path):
    e = _engine(tmp_path)
    e.closer_llm = llm = _AsideLLM(hole=17)
    e.cfg.news.banter_rate = 1.0

    class _Trivia:
        async def article(self, course):
            return "Old Course at St Andrews", "The Old Course is a golf course. The Bruins lead two to one."

    e.trivia = _Trivia()
    e.cfg.trivia.per_round = 1
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False, course="The Ancient Course")
    e.round = r
    await e._prepare_trivia(r)
    [bit] = e._news_bits
    assert bit.kind == "trivia" and bit.hole == 17 and "GSPro's version of the real Old Course" in llm.prompts[0]
    e.round.seq, e.hole_number = 5, 17
    e.speech.is_idle = lambda: True
    e._shot_seq = 1
    assert await e._news_after_shot({"seq": 1, "tags": [], "routine": True})
    assert e.store.recent_asides("trivia", "Old Course at St Andrews")[0].startswith("Had a look")


def test_teams_can_be_named_and_listed_without_their_leagues():
    from commentator.scores import LiveScores

    game = _game("in", NOW, "1st", 1, 0)
    assert parse_games(game, "nhl", ["Bruins"], NOW) and parse_games(game, "nhl", ["boston bruins"], NOW)
    assert LiveScores([], ["Bruins", "Patriots"]).teams == {lg: ["Bruins", "Patriots"]
                                                           for lg in ("nhl", "nba", "mlb", "nfl")}
    assert LiveScores([], {"mls": ["Revolution"], "xfl": ["x"]}).teams == {"mls": ["Revolution"]}


def test_a_playoff_names_its_winner_and_keeps_the_final_rounds_score():
    def finisher(name, order, final, playoff):
        return {"order": order, "athlete": {"displayName": name}, "score": "-11",
                "linescores": [{"period": 4, "value": 73 if final == "+1" else 66, "displayValue": final},
                               {"period": 5, "value": playoff, "displayValue": "E"}]}
    body = {"events": [{"id": "1", "name": "Masters Tournament", "endDate": "2026-10-20T04:00Z",
                        "competitions": [{"status": {"period": 5, "type": {"state": "post", "detail": "Final"}},
                                          "competitors": [finisher("Justin Rose", 2, "-6", 5),
                                                          finisher("Rory McIlroy", 1, "+1", 4)]}]}]}
    [item] = parse_tour(body, "pga", NOW)
    assert ("1. Rory McIlroy -11 (won the playoff, +1 in the final round); "
            "2. Justin Rose -11 (lost in the playoff, -6 in the final round)") in item.fact


async def test_an_expired_check_in_doesnt_block_an_update(tmp_path):
    e = _engine(tmp_path)
    e.closer_llm = _AsideLLM()
    e.scores = _Scores([ScoreItem("nhl:5", "team", "in", "NHL: Bruins 2, Stars 1.", "2-1")])
    r = SimpleNamespace(holes=9, seq=4, ended=False, is_league=False)
    e.round = r
    e._news_bits = [_bit("score", expires=time.monotonic() - 1)]
    e._news_bits[0].topic = "nhl:5"
    await e._prepare_score(r)
    assert any(b.kind == "score" and b.expires > time.monotonic() for b in e._news_bits)


async def test_an_aside_picked_for_a_hole_doesnt_air_once_the_hole_has_moved_on(tmp_path):
    import asyncio

    e = _engine(tmp_path)
    e.cfg.news.banter_rate = 1.0
    e.round = SimpleNamespace(holes=18, seq=17, ended=False, is_league=False)
    e.hole_number, e._shot_seq = 17, 1
    e._news_bits = [_bit("trivia", hole=17)]
    e.speech.is_idle = lambda: True
    task = asyncio.create_task(e._news_after_shot({"seq": 1, "tags": [], "routine": True}))
    await asyncio.sleep(0.3)
    e.round.seq, e.hole_number = 18, 18
    assert await task is False and e._news_bits


def test_a_tournament_going_final_is_news_even_with_the_same_leaders():
    live = {"events": [{"id": "9", "name": "Zozo", "endDate": "2026-10-20T04:00Z",
                        "competitions": [{"status": {"period": 4, "type": {"state": "in", "detail": "Round 4"}},
                                          "competitors": [_player("A B", "-12", 1, 18, "-5")]}]}]}
    final = json.loads(json.dumps(live))
    final["events"][0]["competitions"][0]["status"]["type"] = {"state": "post", "detail": "Final"}
    assert parse_tour(live, "pga", NOW)[0].version != parse_tour(final, "pga", NOW)[0].version


async def test_last_nights_results_come_from_yesterdays_board(monkeypatch):
    import httpx

    from commentator.scores import LiveScores

    asked = []

    async def get(self, url, params=None):
        asked.append(params)
        body = _game("post", NOW - timedelta(hours=10), "Final", 3, 2) if params else {"events": []}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    items = await LiveScores([], {"nhl": ["BOS"]}).fetch(now=NOW)
    assert {"dates": "20261019"} in asked and [i.key for i in items] == ["nhl:5"]


def test_a_withdrawn_leader_isnt_the_winner():
    body = {"events": [{"id": "1", "name": "The Memorial Tournament", "endDate": "2026-10-20T04:00Z",
                        "competitions": [{"status": {"period": 4, "type": {"state": "post", "detail": "Final"}},
                                          "competitors": [_player("Jon Rahm", "-18", 116, 18, "-8"),
                                                          _player("Patrick Cantlay", "-13", 1, 18, "-1"),
                                                          _player("Scottie Scheffler", "-11", 2, 18, "-2")]}]}]}
    for c in body["events"][0]["competitions"][0]["competitors"]:
        for ls in c["linescores"]:
            ls["value"] = 70
    [item] = parse_tour(body, "pga", NOW)
    assert "the Memorial Tournament finished" in item.fact and "Rahm" not in item.fact
    assert "1. Patrick Cantlay -13" in item.fact and "2. Scottie Scheffler -11" in item.fact
