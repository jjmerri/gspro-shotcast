import pytest

from commentator import carry_estimate
from commentator.carry_estimate import Model
from commentator.models import Shot


def test_a_normal_swing_gets_gsprolike_carry_and_a_mishit_none():
    est = lambda **kw: carry_estimate.estimate(Shot(**{"ball_speed": 120.0, "vla": 16.5, "total_spin": 6500.0,
                                                      "spin_axis": -4.0, "units": "Yards", **kw}))
    assert 145 <= est() <= 185   # a 7-iron
    assert est(spin_axis=4.0) == pytest.approx(est(), abs=0.01)   # a draw or a fade, the same size
    assert est(vla=3.0) is None            # topped
    assert est(spin_axis=-40.0) is None    # a big hook
    assert est(ball_speed=190.0) is None   # faster than anything learned from
    assert est(total_spin=None) is None
    assert est(units="Metres") is None
    assert est(putter=True) is None


def test_the_trees_are_walked_left_at_or_below_a_split():
    # one tree: ball speed <= 100 -> +10, else +20; the guard keeps launch 6-60, axis 0-35
    model = Model({"features": ["ball_speed", "launch_angle", "total_spin", "spin_axis"], "base": 100.0,
                   "guard": {"ball_speed": [20, 160], "launch_angle": [6, 60], "total_spin": [500, 11000],
                             "spin_axis": [0, 35]},
                   "trees": [{"feature": [0, -1, -1], "threshold": [100.0, 0, 0], "left": [1, 0, 0],
                              "right": [2, 0, 0], "value": [0, 10.0, 20.0]}]})
    assert model.estimate(100.0, 15.0, 3000.0, -10.0) == 110.0
    assert model.estimate(100.5, 15.0, 3000.0, 10.0) == 120.0
    assert model.estimate(100.0, 5.9, 3000.0, 0.0) is None
