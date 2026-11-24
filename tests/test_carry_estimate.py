import pytest

from commentator import carry_estimate
from commentator.models import Shot


def test_a_shot_gets_gsprolike_carry_and_a_near_flat_launch_none():
    est = lambda **kw: carry_estimate.estimate(Shot(**{"ball_speed": 120.0, "vla": 16.5, "total_spin": 6500.0,
                                                      "spin_axis": -4.0, "units": "Yards", **kw}))
    assert 160 <= est() <= 175   # a 7-iron
    assert est(spin_axis=4.0) == pytest.approx(est(), abs=0.01)   # a draw or a fade, the same size
    assert est(spin_axis=-40.0) < est()   # a big hook: flown, its lift partly sideways
    assert 0 < est(vla=3.0) < est()       # thin, still flown
    assert est(vla=1.5) is None           # near flat: GSPro carries it further than its flight
    assert est(total_spin=None) is None and est(spin_axis=None) is None
    assert est(total_spin=-1.0) is None and est(spin_axis=float("inf")) is None
    assert est(units="Metres") is None
    assert est(putter=True) is None
