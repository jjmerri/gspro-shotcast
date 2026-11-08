import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from commentator.league import League
from commentator.round import Round
from commentator.squabbit import SquabbitClient
from commentator.squabbit_live import Blocked, LiveSync, Target, team_ctp, team_scores
from tests.test_board import LEAGUE_DATA, _engine

UIDS = {"Mike": "a", "Sarah": "b", "Tony": "c", "Priya": "d", "Lou": "e", "Dee": "f"}


def test_commit_sets_only_the_named_fields_if_nothing_changed_since_the_read():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={})

    c = SquabbitClient("e", "p")
    c._token = "t"
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert c.commit([("rounds/R1", {("holeScoresV2", "10"): 4, ("score",): 41,
                                    ("holeCompetitions", "x-1", "results", "u"): 30.0}, "T1")],
                    verify=[("rounds/R2", "T2")])
    doc = "projects/squabbit-2019/databases/(default)/documents/rounds/"
    assert seen["url"].endswith("/documents:commit")
    assert seen["body"]["writes"] == [
        {"update": {"name": doc + "R1", "fields": {
            "holeScoresV2": {"mapValue": {"fields": {"10": {"integerValue": "4"}}}},
            "score": {"integerValue": "41"},
            "holeCompetitions": {"mapValue": {"fields": {"x-1": {"mapValue": {"fields": {
                "results": {"mapValue": {"fields": {"u": {"doubleValue": 30.0}}}}}}}}}}}},
         "updateMask": {"fieldPaths": ["holeScoresV2.`10`", "score", "holeCompetitions.`x-1`.results.u"]},
         "currentDocument": {"updateTime": "T1"}},
        {"verify": doc + "R2", "currentDocument": {"updateTime": "T2"}},
    ]
    from commentator.squabbit import DELETE

    c.commit([("rounds/R1", {("holeScoresV2", "12"): DELETE, ("score",): None}, "T3")])
    w = seen["body"]["writes"][0]
    assert w["update"]["fields"] == {"score": {"nullValue": None}}
    assert w["updateMask"] == {"fieldPaths": ["holeScoresV2.`12`", "score"]}


def test_commit_reports_a_lost_race_instead_of_raising():
    c = SquabbitClient("e", "p")
    c._token = "t"
    c._http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(400, json={"error": {"status": "FAILED_PRECONDITION"}})))
    assert c.commit([("rounds/R1", {("score",): 1}, "x")]) is False


class FakeClient:
    """Squabbit's documents in memory: a commit is all or nothing, and bumps what it writes."""

    def __init__(self, docs):
        self.docs = docs
        self.times = {p: 0 for p in docs}
        self.commits = []
        self.meddle = {}   # path -> fields someone else sets just before the booth's next commit

    def document(self, path):
        return copy.deepcopy(self.docs.get(path))

    def document_at(self, path):
        return (copy.deepcopy(self.docs[path]), str(self.times[path])) if path in self.docs else None

    def commit(self, updates, verify=()):
        for path, other in self.meddle.items():
            _set(self.docs[path], other)
            self.times[path] += 1
        self.meddle = {}
        if any(str(self.times[p]) != at for p, *_, at in [*updates, *verify]):
            return False
        for path, values, _ in updates:
            _set(self.docs[path], values)
            self.times[path] += 1
        self.commits.append(updates)
        return True

    def user_id(self):
        return "me"


def _set(doc, values):
    from commentator.squabbit import DELETE

    for keys, v in values.items():
        node = doc
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        if v is DELETE:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = v


def _target(**kw):
    return Target(**{"team": "Aces", "week": 6, "tournament_id": "T", "tournament_round_id": "TR",
                     "scorecard_id": "S", "start_hole": 10, "num_holes": 3, "rounds": {"a": "Ra", "b": "Rb"},
                     "handicaps": {"a": 12.5, "b": None}, "ctp": {12: "c12"}, **kw})


TR = "tournamentsV2/T/tournamentRounds/TR"


def _client():
    return FakeClient({"rounds/Ra": {"holeScoresV2": {}}, "rounds/Rb": {"holeScoresV2": {}},
                       TR: {"holeCompetitions": {"c12": {"results": {"z": 120.0}, "enteredByUserIds": {"z": "z"}}}}})


