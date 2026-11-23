"""Closest to the pin (shotcast_scoring.ctp)."""

from shotcast_scoring import ctp


def shot(**over):
    return {"shot_number": 1, "is_putt": 0, "mulligan": 0, "finish_yards": 4.5, "lie_after": "green",
            "outcome": None, **over}


def test_only_a_tee_shot_on_the_green_or_holed_counts():
    assert ctp.mark(shot()) == 4.5
    assert ctp.mark(shot(outcome="holed", finish_yards=None, lie_after=None)) == 0.0
    for miss in (shot(shot_number=2), shot(mulligan=1), shot(is_putt=True), shot(lie_after="fringe"),
                 shot(finish_yards=None), shot(shot_number=None)):
        assert ctp.mark(miss) is None, miss


def test_the_closest_on_each_hole_wins_and_the_first_of_two_alike_stands():
    marks = [(3, "Pat", 5.0), (3, "Kim", 2.0), (3, "Lou", 2.0), (7, "Lou", 9.0), (12, "Sam", 1.0)]
    assert ctp.closest(marks, [3, 7, 16]) == {3: ("Kim", 2.0), 7: ("Lou", 9.0)}


def test_hole_numbers_and_distances_in_feet_and_inches():
    assert ctp.hole_number("Hole 12, par 3") == 12
    assert ctp.hole_number("Putting green") is None and ctp.hole_number(None) is None
    assert ctp.text(2.0) == "6 feet" and ctp.text(1.25) == "3 feet 9 inches"
