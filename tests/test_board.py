import random

from commentator import handicap as H
from commentator.board import Board, Entry, place, place_points
from commentator.config import Config
from commentator.engine import Engine, _distance_text
from commentator.hud import Hud, Scorecard
from commentator.league import League
from commentator.models import Shot
from commentator.round import Round
from tests.test_handicap import ROUND, player, tournament

LEAGUE_DATA = {
    "league_name": "Test", "squabbit_invite": "ABC",
    "teams": [
        {"name": "Aces", "players": [{"name": "Mike", "handicap": 12.0, "squabbit_id": "a"},
                                     {"name": "Sarah", "handicap": 2.0, "squabbit_id": "b"}]},
        {"name": "Duffers", "players": [{"name": "Tony", "handicap": 6.0, "squabbit_id": "c"},
                                        {"name": "Priya", "handicap": 9.0, "squabbit_id": "d"}]},
        {"name": "Hackers", "players": [{"name": "Lou", "squabbit_id": "e"}, {"name": "Dee", "squabbit_id": "f"}]},
    ],
    "schedule": [{"week": 6, "course": "Test Links", "format": "Best Ball", "hole_count": 9, "start_hole": 10,
                  "end_date": "2026-12-06", "ctp_holes": [12, 15]},
                 {"week": 7, "course": "Other", "format": "Aggregate", "hole_count": 9, "end_date": "2026-12-06"}],
}
POINTS = [70.0, 60.0, 50.0]


def week(n, entries):
    """A Squabbit tournament and its leaderboard: entries are (team id, user ids, name, net or None)."""
    teams = {tid: {"name": name, "userIds": users} for tid, users, name, _ in entries}
    t = {"_id": f"t{n}", "name": f"Week {n} - Best Ball",
         "formatsV2": {"f": {"formatType": "BestBall", "teams": teams}}}
    lb = {"leaderboards": {"x": {"teamEntries": [
        {"entityId": tid, "entityName": name, "total": None if net is None else float(net)}
        for tid, _, name, net in entries]}}}
    return t, lb


def test_places_ties_and_points():
    assert place(-3, [-5, -3, -3, 0]) == (2, True)
    assert place(-5, [-5, -3, -3, 0]) == (1, False)
    # A tie splits the places it covers; places past the table score nothing.
    assert place_points(1, 2, POINTS) == 65.0 and place_points(3, 2, POINTS) == 25.0


def test_board_maps_teams_by_squabbit_ids_and_counts_only_final_weeks():
    league = League(LEAGUE_DATA)
    items = [week(6, [("x1", ["a", "b"], "Team 1", -4), ("x2", ["c", "d"], "Team 2", -4),
                      ("x3", ["e", "f"], "Hackers", 1)]),
             week(7, [("x1", ["a", "b"], "Team 1", 2), ("x2", ["c", "d"], "Team 2", None),
                      ("x3", ["zz"], "Hackers", -1)])]
    b = Board.from_squabbit(items, league, POINTS)
    assert [e.team for e in b.weeks[6]] == ["Aces", "Duffers", "Hackers"]
    assert b.weeks[7][2].team == "Hackers"              # unknown ids: matched by the team's name
    assert [(e.team, e.net) for e in b.posted(7)] == [("Hackers", -1), ("Aces", 2)]
    # Week 7 isn't final before its deadline (Duffers haven't posted), so only week 6 counts.
    assert b.standings("2026-11-01") == ([("Aces", 65.0, 1), ("Duffers", 65.0, 1), ("Hackers", 50.0, 3)], 1)
    # After the deadline it counts, with Duffers scoring nothing for it.
    table, weeks = b.standings("2026-12-07")
    assert weeks == 2 and table[0] == ("Aces", 125.0, 1) and ("Hackers", 120.0, 2) in table
    # A week nobody posted is never final.
    empty = Board({8: [Entry("Aces", None)]}, league, POINTS)
    assert empty.standings("2099-01-01") == ([], 0)


