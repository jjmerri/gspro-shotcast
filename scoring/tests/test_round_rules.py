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


def test_a_card_with_one_ball_takes_every_players_index_and_with_own_balls_the_first():
    nine = list(range(1, 10))
    shares = {**CARD, "handicap": {**CARD["handicap"], "allowances": [0.25, 0.20]}}
    # 4 x 0.25 + 10 x 0.20 = 3, the lowest handicap first whatever the order.
    assert R.strokes("scramble", [20.0, 8.0], nine, [1.0], shares) == 3
    assert R.strokes("alternate", [8.0, 20.0], nine, [1.0], shares) == 3
    assert R.strokes("best_ball", [12.0, 30.0], nine, [1.0], CARD) == 6
    assert R.strokes("best_ball", [12.0, None], nine, [1.0], CARD) is None
    empty = {**CARD, "handicap": {**CARD["handicap"], "allowances": []}}
    assert R.strokes("best_ball", [12.0], nine, [0.75], empty) == 5


def test_a_course_handicap_is_a_whole_number_before_the_percentage():
    usga = {**CARD, "handicap": {**CARD["handicap"], "use_course_handicap": True}}
    # 3 x 0.5 x 126/113 + (35.5 - 36) = 1.17 -> 1, x 0.5 = 0.5 -> 1 (the percentage first would give 0.59 -> 1 too);
    # 13 x 0.5 x 126/113 - 0.5 = 6.75 -> 7, x 0.5 = 3.5 -> 4 (the percentage first: 3.37 -> 3).
    assert R.strokes("best_ball", [13.0], list(range(1, 10)), [0.5], usga) == 4


def test_a_plus_handicap_gives_strokes_back():
    assert R.strokes("aggregate", [-3.0], list(range(1, 10)), [1.0]) == -2      # -1.5 rounds away from zero
    assert R.strokes("aggregate", [-3.0], list(range(1, 19)), [1.0], CARD) == -3
    assert R.hole_strokes(-2, [1, 2, 3, 4], {2: 1, 3: 2}) == {2: 0, 3: 0, 1: -1, 4: -1}


def test_default_allowances():
    mine = R.default_allowances("best_ball")
    mine[0] = 1.0
    assert R.default_allowances("best_ball") == [0.75]
    assert R.default_allowances("best_ball") == [0.75] and R.default_allowances("scramble") == [0.25, 0.20]
    assert R.default_allowances(None) == R.default_allowances("stableford") == [1.0]


def test_strokes_fall_on_the_hardest_holes_and_ties_and_unindexed_holes_by_the_order_played():
    assert R.hole_strokes(2, [1, 2, 3], {1: 9, 2: 1, 3: 5}) == {2: 1, 3: 1, 1: 0}
    assert R.hole_strokes(1, [10, 11, 12], {}) == {10: 1, 11: 0, 12: 0}
    assert R.hole_strokes(1, [12, 10, 11], {}) == {12: 1, 10: 0, 11: 0}
    assert R.hole_strokes(4, [1, 2, 3], {1: 9, 2: 1, 3: 5}) == {2: 2, 3: 1, 1: 1}
    assert R.hole_strokes(-1, [1, 2, 3], {1: 9, 2: 1, 3: 5}) == {2: 0, 3: 0, 1: -1}
    assert R.hole_strokes(0, [1, 2], {1: 2, 2: 1}) == {1: 0, 2: 0}
    # Some holes indexed: those first, then the rest as played; a tie between indexed holes as played.
    assert R.hole_strokes(3, [14, 15, 16, 17], {16: 2, 15: 2}) == {15: 1, 16: 1, 14: 1, 17: 0}
    assert R.hole_strokes(-1, [18, 1], {}) == {18: 0, 1: -1}


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
    assert R.team_holes("alternate", [a, b], PAR, HOLES, members=["Ann", "Bob", "Cy"]) == {1: 1}


