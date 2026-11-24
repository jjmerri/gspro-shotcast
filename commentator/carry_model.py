"""How far GSPro will actually let a shot carry, relative to the launch monitor's number.

Two separate effects, from what GSPro's code does and what the league's shots showed (nothing is
learned while playing: each shot is checked against the prediction instead, and one well off it is
logged, see CarryModel.check):

- Course baseline: altitude (a global GSPro setting or the designer's course elevation) changes
  every shot on a course. GSPro's flight library takes it, so it's a rule of thumb here.
- Lie penalty, relative to that baseline: GSPro doesn't use a flat percentage. It works the
  penalty out from ball speed and launch angle (low, slow shots suffer most), so it's a curve per
  lie by carry, scaled by launch angle.

What the HUD shows (checked on 3.1.6 gameplay video): CARRY (raw) appears at impact and is
GSPro's carry with no lie penalty (from sand: raw 94.4 m at impact, game 86.2 m on a level green).
CARRY (game) counts up during flight and freezes at touchdown: that one includes the lie
penalty and the landing elevation. So the penalty is game / raw, predicted from raw at impact.
Like the launch monitor's carry, CARRY (raw) leaves out altitude too (both read ~1.00 of each other
on every course), so the course baseline carries it for either base.
"""

# Surfaces with no lie penalty.
NO_PENALTY = ("tee", "fairway", "concrete", "cart path")

# Lie penalty by launch-monitor carry in yards, as (carry, factor) points to interpolate between.
# Measured on the league's own GSPro (shots with both carry tiles and a known landing height, each
# course's own carry level taken out): sand keeps ~0.74 at 30 yards up to ~0.90 at 155, so greenside
# bunkers play much shorter than fairway ones. Rough and deep rough are the typical full swing's
# (launched 20 degrees or more): rough keeps ~0.93 and deep rough ~0.69 at every distance. GSPro
# slows a ball off dirt, leaves and pine straw as it does off rough (pine straw measured ~0.92), and
# off semi rough by only ~1.5% of its speed (measured ~0.98).
DEFAULT_CURVES: dict[str, list[tuple[float, float]]] = {
    "sand": [(29, 0.74), (55, 0.79), (109, 0.87), (155, 0.90)],
    "deep rough": [(0, 0.69)],
    "rough": [(0, 0.93)],
    "pine straw": [(0, 0.93)],
    "dirt": [(0, 0.93)],
    "leaves": [(0, 0.93)],
    "semi rough": [(0, 0.98)],
}

# Out of rough-like lies GSPro also takes more ball speed off a low launch (the launch monitor's
# launch angle), and a slower low ball loses far more carry than speed: the share of the default's
# carry kept by launch degrees, measured on the league's shots (480 from rough, 330 from deep rough).
# Rough keeps ~0.83 of it at 5 degrees; deep rough ~0.3 at 0 and ~0.68 at 10. At or above
# LAUNCH_REFERENCE the default applies unchanged.
LAUNCH_REFERENCE = 20.0
_ROUGH_LAUNCH = [(5, 0.83), (10, 0.94), (15, 1.0)]
LAUNCH_CURVES: dict[str, list[tuple[float, float]]] = {
    "rough": _ROUGH_LAUNCH, "pine straw": _ROUGH_LAUNCH, "dirt": _ROUGH_LAUNCH, "leaves": _ROUGH_LAUNCH,
    "deep rough": [(0, 0.30), (10, 0.68), (15, 0.86), (20, 1.0)],
}

def _interpolate(points: list[tuple[float, float]], x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _launch_share(lie: str, vla: float | None) -> float:
    curve = LAUNCH_CURVES.get(lie)
    if not curve or vla is None:
        return 1.0
    return min(1.0, _interpolate(curve, vla) / _interpolate(curve, LAUNCH_REFERENCE))


def correct_for_elevation(
    game: float, elevation_yards: float | None, pin_yards: float | None, rest_rise: float | None = None,
) -> float | None:
    """CARRY (game) as if the ball had come down level with where it was hit, or None when how high
    it came down isn't known (such a shot can't be checked).

    `rest_rise` is how far above its start the ball came to rest, measured from the HUD: the pin
    doesn't move, so it's this shot's pin elevation less the next shot's. Without it, the pin's own
    height stands in, but only on a shot that came down near the pin (within a quarter of its carry):
    a tee shot's pin says nothing about where it came down, and a hilly hole can put that 10 yards
    above or below the tee.
    """
    if rest_rise is not None:
        return game + rest_rise
    if elevation_yards is not None and pin_yards and abs(pin_yards - game) <= game * 0.25:
        return game + elevation_yards
    return None


# A shot whose level carry is further than this share off the prediction is logged (CarryModel.check).
CHECK_TOLERANCE = 0.05


class CarryModel:
    def __init__(self, course_altitude_ft: dict[str, float] | None = None) -> None:
        """`course_altitude_ft`: each course's altitude in feet, for its baseline (the round start and
        GSPro's log add the courses played)."""
        self.altitude = dict(course_altitude_ft or {})   # a copy: those added in play aren't config

    def baseline(self, course: str | None, base: str = "lm") -> float:
        # About 2% more carry per 1,000 ft, for either base: GSPro's CARRY (raw) leaves altitude out
        # like the launch monitor does (both read ~1.00 at Wynn, 2,080 ft, where the game carry ran
        # ~3.7% long). A rule of thumb, not a published GSPro figure. `base` is "gspro" (the CARRY
        # (raw) tile) or "lm" (launch monitor); both take it the same way.
        return 1 + 0.02 * self.altitude.get(course or "", 0) / 1000

    def penalty(self, lie: str | None, carry: float, vla: float | None = None) -> float:
        if not lie or lie in NO_PENALTY:
            return 1.0
        if lie in DEFAULT_CURVES:
            return _interpolate(DEFAULT_CURVES[lie], carry) * _launch_share(lie, vla)
        return 1.0  # lies with no measured penalty (stone, structure...): a check logs one that's off

    def factor(
        self, lie: str | None, carry: float = 0, course: str | None = None, vla: float | None = None, base: str = "lm"
    ) -> float:
        return self.baseline(course, base) * self.penalty(lie, carry, vla)

    def check(
        self, lie: str | None, base_carry: float | None, game_carry: float | None,
        course: str | None = None, vla: float | None = None, base: str = "lm",
    ) -> str | None:
        """A log line when GSPro's carry (put level with where the ball was hit) is more than
        CHECK_TOLERANCE off the prediction from `base_carry`, else None."""
        if not base_carry or game_carry is None or base_carry < 10:
            return None
        factor = self.factor(lie, base_carry, course, vla, base)
        predicted = base_carry * factor
        if not predicted > 0 or abs(game_carry - predicted) <= predicted * CHECK_TOLERANCE + 1e-9:
            return None   # (no prediction: an altitude far below sea level)
        off = game_carry / predicted - 1
        launch = "unknown" if vla is None else f"{vla:.1f} deg"
        return (f"carry check: GSPro carried {game_carry:.0f} yd level from {lie or 'an unknown lie'}, "
                f"predicted {predicted:.0f} yd ({base_carry:.0f} yd {'GSPro raw' if base == 'gspro' else 'launch monitor'}"
                f" x {factor:.3f}), {off:+.0%} off [launch {launch}, course {course or 'unknown'}]")
