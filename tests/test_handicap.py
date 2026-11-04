import copy
import json

from commentator import handicap as H
from commentator.league import League
from commentator.round import Round

# A Squabbit week as its API returns it: the tournament (format and handicap settings) and its round
# (course card with per-tee par and stroke index, the tee played). Trimmed to what the rules read.
HOLES = [{"par": 0, "handicap": 0, "pars": {"T1": p}, "handicaps": {"T1": si}, "yardages": {"T1": 300 + i}}
         for i, (p, si) in enumerate(zip([4, 5, 3, 4, 4, 3, 4, 5, 4] * 2, [7, 1, 17, 3, 11, 15, 5, 9, 13,
                                                                            8, 2, 18, 4, 12, 16, 6, 10, 14]))]
ROUND = {"numHoles": 9, "startingHole": {"holeNumber": 10}, "teeIds": {"c1": ["T1"]},
         "courses": {"c1": {"name": "Test Links", "tees": [{"id": "T1", "name": "White", "rating": 70.2, "slope": 130}],
                            "holes": HOLES}}}


def tournament(fmt_type, settings, where="main", max_index=99.0, max_hole=None):
    fmt = {"formatType": fmt_type, "maxHandicap": max_index, "maxHoleScore": max_hole,
           "teams": {"tm": {"name": "Team 1", "userIds": ["a", "b"]}}}
    if where == "main":
        fmt["mainGameTemplate"] = {"handicapSettings": settings}
    else:
        fmt["tournamentRoundsGameTemplate"] = {"tr": {"handicapSettings": settings}}
    return {"id": "t1", "name": f"Week 6 - {fmt_type}", "date": "2026-09-28", "formatsV2": {"f": fmt}}


def player(uid, index, scores):
    return {"userId": uid, "tournamentId": "t1", "userHandicap": index, "tee": {"id": "T1", "name": "White",
            "rating": 70.2, "slope": 130}, "holeScoresV2": {str(h): s for h, s in zip(range(10, 19), scores)}}


def test_week_card_reads_the_tee_par_index_and_settings():
    card = H.week_card(tournament("BestBall", {"handicapPercentageForAll": 75.0, "useCourseHandicaps": False}), ROUND)
    assert (card["week"], card["format"], card["holes"], card["start_hole"]) == (6, "best_ball", 9, 10)
    assert card["tee"] == {"name": "White", "rating": 70.2, "slope": 130}
    assert card["par"]["11"] == 5 and card["index"]["11"] == 2 and card["yards"]["11"] == 310
    assert card["handicap"]["allowances"] == [0.75] and not card["handicap"]["use_course_handicap"]


def test_aggregate_with_course_handicaps_uses_slope_rating_and_the_cap():
    # Squabbit's 2025 aggregate: USGA course handicap over nine (half the index, half the rating
    # against the nine's par), index capped at 54. A 56.2 plays as 54.
    card = H.week_card(tournament("TeamSum", {"useCourseHandicaps": True}, where="round", max_index=54.0), ROUND)
    back = list(range(10, 19))
    par = sum(int(card["par"][str(h)]) for h in back)
    assert H.strokes_for(card, [12.0], back, False) == H.round_half_up(6 * 130 / 113 + 35.1 - par)
    assert H.strokes_for(card, [56.2], back, False) == H.strokes_for(card, [54.0], back, False)


def test_one_ball_sums_unrounded_shares_lowest_first():
    card = H.week_card(tournament("Scramble", {"handicapPercentagesPerPlayer": [25.0, 20.0]}), ROUND)
    # Over nine: 4.9 and 11.1 -> 25% of 4.9 + 20% of 11.1 = 3.445 -> 3 (rounding each share first gives 4)
    assert H.strokes_for(card, [22.2, 9.8], list(range(10, 19)), True) == 3