def test_a_hole_one_side_has_no_entry_for_is_left_out_in_any_format():
    a, b = side(["Ann"], {1: 4, 2: 3}, 0), side(["Bob"], {1: 5}, 0)
    assert R.team_holes("aggregate", [a, b], PAR, HOLES) == {1: 1}


def test_the_cards_max_hole_score_caps_a_blow_up():
    capped = {**CARD, "handicap": {**CARD["handicap"], "max_hole_score": {"amountOverPar": 2, "isGross": True}}}
    a = side(["Ann"], {1: 11}, 0)
    assert R.team_holes("aggregate", [a], PAR, HOLES, capped) == {1: 2}
    assert R.team_holes("aggregate", [a], PAR, HOLES) == {1: 7}
    # A net cap moves with the strokes on the hole: par 4 + 2 + 1 stroke = 7 counts, less the stroke, to par.
    net = {**CARD, "handicap": {**CARD["handicap"], "max_hole_score": {"amountOverPar": 2, "isGross": False}}}
    assert R.team_holes("aggregate", [side(["Ann"], {1: 11}, 1, {1: 1})], PAR, HOLES, net) == {1: 2}
    double = {**CARD, "handicap": {**CARD["handicap"], "max_hole_score": {"doublePar": True, "isGross": True}}}
    assert R.team_holes("aggregate", [side(["Ann"], {1: 11}, 1, {1: 1})], PAR, HOLES, double) == {1: 3}


def test_holes_may_be_keyed_by_the_courses_numbers():
    par = {10: 4, 11: 3}
    a = side(["Ann"], {10: 5, 11: 3}, 1, R.hole_strokes(1, [10, 11], {10: 3, 11: 1}))
    assert R.team_holes("aggregate", [a], par, [10, 11]) == {10: 1, 11: -1}


def test_a_side_card_is_immutable():
    with pytest.raises(AttributeError):
        side(["Ann"], {}, 0).strokes = 2


def test_a_leagues_name_for_a_format():
    assert R.normalize_format("Team Sum") == R.normalize_format("aggregate") == "aggregate"
    assert R.normalize_format("Alt Shot") == R.normalize_format("FOURSOMES") == "alternate"
    assert R.normalize_format("best-ball") == "best_ball" and R.normalize_format("2 Man Scramble") is None
    assert R.normalize_format("Two-Man Scramble") == "scramble"
    assert R.normalize_format(None) is R.normalize_format("") is R.normalize_format("stableford") is None
    assert R.normalize_format(4) is None


def test_a_leagues_own_allowances_win_over_the_defaults():
    custom = {"Best Ball": [0.9], "scramble": "half", "Alt Shot": [1, 0]}
    assert R.allowances("best_ball", custom) == [0.9]
    assert R.allowances("scramble", custom) == [0.25, 0.20]        # not a list: the default
    assert R.allowances("alternate", custom) == [1.0, 0.0]
    assert R.allowances("aggregate", custom) == R.allowances("aggregate") == R.allowances("aggregate", None) == [1.0]
    assert R.allowances(None, {"nonsense": [0.5]}) == [0.5]        # as the app has always matched them
    # The first list naming the format wins, even an empty one; one that isn't a list is passed over.
    assert R.allowances("aggregate", {"sum": "skip", "Team Sum": ["0.4"], "total": [0.8]}) == [0.4]
    assert R.allowances("scramble", {"scramble": [], "Two Man Scramble": [0.2]}) == []
    assert R.allowances("scramble", {}) == [0.25, 0.20] and R.allowances("", {"nonsense": [0.5]}) == [1.0]
    with pytest.raises(ValueError):
        R.allowances("scramble", {"scramble": ["bad"]})


def test_a_format_that_is_not_text_is_read_as_its_text():
    # As the app's league file has always been read: a caller with untrusted input checks for a string first.
    assert R.normalize_format(["Alt Shot"]) == "alternate" and R.normalize_format([]) is None
