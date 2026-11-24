"""A full swing's shape and how far it curved: the shared copy is shotcast_scoring.shot_shape (the
cloud draws replays with it too); this module keeps the app's old import path."""
from shotcast_scoring.shot_shape import (  # noqa: F401
    CURVE_DEGREES,
    CURVE_FACTOR,
    HARD_CURVE_DEGREES,
    MAX_CURVE_SHARE,
    MIN_CARRY_YARDS,
    START_DEGREES,
    bend_yards,
    curve_yards,
    lefty,
    shape,
)
