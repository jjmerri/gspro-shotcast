"""A full swing's shape (push, pull, draw, fade, hook, slice) and how far it curved.

From the launch monitor: start direction (degrees, positive = right of the target line) and spin axis
(degrees, positive = tilted right, so the ball curves right). Labels are a right-hander's, as the
gapping table's typical miss (season_analytics.miss_label): GSPro's handedness isn't stored per shot.
GSPro's own "offline" yardage would be better than an estimated curve, but it isn't in its API.
"""
from __future__ import annotations

import math

MIN_CARRY_YARDS = 40      # shorter than this (a chip, a pitch) and a curve label means little
START_DEGREES = 2.0       # starting this far either side of the target line is a push or a pull
# Spin axis bands, as miss_label: within 3 degrees no curve, 3-9 a draw or fade, beyond a hook or slice.
CURVE_DEGREES = 3.0
HARD_CURVE_DEGREES = 9.0
# Lateral curve over the flight is roughly this share of carry x sin(spin axis): a 250-yard drive on a
# 10-degree axis bends about 25 yards, a 160-yard iron on 5 degrees about 8. An estimate, for drawing.
CURVE_FACTOR = 0.6
MAX_CURVE_SHARE = 0.25    # never more than a quarter of the carry sideways


def shape(start: float | None, axis: float | None, carry: float | None, putt: bool = False) -> str | None:
    """"Push fade", "Draw", "Straight", ...; None for a putt, a short shot, or missing numbers."""
    if putt or start is None or axis is None or carry is None or carry < MIN_CARRY_YARDS:
        return None
    line = "Push" if start >= START_DEGREES else "Pull" if start <= -START_DEGREES else ""
    curve = ("slice" if axis >= HARD_CURVE_DEGREES else "fade" if axis >= CURVE_DEGREES
             else "hook" if axis <= -HARD_CURVE_DEGREES else "draw" if axis <= -CURVE_DEGREES else "")
    if line and curve:
        return f"{line} {curve}"
    return line or curve.capitalize() or "Straight"


def curve_yards(axis: float | None, carry: float | None) -> float | None:
    """How far the ball bent sideways off its start line, yards (positive = to the right)."""
    if axis is None or not carry or carry <= 0:
        return None
    c = CURVE_FACTOR * carry * math.sin(math.radians(axis))
    cap = MAX_CURVE_SHARE * carry
    return max(-cap, min(cap, c))


def bend_yards(axis: float | None, carry: float | None) -> float | None:
    """How far the flight bows off the straight line from where it started to where it finished, at
    its middle, yards (positive = to the right, looking down the shot). A ball curving right starts
    left of that line and comes back to it, so it bows left: a quarter of the curve, the other way
    (a parabola's sagitta off its chord)."""
    c = curve_yards(axis, carry)
    return None if c is None else -c / 4