def test_scores_go_on_by_course_hole_and_only_once(tmp_path):
    live, c, t = LiveSync(tmp_path / "s.json"), _client(), _target()
    assert live.push(c, t, {"a": {1: 5, 2: 4}, "b": {1: 5}}, {}) == ["a 10:5 11:4", "b 10:5"]
    ra = c.docs["rounds/Ra"]
    assert ra["holeScoresV2"] == {"10": 5, "11": 4} and ra["score"] == 9 and ra["userHandicap"] == 12.5
    assert ra["holeStats"] == {"10": {"holeEvents": {}}, "11": {"holeEvents": {}}} and "finishedDate" not in ra
    assert "userHandicap" not in c.docs["rounds/Rb"]   # no event handicap to copy
    n = len(c.commits)
    assert live.push(c, t, {"a": {1: 5, 2: 4}}, {}) == [] and len(c.commits) == n
    # A booth score corrected (the log beat a misread card) is the booth's to fix; the last hole finishes it.
    live.push(c, t, {"a": {1: 5, 2: 5, 3: 4}}, {})
    assert ra["holeScoresV2"] == {"10": 5, "11": 5, "12": 4} and ra["score"] == 14 and ra["finishedDate"]
    # What the booth wrote survives a restart.
    assert LiveSync(tmp_path / "s.json").others(c, t) == []


def test_anyone_elses_entry_on_the_card_stops_the_posting(tmp_path):
    live, c, t = LiveSync(tmp_path / "s.json"), _client(), _target()
    live.push(c, t, {"a": {1: 5}}, {})
    c.docs["rounds/Ra"]["holeScoresV2"]["10"] = 4          # a player fixed the booth's score in the app
    with pytest.raises(Blocked):
        live.push(c, t, {"a": {1: 5, 2: 4}}, {})
    assert c.docs["rounds/Ra"]["holeScoresV2"] == {"10": 4}
    assert live.others(c, t) == ["round Ra hole 10: 4"]
    # On the teammate's round, too: nothing goes on either.
    live, c = LiveSync(tmp_path / "s2.json"), _client()
    c.docs["rounds/Rb"]["holeScoresV2"]["10"] = 4
    with pytest.raises(Blocked):
        live.push(c, t, {"a": {1: 5}}, {(3, "a"): 4.0})
    assert c.commits == []


def test_an_entry_made_between_read_and_write_isnt_overwritten(tmp_path):
    live, c, t = LiveSync(tmp_path / "s.json"), _client(), _target()
    c.meddle["rounds/Rb"] = {("holeScoresV2", "10"): 6}   # the teammate's round, which the booth isn't writing
    with pytest.raises(Blocked):
        live.push(c, t, {"a": {1: 5}}, {})
    assert c.docs["rounds/Ra"]["holeScoresV2"] == {} and c.commits == []
    # A CTP mark entered just then stays, and the booth's goes in alongside it on the next look.
    live, c = LiveSync(tmp_path / "s2.json"), _client()
    c.meddle[TR] = {("holeCompetitions", "c12", "results", "a"): 50.0}
    assert live.push(c, t, {}, {(3, "a"): 4.0, (3, "b"): 1.0}) == ["b CTP hole 12 36 in"]
    assert c.docs[TR]["holeCompetitions"]["c12"]["results"] == {"z": 120.0, "a": 50.0, "b": 36.0}


def test_ctp_marks_go_in_alongside_others_but_never_over_a_players_own(tmp_path):
    live, c, t = LiveSync(tmp_path / "s.json"), _client(), _target()
    # Hole 12 is the event's CTP hole; hole 11 isn't.
    assert live.push(c, t, {}, {(3, "a"): 4.0, (2, "b"): 3.0}) == ["a CTP hole 12 144 in"]
    comp = c.docs[TR]["holeCompetitions"]["c12"]
    assert comp["results"] == {"z": 120.0, "a": 144.0} and comp["enteredByUserIds"]["a"] == "me"
    live.push(c, t, {}, {(3, "a"): 3.5})                  # a mulligan's better ball: the booth's to update
    assert comp["results"]["a"] == 126.0
    del comp["results"]["a"]                               # cleared by someone on Squabbit: left cleared
    assert live.push(c, t, {}, {(3, "a"): 3.0}) == [] and "a" not in comp["results"]
    comp["results"]["a"] = 126.0
    comp["results"]["b"] = 200.0                           # Sarah entered her own
    assert live.push(c, t, {}, {(3, "b"): 1.0}) == [] and comp["results"]["b"] == 200.0
    # A tee shot taken back takes the booth's mark with it, never a player's own.
    assert live.push(c, t, {}, {(3, "a"): None, (3, "b"): None}) == ["a CTP hole 12 taken back"]
    assert comp["results"] == {"z": 120.0, "b": 200.0} and "a" not in comp["enteredByUserIds"]