def _round(fmt="BestBall", settings=None, max_hole=None):
    card = H.week_card(tournament(fmt, settings or {"handicapPercentageForAll": 75.0}, max_hole=max_hole), ROUND)
    league = League(LEAGUE_DATA)
    league.week_cards = {6: card}
    sched = {**LEAGUE_DATA["schedule"][0], "format": {"BestBall": "Best Ball", "TeamSum": "Aggregate"}[fmt]}
    r = Round(league=league, gspro_players=["Mike", "Sarah"], course="Test Links",
              scheduled_format=sched["format"], holes=9, start_hole=10, schedule=sched)
    return r, card


def test_round_team_net_matches_squabbit_scoring():
    mike = [5, 6, 3, 5, 4, 4, 4, 6, 5]
    sarah = [4, 5, 4, 4, 5, 3, 4, 5, 4]
    for fmt, settings, rule in [("BestBall", {"handicapPercentageForAll": 75.0}, None),
                                ("TeamSum", {}, {"doublePar": False, "isGross": False, "amountOverPar": 1})]:
        r, card = _round(fmt, settings, rule)
        r.apply_scorecard(Scorecard(holes=list(range(10, 19)), scores={
            "Mike": dict(zip(range(10, 19), mike)), "Sarah": dict(zip(range(10, 19), sarah))}))
        ours = r.team_net("Aces")
        squabbit = H.team_net(card, [player("a", 12.0, mike), player("b", 2.0, sarah)], fmt)
        assert ours == (squabbit, 9), fmt
    # Part way through: just the holes both have scored.
    r, _ = _round()
    r.apply_scorecard(Scorecard(holes=list(range(10, 19)), scores={"Mike": {10: 5, 11: 6}, "Sarah": {10: 4}}))
    assert r.team_net("Aces")[1] == 1
    # A partner missing from the round: no team score.
    r.sides = [s for s in r.sides if s.label == "Mike"]
    assert r.team_net("Aces") is None


def _engine(tmp_path, league):
    from commentator.sources.replay import ReplaySource
    from commentator.speech import SpeechQueue
    from commentator.tts.console_tts import ConsoleTTS
    from tests.test_core import FakeLLM

    cfg = Config(log_dir=str(tmp_path))
    llm = FakeLLM(cfg.llm.live)
    return Engine(cfg=cfg, league=league, source=ReplaySource("data/sample_shots.jsonl"), live_llm=llm,
                  segment_llm=llm, speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0))


def _tee_shot(e, who, hole, finish, lie="green", fmt="best_ball", league=True, course="Test Links"):
    sid = e.store.record_shot(player=who, hole=f"Hole {hole}, par 3, 150 yards", shot=Shot(club="8I"),
                              before=Hud(lie="tee", shot_number=1), after=Hud(lie=lie), tags=[],
                              carry_game_yards=None, total_yards=None, offline_yards=None, course=course,
                              to_pin_yards=150.0, finish_yards=finish, fmt=fmt, is_league=league)
    return sid


