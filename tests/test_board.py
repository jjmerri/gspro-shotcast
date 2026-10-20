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
    _tee_shot(e, "Priya", 12, 2.5, lie="fringe")           # off the green: doesn't count
    _tee_shot(e, "Lou", 12, 1.0, league=False)              # practice: doesn't count
    _tee_shot(e, "Dee", 12, 0.5, fmt="scramble")            # team ball: finish unknown
    _tee_shot(e, "Mike", 12, 3.0, course="Elsewhere")       # another course
    _tee_shot(e, "Sarah", 14, 1.0)                          # not a CTP hole
    assert e.store.week_ctp(["Test Links"], [12, 15], "2026-01-01") == {12: ("Tony", 4.0)}
    r, _ = _round()
    e.round = r
    facts = e._week_ctp_facts(r)
    assert facts == ["Closest to the pin on hole 12 this week so far: Tony, 12 feet from the hole.",
                     "Nobody has a tee shot on the green on hole 15 this week yet."]


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