def test_find_picks_the_teams_scorecard_and_the_weeks_holes():
    class C:
        def tournaments(self, league_id):
            return [{"_id": "T5", "name": "Week 5 - Scramble"},
                    {"_id": "T6", "name": "Week 6 - Best Ball", "users": {"a": {"tournamentHandicap": 12.0}}}]

        def player_rounds(self, tid):
            assert tid == "T6"
            return [{"_id": "R1", "userId": "a", "scorecardId": "S1"}, {"_id": "R2", "userId": "b", "scorecardId": "S1"},
                    {"_id": "R3", "userId": "c", "scorecardId": "S2"}, {"_id": "R4", "userId": "d", "scorecardId": "S2"}]

        def document(self, path):
            return {"tournamentRoundId": "TR"} if path == "scorecards/S1" else None

        def tournament_rounds(self, tid):
            return [{"_id": "TR", "startingHole": {"holeNumber": 10}, "numHoles": 9, "holeCompetitions": {
                "x": {"type": "ClosestToThePin", "holeNumber": 14}, "y": {"type": "LongestDrive", "holeNumber": 15}}}]

    t = LiveSync.find(C(), "L", 6, "Aces", ["a", "b"])
    assert (t.scorecard_id, t.rounds, t.start_hole, t.num_holes, t.ctp, t.handicaps) == \
        ("S1", {"a": "R1", "b": "R2"}, 10, 9, {14: "x"}, {"a": 12.0, "b": None})
    assert LiveSync.find(C(), "L", 6, "Aces", ["a", "z"]) is None
    assert LiveSync.find(C(), "L", 8, "Aces", ["a", "b"]) is None


def _league_round(fmt):
    league = League(LEAGUE_DATA)
    sched = {**LEAGUE_DATA["schedule"][0], "format": fmt}
    r = Round(league=league, gspro_players=["Mike", "Sarah"], course="Test Links", scheduled_format=fmt,
              holes=9, start_hole=10, schedule=sched)
    return r, league


def test_a_shared_ball_goes_on_both_rounds_and_best_ball_on_each_players_own():
    r, league = _league_round("Scramble")
    r.log_scores = {"Mike": {1: 4, 2: 5}}
    assert team_scores(r, "Aces", UIDS) == {"a": {1: 4, 2: 5}, "b": {1: 4, 2: 5}}
    r, league = _league_round("Best Ball")
    r.log_scores = {"Mike": {1: 5}, "Sarah": {1: 4, 2: 3}}
    assert team_scores(r, "Aces", UIDS) == {"a": {1: 5}, "b": {1: 4, 2: 3}}
    assert team_scores(r, "Duffers", UIDS) == {}
    r.ctp_results = {3: {"Sarah": "4 yards", "Aces": "2 yards"}, 5: {"Mike": "10 feet 6 inches"}}
    assert team_ctp(r, "Aces", UIDS, league.match_player) == {(3, "b"): 4.0, (5, "a"): 3.5}
    # An ace, which leaves no second-stroke setup to read a mark from.
    assert team_ctp(r, "Aces", UIDS, league.match_player, {(7, "Mike"): 0.0}) == \
        {(3, "b"): 4.0, (5, "a"): 3.5, (7, "a"): 0.0}
    # One GSPro profile for the team: its ball is no one's mark, whatever name it's under.
    league = League(LEAGUE_DATA)
    r = Round(league=league, gspro_players=["Mike"], course="Test Links", scheduled_format="Alternate Shot",
              holes=9, start_hole=10, schedule={**LEAGUE_DATA["schedule"][0], "format": "Alternate Shot"})
    assert r.anonymous_players()
    r.ctp_results = {3: {"Mike": "4 yards", "Aces": "2 yards"}}
    assert team_ctp(r, "Aces", UIDS, league.match_player, {(5, "Sarah"): 0.0}) == {}


