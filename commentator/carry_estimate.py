"""GSPro's carry, estimated from the ball numbers, for a shot whose monitor sends none.

The mid-flight call projects where the ball comes down from the carry at impact. Some monitors leave
the carry to GSPro (springbok's connector sends 0, as do other bridges into GSPro's Open Connect API),
so their shots had no mid-flight call. GSPro works its carry out from the ball speed, launch angle and
spin, whatever monitor sent them, so the shot is flown as GSPro flies it (ball_flight). Like a
monitor's carry, it leaves out the lie and the course, which the carry model applies on top.

On the league's shots against GSPro's no-penalty carry (the CARRY (raw) tile): a typical miss of 0.3
yd, 95% within 1.3 yd, for any spin axis. A ball launched under MIN_LAUNCH degrees gets None, and no
mid-flight call: GSPro carried those up to 40 yd further than its flight alone does.

It's never stored as the monitor's carry: the carry model learns from measured carries only.
"""

import math

from . import ball_flight

MIN_LAUNCH = 2.0


def estimate(shot) -> float | None:
    """GSPro's no-penalty carry in yards, or None for a putt, a ball launched under MIN_LAUNCH degrees
    or missing (or impossible) numbers."""
    if shot.is_putt or not (shot.units or "yards").lower().startswith("y"):
        return None
    ball = (shot.ball_speed, shot.vla, shot.total_spin, shot.spin_axis)
    if any(v is None or not math.isfinite(v) for v in ball) or shot.vla < MIN_LAUNCH or shot.total_spin < 0:
        return None
    return ball_flight.carry_yards(*ball)
