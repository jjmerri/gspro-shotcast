import json
import random
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from commentator.engine import PreparedNews
from commentator.models import Line
from commentator.scores import ScoreItem, parse_games, parse_tour
import asyncio

from commentator.signature_holes import signature_hole
from commentator.trivia import CourseTrivia, _words, clean, course_key, section_of
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
                         "Bruins 2, Stars 1, 8:14 left in the 2nd.")
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


class _Page:
    def __init__(self, title, text):
        self.title, self.text = title, text

    def raise_for_status(self):
        pass

    def json(self):
        return {"query": {"pages": [{"title": self.title, "extract": self.text}]}}


def test_trivia_goes_by_gspros_course_id_and_never_by_screen_text(monkeypatch):
    t, asked = CourseTrivia({"The Ancient Course": "Old Course at St Andrews"}), []

    async def _extract(client, title, known=False):
        asked.append((title, known))
        return title, "text"

    async def _search(client, course):
        asked.append(("search", course))
        return []

    monkeypatch.setattr(t, "_extract", _extract)
    monkeypatch.setattr(t, "_search", _search)
    assert course_key("dpc_sodgrass_gsp") == course_key("DPC Sodgrass") == "dpc sodgrass"
    assert asyncio.run(t.article("Sodgrass Links", "SODGRASS LINKS", "dpc_sodgrass_gsp"))[0] == "TPC at Sawgrass"
    assert asyncio.run(t.article("GAME MENU")) is None                 # OCR'd a menu: no lookup at all
    assert asyncio.run(t.article("Cabot Cliffs", None, "cabotcliffs_gsp")) is None   # known: no article
    assert asyncio.run(t.article("the_ancient_course"))[0] == "Old Course at St Andrews"
    assert asyncio.run(t.article("Georgia Golf Club"))[0] == "Augusta National Golf Club"  # renamed replica
    assert asked == [("TPC at Sawgrass", True), ("Old Course at St Andrews", True),
                     ("Augusta National Golf Club", True)]


def test_a_searched_article_has_to_be_about_a_golf_course_and_a_known_section_is_cut_out():
    words = " ".join(["word"] * 90)

    class _Client:
        def __init__(self, text):
            self.text = text

        async def get(self, url, params):
            return _Page(params["titles"], self.text)

    t = CourseTrivia()
    game = f"Golf is a golf video game published by Nintendo.\n\n== Gameplay ==\n{words}"
    assert asyncio.run(t._extract(_Client(game), "Golf (1984 video game)")) is None
    club = f"Riviera Country Club is a private club with a championship golf course.\n\n== History ==\n{words}"
    assert asyncio.run(t._extract(_Client(club), "Riviera Country Club"))[0] == "Riviera Country Club"
    hotel = (f"Wynn is a casino.\n\n== Features ==\nShows.\n\n=== Golf course ===\nThe Wynn Golf Club. {words}"
             f"\n\n=== Pools ===\nWet.\n\n== References ==\nx")
    assert section_of(hotel, "Golf course") == f"The Wynn Golf Club. {words}"
    assert asyncio.run(t._extract(_Client(hotel), "Wynn Las Vegas#Golf course", known=True))[1].startswith("The Wynn")


def test_a_famous_hole_is_told_once_on_the_first_tee_shot_there(tmp_path):
    assert "island-green" in signature_hole(["Sodgrass?", "dpc_sodgrass_gsp"], 17)
    assert signature_hole(["dpc_sodgrass_gsp"], 5) is None and signature_hole(["Premonition"], 17) is None
    e = _engine(tmp_path)
    e.round = SimpleNamespace(id=1, course="DPC Sodgrass")
    e.hole_number = 17
    [fact] = e._signature_facts("Joe")
    assert fact.startswith("Famous hole: this is GSPro's version of TPC Sawgrass's 17th, the island-green par 3")
    assert e._signature_facts("Joe") == [fact]            # Joe's setup rebuilt: still his to hear
    assert e._signature_facts("Ann") == []                # the rest of the group don't hear it again
    e.hole_number = 18
    assert e._signature_facts("Ann")


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
    item = ScoreItem("nhl:5", "team", "in", "NHL, live as of 6:05 pm: Bruins 2, Stars 1, 8:14 left in the 2nd.", "2-1")
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
        async def article(self, *names):
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


