import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from commentator.league import League
from commentator.round import Round
from commentator.squabbit import SquabbitClient
from commentator.squabbit_live import Blocked, LiveSync, Mark, Target, feet_text, parse_feet, team_ctp, team_scores
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
    live.push(c, t, {}, {(3, "a"): 3.5})                  # a corrected read: the booth's to update
    assert comp["results"]["a"] == 126.0
    del comp["results"]["a"]                               # cleared by someone on Squabbit: left cleared
    assert live.push(c, t, {}, {(3, "a"): 3.0}) == [] and "a" not in comp["results"]
    comp["results"]["a"] = 126.0
    comp["results"]["b"] = 200.0                           # Sarah entered her own
    assert live.push(c, t, {}, {(3, "b"): 1.0}) == [] and comp["results"]["b"] == 200.0


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
    found = [Mark(3, "Sarah", 4.0), Mark(3, "Aces", 2.0), Mark(5, "Mike", 3.5), Mark(5, "Mike", 3.0, "unsure")]
    assert {k: (m.yards, m.sure) for k, m in team_ctp(r, "Aces", UIDS, league.match_player, found).items()} == \
        {(3, "b"): (4.0, True), (5, "a"): (3.5, True)}   # a team label is no one's; a sure mark beats a guess
    # One GSPro profile for the team: its ball is no one's mark, whatever name it's under.
    league = League(LEAGUE_DATA)
    r = Round(league=league, gspro_players=["Mike"], course="Test Links", scheduled_format="Alternate Shot",
              holes=9, start_hole=10, schedule={**LEAGUE_DATA["schedule"][0], "format": "Alternate Shot"})
    assert r.anonymous_players()
    assert team_ctp(r, "Aces", UIDS, league.match_player, [Mark(3, "Mike", 4.0), Mark(5, "Sarah", 0.0)]) == {}


def test_a_typed_ctp_distance_reads_in_feet_and_inches():
    for text in ("9' 8\"", "9 8", "9ft 8in", "9 feet 8 inches", "116\"", "116 in"):
        assert round(parse_feet(text) * 36) == 116, text
    assert round(parse_feet("9.5") * 36) == 114 and parse_feet("0") == 0
    for text in ("", "abc", "9 13", "150", "-3"):
        assert parse_feet(text) is None, text
    assert feet_text(116 / 36) == "9' 8\""