def test_the_round_is_posted_once_the_players_say_yes(tmp_path, monkeypatch):
    import commentator.popup as popup
    import commentator.squabbit as sq

    asked = []

    class Q:
        def __init__(self, title, detail, yes, no, on_answer, where=None):
            asked.append(self)
            self.on_answer, self.closed = on_answer, False

        def show(self):
            return self

        def close(self):
            self.closed = True

    fake = _client()
    fake.tournaments = lambda lid: [{"_id": "T", "name": "Week 6 - Best Ball"}]
    fake.player_rounds = lambda tid: [{"_id": "Ra", "userId": "a", "scorecardId": "S"},
                                      {"_id": "Rb", "userId": "b", "scorecardId": "S"}]
    fake.tournament_rounds = lambda tid: [{"_id": "TR", "startingHole": 10, "numHoles": 9}]
    fake.league_by_invite = lambda code: {"_id": "L"}

    class Client:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(popup, "Question", Q)
    monkeypatch.setattr(sq, "SquabbitClient", Client)

    async def go():
        e = _engine(tmp_path, League(LEAGUE_DATA))
        e.cfg.squabbit.enabled = True
        e.cfg.source.type = "gspro"
        e.cfg.db_path = str(tmp_path / "season.db")
        r, _ = _league_round("Best Ball")
        r.league = e.league
        e.round = r
        r.log_scores = {"Mike": {1: 5}}
        e._squabbit_kick()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if asked:
                break
        assert e._sq["status"] == "asking" and fake.commits == []
        e._squabbit_kick()                  # holes finish while nobody's answered: nothing goes in
        asked[0].on_answer(True)            # called on the box's thread in real life; handed to the loop
        await asyncio.sleep(0.05)
        await e._sq["task"]
        assert fake.docs["rounds/Ra"]["holeScoresV2"] == {"10": 5}
        r.log_scores["Sarah"] = {1: 4}
        e._squabbit_kick()
        await e._sq["task"]
        assert fake.docs["rounds/Rb"]["holeScoresV2"] == {"10": 4}
        # The answer is kept: a restarted booth carries on without asking again.
        assert json.loads((tmp_path / "squabbit_live.json").read_text())["answers"] == {"S": True}

        # The booth restarts mid-round: the rebuilt round hasn't got its scores back yet, and that takes
        # nothing back off the card.
        e._sq = None
        rebuilt, _ = _league_round("Best Ball")
        rebuilt.league = e.league
        e.round = rebuilt
        e._squabbit_kick()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if e._sq.get("task"):
                break
        await e._sq["task"]
        assert fake.docs["rounds/Ra"]["holeScoresV2"] == {"10": 5}
        assert fake.docs["rounds/Rb"]["holeScoresV2"] == {"10": 4}

        # A new round's question still up when a practice round replaces it: it comes down.
        e._sq_live.state["answers"].clear()      # not asked yet for the new round
        r2, _ = _league_round("Best Ball")
        r2.league = e.league
        e.round = r2
        e._squabbit_kick()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(asked) == 2:
                break
        practice, _ = _league_round("Best Ball")
        practice.is_league = False
        e.round = practice
        e._squabbit_kick()
        assert asked[1].closed and e._sq is None

    asyncio.run(go())


def test_a_reopened_hole_is_taken_back_and_the_round_unfinished(tmp_path):
    live, c, t = LiveSync(tmp_path / "s.json"), _client(), _target()
    live.push(c, t, {"a": {1: 5, 2: 4, 3: 3}}, {})
    assert c.docs["rounds/Ra"]["finishedDate"]
    assert live.push(c, t, {"a": {1: 5, 2: 4, 3: None}}, {}) == ["a 12:-"]
    ra = c.docs["rounds/Ra"]
    assert ra["holeScoresV2"] == {"10": 5, "11": 4} and ra["score"] == 9 and ra["finishedDate"] is None
    assert live.others(c, t) == []
    # A restarted booth still knows which holes are its own to take back.
    assert LiveSync(tmp_path / "s.json").posted(t) == {"a": {1, 2}}
    # A booth score someone cleared on Squabbit is their call: the booth stops rather than put it back.
    del c.docs["rounds/Ra"]["holeScoresV2"]["11"]
    assert live.others(c, t) == ["round Ra hole 11: cleared"]


def test_a_card_that_keeps_changing_is_a_failed_post_not_a_quiet_one(tmp_path):
    from commentator.squabbit_live import Busy

    live, c, t = LiveSync(tmp_path / "s.json"), _client(), _target()
    c.commit = lambda updates, verify=(): False
    with pytest.raises(Busy):
        live.push(c, t, {"a": {1: 5}}, {})
