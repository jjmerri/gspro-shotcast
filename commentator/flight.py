"""A rough ball-flight model: where a shot was (height and distance out) some seconds into its flight.

Used to say what a ball hit when GSPro's log reports it went into foliage: GSPro logs only the time of
the hit ("Leaves hitTime"), never the object, so the height there tells a tree from a bush. Drag and
lift with a spin-dependent lift coefficient, integrated in the vertical plane of the shot; the drag is
then scaled so the flight carries what the launch monitor said it would. Level ground is assumed, so
the height is above the ball's starting point, not the ground under the hit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_MASS = 0.04593          # kg
_RADIUS = 0.02134        # m
_AREA = math.pi * _RADIUS ** 2
_RHO = 1.2               # kg/m^3, sea level
_G = 9.81
_MPH = 0.44704           # m/s per mph
_DT = 0.01
_SPIN_DECAY = 0.04       # fraction of spin lost per second
M_TO_YD = 1.0936133
M_TO_FT = 3.2808399


@dataclass
class FlightPoint:
    height_ft: float     # above the launch point (negative: below it, e.g. landed downhill)
    out_yd: float        # distance travelled along the ground from the launch point
    landed: bool         # the time is past the model's touchdown (the ball was on the way down to, or on, the ground)


def _fly(speed_ms: float, launch_deg: float, backspin_rpm: float, drag_scale: float,
         until: float | None = None) -> tuple[float, float, float, bool]:
    """(x, y, time) at `until` seconds, or at touchdown (y back to 0) if that's sooner or no time is given."""
    a = math.radians(launch_deg)
    vx, vy = speed_ms * math.cos(a), speed_ms * math.sin(a)
    omega = backspin_rpm * 2 * math.pi / 60
    x = y = t = 0.0
    k = 0.5 * _RHO * _AREA / _MASS
    while True:
        if until is not None and t >= until:
            return x, y, t, False
        v = math.hypot(vx, vy)
        if v < 0.1:
            return x, y, t, True
        s = _RADIUS * omega / v
        cd = (0.24 + 0.18 * s) * drag_scale
        cl = min(0.4, 0.45 * s ** 0.4) if s > 0 else 0.0
        ax = -k * v * (cd * vx + cl * vy)
        ay = -_G + k * v * (cl * vx - cd * vy)
        vx, vy = vx + ax * _DT, vy + ay * _DT
        x, y, t = x + vx * _DT, y + vy * _DT, t + _DT
        omega *= 1 - _SPIN_DECAY * _DT
        if y < 0 and vy < 0:
            return x, 0.0, t, True
        if t > 20:
            return x, y, t, True


def point_at(ball_mph: float | None, launch_deg: float | None, spin_rpm: float | None, spin_axis: float | None,
             carry_yd: float | None, t: float) -> FlightPoint | None:
    """Where the ball was `t` seconds after launch, or None without the launch numbers to model it."""
    if not ball_mph or ball_mph <= 0 or launch_deg is None or t < 0:
        return None
    speed = ball_mph * _MPH
    backspin = max(0.0, (spin_rpm or 0.0) * math.cos(math.radians(spin_axis or 0.0)))
    scale = 1.0
    if carry_yd and carry_yd > 5:
        # Fit the drag so the modelled carry matches the monitor's: bisection over a sane range.
        lo, hi = 0.3, 3.0
        for _ in range(18):
            mid = (lo + hi) / 2
            if _fly(speed, launch_deg, backspin, mid)[0] * M_TO_YD > carry_yd:
                lo = mid
            else:
                hi = mid
        scale = (lo + hi) / 2
    x, y, _, landed = _fly(speed, launch_deg, backspin, scale, until=t)
    return FlightPoint(height_ft=y * M_TO_FT, out_yd=x * M_TO_YD, landed=landed)