def test_the_booth_asks_about_ctp_marks_it_isnt_sure_of(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    r, _ = _league_round("Scramble")
    r.ctp_holes = [12, 13, 14, 15, 17]
    e.round = r
    tee = lambda sid, seq, who, outcome=None, picked=None: SimpleNamespace(
        shot_id=sid, round_id=r.id, hole_seq=seq, player=who, shot_number=1, mulligan=False,
        outcome=outcome, picked=picked)
    e.state.shots = [tee(1, 3, "Mike"), tee(2, 4, "Mike", "conceded", True), tee(3, 5, "Sarah", "conceded"),
                     tee(4, 6, "Mike", "conceded"), tee(5, 3, "Sarah", picked=False), tee(6, 1, "Sarah", "holed"),
                     tee(7, 8, "Sarah", "conceded")]
    finishes = {1: 4.0, 2: 2.0, 3: 3.5, 4: 1.0, 5: 5.0, 6: 0.0}
    e.store.tee_finishes = lambda ids: {i: finishes[i] for i in ids if i in finishes}
    gimme = {2: 2.0, 3: 3.5}   # yards GSPro logged inside the gimme: 6 ft, 10.5 ft; shot 4's not known
    e._gimme_left = lambda sid: gimme.get(sid)
    r.ctp_results = {3: {"Mike": "4 yards"}, 7: {"Sarah": "20 feet"}}
    r.ctp_tees = {(3, "Mike"): 1, (7, "Sarah"): 9}
    e._log_finish = {1: 4.1, 9: 12.0}   # Mike's read agrees with the log; Sarah's doesn't
    got = {(m.seq, m.who): (m.yards and round(m.yards, 2), m.sure) for m in e._ctp_marks(r)}
    assert got == {
        (3, "Mike"): (4.0, True),
        (7, "Sarah"): (12.0, False),    # the screen and the log disagree: the log's offered
        (4, "Mike"): (2.0, True),       # conceded inside the default 8 ft: on the green
        (5, "Sarah"): (3.5, False),     # conceded at 10.5 ft: maybe the fringe
        (6, "Mike"): (1.0, False),      # conceded, nobody knows how close
        (3, "Sarah"): (5.0, False),     # a lifted scramble ball the pick screen read on the green
        (8, "Sarah"): (None, False),    # conceded, no distance at all: asked, with nothing filled in
    }   # hole 1 isn't a CTP hole
    e.cfg.squabbit.ctp_sure_gimme_feet = 0     # pins anywhere: every gimme's asked about
    assert not next(m for m in e._ctp_marks(r) if m.seq == 4).sure


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


def test_an_unsure_ctp_mark_goes_in_as_the_players_confirm_it(tmp_path, monkeypatch):
    import commentator.popup as popup
    import commentator.squabbit as sq

    boxes = []

    class Box:
        def __init__(self, title, detail, *a, on_answer, valid=None, where=None):
            boxes.append(self)
            self.title, self.on_answer, self.valid, self.closed = title, on_answer, valid, False
            self.value = a[0] if len(a) == 3 else None

        def show(self):
            return self

        def close(self):
            self.closed = True

    fake = _client()
    fake.docs[TR]["holeCompetitions"]["c13"] = {"results": {}}
    fake.tournaments = lambda lid: [{"_id": "T", "name": "Week 6 - Best Ball"}]
    fake.player_rounds = lambda tid: [{"_id": "Ra", "userId": "a", "scorecardId": "S"},
                                      {"_id": "Rb", "userId": "b", "scorecardId": "S"}]
    fake.tournament_rounds = lambda tid: [{"_id": "TR", "startingHole": 10, "numHoles": 9, "holeCompetitions": {
        "c12": {"type": "ClosestToThePin", "holeNumber": 12}, "c13": {"type": "ClosestToThePin", "holeNumber": 13}}}]
    fake.league_by_invite = lambda code: {"_id": "L"}

    class Client:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(popup, "Question", Box)
    monkeypatch.setattr(popup, "Entry", Box)
    monkeypatch.setattr(sq, "SquabbitClient", Client)

    async def settle(e):
        for _ in range(50):
            await asyncio.sleep(0.01)
        if e._sq.get("task"):
            await e._sq["task"]

    async def go():
        e = _engine(tmp_path, League(LEAGUE_DATA))
        e.cfg.squabbit.enabled = True
        e.cfg.source.type = "gspro"
        e.cfg.db_path = str(tmp_path / "season.db")
        r, _ = _league_round("Best Ball")
        r.league = e.league
        r.ctp_holes = [12, 13]
        e.round = r
        r.seq = 3
        e._ctp_marks = lambda r: [Mark(3, "Mike", 3.0, "Is it on the green?"), Mark(4, "Sarah", 2.0, "Hm?")]
        e._squabbit_kick()
        await settle(e)
        boxes[0].on_answer(True)
        await settle(e)
        assert len(boxes) == 1     # hole 12's still being played: nothing asked yet
        r.log_scores = {"Mike": {3: 3, 4: 4}}   # Sarah's still on hole 13: hers waits for her
        e._squabbit_kick()
        await settle(e)
        assert len(boxes) == 2 and boxes[1].title.startswith("Mike") and boxes[1].value == "9' 0\""
        assert boxes[1].valid("9 4") and not boxes[1].valid("close")
        assert fake.docs[TR]["holeCompetitions"]["c12"]["results"] == {"z": 120.0}   # not until they say
        boxes[1].on_answer("9' 4\"")
        await settle(e)
        assert fake.docs[TR]["holeCompetitions"]["c12"]["results"] == {"z": 120.0, "a": 112.0}
        r.log_scores["Sarah"] = {4: 3}
        e._squabbit_kick()
        await settle(e)
        assert len(boxes) == 3 and boxes[2].title.startswith("Sarah")
        boxes[2].on_answer(None)        # "I'll enter it myself"
        await settle(e)
        assert fake.docs[TR]["holeCompetitions"]["c13"]["results"] == {}
        e._squabbit_kick()
        await settle(e)
        assert len(boxes) == 3           # answered: never asked again
        assert json.loads((tmp_path / "squabbit_live.json").read_text())["ctp_answers"] == \
            {"TR/12/a": 112 / 36, "TR/13/b": None}

    asyncio.run(go())
