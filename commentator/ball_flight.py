"""GSPro's ball flight, closely enough to say how much a course's altitude changes a shot's carry.

GSPro flies the ball step by step (every 0.01 s): gravity, drag against the flight and lift from the
backspin, both scaling with the air's density. Altitude only changes that density (by the air
pressure: GSPro holds the air at 80 F and 40% humidity), and it works both ways: thin air drags less
but also lifts less. A normal shot carries further up high (a drive ~1.5% per 1,000 ft, an iron
~1.8%), a low-spin drive hardly gains, and a low launch kept up by its spin comes down sooner.

The drag and lift coefficients are this module's own curves, fitted to GSPro's flights: drag by the
spin ratio (the ball's surface speed from its spin over its speed), lift by that and the speed. Fitted
on the league's shots (the curves come from GSPro's output, not its tables), the carry comes out within ~0.3 yd of GSPro's
CARRY (raw), and the altitude factor within 0.75 yd for 95% of shots even at 10,000 ft (a flat
2%-per-1,000-ft rule was 3 yd out at 1,000 ft, 35 yd at 10,000 ft).
"""

import math
from functools import lru_cache

G = 32.174                 # ft/s^2
WEIGHT_LB = 0.10125        # a 45.9 g ball
AREA_FT2 = 0.0153924       # a 1.68 in ball's cross-section
RADIUS_FT = 0.07
TEMP_F = 80.0              # what GSPro flies every shot at
HUMIDITY = 40.0
# GSPro steps every 0.01 s; five times coarser with the touchdown interpolated between steps gives
# the same altitude factor (within 0.05 yd at the 95th percentile) at a fifth of the cost. The first
# FINE_SECONDS go at GSPro's step, so a ball launched barely above level is caught coming down.
DT = 0.05
FINE_DT = 0.01
FINE_SECONDS = 0.1
SPIN_DECAY_STEP = 0.99992  # GSPro decays spin twice: by this every 0.01 s and by e^(-t / SPIN_DECAY_SECONDS)
SPIN_DECAY_SECONDS = 20.21
SCALE_HEIGHT_M = 8434.6    # air pressure falls by e^(-altitude / this)

# Coefficients as C[i][j] * ln(spin ratio)^i * (speed mph / 100)^j, fitted over spin ratios 0.03-1.5.
# Outside that range the ratio is clamped, except that lift fades to nothing below it (no spin, no lift).
_CD = (
    (0.499622, -0.0783366, 0.0209308, 0.0239888),
    (0.249855, -0.412769, 0.313902, -0.0491592),
    (0.158942, -0.451015, 0.369679, -0.0852745),
    (0.0470588, -0.131788, 0.108385, -0.0269363),
)
_CL = (
    (-0.124988, 2.65631, -3.67594, 1.49243),
    (-0.0352726, 1.61307, -3.19596, 1.56536),
    (0.0717779, 0.243494, -0.955456, 0.561212),
    (0.0156953, 0.0052315, -0.101792, 0.0689361),
)
_S_MIN = 0.03
_X_MIN, _X_MAX = math.log(_S_MIN), math.log(1.5)


def air_density(altitude_ft: float) -> float:
    """lb/ft^3 at the altitude, at GSPro's 80 F and 40% humidity."""
    pressure_pa = 101325 * math.exp(-altitude_ft * 0.3048 / SCALE_HEIGHT_M)
    temp_c = (TEMP_F - 32) * 5 / 9
    vapour_pa = 610.78 * 10 ** (7.5 * temp_c / (temp_c + 237.3)) * HUMIDITY / 100
    kg_m3 = (vapour_pa * 0.018016 + pressure_pa * 0.028964) / ((temp_c + 273.15) * 8.314)
    return kg_m3 * 0.062427961


def _poly(c: tuple, x: float, u: float) -> float:
    return sum((((r[3] * u + r[2]) * u + r[1]) * u + r[0]) * x ** i for i, r in enumerate(c))


