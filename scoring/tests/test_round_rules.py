import pytest

from shotcast_scoring import round as R

CARD = {
    "tee": {"rating": 71.0, "slope": 126},
    "par": {str(h): 4 for h in range(1, 19)},
    "handicap": {"use_course_handicap": False, "allowances": None, "max_index": None, "max_hole_score": None},
}


def test_strokes_without_a_card_scale_the_index_to_the_holes_then_take_the_allowance():
    nine = list(range(1, 10))
    assert R.strokes("best_ball", [12.0], nine, [0.75]) == 5          # 6 x 0.75 = 4.5, half up
    assert R.strokes("aggregate", [12.0], nine, [1.0]) == 6
    assert R.strokes("aggregate", [12.0], list(range(1, 19)), [1.0]) == 12
    # One ball: the lowest handicap takes the first share, whatever the order of the players.
    assert R.strokes("scramble", [20.0, 8.0], nine, [0.25, 0.20]) == R.strokes("scramble", [8.0, 20.0], nine, [0.25, 0.20]) == 3
    # A side of its own ball is its first player.
    assert R.strokes("best_ball", [12.0, 30.0], nine, [1.0]) == 6


def test_strokes_are_unknown_without_every_players_index():
    assert R.strokes("best_ball", [], [1, 2, 3], [0.75]) is None
    assert R.strokes("scramble", [8.0, None], [1, 2, 3], [0.25, 0.2]) is None
    assert R.strokes("best_ball", [0.0], [1, 2, 3], [0.75]) == 0


def test_a_card_gives_its_own_allowances_and_the_formats_where_it_sets_none():
    nine = list(range(1, 10))
    assert R.strokes("best_ball", [12.0], nine, [0.75], CARD) == 5
    own = {**CARD, "handicap": {**CARD["handicap"], "allowances": [1.0]}}
    assert R.strokes("best_ball", [12.0], nine, [0.75], own) == 6
    assert CARD["handicap"]["allowances"] is None   # the caller's card is left as it was


def test_a_cards_course_handicap_and_max_index_are_used():
    usga = {**CARD, "handicap": {**CARD["handicap"], "use_course_handicap": True, "max_index": 20.0}}
    nine = list(range(1, 10))
    # 20 (capped from 30) x 9/18 x 126/113 + (35.5 - 36) = 10.65 -> 11, then 100%.
    assert R.strokes("aggregate", [30.0], nine, [1.0], usga) == 11


def test_a_card_missing_a_played_holes_par_is_not_used():
    short = {**CARD, "par": {str(h): 4 for h in range(1, 10)},
             "handicap": {**CARD["handicap"], "allowances": [0.0]}}
    assert R.strokes("aggregate", [12.0], list(range(1, 10)), [1.0], short) == 0
    assert R.strokes("aggregate", [12.0], list(range(10, 19)), [1.0], short) == 6


def test_default_allowances():
    assert R.default_allowances("best_ball") == [0.75] and R.default_allowances("scramble") == [0.25, 0.20]
    assert R.default_allowances(None) == R.default_allowances("stableford") == [1.0]


def test_strokes_fall_on_the_hardest_holes_and_ties_and_unindexed_holes_by_the_order_played():
    assert R.hole_strokes(2, [1, 2, 3], {1: 9, 2: 1, 3: 5}) == {2: 1, 3: 1, 1: 0}
    assert R.hole_strokes(1, [10, 11, 12], {}) == {10: 1, 11: 0, 12: 0}
    assert R.hole_strokes(1, [12, 10, 11], {}) == {12: 1, 10: 0, 11: 0}
    assert R.hole_strokes(4, [1, 2, 3], {1: 9, 2: 1, 3: 5}) == {2: 2, 3: 1, 1: 1}
    assert R.hole_strokes(-1, [1, 2, 3], {1: 9, 2: 1, 3: 5}) == {2: 0, 3: 0, 1: -1}
    assert R.hole_strokes(0, [1, 2], {1: 2, 2: 1}) == {1: 0, 2: 0}