def test_week_ctp_counts_only_league_tee_shots_on_the_green(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    _tee_shot(e, "Tony", 12, 4.0)
    _tee_shot(e, "Ann", 15, 25.5)                           # past 20 yd: still feet
    _tee_shot(e, "Priya", 12, 2.5, lie="fringe")           # off the green: doesn't count
    _tee_shot(e, "Lou", 12, 1.0, league=False)              # practice: doesn't count
    _tee_shot(e, "Mike", 12, 3.0, course="Elsewhere")       # another course
    _tee_shot(e, "Sarah", 14, 1.0)                          # not a CTP hole
    assert e.store.week_ctp(["Test Links"], [12, 15], "2026-01-01") == {12: ("Tony", 4.0), 15: ("Ann", 25.5)}
    r, _ = _round()
    e.round = r
    facts = e._week_ctp_facts(r)
    assert facts == ["Closest to the pin on hole 12 this week so far: Tony, 12 feet from the hole.",
                     "Closest to the pin on hole 15 this week so far: Ann, 76 feet 6 inches from the hole."]


def test_week_ctp_counts_scramble_picks_and_credits_an_unknown_hitter_to_the_team(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    _tee_shot(e, "Tony", 12, 4.0)
    _tee_shot(e, "Dee", 12, 3.0, fmt="scramble")              # the ball Hackers picked: counts
    one = _tee_shot(e, "Mike", 15, 25.5, fmt="alternate")     # one GSPro profile: who hit it isn't known
    e.store.set_shot_player(one, "Mike", attributed=False, fmt="alternate", shared_ball=True)
    assert e.store.week_ctp(["Test Links"], [12, 15], "2026-01-01") == {12: ("Dee", 3.0)}
    r, _ = _round()
    e.round = r
    assert e._week_ctp(r) == {12: ("Dee", 3.0), 15: ("Aces", 25.5)}
    card = e._ctp_card(6)                                       # the card is in feet, even past 20 yd
    assert card["holes"] == [{"hole": 12, "player": "Dee", "yards": 3.0, "distance": "9 feet"},
                             {"hole": 15, "player": "Aces", "yards": 25.5, "distance": "76 feet 6 inches"}]
    assert e._ctp_card(7) is None                              # no CTP holes that week


def test_week_ctp_counts_an_ace_with_no_logged_finish(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    _tee_shot(e, "Tony", 12, 3.0)
    ace = _tee_shot(e, "Mike", 12, None, lie=None, fmt="alternate")
    e.store.set_shot_player(ace, "Mike", attributed=False, fmt="alternate", shared_ball=True)
    e.store.set_shot_result(ace, outcome="holed")
    assert e.store.week_ctp(["Test Links"], [12], "2026-01-01", team_of=e._team_label) == {12: ("Aces", 0.0)}


def test_alternate_shot_ctp_credits_the_partner_who_teed_off(tmp_path):
    from commentator.state import ShotRecord

    league = League(LEAGUE_DATA)
    e = _engine(tmp_path, league)
    sched = {**LEAGUE_DATA["schedule"][0], "format": "Alternate"}
    r = Round(league=league, gspro_players=["Mike", "Sarah"], course="Test Links", scheduled_format="alternate",
              holes=9, start_hole=10, schedule=sched, is_league=True)
    assert r.format == "alternate" and not r.anonymous_players()   # GSPro's mode names each hitter
    e.round, r.seq = r, 3                                            # hole 12
    sid = _tee_shot(e, "Sarah", 12, None, lie=None, fmt="alternate")
    e.state.record_shot(ShotRecord(player="Sarah", shot=Shot(club="8I"), hud=Hud(), hole="Hole 12", tags=[],
                                   round_id=r.id, hole_seq=3, shot_number=1, shot_id=sid))
    # Mike plays the second stroke: the tee shot was Sarah's.
    facts = e._ctp_facts(2, Hud(lie="green", to_pin="9 feet", shot_number=2), "Mike")
    assert facts[0].startswith("Sarah's tee shot")
    assert e.store.week_ctp(["Test Links"], [12], "2026-01-01") == {12: ("Sarah", 3.0)}


def test_live_ctp_compares_with_the_week(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    _tee_shot(e, "Tony", 12, 4.0)        # an earlier round, the same booth session
    r, _ = _round()
    r.apply_scorecard(Scorecard(holes=list(range(10, 19)), scores={"Mike": {10: 4, 11: 5}, "Sarah": {10: 4, 11: 5}}))
    r.seq = 3
    e._start_round(r)
    # This round's own tee shot, once the store has it, isn't double counted against the HUD's read.
    _tee_shot(e, "Mike", 12, 1.0)
    assert "The one to beat this week: Tony, 12 feet from the hole." in e._ctp_facts(1)
    near = e._ctp_facts(2, Hud(lie="green", to_pin="9 feet 2 inches"), "Mike")
    assert "That's the new closest this week, inside Tony's 12 feet." in near
    far = e._ctp_facts(2, Hud(lie="green", to_pin="20 feet"), "Sarah")
    assert "Mike's 9 feet 2 inches is still the closest this week." in far


def test_week_board_facts_for_opener_closer_and_standings(tmp_path):
    league = League(LEAGUE_DATA)
    e = _engine(tmp_path, league)
    e.history.seasons, e.history.current_year = [{"year": 2026, "points_table": POINTS}], 2026
    e.board = Board.from_squabbit(
        [week(6, [("x1", ["a", "b"], "Team 1", None), ("x2", ["c", "d"], "Team 2", -3),
                  ("x3", ["e", "f"], "Hackers", 1)])], league, POINTS)
    r, _ = _round()
    e.round = r
    opener = e._week_board_facts(r, closing=False)
    assert opener == ["Week 6 so far on Squabbit: 2 of the other 2 teams have posted. Duffers have the best at "
                      "net 3 under par, then Hackers at net 1 over par."]
    assert e._board_standings(r) == ["No league week is final yet this season, so there are no standings to talk about."]
    r.apply_scorecard(Scorecard(holes=list(range(10, 19)), scores={
        "Mike": dict(zip(range(10, 19), [4] * 9)), "Sarah": dict(zip(range(10, 19), [4] * 9))}))
    assert r.team_net("Aces") == (-5, 9)   # par 36, both 36 gross, Mike's 5 strokes all count
    assert e._week_board_facts(r, closing=True) == [
        "By the booth's count Aces finish week 6 at net 5 under par. Against the 2 other teams posted on Squabbit "
        "so far, that would be 1st for the week (Squabbit posts the official result)."]
    # Late in the round, the match is on net.
    r.seq = 9
    stakes = e._net_stakes(r, "Mike", 1)
    assert stakes[:2] == ["1 hole to play.", "Aces are net 5 under par through 9 holes."]
    assert "The best week 6 score posted on Squabbit: Duffers at net 3 under par." in stakes


def test_the_live_tabs_week_board_shows_the_week_and_the_team_on_the_sim(tmp_path):
    league = League(LEAGUE_DATA)
    e = _engine(tmp_path, league)
    assert e._week_panel() is None                        # no round
    r, _ = _round()
    e.round = r
    assert e._week_panel() is None                        # no Squabbit board read
    e.board = Board.from_squabbit(
        [week(6, [("x1", ["a", "b"], "Team 1", None), ("x2", ["c", "d"], "Team 2", -3),
                  ("x3", ["e", "f"], "Hackers", None)])], league, POINTS)
    assert e._week_panel() == {"week": 6, "course": "Test Links", "deadline": "2026-12-06", "holes": 9,
                               "rows": [{"team": "Duffers", "players": ["Tony", "Priya"], "net": -3},
                                        {"team": "Aces", "players": ["Mike", "Sarah"], "net": None, "thru": 0,
                                         "on": True}],
                               "waiting": ["Hackers"]}
    r.apply_scorecard(Scorecard(holes=[10, 11], scores={"Mike": {10: 4, 11: 4}, "Sarah": {10: 4, 11: 4}}))
    assert e._week_panel()["rows"][1] == {"team": "Aces", "players": ["Mike", "Sarah"],
                                          "net": r.team_net("Aces")[0], "thru": 2, "on": True}
    r.ended = True
    assert e._week_panel() is None


def test_the_home_tabs_league_cards_show_this_week_and_the_standings():
    league = League(LEAGUE_DATA)
    items = [week(6, [("x1", ["a", "b"], "Team 1", -4), ("x2", ["c", "d"], "Team 2", -4),
                      ("x3", ["e", "f"], "Hackers", 1)]),
             week(7, [("x1", ["a", "b"], "Team 1", 2), ("x2", ["c", "d"], "Team 2", None),
                      ("x3", ["e", "f"], "Hackers", None)])]
    b = Board.from_squabbit(items, league, POINTS)
    page = b.page("2026-11-01")
    # Week 7 has a post and isn't final: it's this week's.
    assert page["week"] == {"week": 7, "course": "Other", "deadline": "2026-12-06", "final": False,
                            "rows": [{"team": "Aces", "players": ["Mike", "Sarah"], "net": 2}],
                            "waiting": ["Duffers", "Hackers"]}
    assert page["standings"] == {"weeks": 1, "rows": [
        {"team": "Aces", "players": ["Mike", "Sarah"], "points": 65.0, "place": 1},
        {"team": "Duffers", "players": ["Tony", "Priya"], "points": 65.0, "place": 1},
        {"team": "Hackers", "players": ["Lou", "Dee"], "points": 50.0, "place": 3}], "provisional": False}
    # Past week 7's deadline both weeks are final: the latest one shows, with who never posted.
    after = b.page("2026-12-07")["week"]
    assert (after["week"], after["final"], after["waiting"]) == (7, True, ["Duffers", "Hackers"])
    assert Board({}, league, POINTS).page("2026-11-01") == {"week": None, "dates": [], "current": None,
                                                             "standings": None}


def test_the_home_tab_groups_league_weeks_by_due_date_and_keeps_past_ones():
    data = {**LEAGUE_DATA, "schedule": [*LEAGUE_DATA["schedule"],
                                        {"week": 8, "course": "Later", "end_date": "2026-12-13"},
                                        {"week": 9, "course": "Unplayed", "end_date": "2026-12-20"}]}
    league = League(data)
    teams = [("x1", ["a", "b"], "Team 1"), ("x2", ["c", "d"], "Team 2"), ("x3", ["e", "f"], "Hackers")]
    b = Board.from_squabbit([week(6, [(*t, n) for t, n in zip(teams, (-4, -2, None))])], league, POINTS)
    page = b.page("2026-11-01")
    # Weeks 6 and 7 are due the same day: shown together, week 7 with nobody posted yet. Weeks
    # nobody's posted in on their own date aren't.
    assert [(d["date"], [w["week"] for w in d["weeks"]]) for d in page["dates"]] == [("2026-12-06", [6, 7])]
    assert page["dates"][0]["weeks"][1] == {"week": 7, "course": "Other", "deadline": "2026-12-06", "final": False,
                                            "rows": [], "waiting": ["Aces", "Duffers", "Hackers"]}
    assert page["current"] == 0 and page["week"] == page["dates"][0]["weeks"][0]
    b = Board.from_squabbit([week(6, [(*t, n) for t, n in zip(teams, (-4, -2, None))]),
                             week(8, [(*t, n) for t, n in zip(teams, (1, None, None))])], league, POINTS)
    # Week 8 alone on its date. While 6 and 7 are open they're what the page opens on...
    page = b.page("2026-11-01")
    assert [[w["week"] for w in d["weeks"]] for d in page["dates"]] == [[6, 7], [8]]
    assert page["current"] == 0
    # ...and once their deadline's passed, week 8; 6 and 7 stay one arrow back, final.
    page = b.page("2026-12-07")
    assert page["current"] == 1
    assert [(w["final"], w["waiting"]) for w in page["dates"][0]["weeks"]] == [
        (True, ["Hackers"]), (True, ["Aces", "Duffers", "Hackers"])]                # week 7: nobody posted


def test_the_sims_finished_rounds_stand_in_until_a_team_posts_on_squabbit():
    league = League(LEAGUE_DATA)
    b = Board.from_squabbit([week(6, [("x1", ["a", "b"], "Team 1", -4), ("x2", ["c", "d"], "Team 2", None),
                                      ("x3", ["e", "f"], "Hackers", None)])], league, POINTS)
    m = b.with_sim({6: {"Aces": 0, "Duffers": -2}, 7: {"Hackers": 3}})
    # Squabbit's posted number wins; the sim fills in a team that hasn't posted.
    assert [(e.team, e.net, e.sim) for e in m.weeks[6]] == [("Aces", -4, False), ("Duffers", -2, True),
                                                            ("Hackers", None, False)]
    # A week Squabbit has no board for yet: every league team, the sim's in.
    assert [(e.team, e.net, e.sim) for e in m.weeks[7]] == [("Aces", None, False), ("Duffers", None, False),
                                                            ("Hackers", 3, True)]
    page = m.page("2026-12-07")      # both weeks past their deadline: final
    assert page["week"]["rows"][-1] == {"team": "Hackers", "players": ["Lou", "Dee"], "net": 3, "sim": True}
    assert page["standings"]["provisional"] is True
    assert b.page("2026-12-07")["standings"]["provisional"] is False
    assert b.weeks[6][1].net is None                     # the Squabbit board itself is untouched


def test_a_finished_league_round_is_kept_by_week_and_puts_the_league_cards_up(tmp_path):
    import asyncio
    import json

    league = League(LEAGUE_DATA)
    e = _engine(tmp_path, league)
    e.cfg.publish.bucket, e.cfg.publish.prefix = "b", "site/"
    e._league_page_on = True
    puts = []

    class S3:
        def put_object(self, **kw):
            puts.append(kw)
    e._s3 = S3
    r, _ = _round()
    holes = list(range(10, 19))
    r.apply_scorecard(Scorecard(holes=holes, scores={"Mike": {h: 4 for h in holes}, "Sarah": {h: 4 for h in holes}}))
    net = r.team_net("Aces")
    assert net and net[1] == 9

    def row():
        return e.store.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, is_league)"
                                  " VALUES (?, 0, '2026-12-01', 'x', 9, 1)", (e.store.session_id,)).lastrowid
    aces, duffers = row(), row()

    async def go():
        e._record_week_results(r, {"Aces": aces})   # no Squabbit read yet: kept, nothing put up
        assert puts == []
        e.board = Board.from_squabbit([week(6, [("x1", ["a", "b"], "Team 1", None)])], league, POINTS)
        e._publish_league_page()
        # Another result while that's going up: one upload at a time, the newest last.
        e.store.record_week_result(6, "2026-12-06", "Duffers", -1, duffers)
        e._publish_league_page()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not e._league_uploading:
                break
    asyncio.run(go())
    # Another season's week 6 (another deadline) doesn't count.
    e.store.record_week_result(6, "2025-12-06", "Duffers", -9, duffers)
    assert e.store.week_results(LEAGUE_DATA["schedule"]) == {6: {"Aces": net[0], "Duffers": -1}}
    # The sim's hole by hole nets, by the course's hole number, for the recap's story.
    holes = e.store.week_holes(LEAGUE_DATA["schedule"])
    assert set(holes) == {6} and set(holes[6]) == {"Aces"}
    assert sorted(holes[6]["Aces"]) == list(range(10, 19)) and sum(holes[6]["Aces"].values()) == net[0]
    # A round deleted (or excluded, or made practice) on the stats page drops out.
    e.store.db.execute("UPDATE rounds SET excluded = 2 WHERE id = ?", (duffers,))
    e.store.db.execute("UPDATE rounds SET is_league = 0 WHERE id = ?", (aces,))
    assert e.store.week_results(LEAGUE_DATA["schedule"]) == {}
    assert {p["Key"] for p in puts} == {"site/league.json"}
    body = json.loads(puts[-1]["Body"])
    assert {r["team"]: (r["net"], r.get("sim")) for r in body["week"]["rows"]} == {"Aces": (net[0], True),
                                                                                    "Duffers": (-1, True)}


def test_distance_text():
    assert _distance_text(4.0) == "12 feet" and _distance_text(3.0556) == "9 feet 2 inches"
    assert _distance_text(25.04) == "25.0 yards"


def test_standings_facts_from_final_weeks(tmp_path):
    league = League(LEAGUE_DATA)
    e = _engine(tmp_path, league)
    e.board = Board.from_squabbit(
        [week(6, [("x1", ["a", "b"], "Team 1", -2), ("x2", ["c", "d"], "Team 2", -2),
                  ("x3", ["e", "f"], "Hackers", 1)])], league, POINTS)
    r, _ = _round()
    assert e._board_standings(r) == [
        "Season standings after 1 final week (from Squabbit): tied 1st Aces with 65 points, tied 1st Duffers with "
        "65 points, 3rd Hackers with 50 points, of 3 teams."]
    # No points table: the history file's standings stand in.
    e.board.points_table = []
    assert e._board_standings(r) is None


def test_ctp_ranks_on_the_huds_distance_not_the_rounded_feet(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    _tee_shot(e, "Tony", 12, 25.105)
    r, _ = _round()
    r.seq = 3
    e._start_round(r)
    e._ctp_facts(2, Hud(lie="green", to_pin="25.1 yards"), "Mike")   # said as 75 feet 4 inches (25.111 yd)
    assert e._ctp_leader(r, 12)[:2] == ("Mike", "75 feet 4 inches")


def test_ctp_facts_name_the_group_and_where_the_mark_came_from(tmp_path):
    e = _engine(tmp_path, League(LEAGUE_DATA))
    _tee_shot(e, "Tony", 12, 4.0)
    r, _ = _round()
    r.seq = 3
    e._start_round(r)
    e._ctp_facts(2, Hud(lie="green", to_pin="22 feet"), "Mike")
    tee = e._ctp_facts(1, player="Sarah")
    assert "Tony's mark came from an earlier round this week, not this one." in tee
    assert "Earlier on this hole in this round, Mike's tee shot finished on the green, 22 feet from the hole." in tee
    green = e._ctp_facts(2, Hud(lie="green", to_pin="9 feet 2 inches"), "Sarah")
    assert "That's the new closest this week, inside Tony's 12 feet." in green
    assert "Tony's mark came from an earlier round this week, not this one." in green
    assert any("Mike's tee shot finished on the green, 22 feet" in f for f in green)
    long = e._ctp_facts(2, Hud(lie="green", to_pin="25.5 yards"), "Pat")   # the HUD's yards, said in feet
    assert "Pat's tee shot on this closest-to-the-pin hole finished on the green, 76 feet 6 inches from the hole." in long
    # The closer measures tonight's best against the week's earlier rounds.
    closing = " ".join(r.closing_facts([], {}, week_ctp=e._closer_week_ctp(r)))
    assert ("Closest to the pin today on hole 12: Sarah, 9 feet 2 inches from the hole. That's the closest on "
            "this hole this week so far, inside Tony's 12 feet from an earlier round.") in closing


def test_week_gap_fact():
    from commentator.engine import _week_gap_fact
    assert _week_gap_fact("Aces", -3, "Duffers", -4, 2) == (
        "Aces are 1 shot behind Duffers: net 1 under par over the last 2 holes ties them, net 2 under par beats them.")
    assert _week_gap_fact("Aces", -4, "Duffers", -4, 1) == (
        "Aces are level with Duffers: net under par over the last hole puts them ahead, net par keeps it tied.")
    assert _week_gap_fact("Aces", -6, "Duffers", -4, 2) == (
        "Aces are 2 shots ahead of Duffers: net 1 over par or better over the last 2 holes keeps them in front, "
        "net 2 over par ties.")


def test_a_promoted_alternate_round_backfills_tee_finishes_from_the_partners_stroke(tmp_path):
    from commentator.state import ShotRecord

    league = League(LEAGUE_DATA)
    e = _engine(tmp_path, league)
    sched = {**LEAGUE_DATA["schedule"][0], "format": "Alternate"}
    r = Round(league=league, gspro_players=["Mike", "Sarah"], course="Test Links", scheduled_format="alternate",
              holes=9, start_hole=10, schedule=sched, is_league=True)
    sid = _tee_shot(e, "Mike", 12, None, lie=None, fmt="alternate")
    for who, n, lie, pin, shot_id in (("Mike", 1, "tee", "150 yards", sid), ("Sarah", 2, "green", "9 feet", None)):
        e.state.record_shot(ShotRecord(player=who, shot=Shot(club="8I"), hud=Hud(), hole="Hole 12", tags=[],
                                       round_id=r.id, hole_seq=3, shot_number=n, lie_before=lie,
                                       to_pin_before=pin, shot_id=shot_id))
    e._alternate_tee_finishes(r)
    assert e.store.week_ctp(["Test Links"], [12], "2026-01-01") == {12: ("Mike", 3.0)}