def carry_yards(speed_mph: float, launch_deg: float, spin_rpm: float, spin_axis_deg: float = 0.0,
                altitude_ft: float = 0.0) -> float | None:
    """The carry in yards (ground level with the start, no lie), or None for a ball that never gets
    up (no speed, launched at or below level)."""
    if not speed_mph > 0 or not launch_deg > 0:
        return None
    rho = air_density(altitude_ft)
    axis = math.radians(-spin_axis_deg)
    s_a, c_a = math.sin(axis), math.cos(axis)
    v = speed_mph * 88 / 60
    launch = math.radians(launch_deg)
    vx, vy, vz = v * math.cos(launch), v * math.sin(launch), 0.0
    x = y = z = 0.0
    spin = max(spin_rpm, 0.0) * 2 * math.pi / 60 * RADIUS_FT   # the ball's surface speed, ft/s
    drag_k = rho * 0.5 * AREA_FT2 / WEIGHT_LB                    # times V^2 and the coefficient: g's

    def accel(vx: float, vy: float, vz: float, t: float) -> tuple[float, float, float]:
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        ratio = spin * math.exp(-t / SPIN_DECAY_SECONDS) / speed
        lx = min(max(math.log(ratio), _X_MIN), _X_MAX) if ratio > 0 else _X_MIN
        u = speed * 60 / 88 / 100
        k = drag_k * speed   # D/(m v) per unit coefficient: the force along each velocity component
        d, lift = _poly(_CD, lx, u) * k, _poly(_CL, lx, u) * k
        if ratio < _S_MIN:
            lift *= max(ratio, 0.0) / _S_MIN
        return ((vz * s_a - vy * c_a) * lift - d * vx,
                -G - (d * vy - lift * vx * c_a),
                -d * vz - lift * vx * s_a)

    t = 0.0
    while t < 24:
        h = FINE_DT if t < FINE_SECONDS - 1e-9 else DT
        half = h / 2
        t += h
        spin *= SPIN_DECAY_STEP ** (h / 0.01)
        a1 = accel(vx, vy, vz, t)
        a2 = accel(vx + a1[0] * half, vy + a1[1] * half, vz + a1[2] * half, t + half)
        a3 = accel(vx + a2[0] * half, vy + a2[1] * half, vz + a2[2] * half, t + half)
        a4 = accel(vx + a3[0] * h, vy + a3[1] * h, vz + a3[2] * h, t + h)
        px, py, pz = x, y, z
        x += (vx * 6 + (a1[0] + a2[0] + a3[0]) * h) * h / 6
        y += (vy * 6 + (a1[1] + a2[1] + a3[1]) * h) * h / 6
        z += (vz * 6 + (a1[2] + a2[2] + a3[2]) * h) * h / 6
        vx += (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0]) * h / 6
        vy += (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1]) * h / 6
        vz += (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2]) * h / 6
        if y <= 0 and vy < 0:
            if py <= 0:   # down within GSPro's first step: never got up
                return None
            f = py / (py - y)   # where between the steps it crossed the ground
            return math.hypot(px + (x - px) * f, pz + (z - pz) * f) / 3
    return None


def altitude_factor(speed_mph: float | None, launch_deg: float | None, spin_rpm: float | None,
                    spin_axis_deg: float | None, altitude_ft: float) -> float | None:
    """How much further (or shorter) the shot carries at the altitude than at sea level, or None
    without the ball numbers or for a ball that never gets up."""
    if not altitude_ft:
        return 1.0
    if speed_mph is None or launch_deg is None or spin_rpm is None:
        return None
    # (A replayed shot is the cache's; the axis's side doesn't change how far it carries.)
    return _factor(speed_mph, launch_deg, spin_rpm, abs(spin_axis_deg or 0.0), altitude_ft)


@lru_cache(maxsize=512)
def _factor(speed: float, launch: float, spin: float, axis: float, altitude: float) -> float | None:
    level, high = carry_yards(speed, launch, spin, axis), carry_yards(speed, launch, spin, axis, altitude)
    return high / level if level and high else None