def test_an_aside_lands_on_what_the_round_just_did_not_only_on_ball_flight(tmp_path):
    e = _engine(tmp_path)
    e.cfg.news.banter_rate = 1.0

    def _round(holes, seq, scores):
        side = SimpleNamespace()
        return SimpleNamespace(holes=holes, seq=seq, ended=False, is_league=False, sides=[side],
                               par={h: 4 for h in scores}, card_score=lambda s, q: scores.get(q))

    assert e._round_moment_tags(_round(9, 3, {1: 4, 2: 3})) == {"birdie_posted"}
    assert e._round_moment_tags(_round(9, 3, {1: 4, 2: 6})) == {"blow_up_posted"}
    assert e._round_moment_tags(_round(9, 3, {1: 3, 2: 5})) == set()     # only the hole just finished
    assert e._round_moment_tags(_round(9, 1, {})) == set()               # nothing finished yet
    assert e._round_moment_tags(None) == set()
    assert e._round_moment_tags(_round(18, 10, {9: 4})) == {"at_the_turn"}
    assert e._round_moment_tags(_round(9, 5, {4: 4})) == set()           # a nine has no turn

    held = _bit("news", trigger="good_hole")
    e._news_bits = [held]
    e.round = _round(9, 3, {1: 4, 2: 3})
    assert e._news_pick({"tags": [], "routine": False}) is None          # the tag comes with the moment
    assert e._news_pick({"tags": ["birdie_posted"], "routine": False}) == (held, True)


def test_an_aside_held_for_a_long_putt_doesnt_air_on_a_short_one(tmp_path):
    # "makeable_putt" is a 3-to-12-footer. Firing a story written for a long putt on one would have
    # the bridge line promise a moment that didn't happen.
    from commentator.engine import NEWS_TRIGGER_TAGS

    e = _engine(tmp_path)
    e.cfg.news.banter_rate = 1.0
    e.round = SimpleNamespace(holes=9, seq=3, ended=False, is_league=False)
    e._news_bits = [held := _bit("news", trigger="long_putt")]
    assert "makeable_putt" not in NEWS_TRIGGER_TAGS["long_putt"]
    assert e._news_pick({"tags": ["putt", "makeable_putt"], "routine": False}) is None
    assert e._news_pick({"tags": ["putt", "long_putt"], "routine": False}) == (held, True)


async def test_each_nine_writes_its_asides_at_its_second_hole_to_its_airtime(tmp_path):
    e = _engine(tmp_path)
    # Five quiet moments across an 18 (one every three holes, never the first or the last two): three
    # on the front, two on the back. A nine on its own has two.
    assert e._aside_slots(18) == 5 and e._aside_slots(9) == 2
    e.news, e.trivia = object(), object()
    e.cfg.news.per_nine, e.cfg.trivia.per_round = 2, 1
    r = SimpleNamespace(holes=18, seq=1, ended=False, is_league=False)
    e.round = r
    written = []

    async def _trivia(r, n=None):
        written.append(("trivia", r.seq, n))

    async def _news(r, n=None):
        written.append(("news", r.seq, n))

    e._prepare_trivia, e._prepare_news = _trivia, _news

    async def _hole(seq):
        r.seq = seq
        e._maybe_prep_asides(r)
        for t in list(e._aside_tasks):
            await t

    for seq in range(1, 11):
        await _hole(seq)
    assert sorted(written) == [("news", 2, 2), ("trivia", 2, 1)]    # front nine: 3 slots, at hole 2
    e._news_bits = [_bit("news")]                                     # one of them never found its moment
    await _hole(11)
    assert written[2:] == [("news", 11, 1)]                          # back: 2 slots, 1 still waiting, trivia spent
    await _hole(12)
    assert len(written) == 3                                          # once per nine

    # A live score claims its check-in first, inside its own nine.
    item = ScoreItem("nhl:5", "team", "in", "NHL, live: Bruins 2, Stars 1.", "2-1")
    e.scores = _Scores([item])
    e.cfg.scores.per_nine, e.cfg.trivia.per_round = 1, 2
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round, e._news_bits, written[:] = r, [], []
    e._start_round_asides(r)
    await _hole(2)
    assert len(e._score_holes) == 1 and 3 <= e._score_holes[0] <= 7 and written == [("news", 2, 1)]


