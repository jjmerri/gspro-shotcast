"""GSPro's carry, estimated from the ball numbers, for a shot whose monitor sends none.

The mid-flight call projects where the ball comes down from the carry at impact. Some monitors leave
the carry to GSPro (springbok's connector sends 0, as do other bridges into GSPro's Open Connect API),
so their shots had no mid-flight call. GSPro works its carry out from the ball speed, launch angle and
spin, whatever monitor sent them, so it's learned from the league's own shots: GSPro's no-penalty
carry (the CARRY (raw) tile) against the numbers it came from. Like a monitor's carry, it leaves out
the lie and the course, which the carry model applies on top.

Held-out rounds (tools/train_carry_estimate.py prints it): a typical miss of about a yard, and 99% of
50+ yard shots within the flight call's own tolerance. Only a normal full swing gets an estimate: a
top or thin shot (launch under 6 degrees), a big hook or slice (spin axis past 35 degrees), or numbers
outside what it learned from get None, and no mid-flight call, rather than a confident wrong one.

It's never stored as the monitor's carry: the carry model learns from measured carries only.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

FILE = Path(__file__).with_name("carry_estimate.json")


class Model:
    def __init__(self, data: dict) -> None:
        self.features: list[str] = data["features"]
        self.base: float = data["base"]
        self.guard: dict[str, list[float]] = data["guard"]
        self.trees = [(t["feature"], t["threshold"], t["left"], t["right"], t["value"]) for t in data["trees"]]

    def predict(self, *x: float) -> float:
        total = self.base
        for feature, threshold, left, right, value in self.trees:
            i = 0
            while (f := feature[i]) >= 0:
                i = left[i] if x[f] <= threshold[i] else right[i]
            total += value[i]
        return total

    def estimate(self, ball_speed: float | None, launch_angle: float | None, total_spin: float | None,
                 spin_axis: float | None) -> float | None:
        x = (ball_speed, launch_angle, total_spin, None if spin_axis is None else abs(spin_axis))
        if any(v is None for v in x):
            return None
        for name, v in zip(self.features, x):
            lo, hi = self.guard[name]
            if not lo <= v <= hi:
                return None
        return self.predict(*x)


@lru_cache(maxsize=1)
def _model() -> Model | None:
    try:
        return Model(json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("Carry estimate unavailable (%s): shots with no carry get no mid-flight call", e)
        return None


def estimate(shot) -> float | None:
    """GSPro's no-penalty carry in yards for a normal full swing in yards, else None."""
    if shot.is_putt or not (shot.units or "yards").lower().startswith("y") or (model := _model()) is None:
        return None
    return model.estimate(shot.ball_speed, shot.vla, shot.total_spin, shot.spin_axis)