def test_strokes_go_hardest_first_and_plus_handicaps_give_back():
    assert H.allocate(3, [11, 13, 17, 10]) == {11: 1, 13: 1, 17: 1, 10: 0}
    assert H.allocate(-1, [11, 13, 17, 10]) == {11: 0, 13: 0, 17: 0, 10: -1}
    assert H.round_half_up(4.5) == 5 and H.round_half_up(-0.5) == -1


def test_validate_recomputes_team_nets():
    t = tournament("BestBall", {"handicapPercentageForAll": 75.0})
    a = player("a", 12.0, [5, 6, 4, 5, 4, 3, 5, 6, 4])     # 12 -> 6 over nine -> 75% = 4.5 -> 5 strokes
    b = player("b", 2.0, [4, 5, 3, 4, 4, 3, 4, 5, 4])      # 2 -> 1 -> 0.75 -> 1 stroke (on 11, index 2)
    item = {"t": t, "rounds": [ROUND], "lb": {"leaderboards": {"f": {"teamEntries": [
        {"entityId": "tm", "entityName": "Team 1", "total": -1}]}}}}
    # a's strokes land on 11, 13, 16, 10, 17 (index 2, 4, 6, 8, 10), b's on 11. Best nets per hole
    # 4,4,3,4,4,3,4,5,4 = 35 on a par 36.
    [(week, team, ours, theirs)] = H.validate_season([item], [a, b])["best_ball"]
    assert ours == theirs == -1
    # Hole by hole for the recap's story, by the league team the Squabbit team's players are on.
    from commentator.board import squabbit_holes
    from tests.test_board import LEAGUE_DATA
    card = H.week_card(t, ROUND)
    assert squabbit_holes(t, [a, b], card, League(LEAGUE_DATA)) == \
        {"Aces": {10: 0, 11: -1, 12: 0, 13: 0, 14: 0, 15: 0, 16: 0, 17: 0, 18: 0}}


def test_net_max_hole_score_caps_a_blow_up():
    rule = {"doublePar": False, "isGross": False, "amountOverPar": 4}
    card = H.week_card(tournament("TeamSum", {}, where="round", max_index=None, max_hole=rule), ROUND)
    assert H.hole_cap(card, 4, 1) == 9    # net par + 4: gross 9 with a stroke
    assert H.team_net(card, [player("a", 0.0, [12, 5, 3, 4, 4, 3, 4, 5, 4])], "TeamSum") == 4


def test_round_uses_the_week_card(tmp_path):
    card = H.week_card(tournament("BestBall", {"handicapPercentageForAll": 75.0}), ROUND)
    data = {"squabbit_invite": "ABC", "teams": [{"name": "Aces", "players": [
        {"name": "Mike", "handicap": 12.0}, {"name": "Sarah", "handicap": 2.0}]}],
            "schedule": [{"week": 6, "course": "Test Links", "format": "Best Ball", "hole_count": 9, "start_hole": 10}]}
    path = tmp_path / "league.json"
    path.write_text(json.dumps(data))
    (tmp_path / "squabbit_cards.json").write_text(json.dumps({"invite": "ABC", "weeks": [card]}))
    league = League.load(path)
    r = Round(league=league, gspro_players=["Mike", "Sarah"], course="Test Links", scheduled_format="best ball",
              holes=9, start_hole=10, schedule=data["schedule"][0])
    assert r.par[2] == 5 and r.index[2] == 2          # hole 11, from Squabbit's card for the tee
    mike = next(s for s in r.sides if s.label == "Mike")
    assert mike.strokes == 5 and mike.hole_strokes[2] == 1 and mike.hole_strokes[3] == 0
    # Another season's cards are ignored.
    (tmp_path / "squabbit_cards.json").write_text(json.dumps({"invite": "OLD", "weeks": [card]}))
    assert League.load(path).week_card(6) is None
    # A practice round takes no week card.
    p = Round(league=league, gspro_players=["Mike"], course="Test Links", scheduled_format=None, holes=9,
              start_hole=10, schedule=data["schedule"][0], is_league=False)
    assert p.week_card is None and copy.deepcopy(p.par) == {}
