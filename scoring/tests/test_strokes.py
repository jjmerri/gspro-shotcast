import pytest

from shotcast_scoring import handicap as H


def test_strokes_go_hardest_first_and_plus_handicaps_give_back():
    assert H.allocate(3, [11, 13, 17, 10]) == {11: 1, 13: 1, 17: 1, 10: 0}
    assert H.allocate(-1, [11, 13, 17, 10]) == {11: 0, 13: 0, 17: 0, 10: -1}
    assert H.round_half_up(4.5) == 5 and H.round_half_up(-0.5) == -1


def test_a_team_net_counts_a_hole_once_every_side_or_with_partial_sides_any_has_scored():
    par = {1: 4, 2: 3, 3: 5}
    gross = [{1: 5, 2: 3, 3: None}, {1: 4}]
    strokes = [{1: 1}, {}]
    assert H.team_holes("best_ball", gross, strokes, par) == {1: 0}
    assert H.team_holes("aggregate", gross, strokes, par, complete_sides=False) == {1: 0, 2: 0}
    assert H.team_holes("best_ball", gross, strokes, par, complete_sides=False) == {1: 0, 2: 0}


def test_a_team_net_leaves_out_holes_without_a_score_or_a_par():
    assert H.team_holes("aggregate", [{1: 5, 2: 3, 3: None}], [{}], {1: 4, 2: None, 3: 4, 9: 4},
                        holes=[1, 2, 3, 9, 10]) == {1: 1}


def test_a_team_net_refuses_sides_that_dont_line_up_and_a_one_ball_team_of_several():
    for fmt, gross, strokes in [("aggregate", [{1: 4}, {1: 4}], [{}]), ("scramble", [{1: 4}, {1: 4}], [{}, {}]),
                                ("alternate", [{1: 4}, {1: 4}], [{}, {}])]:
        with pytest.raises(ValueError):
            H.team_holes(fmt, gross, strokes, {1: 4})
    assert H.team_holes("scramble", [{1: 4}], [{1: 1}], {1: 4}) == {1: -1}