async def test_a_superseded_round_start_doesnt_pay_for_its_asides(tmp_path):
    from tests.test_round import _NewsLLM, _news_engine

    e = _news_engine(tmp_path, llm := _NewsLLM())
    e.cfg.news.per_nine = 3
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)   # already replaced
    await e._prepare_news(r)
    assert llm.prompts == [] and e._news_bits == []      # not one call spent on a round that's gone

    # A round quit leaves it in place but ended: still nothing to write.
    e.round = r
    r.ended = True
    await e._prepare_news(r)
    assert llm.prompts == [] and e._news_bits == []


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


async def test_off_air_an_aside_is_kept_and_a_postponed_game_is_no_result(tmp_path):
    e = _engine(tmp_path)
    e.cfg.news.banter_rate = 1.0
    e.round = SimpleNamespace(holes=9, seq=4, ended=False, is_league=False)
    e._shot_seq = 1
    e._news_bits = [_bit("trivia")]
    e.speech.is_idle = lambda: True
    e.speech.muted = True
    assert await e._news_after_shot({"seq": 1, "tags": [], "routine": True}) is False and e._news_bits
    postponed = _game("post", NOW - timedelta(hours=3), "Postponed")
    postponed["events"][0]["status"]["type"]["completed"] = False
    assert parse_games(postponed, "nhl", ["BOS"], NOW) == []


def test_news_is_dated_and_old_stories_are_left_out():
    from commentator.news import GolfNews

    rss = """<rss><channel>
      <item><title>Fresh</title><description>Scottie Scheffler won by five shots on Sunday in Texas.</description>
            <pubDate>Sun, 18 Oct 2026 18:54:19 EST</pubDate></item>
      <item><title>Stale</title><description>Rory McIlroy won by two shots in Ireland after a long week.</description>
            <pubDate>Sun, 27 Sep 2026 21:43:33 EST</pubDate></item>
    </channel></rss>"""
    news = GolfNews(["x"], max_age_days=3)
    news.headlines = news._parse(rss, "espn.com")
    assert [h.title for h in news.pick_many(random.Random(0), 5, now=NOW)] == ["Fresh"]
    assert news.headlines[0].as_fact(NOW).endswith("(via espn.com, published on Sunday, 2 days ago)")


def test_game_clock_is_labelled_as_time_left():
    from commentator.scores import _game_time

    assert _game_time("1:21 - 2nd Period") == "1:21 left in the 2nd Period"
    assert _game_time("5:32 - 3rd Quarter") == "5:32 left in the 3rd Quarter"
    assert _game_time("0:04.2 - 4th Quarter") == "0:04.2 left in the 4th Quarter"
    assert _game_time("Halftime") == "Halftime"
    assert _game_time("Top 5th") == "Top 5th"
    assert _game_time("End of 2nd Period") == "End of 2nd Period"


def test_news_dates_accept_a_naive_local_now():
    from commentator.news import GolfNews

    news = GolfNews(["x"])
    news.headlines = news._parse("""<rss><channel><item><title>T</title>
      <description>Scottie Scheffler won by five shots on Sunday in Texas this week.</description>
      <pubDate>Sun, 18 Oct 2026 18:54:19 EST</pubDate></item></channel></rss>""", "espn.com")
    naive = datetime(2026, 10, 20, 21, 0)
    assert [h.title for h in news.pick_many(random.Random(0), 5, now=naive)] == ["T"]
    assert "published on Sunday, 2 days ago" in news.headlines[0].as_fact(naive)
