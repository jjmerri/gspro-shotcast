"""Closest to the pin: which strokes count and who is closest on each hole. The app (the booth's own
week) and Shotcast Cloud (a league's results) both run this, so they name the same winner."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


def hole_number(hole: str | None) -> int | None:
    """The hole's number from GSPro's name for it ("Hole 3, par 3")."""
    m = re.match(r"Hole (\d+)\b", hole or "")
    return int(m.group(1)) if m else None


def mark(shot: Mapping[str, Any]) -> float | None:
    """How far from the hole (yards) a stroke finished, if it counts for closest to the pin: a tee
    shot, not a mulligan or a putt, that finished on the green or in the hole. Else None. An ace may
    have no logged finish (one GSPro profile's shared ball has no log index): it is 0."""
    if shot.get("shot_number") != 1 or shot.get("mulligan") or shot.get("is_putt"):
        return None
    if shot.get("outcome") == "holed":
        return 0.0
    finish = shot.get("finish_yards")
    return float(finish) if finish is not None and shot.get("lie_after") == "green" else None


def closest(marks: Iterable[tuple[int, str, float]], holes: Iterable[int]) -> dict[int, tuple[str, float]]:
    """The closest mark on each of `holes`: hole -> (who, yards). `marks`: (hole, who, yards) in the
    order hit; of two the same distance the first stands."""
    wanted = set(holes)
    best: dict[int, tuple[str, float]] = {}
    for hole, who, yards in marks:
        if hole in wanted and (hole not in best or yards < best[hole][1]):
            best[hole] = (who, yards)
    return best


def text(yards: float) -> str:
    """A closest-to-the-pin distance: always feet and inches, however far."""
    feet, inches = divmod(round(yards * 36), 12)
    return f"{feet} feet {inches} inches" if inches else f"{feet} feet"