PAR = {1: 4, 2: 3, 3: 5}
HOLES = [1, 2, 3]


def side(players, gross, strokes, on=None):
    return R.SideCard(players, gross, strokes, on or {})


def test_best_ball_takes_the_better_net_and_aggregate_both():
    a = side(["Ann"], {1: 5, 2: 3, 3: 6}, 1, {1: 1})
    b = side(["Bob"], {1: 4, 2: 4, 3: 5}, 0)
    assert R.team_holes("best_ball", [a, b], PAR, HOLES, members=["Ann", "Bob"]) == {1: 0, 2: 0, 3: 0}
    assert R.team_holes("aggregate", [a, b], PAR, HOLES, members=["Bob", "Ann"]) == {1: 0, 2: 1, 3: 1}
    assert R.team_net("aggregate", [a, b], PAR, HOLES) == (2, 3)


def test_a_hole_counts_once_every_side_has_scored_it_and_it_has_a_par():
    a = side(["Ann"], {1: 5, 2: 3, 3: None}, 0)
    b = side(["Bob"], {1: 4, 2: 4}, 0)
    assert R.team_holes("best_ball", [a, b], {1: 4, 2: None, 3: 5}, HOLES) == {1: 0}
    assert R.team_holes("best_ball", [a, b], {1: None}, HOLES) is None
    assert R.team_net("best_ball", [a, b], {1: None}, HOLES) is None


def test_no_net_without_strokes_or_with_strokes_on_no_hole():
    ok = side(["Ann"], {1: 4}, 0)
    assert R.team_holes("aggregate", [ok, side(["Bob"], {1: 4}, None)], PAR, HOLES) is None
    assert R.team_holes("aggregate", [ok, side(["Bob"], {1: 4}, 3)], PAR, HOLES) is None
    assert R.team_holes("aggregate", [ok, side(["Bob"], {1: 4}, 3, {1: 3})], PAR, HOLES) == {1: -3}
    assert R.team_holes(None, [ok], PAR, HOLES) is None
    assert R.team_holes("aggregate", [], PAR, HOLES) is None


def test_a_team_missing_a_partner_has_no_net_except_on_one_ball():
    ann = side(["Ann"], {1: 4}, 0)
    assert R.team_holes("best_ball", [ann], PAR, HOLES, members=["Ann", "Bob"]) is None
    assert R.team_holes("best_ball", [ann], PAR, HOLES, members=[]) == {1: 0}
    assert R.team_holes("scramble", [ann], PAR, HOLES, members=["Ann", "Bob"]) == {1: 0}


def test_a_one_ball_team_played_as_two_balls_sums_them():
    a, b = side(["Ann"], {1: 4}, 0), side(["Bob"], {1: 5}, 0)
    assert R.team_holes("scramble", [a, b], PAR, HOLES) == {1: 1}


def test_the_cards_max_hole_score_caps_a_blow_up():
    capped = {**CARD, "handicap": {**CARD["handicap"], "max_hole_score": {"amountOverPar": 2, "isGross": True}}}
    a = side(["Ann"], {1: 11}, 0)
    assert R.team_holes("aggregate", [a], PAR, HOLES, capped) == {1: 2}
    assert R.team_holes("aggregate", [a], PAR, HOLES) == {1: 7}


def test_holes_may_be_keyed_by_the_courses_numbers():
    par = {10: 4, 11: 3}
    a = side(["Ann"], {10: 5, 11: 3}, 1, R.hole_strokes(1, [10, 11], {10: 3, 11: 1}))
    assert R.team_holes("aggregate", [a], par, [10, 11]) == {10: 1, 11: -1}


def test_a_side_card_is_immutable():
    with pytest.raises(AttributeError):
        side(["Ann"], {}, 0).strokes = 2
