"""How far GSPro will actually let a shot carry, relative to the launch monitor's number.

Two separate effects, learned separately:

- Course baseline: altitude (a global GSPro setting or the designer's course elevation) and
  conditions change every shot on a course. Learned from tee and fairway shots, which carry
  no lie penalty.
- Lie penalty, relative to that baseline: GSPro doesn't use a flat percentage. It works the
  penalty out from ball speed and launch angle (low, slow shots suffer most), so it's learned
  per lie, per carry band and launch band, starting from measured curves until there's enough data.

What the HUD shows (checked on 3.1.6 gameplay video): CARRY (raw) appears at impact and is
GSPro's carry with no lie penalty (from sand: raw 94.4 m at impact, game 86.2 m on a level green).
CARRY (game) counts up during flight and freezes at touchdown: that one includes the lie
penalty and the landing elevation. So the penalty is learned as game / raw, and predicted
from raw at impact. Like the launch monitor's carry, CARRY (raw) leaves out altitude too (both
read ~1.00 of each other on every course), so the course baseline carries it for either base.
"""

CLEAN_LIES = ("tee", "fairway")
# Surfaces with no lie penalty that never need learning.
NO_PENALTY = ("tee", "fairway", "concrete", "cart path")

# Lie penalty by launch-monitor carry in yards, as (carry, factor) points to interpolate between.
# Measured on the league's own GSPro (340 shots with both carry tiles and a known landing height,
# each course's own carry level taken out): sand keeps ~0.74 at 30 yards up to ~0.90 at 155, so
# greenside bunkers play much shorter than fairway ones; rough keeps ~0.91 and deep rough ~0.72 at
# every distance. Pine straw is a starting guess (too few shots yet). Shots on each course keep
# refining these per carry band.
DEFAULT_CURVES: dict[str, list[tuple[float, float]]] = {
    "sand": [(29, 0.74), (55, 0.79), (109, 0.87), (155, 0.90)],
    "deep rough": [(0, 0.72)],
    "rough": [(0, 0.91)],
    "pine straw": [(0, 0.97)],
}

MIN_SAMPLES = 3
# Exponential average so learned values follow a change in the league's GSPro settings.
WEIGHT = 0.2
CARRY_BANDS = (40, 80, 130, 180)  # yards
VLA_BANDS = (12, 20)  # degrees: low launch is punished harder out of rough


def _interpolate(points: list[tuple[float, float]], x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _band(value: float | None, edges: tuple[float, ...]) -> int | None:
    if value is None:
        return None
    return sum(value >= e for e in edges)


class _Average:
    __slots__ = ("value", "n")

    def __init__(self) -> None:
        self.value, self.n = 0.0, 0

    def add(self, x: float) -> None:
        self.value = x if self.n == 0 else (1 - WEIGHT) * self.value + WEIGHT * x
        self.n += 1


def correct_for_elevation(
    game: float, elevation_yards: float | None, pin_yards: float | None, carry: float,
    rest_rise: float | None = None,
) -> float | None:
    """CARRY (game) as if the ball had come down level with where it was hit, or None when how high
    it came down isn't known (such a shot can't teach the model).

    `rest_rise` is how far above its start the ball came to rest, measured from the HUD: the pin
    doesn't move, so it's this shot's pin elevation less the next shot's. Without it, the pin's own
    height stands in, but only on a shot that landed near the pin: a tee shot's pin says nothing
    about where it came down, and a hilly hole can put that 10 yards above or below the tee.
    """
    if rest_rise is not None:
        return game + rest_rise
    if elevation_yards is not None and pin_yards and pin_yards <= carry * 1.25:
        return game + elevation_yards
    return None


class CarryModel:
    def __init__(
        self,
        defaults: dict[str, float] | None = None,
        history: list[tuple] | None = None,
        course_altitude_ft: dict[str, float] | None = None,
    ) -> None:
        """`defaults` overrides a lie's starting factor with a flat value. `history` is past shots
        as (lie, base carry, GSPro game carry, course, launch angle, base), oldest first, where base
        is "gspro" (the CARRY (raw) tile) or "lm" (launch monitor). `course_altitude_ft` seeds a
        course's launch-monitor baseline before it has shots of its own."""
        self.flat_defaults = defaults or {}
        self.altitude = dict(course_altitude_ft or {})   # a copy: learned ones aren't config
        self.baselines: dict[tuple, _Average] = {}
        self.penalties: dict[tuple, _Average] = {}
        for row in history or []:
            self.observe(*row)

    def baseline(self, course: str | None, base: str = "lm") -> float:
        learned = self.baselines.get((course, base))
        if learned and learned.n >= MIN_SAMPLES:
            return learned.value
        # About 2% more carry per 1,000 ft, for either base: GSPro's CARRY (raw) leaves altitude out
        # like the launch monitor does (both read ~1.00 at Wynn, 2,080 ft, where the game carry ran
        # ~3.7% long). A rule of thumb, not a published GSPro figure.
        return 1 + 0.02 * self.altitude.get(course or "", 0) / 1000

    def penalty(self, lie: str | None, carry: float, vla: float | None = None) -> float:
        if not lie or lie in NO_PENALTY:
            return 1.0
        band = _band(carry, CARRY_BANDS)
        for key in ((lie, band, _band(vla, VLA_BANDS)), (lie, band)):
            learned = self.penalties.get(key)
            if learned and learned.n >= MIN_SAMPLES:
                return learned.value
        if lie in self.flat_defaults:
            return self.flat_defaults[lie]
        if lie in DEFAULT_CURVES:
            return _interpolate(DEFAULT_CURVES[lie], carry)
        return 1.0  # lies we have no data for (pine straw, cart path...) until shots teach us

    def factor(
        self, lie: str | None, carry: float = 0, course: str | None = None, vla: float | None = None, base: str = "lm"
    ) -> float:
        return self.baseline(course, base) * self.penalty(lie, carry, vla)

    def observe(
        self, lie: str | None, base_carry: float | None, game_carry: float | None,
        course: str | None = None, vla: float | None = None, base: str = "lm",
    ) -> None:
        if not lie or not base_carry or not game_carry or base_carry < 10:
            return
        ratio = game_carry / base_carry
        if not 0.3 <= ratio <= 1.3:  # a mis-read tile or a shot that clipped a tree
            return
        if lie in CLEAN_LIES:
            self.baselines.setdefault((course, base), _Average()).add(ratio)
            return
        if lie in NO_PENALTY:
            return
        relative = ratio / self.baseline(course, base)
        band = _band(base_carry, CARRY_BANDS)
        self.penalties.setdefault((lie, band), _Average()).add(relative)
        if vla is not None:
            self.penalties.setdefault((lie, band, _band(vla, VLA_BANDS)), _Average()).add(relative)
