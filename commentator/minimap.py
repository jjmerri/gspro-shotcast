"""The pre-shot hazard read from GSPro's minimap, by TypeSafe's Jev (minimap.provider: jev).

Jev answers typed questions about an image (yes/no probabilities, multiple choice, boxes) in a few
hundred milliseconds, where a vision LLM takes seconds. It is good at "is there water in this
picture?" and poor at judging left from right, so the geometry is done here in code:

1. Jev boxes the ball (a coloured dot, usually red) and the pin (white dot) on the minimap.
2. The map is split along the ball-to-pin line into a left and a right image, the other side greyed.
3. Jev says what each half holds (water, bunkers), and reads the yardage
   numbers GSPro prints on the map (carry over water, front and back of the green) as two choices:
   the hundreds digit and the last two digits.
4. GSPro doesn't number bunkers, but the map is drawn to scale: the ball-to-pin pixel distance over
   the yards to the pin gives yards per pixel. Code finds sand-coloured patches near the line of
   play, Jev confirms each one is a bunker (it can't box them reliably itself, but it's good at
   "is this patch sand?"), and code measures how far out each starts and ends.
5. Trees the same way: code finds leafy, textured patches near the line, Jev confirms each is a tree,
   and a tree the line to the pin runs straight through, close to the ball, is a stymie.
6. Penalty-area (red) and out-of-bounds (white) lines are drawn in flat colours, so code finds them
   and measures how far off the line of play they run. A red line's hazard is on its side without
   grass, which on a dogleg can be the side the straight line to the pin runs over.
7. On an approach the green is big enough on the map for Jev to say which side the pin is on; the
   front/back comes from GSPro's own front and back numbers. On a long shot the ball-to-pin line is
   drawn on the map and Jev says whether the fairway bows off it: a dogleg.

Only answers at or above `threshold` become facts, so a doubtful read stays silent rather than wrong.
"""

import asyncio
import io
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .config import MinimapConfig

if TYPE_CHECKING:
    import numpy as np
from .flight_pick import Hazard

log = logging.getLogger(__name__)

IMAGES_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}
# Jev's image limits: each side 256..1568 px.
MIN_SIDE, MAX_SIDE = 256, 1568

WATER = ("Water is an open surface with no grass or plants on it: black, dark blue, blue-grey or pale "
         "silvery grey (the sea shimmers), smooth or rippled")
PENALTY = "a solid red line is a penalty area, which often holds no water at all, just scrub, bushes or rough"
MAP = ("`map` is GSPro's overhead minimap of a golf hole. The ball is the small solid dot near the bottom "
       "middle (usually red, but it can be any colour); the hole "
       f"plays up toward the white pin dot. {WATER}; bunkers and sandy waste areas are pale tan or grey-white "
       f"patches; a solid white line is an out-of-bounds boundary; {PENALTY}. White numbers printed on the map "
       "are yardages from the ball. Ignore the header text at the top.")
# A red line alone isn't water: Cabot Cliffs' gorse-filled penalty areas read as "water down the left".
HALF = ("`{side}` is part of GSPro's overhead minimap of a golf hole; the grey area is masked out, ignore it. "
        f"{WATER}; bunkers and sandy waste areas are pale tan or grey-white patches; a solid white line is an "
        f"out-of-bounds boundary; {PENALTY}.")
# Describing what water looks like nearly doubled Jev's hits on 33 saved frames (18 -> 33 of 44 water sides) with
# no new false alarms; the "not water" lines keep dark woods and gorse from reading as a pond or the sea.
WATER_Q = ("Water in GSPro's minimap looks like: a black or very dark navy pond or lake; a light blue-grey canal "
           "or river with fine ripples; or the sea along a coastline, a large pale silvery-grey or white-flecked "
           "shimmering area, often beyond a beach. A pond or lake is smooth and uniform with a crisp shoreline. "
           "Not water: dark woods or deep-shaded rough (lumpy, made of tree crowns, dark green rather than navy or "
           "black, with no smooth shoreline), or rocky, scrubby or gorse-covered ground (brown-grey and clumpy, not "
           "shimmering). Is there any water in `{side}`, even just at its edge? Answer no if all there is is a red "
           "line around grass, scrub, bushes, rocks or sand.")
# A red or white boundary further than this off the line of play all the way along isn't part of the hole.
SIDE_LINE_MAX_YARDS = 60
LINE_MIN_SPAN = 0.08   # of the map's height: digits, dots and the ball are smaller
OB_CELL = 3            # px per cell of the out-of-bounds flood
CROSSING_SPAN = 0.1    # a line spreading wider than this off the line of play in one step runs across it
STRAND_GAP = 0.02      # a gap this wide (off the line of play) between pixels in one step separates strands
EDGE_STEP = 0.05       # the edge is kept per this fraction of the ball-to-pin distance
HUNDREDS = {str(i): (f"{i * 100} to {i * 100 + 99}" if i else "under 100") for i in range(6)}
LAST_TWO = {f"{i:02d}": f"the last two digits are {i:02d}" for i in range(100)}
NUMBERS = {
    "carry": "the yardage number printed at the FAR edge of the water the shot must carry over (where the carry ends)",
    "front": "the yardage number printed at the FRONT edge of the green (the edge nearest the ball)",
    "back": "the yardage number printed at the BACK edge of the green (the edge farthest from the ball)",
}


# Only bunkers within this many yards of the ball-to-pin line count: the fairway and greenside sand,
# not the beige roofs and waste areas at the edges of the map.
SAND_CORRIDOR_YARDS = 35
MAX_SAND_CHECKS = 12
SAND_MERGE = 0.015   # pieces on one side this close (fraction of the ball-to-pin distance) are one bunker
# Asking for a cleanly cut trap set in grass keeps beaches and rocky shorelines (Pebble Beach) out.
SAND_Q = ("`{k}` is a close-up of GSPro's overhead golf course minimap, centred on one pale patch. Is the pale "
          "patch in the centre a golf bunker: a sand trap with smooth, cleanly cut edges set into the grass of "
          "the course? Answer no for a beach, a shoreline, rocks, a cliff, a house roof, a building, a path, or text.")
# Trees: canopies within this many yards of the line, and a tree the line runs through within
# BLOCK_REACH_YARDS of the ball is in the way.
TREE_CORRIDOR_YARDS = 30
MAX_TREE_CHECKS = 10
TREE_MERGE_YARDS = 15   # canopies on one side this close are one stand of trees
BLOCK_REACH_YARDS = 60
TREE_Q = ("`{k}` is a close-up of GSPro's overhead golf course minimap. Is the centre of `{k}` a tree or a clump "
          "of trees seen from above (a tall leafy canopy, often with a shadow)? Answer no for grass, mown stripes, "
          "rough, low scrub, gorse or bushes spread over rough ground, water, sand, rocks, a house, a path, or "
          "text.")
# A lone tree gets one distance (the middle of its patch); a row or stand keeps its range. Jev's
# "single tree?" alone is the test: a lone tree's patch can run long when it touches textured rough.
SINGLE_Q = ("`{k}` is a close-up of GSPro's overhead golf course minimap. Is the canopy in the centre of `{k}` a "
            "single tree standing on its own, rather than a row, stand, or wood of several trees?")
SINGLE_MAX_YARDS = 50
# A lone tree in the way is placed this far short of its canopy's far edge (about its trunk): the
# patch's near end often runs on toward the ball along a bunker lip or rough, so its middle is short.
LONE_TREE_RADIUS_YARDS = 10
# The pin's side of the green is only readable when the map is zoomed in enough that the pin dot is
# small next to the green: a map spanning at most this many yards top to bottom.
PIN_SIDE_MAX_MAP_YARDS = 350
GREEN = ("`green` is a close-up of GSPro's overhead minimap around the putting green (the smooth, lighter green "
         "area). The white dot is the pin. The golfer plays from the bottom of the image toward the top, so the "
         "bottom of the green is its front and the top is its back. Ignore white numbers and the header.")
PIN_SIDE = {"left": "the pin is on the left part of the green",
            "center": "the pin is near the middle, left to right",
            "right": "the pin is on the right part of the green"}
# A dogleg only matters from far enough back to see the bend.
DOGLEG_MIN_YARDS = 250
DOGLEG_Q = ("`aimed` is GSPro's overhead minimap of a golf hole. A thin cyan line has been drawn straight from the "
            "ball (small solid dot, bottom) to the pin (white dot, top). The fairway is the smooth, mown, lighter green "
            "strip the hole is designed to be played along. Where is the fairway relative to the cyan line?")
FAIRWAY = {"along": "the fairway runs along the cyan line, roughly centred on it",
           "left": "the fairway bows away to the left of the cyan line for much of the way",
           "right": "the fairway bows away to the right of the cyan line for much of the way",
           "none": "no fairway is visible between the ball and the green"}
# Yardage-marker dots look just like the pin, so steer the box to the green.
PIN_BOX = ("The pin: the small solid white dot on the putting green (the smoothest, lightest green area, usually "
           "ringed by bunkers, near the top of `map`). Not a yardage-marker dot printed beside a white number.")
# A long tee shot's map can stop short of the green; then the only white dot is GSPro's aim point,
# labelled with its own distance. A dot labelled well short of the to-pin is that, not the pin.
AIM_CHECK_MIN_YARDS = 200
AIM_SHORT = 0.8
# On a long tee shot the map can stop at an aim point this near, so bunkers and trees are searched on
# that scale; the corridors and reach are then applied on whichever scale the read settles on.
AIM_SEARCH_YARDS = 100
AIM_CONFIDENCE = 0.9
# GSPro's tee-shot aim point sits in the fairway, so a dot well off the straight line to the pin shows
# which way the hole bends; a pale disc that Jev confirms is that dot, not a roof or a rock.
AIM_DOT_SURE = 0.45
BEND_DEGREES = 10
MAX_AIM_CHECKS = 5
# A tee shot's aim point is a drive out; a marker dot just ahead of the ball is something else.
AIM_MIN_ALONG = 0.25
AIM_DOT_Q = ("`{k}` is a close-up of GSPro's overhead golf minimap. Is the thing at the very centre of `{k}` "
             "GSPro's aim marker: a small, pale white round dot with a white yardage number printed next to it? "
             "Not a roof, rock, cart or tree.")
SPOT = "`spot` is a close-up of GSPro's overhead golf minimap centred on a small white dot."
# Where a shot is likely to land: the pin on an approach, about a drive from a long tee.
LANDING_MAX_YARDS = 300
# Past this the shot isn't an approach to the green, so no read on which side of it to miss.
APPROACH_MAX_YARDS = 230
# A bunker this far short of the front edge or past the back still guards the green.
GREENSIDE_YARDS = 15
NAMED = {"water": "the water down the {side}", "penalty area": "the penalty area down the {side}",
         "out of bounds": "out of bounds down the {side}", "bunker": "the bunker on the {side}"}


@dataclass
class Sand:
    """A confirmed bunker. Distances are fractions of the ball-to-pin distance, so the yards come out
    right with whichever to-pin is final (the GSPro log's, which can land after the read started)."""

    side: str
    near: float
    far: float
    sure: float   # Jev's probability it's sand
    off: float | None = None   # closest it comes to the line of play, same fraction; None if not measured
    wide: float | None = None  # farthest it reaches off the line of play, same fraction

    def yards(self, to_pin: float) -> tuple[int, int]:
        return round(self.near * to_pin), round(self.far * to_pin)


@dataclass
class Tree(Sand):
    """A confirmed tree or tree line near the line of play. `blocks` is where the line to the pin runs
    into it (a fraction of the ball-to-pin distance), when that's within reach of the ball."""

    blocks: float | None = None
    single: float = 0.0   # Jev's probability it's one tree on its own, not a row or stand


@dataclass
class Boundary:
    """A penalty-area or out-of-bounds line down one side. `edge` is (along, off) pairs as fractions of
    the ball-to-pin distance: how far off the line of play the hazard starts at each point along it
    (0 where the line of play runs over it)."""

    kind: str   # "penalty area" or "out of bounds"
    side: str
    edge: list[tuple[float, float]]

    def nearest(self) -> float:
        return min(off for _, off in self.edge)


@dataclass
class Past:
    """A red or white line beyond the pin: `points` are (along, off) fractions of the ball-to-pin
    distance (along > 1 is past the pin; off is either side of the line of play). `water` when the
    ground beyond a red line is water."""

    kind: str
    points: list[tuple[float, float]]
    water: bool = False

    def nearest(self, pin: float, corridor: float) -> float | None:
        """Yards past the pin to the nearest point within `corridor` yards of the line of play."""
        past = [(a - 1) * pin for a, off in self.points if abs(off) * pin <= corridor]
        return min(past, default=None)


def _merge(items: list, threshold: float, gap: float) -> list:
    """The confirmed items, nearest first, with overlapping pieces on one side joined: a tree or a lip
    can split one bunker into pieces, and a tree line comes out as several canopies."""
    out: list = []
    for b in sorted((b for b in items if b.sure >= threshold), key=lambda b: b.near):
        same = next((m for m in reversed(out) if m.side == b.side), None)
        if same and b.near <= same.far + gap:
            offs = [p.off for p in (same, b) if p.off is not None]
            wides = [p.wide for p in (same, b) if p.wide is not None]
            joined = type(b)(b.side, same.near, max(same.far, b.far), max(same.sure, b.sure),
                             off=min(offs) if offs else None, wide=max(wides) if wides else None)
            # (two pieces joined are several trees, so `single` stays at its default of 0)
            if isinstance(b, Tree):
                hits = [t.blocks for t in (same, b) if t.blocks is not None]
                joined.blocks = min(hits) if hits else None
            out[out.index(same)] = joined
        else:
            out.append(b)
    return out


def _round5(y: float) -> int:
    return int(5 * round(y / 5))


@dataclass
class MinimapRead:
    """What Jev saw, as probabilities and numbers; `facts` and `hazards` apply the thresholds."""

    threshold: float
    seen: dict[str, float] = field(default_factory=dict)      # question -> probability of yes
    numbers: dict[str, int] = field(default_factory=dict)     # carry/front/back -> yards
    sides: bool = False   # the ball and pin were found, so the left/right reads are valid
    sand: list[Sand] = field(default_factory=list)
    sand_threshold: float = 0.85
    trees: list[Tree] = field(default_factory=list)
    tree_threshold: float = 0.85
    # Multiple-choice reads: "pin_side" and "fairway" -> (choice, confidence).
    choices: dict[str, tuple[str, float]] = field(default_factory=dict)
    choice_confidence: float = 0.8
    to_pin_hint: float | None = None   # the to-pin when the read started; facts() can override it
    # Set when the dot taken for the pin is GSPro's aim point (the pin is off the map): its yards. The
    # sides and distances are then along the line to it, and the pin and dogleg reads don't apply.
    aim: int | None = None
    # Degrees from the line to the pin round to GSPro's aim point, right positive: the way the fairway
    # heads off the tee.
    aim_turn: float | None = None
    # The red and white lines measured on the map; None when they weren't (then `seen` holds Jev's
    # yes/no for each side instead).
    lines: list[Boundary] | None = None
    past: list[Past] = field(default_factory=list)   # red/white lines beyond the pin
    seconds: float = 0.0

    def _scale(self, to_pin: float | None) -> float | None:
        """The yards from the ball to the dot the read was measured against."""
        return self.aim or to_pin or self.to_pin_hint

    def _line(self) -> str:
        return "the target line" if self.aim else "the line to the pin"

    def _sand(self, to_pin: float | None) -> list[Sand]:
        if not self.sides or not (scale := self._scale(to_pin)):
            return []
        near = [s for s in self.sand if s.off is None or s.off * scale <= SAND_CORRIDOR_YARDS]
        return _merge(near, self.sand_threshold, SAND_MERGE)

    def _trees(self, to_pin: float | None) -> list[Tree]:
        if not self.sides or not (scale := self._scale(to_pin)):
            return []
        near = [t if t.blocks is None or t.blocks * scale <= BLOCK_REACH_YARDS else replace(t, blocks=None)
                for t in self.trees if t.off is None or t.off * scale <= TREE_CORRIDOR_YARDS]
        return _merge(near, self.tree_threshold, TREE_MERGE_YARDS / scale)

    def _choice(self, key: str) -> str | None:
        choice, confidence = self.choices.get(key, (None, 0.0))
        return choice if confidence >= self.choice_confidence else None

    def _pin_spot(self, to_pin: float | None) -> str | None:
        """Where the pin sits on the green: front/back from GSPro's front and back numbers against the
        to-pin, left/right from Jev on a close-up of the green."""
        front, back = self._number("front", to_pin), self._number("back", to_pin)
        pin = to_pin or self.to_pin_hint
        depth = None
        if front and back and pin and back - front >= 8:
            at = (pin - front) / (back - front)
            depth = "front" if at < 0.33 else "back" if at > 0.67 else "middle"
        side = self._choice("pin_side") if self.sides and not self.aim else None
        if depth and side and side != "center":
            return f"Pin: on the {depth} {side} of the green." if depth != "middle" else \
                f"Pin: on the {side} side of the green, middle front to back."
        if depth and depth != "middle":
            return f"Pin: toward the {depth} of the green."
        if side and side != "center":
            return f"Pin: on the {side} side of the green."
        return None   # a middle pin isn't worth a line

    def _boundary(self, kind: str, side: str, to_pin: float | None) -> Boundary | None:
        """The measured line of this kind down this side, if it comes within SIDE_LINE_MAX_YARDS."""
        pin = self._scale(to_pin)
        if self.lines is None or not pin:
            return None
        return next((b for b in self.lines if b.kind == kind and b.side == side
                     and b.nearest() * pin <= SIDE_LINE_MAX_YARDS), None)

    def _has_line(self, kind: str, side: str, to_pin: float | None) -> bool:
        if self.lines is None:
            return self._yes(f"{'ob' if kind == 'out of bounds' else 'penalty'}_{side}")
        return self._boundary(kind, side, to_pin) is not None

    def _penalty(self, side: str, to_pin: float | None = None) -> bool:
        """A red-lined penalty area with no water in it (scrub, gorse, desert)."""
        return self._has_line("penalty area", side, to_pin) and not self._yes(f"water_{side}")

    def _edge(self, kind: str, side: str, to_pin: float | None) -> tuple[tuple[float, float], ...]:
        b, pin = self._boundary(kind, side, to_pin), self._scale(to_pin)
        return tuple((round(a * pin), round(off * pin)) for a, off in b.edge) if b and pin else ()

    def _where(self, kind: str, side: str, to_pin: float | None) -> str:
        """How far off the line a measured boundary runs, for the fact."""
        b, pin = self._boundary(kind, side, to_pin), self._scale(to_pin)
        if not b or not pin:
            return ""
        along, off = min(b.edge, key=lambda e: (e[1], e[0]))
        out = max(5, _round5(along * pin))
        if off * pin < 5:
            return f", reaching {self._line()} about {out} yards out"
        return f", about {max(5, _round5(off * pin))} yards off {self._line()} at its closest ({out} yards out)"

    def _yes(self, key: str) -> bool:
        return self.seen.get(key, 0.0) >= self.threshold

    def _number(self, key: str, to_pin: float | None) -> int | None:
        n = self.numbers.get(key)
        if n is None or not self._yes(f"{key}_shown"):
            return None
        if to_pin is not None:
            # Sanity against GSPro's own distance: water to carry ends before the green's far side,
            # the front of the green is short of the pin and the back past it.
            if key == "carry" and n > to_pin + 15:
                return None
            if key == "front" and not (to_pin - 60 <= n <= to_pin + 3):
                return None
            if key == "back" and not (to_pin - 3 <= n <= to_pin + 60):
                return None
        return n

    def facts(self, to_pin: float | None = None, lie: str | None = None) -> list[str]:
        """`lie` is the ball's lie from the HUD; a dogleg is only mentioned from the tee, since from the
        rough or a fairway's edge the fairway always sits off the straight line to the pin."""
        out: list[str] = []
        if self._yes("island"):
            out.append("Hole layout: an island green, water all around it.")
        if self._yes("water_ahead"):
            carry = self._number("carry", to_pin)
            out.append(f"Hole layout: water in front, about {carry} yards to carry it." if carry
                       else "Hole layout: water in front that the shot has to carry.")
        if self.sides:
            for side in ("left", "right"):
                if self._yes(f"water_{side}") and not self._yes("island"):
                    out.append(f"Hole layout: water down the {side}.")
                elif self._penalty(side, to_pin):
                    out.append(f"Hole layout: a penalty area (red stakes) down the {side}"
                               f"{self._where('penalty area', side, to_pin)}.")
                if self._has_line("out of bounds", side, to_pin):
                    out.append(f"Hole layout: out of bounds down the {side}"
                               f"{self._where('out of bounds', side, to_pin)}.")
                if self._yes(f"bunker_{side}") and not any(b.side == side for b in self._sand(to_pin)):
                    out.append(f"Hole layout: a bunker (or bunkers) {side} of {self._line()}.")
        pin = self._scale(to_pin)
        for b in self._sand(to_pin)[:4]:
            near, far = b.yards(pin)
            if far < 5:
                continue
            span = (f"{_round5(near)} to {_round5(far)}" if _round5(far) > _round5(near) else str(_round5(near)))
            out.append(f"Hole layout: a bunker {b.side}, about {span} yards out.")
        trees = self._trees(to_pin)
        if stymie := next((t for t in trees if t.blocks is not None), None):
            # A lone tree is where its trunk is, not where the line clips its near edge.
            at = stymie.blocks
            if stymie.single >= self.threshold:
                at = max((stymie.near + stymie.far) / 2, stymie.far - LONE_TREE_RADIUS_YARDS / pin)
            out.append(f"Hole layout: a tree stands right on {self._line()}, about "
                       f"{max(5, _round5(at * pin))} yards ahead.")
        for t in [t for t in trees if t is not stymie][:3]:
            near, far = t.yards(pin)
            if far < 5:
                continue
            if t.single >= self.threshold and far - near <= SINGLE_MAX_YARDS:
                out.append(f"Hole layout: a tree {t.side} of the line, about "
                           f"{max(5, _round5((near + far) / 2))} yards out.")
                continue
            lo, hi = 10 * round(near / 10), 10 * round(far / 10)
            span = (f"within about {max(hi, 10)} yards" if lo < 10 else
                    f"about {lo} to {hi} yards out" if hi > lo else f"about {lo} yards out")
            out.append(f"Hole layout: trees close to the {t.side} of the line, {span}.")
        from_tee = (lie or "").lower() == "tee"
        bend = None
        if self.sides and not self.aim and from_tee and pin and pin >= DOGLEG_MIN_YARDS:
            if self.aim_turn is not None and abs(self.aim_turn) >= BEND_DEGREES:
                bend = "right" if self.aim_turn > 0 else "left"
            else:
                bend = self._choice("fairway")
        if bend in ("left", "right"):
            # The fairway bowing out right of the straight line means it turns back left: a dogleg left.
            turn = "left" if bend == "right" else "right"
            out.append(f"Hole shape: a dogleg {turn}, the fairway bending {turn} toward the green.")
        if spot := self._pin_spot(to_pin):
            out.append(spot)
        front, back = self._number("front", to_pin), self._number("back", to_pin)
        if front and back and back - front >= 5:
            out.append(f"Green: {front} yards to the front edge, {back} to the back.")
        elif front:
            out.append(f"Green: {front} yards to the front edge.")
        return out

    def strategy(self, to_pin: float | None = None, lie: str | None = None) -> str | None:
        """Where the miss is, from the trouble read: on an approach, which side of the green (short,
        long, left, right) has water or sand and which has nothing read; off a long tee, the side away
        from water or out of bounds. None when nothing is clear enough to say."""
        pin = self._scale(to_pin)
        if not pin or not self.sides or (lie or "").lower() == "green":
            return None
        penal = {side: "water" if self._yes(f"water_{side}") else "out of bounds"
                 if self._has_line("out of bounds", side, to_pin) else "a penalty area"
                 if self._penalty(side, to_pin) else None for side in ("left", "right")}
        if pin > APPROACH_MAX_YARDS:
            if (lie or "").lower() != "tee" or bool(penal["left"]) == bool(penal["right"]):
                return None
            bad = "left" if penal["left"] else "right"
            safe = "right" if bad == "left" else "left"
            return f"Strategy: {penal[bad]} down the {bad}, so the {safe} side is the safer miss off the tee."
        if self.aim:
            return None   # the dot is an aim point, not the pin: the green's sides aren't known
        front = self._number("front", to_pin) or pin - 12
        back = self._number("back", to_pin) or pin + 12
        trouble: dict[str, str] = {}
        if self._yes("water_ahead") or self._yes("island"):
            trouble["short"] = "water"
        for b in self._sand(to_pin):
            near, far = b.yards(pin)
            if far < front - GREENSIDE_YARDS or near > back + GREENSIDE_YARDS:
                continue   # not by the green
            where = "short" if far <= pin - 3 and near < front else "long" if near >= back - 3 else b.side
            trouble.setdefault(where, "bunkers")
        for side in ("left", "right"):
            if penal[side] and not self._yes("island") and self._by_green(side, pin, front):
                trouble[side] = penal[side]
        for p in self.past:
            if (gap := p.nearest(pin, GREENSIDE_YARDS + 5)) is not None and gap <= back - pin + GREENSIDE_YARDS:
                trouble["long"] = "water" if p.water else p.kind if p.kind == "out of bounds" else "a penalty area"
                break
        if not trouble:
            return None
        def spot(where: str) -> str:
            return "short of" if where == "short" else "past" if where == "long" else f"{where} of"

        def miss(where: str) -> str:
            return {"short": "coming up short", "long": "going long"}.get(where, f"missing {where}")

        ranked = sorted(trouble, key=lambda w: trouble[w] == "bunkers")   # stroke-costing trouble first
        parts = [f"{trouble[where]} {spot(where)} the green" for where in ranked[:3]]
        # Water or a stake line costs a stroke; sand only a harder up-and-down. The worst is the miss to avoid.
        worst = max(trouble, key=lambda w: trouble[w] != "bunkers")
        opposite = {"short": "long", "long": "short", "left": "right", "right": "left"}[worst]
        line = f"Strategy: {' and '.join(parts)}, so {miss(worst)} is the miss to avoid"
        if opposite not in trouble:
            line += f"; nothing read {spot(opposite)} the green"
        elif trouble[opposite] == "bunkers" and trouble[worst] != "bunkers":
            line += f"; {miss(opposite)} finds sand at worst"
        return line + "."

    def _by_green(self, side: str, pin: float, front: float) -> bool:
        """A measured red or white line down this side comes within GREENSIDE_YARDS of the line of play
        once level with the green. Unmeasured (Jev's yes/no for the half) can't be placed, so it counts."""
        if self.lines is None:
            return True
        return any(b.side == side and any(a * pin >= front - GREENSIDE_YARDS and off * pin <= GREENSIDE_YARDS
                                          for a, off in b.edge) for b in self.lines)

    def hazards(self, to_pin: float | None = None) -> list[Hazard]:
        """For the flight projection: water, penalty areas and out of bounds run down a side, with
        their measured edge where the lines were read; bunkers carry their yards out and how far off
        the line their nearest edge is."""
        out: list[Hazard] = []
        if self.sides:
            for side in ("left", "right"):
                # Water inside red stakes is as far off the line as the stakes.
                if self._yes(f"water_{side}"):
                    out.append(Hazard("water", side, edge=self._edge("penalty area", side, to_pin)))
                elif self._penalty(side, to_pin):
                    out.append(Hazard("penalty area", side, edge=self._edge("penalty area", side, to_pin)))
                if self._has_line("out of bounds", side, to_pin):
                    out.append(Hazard("out of bounds", side, edge=self._edge("out of bounds", side, to_pin)))
        pin = self._scale(to_pin)
        for b in self._sand(to_pin):
            near, far = b.yards(pin)
            off = round(b.off * pin) if b.off is not None and pin else None
            wide = round(b.wide * pin) if b.wide is not None and pin else None
            out.append(Hazard("bunker", b.side, near, far, off_yards=off, wide_yards=wide))
        if self._yes("water_ahead"):
            out.append(Hazard("water", "short", to_yards=self._number("carry", to_pin)))
        return out

    def named(self, to_pin: float | None = None) -> dict[str, str]:
        """Per side, the one hazard kind the flight line may name: water, then a measured bunker where
        this shot is likely to land, then a penalty area, then out of bounds, then any measured bunker. Impact plays that
        line only when this kind is the trouble actually in play (see flight_pick.pick_line). Out of
        bounds has no distance, so it's "in play" for any miss that way; a bunker at the landing
        distance is in play only when the carry gets there, so it's the truer call for a modest miss."""
        pin = self._scale(to_pin)
        reach = min(pin, LANDING_MAX_YARDS) if pin else None
        out = {}
        for side in ("left", "right"):
            here = [h for h in self.hazards(to_pin) if h.side == side]
            kinds = {h.kind for h in here}
            landing = reach is not None and any(
                h.kind == "bunker" and h.from_yards is not None
                and h.from_yards <= reach + 15 and (h.to_yards or h.from_yards) >= reach * 0.75 for h in here)
            if "water" in kinds:
                out[side] = "water"
            elif landing:
                out[side] = "bunker"
            elif kind := next((k for k in NAMED if k in kinds), None):
                out[side] = kind
        return out


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def fit(img: Image.Image) -> Image.Image:
    """Scale into Jev's size limits, keeping the shape."""
    scale = 1.0
    if min(img.size) < MIN_SIDE:
        scale = MIN_SIDE / min(img.size)
    if max(img.size) * scale > MAX_SIDE:
        scale = MAX_SIDE / max(img.size)
    if scale == 1.0:
        return img
    return img.resize((max(MIN_SIDE, round(img.width * scale)), max(MIN_SIDE, round(img.height * scale))))


def half(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float], keep: str) -> Image.Image:
    """The map with everything on the other side of the ball-to-pin line greyed out. Left and right are
    as the golfer faces the pin; `ball` and `pin` are (x, y) fractions of the image."""
    import numpy as np

    w, h = img.size
    bx, by, px, py = ball[0] * w, ball[1] * h, pin[0] * w, pin[1] * h
    ys, xs = np.mgrid[0:h, 0:w]
    # The cross product's sign says which side of the line a pixel is on (y grows downward, so
    # positive is to the golfer's right).
    cross = (px - bx) * (ys - by) - (py - by) * (xs - bx)
    pixels = np.array(img.convert("RGB"))
    pixels[(cross > 0) if keep == "left" else (cross < 0)] = (128, 128, 128)
    out = Image.fromarray(pixels)
    ImageDraw.Draw(out).line([(bx, by), (px, py)], fill=(128, 128, 128), width=3)
    return out


def _components(mask, min_size: int) -> list[tuple]:
    """Connected patches of a boolean mask (4-neighbour), as (ys, xs) index arrays. Works on each
    row's runs rather than pixels, so a big beach or a page of white is as quick as a few specks."""
    import numpy as np

    h, w = mask.shape
    padded = np.zeros((h, w + 2), np.int8)
    padded[:, 1:-1] = mask
    diff = np.diff(padded, axis=1)
    starts_y, starts_x = np.nonzero(diff == 1)
    _, ends_x = np.nonzero(diff == -1)          # same row order: each run's end follows its start
    n = len(starts_y)
    if not n:
        return []
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    row_first = np.searchsorted(starts_y, np.arange(h + 1))
    for y in range(1, h):
        a0, a1 = row_first[y - 1], row_first[y]      # runs on the row above
        b0, b1 = row_first[y], row_first[y + 1]
        i, j = a0, b0
        while i < a1 and j < b1:
            if starts_x[i] < ends_x[j] and starts_x[j] < ends_x[i]:   # the runs share a column
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
            if ends_x[i] < ends_x[j]:
                i += 1
            else:
                j += 1
    groups: dict[int, list[int]] = {}
    for k in range(n):
        groups.setdefault(find(k), []).append(k)
    lengths = ends_x - starts_x
    out = []
    for runs in groups.values():
        runs = np.array(runs)
        if lengths[runs].sum() < min_size:
            continue
        ys = np.repeat(starts_y[runs], lengths[runs])
        xs = np.concatenate([np.arange(starts_x[k], ends_x[k]) for k in runs])
        out.append((ys, xs))
    return out


def boundary_lines(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float]) -> list[Boundary]:
    """GSPro's red penalty-area and white out-of-bounds lines between the ball and the pin. An
    out-of-bounds line's hazard is beyond it, away from the line of play; a red line's is on its side
    without grass (rock, sea, scrub), which on a dogleg round a cliff is the side the straight line to
    the pin runs over. `ball` and `pin` are (x, y) fractions of the image."""
    import numpy as np
    from PIL import ImageFilter

    a = np.asarray(img.convert("RGB")).astype(int)
    h, w = a.shape[:2]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = (r > 140) & (r - g > 100) & (g < 70) & (b < 90)
    white = (r > 220) & (g > 220) & (b > 220)
    grass = ((g > r + 10) & (g > b + 10) & (g > 90)).astype(np.uint8) * 255
    grassy = np.asarray(Image.fromarray(grass).filter(ImageFilter.BoxBlur(3))).astype(float) / 255
    bx, by, px, py = ball[0] * w, ball[1] * h, pin[0] * w, pin[1] * h
    length = math.hypot(px - bx, py - by)
    if length < 1:
        return []
    nx, ny = -(py - by) / length, (px - bx) / length   # unit normal to the golfer's right
    reach = max(8, round(w * 0.02))                     # past the line's own width
    found: dict[tuple[str, str], dict[int, float]] = {}
    for kind, mask in (("penalty area", red), ("out of bounds", white)):
        for ys, xs in _components(mask, 40):
            if max(np.ptp(ys), np.ptp(xs)) < LINE_MIN_SPAN * h:
                continue
            along = ((xs - bx) * (px - bx) + (ys - by) * (py - by)) / length ** 2
            off = ((px - bx) * (ys - by) - (py - by) * (xs - bx)) / length ** 2   # right positive
            keep = (along >= 0.05) & (along <= 1.05)
            if keep.sum() < 20:
                continue
            ys, xs, off = ys[keep], xs[keep], off[keep]
            steps = np.floor(along[keep] / EDGE_STEP).astype(int)
            toward = np.where(off >= 0, 1, -1)   # away from the line of play
            if kind == "penalty area":
                def grass_at(sign):
                    sx = np.clip(np.round(xs + sign * nx * reach).astype(int), 0, w - 1)
                    sy = np.clip(np.round(ys + sign * ny * reach).astype(int), 0, h - 1)
                    return grassy[sy, sx]
                lean = grass_at(-1) - grass_at(1)   # grass to the line's left: the hazard is to its right
                # One call per stretch of line, not per pixel (the line's own width makes pixels noisy),
                # and a stretch with no clear grass side goes with the rest of the line.
                calls = {}
                for step in np.unique(steps):
                    mean = lean[steps == step].mean()
                    if abs(mean) > 0.15:
                        calls[step] = 1 if mean > 0 else -1
                if calls:
                    overall = 1 if sum(calls.values()) >= 0 else -1
                    for step in np.unique(steps):
                        toward[steps == step] = calls.get(step, overall)
            # Where a strand of line runs across the hole rather than down it, which side of play its
            # hazard is on means nothing: leave it out. Each step is split into strands at a gap off
            # the line of play, so a pond's two sides in the same step aren't taken for one line across.
            across = np.zeros(len(off), bool)
            for step in np.unique(steps):
                idx = np.nonzero(steps == step)[0]
                idx = idx[np.argsort(off[idx])]
                for part in np.split(idx, np.nonzero(np.diff(off[idx]) > STRAND_GAP)[0] + 1):
                    if np.ptp(off[part]) > CROSSING_SPAN:
                        across[part] = True
            toward[across] = 0
            dist = np.maximum(0.0, toward * off)
            for sign, side in ((1, "right"), (-1, "left")):
                here = toward == sign
                if here.sum() < 20:
                    continue
                edge = found.setdefault((kind, side), {})
                for step, d in zip(steps[here], dist[here]):
                    edge[step] = min(edge.get(step, d), float(d))
    return [Boundary(kind, side, [((k + 0.5) * EDGE_STEP, d) for k, d in sorted(edge.items())])
            for (kind, side), edge in found.items()]


def _water(px) -> "np.ndarray":
    """GSPro's minimap water: grey-teal (green and blue alike, both above red), not grass (blue well
    under green), a bunker's tan (red on top), a shadow or a line's white."""
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    return (g >= r + 5) & (abs(g - b) <= 12) & (r > 60) & (r < 215)


def past_trouble(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float]) -> list[Past]:
    """GSPro's red penalty and white out-of-bounds lines beyond the pin (a long approach's trouble),
    which boundary_lines leaves out: it looks only between the ball and the pin."""
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype(int)
    h, w = a.shape[:2]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = (r > 140) & (r - g > 100) & (g < 70) & (b < 90)
    white = (r > 220) & (g > 220) & (b > 220)
    bx, by, px, py = ball[0] * w, ball[1] * h, pin[0] * w, pin[1] * h
    length = math.hypot(px - bx, py - by)
    if length < 1:
        return []
    ux, uy = (px - bx) / length, (py - by) / length
    reaches = [max(8, round(w * f)) for f in (0.04, 0.07, 0.10)]   # past the line and its dark verge
    out = []
    for kind, mask in (("penalty area", red), ("out of bounds", white)):
        pts, wet, seen = [], 0, 0
        for ys, xs in _components(mask, 40):
            if max(np.ptp(ys), np.ptp(xs)) < LINE_MIN_SPAN * h:
                continue
            along = ((xs - bx) * (px - bx) + (ys - by) * (py - by)) / length ** 2
            off = ((px - bx) * (ys - by) - (py - by) * (xs - bx)) / length ** 2
            keep = along > 1.0
            if keep.sum() < 20:
                continue
            pts += list(zip(along[keep][::5].tolist(), off[keep][::5].tolist()))
            if kind == "penalty area":
                hit = np.zeros(int(keep.sum()), bool)
                for reach in reaches:
                    sx = np.clip(np.round(xs[keep] + ux * reach).astype(int), 0, w - 1)
                    sy = np.clip(np.round(ys[keep] + uy * reach).astype(int), 0, h - 1)
                    hit |= _water(a[sy, sx])
                wet += int(hit.sum())
                seen += len(hit)
        if pts:
            out.append(Past(kind, pts, water=bool(seen) and wet / seen >= 0.4))
    return out


def _greyed(img: Image.Image, out) -> Image.Image:
    import numpy as np

    pixels = np.asarray(img.convert("RGB")).copy()
    pixels[out] = (128, 128, 128)
    return Image.fromarray(pixels)


def out_of_play(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float]):
    """Where the map is out of bounds, as a boolean array the map's size. Out of bounds is what
    the ball's and pin's ground can't reach without crossing a long white line, so a short line
    across a corner cuts off only that corner, and the ground between white-outlined housing
    blocks stays in play."""
    import numpy as np

    # Every long white line is a wall, sided or not: one running across the hole beyond the green
    # gets no left or right in `lines`, but the ground past it is still out.
    a = np.asarray(img.convert("RGB"))
    h, w = a.shape[:2]
    white = (a[..., 0] > 220) & (a[..., 1] > 220) & (a[..., 2] > 220)
    walls = np.zeros((h, w), bool)
    # GSPro frames the map with a dark header and footer that would join the two sides of every line.
    dark = (a.max(axis=2) < 70).mean(axis=1) >= 0.7
    for rows in (range(int(h * 0.15)), range(h - 1, h - int(h * 0.15), -1)):
        for y in rows:
            if not dark[y]:
                break
            walls[y] = True
    for ys, xs in _components(white, 40):
        if max(np.ptp(ys), np.ptp(xs)) >= LINE_MIN_SPAN * h:
            walls[ys, xs] = True
    # Flood on a coarse grid: quick in plain Python, and a cell with any line in it is a wall, which
    # also closes the odd pixel gap in a drawn line.
    k = OB_CELL
    gh, gw = -(-h // k), -(-w // k)
    padded = np.zeros((gh * k, gw * k), bool)
    padded[:h, :w] = walls
    wall = padded.reshape(gh, k, gw, k).any(axis=(1, 3))
    reached = np.zeros_like(wall)
    queue = deque()

    def seed(ts):
        before = len(queue)
        for t in ts:
            cy = min(gh - 1, int((ball[1] + (pin[1] - ball[1]) * t) * h) // k)
            cx = min(gw - 1, int((ball[0] + (pin[0] - ball[0]) * t) * w) // k)
            if not wall[cy, cx] and not reached[cy, cx]:
                reached[cy, cx] = True
                queue.append((cy, cx))
        return len(queue) > before

    # From the ball's and the pin's ends, not the whole straight line: a dogleg's line can cut
    # across white-outlined housing in the corner, and that's still out. An end sat on a line
    # falls back to the whole line.
    from_ball = seed(np.linspace(0, 0.05, 4))
    from_pin = seed(np.linspace(0.95, 1, 4))
    if not (from_ball and from_pin):
        seed(np.linspace(0, 1, 60))
    while queue:
        y, x = queue.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < gh and 0 <= nx < gw and not wall[ny, nx] and not reached[ny, nx]:
                reached[ny, nx] = True
                queue.append((ny, nx))
    # A line's own cells stay; only ground wholly on the far side is greyed.
    out = ~reached & ~wall
    return np.repeat(np.repeat(out, k, axis=0), k, axis=1)[:h, :w]


def sand_patches(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float],
                 corridor: float, out=None) -> list[tuple[str, float, float, float, Image.Image, float]]:
    """Sand-coloured patches near the line of play, as (side, near, far, off_line, close-up, wide) with the
    distances as fractions of the ball-to-pin distance, nearest the line first. `corridor` is the
    widest a patch may sit off the line, in the same fraction. Sand on GSPro's minimap is tan or a
    grey-beige: warm (red above blue) and pale. Neutral greys (roofs, paths, text) and blues (water)
    fail the colour test; tan roofs further out fail the corridor; what's left goes to Jev. Sand on
    `out` (out_of_play's array) is left out."""
    import numpy as np
    from PIL import ImageFilter

    w, h = img.size
    a = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # Links sand is a deeper tan (blue ~55) than parkland sand; fescue and cart paths are darker (red < 125).
    mask = (r >= 135) & (b >= 45) & (r - b >= 18) & (r - b <= 100) & (g >= 0.72 * r) & (g <= 1.03 * r) & (b < g)
    mask[: int(h * 0.1)] = False   # the header ("Tee", "Fairway")
    mask[int(h * 0.975):] = False
    # Open (erode then dilate) to drop speckle and thin edges, scaled to the map's size.
    k = 5 if w >= 400 else 3
    m = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))
    mask = np.asarray(m) > 0
    if out is not None:
        mask = mask & ~out
    bx, by, px, py = ball[0] * w, ball[1] * h, pin[0] * w, pin[1] * h
    length = max(1.0, ((px - bx) ** 2 + (py - by) ** 2) ** 0.5)
    found = []
    for ys, xs in _components(mask, max(20, round(w * h * 1.6e-4))):
        if xs.min() - 3 <= bx <= xs.max() + 3 and ys.min() - 3 <= by <= ys.max() + 3:
            continue   # the bunker the ball is sitting in
        along = ((xs - bx) * (px - bx) + (ys - by) * (py - by)) / length ** 2
        if along.max() < 0.02:
            continue   # behind the ball
        # A bunker beside the ball is only measured, and placed, by its part ahead of it.
        ahead = along >= 0
        cx, cy = int(xs.mean()), int(ys.mean())   # Jev sees the whole patch to judge it's sand
        rad = max(int(np.ptp(xs)), int(np.ptp(ys)), 30)
        xs, ys = xs[ahead], ys[ahead]
        cross = (px - bx) * (ys - by) - (py - by) * (xs - bx)
        off = float(np.abs(cross).min()) / length / length
        if off > corridor:
            continue
        # Straight from the ball, the way a golfer gives the yards to a bunker.
        dist = np.hypot(xs - bx, ys - by) / length
        side = "right" if (cross > 0).mean() > 0.5 else "left"
        close = img.crop((cx - rad, cy - rad, cx + rad, cy + rad)).resize((300, 300))
        wide = float(np.abs(cross).max()) / length / length
        found.append((side, float(dist.min()), float(dist.max()), off, close, wide))
    return sorted(found, key=lambda p: p[3])


def aim_dots(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float]) -> list[tuple[float, float]]:
    """Pale round dots the size of GSPro's aim marker (smaller than the pin's), other than the pin's,
    at least a quarter of the way up to the pin, nearest the ball first. Roofs and rocks can pass; Jev weeds those out."""
    import numpy as np

    # Half size: a beach or a row of roofs is a lot of pale pixels for the pure-Python grouping.
    small = img.convert("RGB").resize((max(1, img.width // 2), max(1, img.height // 2)), Image.BOX)
    a = np.asarray(small).astype(int)
    h, w, _ = a.shape
    k = 2 * w / 459   # the sizes below were measured on a full-size 459-wide map
    pale = (a.min(axis=2) > 165) & (a.max(axis=2) - a.min(axis=2) < 30)
    pale[: int(h * 0.08)] = False   # header
    pale[int(h * 0.93):] = False    # footer
    out = []
    for ys, xs in _components(pale, max(6, round(30 * k * k / 4))):
        hh, ww = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
        x, y = xs.mean() / w, ys.mean() / h
        if (len(ys) / (hh * ww) >= 0.55 and 0.6 <= hh / ww <= 1.6 and 4 * k <= min(hh, ww)
                and max(hh, ww) <= 9 * k and math.dist((x, y), pin) > 0.03
                and (ball[1] - y) >= AIM_MIN_ALONG * (ball[1] - pin[1])):
            out.append((x, y))
    return sorted(out, key=lambda c: math.dist(c, ball))


def _turn(ball: tuple[float, float], pin: tuple[float, float], dot: tuple[float, float], w: int, h: int) -> float:
    """Degrees from the ball-to-pin line round to the ball-to-dot line, right positive."""
    pa = math.atan2((pin[0] - ball[0]) * w, (ball[1] - pin[1]) * h)
    da = math.atan2((dot[0] - ball[0]) * w, (ball[1] - dot[1]) * h)
    return math.degrees(da - pa)


def tree_patches(img: Image.Image, ball: tuple[float, float], pin: tuple[float, float],
                 to_pin: float, out=None) -> list[tuple[str, float, float, float, Image.Image, float | None]]:
    """Leafy patches near the line of play, as (side, near, far, off_line, close-up, blocks), distances
    as fractions of the ball-to-pin distance. Trees on GSPro's minimap are drawn as canopies: green,
    darker than the fairway and textured (leaves and shadow) where grass is smooth. Water, shorelines
    and roof edges can pass that too, so Jev confirms each. `blocks` is where the straight line to the
    pin runs into the patch for a couple of yards or more, if that's within BLOCK_REACH_YARDS. Canopy
    on `out` (out_of_play's array) is left out."""
    import numpy as np
    from PIL import ImageFilter

    # Trees are big blobs, so they're found on a half-size map (nearest-neighbour keeps the leaf
    # texture) and the close-ups cut from the full one.
    k = 2 if img.width >= 400 else 1
    small = img.convert("RGB").resize((img.width // k, img.height // k), Image.NEAREST)
    w, h = small.size
    a = np.asarray(small).astype(float)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    lum = 0.3 * r + 0.59 * g + 0.11 * b
    blur = ImageFilter.BoxBlur(4 // k)
    mean = np.asarray(Image.fromarray(lum.astype(np.uint8)).filter(blur)).astype(float)
    sq = np.asarray(Image.fromarray(np.clip(lum ** 2 / 255, 0, 255).astype(np.uint8)).filter(blur)).astype(float) * 255
    texture = np.sqrt(np.clip(sq - mean ** 2, 0, None))
    mask = (g >= r) & (g > b + 5) & (texture > 14) & (lum < 150)
    mask[: int(h * 0.1)] = False
    mask[int(h * 0.975):] = False
    # Close the gaps between leaves, drop speckle (mown-stripe edges), then restore the size.
    grow, shrink = (5, 9) if k == 1 else (3, 5)
    m = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(grow)) \
        .filter(ImageFilter.MinFilter(shrink)).filter(ImageFilter.MaxFilter(grow))
    bx, by, px, py = ball[0] * w, ball[1] * h, pin[0] * w, pin[1] * h
    length = max(1.0, ((px - bx) ** 2 + (py - by) ** 2) ** 0.5)
    ux, uy = (px - bx) / length, (py - by) / length
    yards_px = to_pin / length
    corridor = TREE_CORRIDOR_YARDS / to_pin
    reach = min(1.0, BLOCK_REACH_YARDS / to_pin)
    run = max(2, round(2 / yards_px))   # the line must cross at least ~2 yards of canopy
    # Only the corridor ahead of the ball counts; masking the rest first keeps the grouping quick.
    gy, gx = np.mgrid[0:h, 0:w]
    along_map = ((gx - bx) * ux + (gy - by) * uy) / length
    cross_map = ((px - bx) * (gy - by) - (py - by) * (gx - bx)) / length / length
    mask = (np.asarray(m) > 0) & (np.abs(cross_map) <= corridor) & (along_map > 0.02) & (along_map < 1.15)
    if out is not None:
        mask = mask & ~out[::k, ::k][:h, :w]
    found = []
    for ys, xs in _components(mask, max(25 // (k * k), round(w * h * 2.5e-4))):
        ck, ak = cross_map[ys, xs], along_map[ys, xs]
        pixels = set(zip(ys.tolist(), xs.tolist()))
        blocks, streak = None, 0
        for step in range(int(0.02 * length), int(reach * length) + 1):
            x, y = round(bx + ux * step), round(by + uy * step)
            streak = streak + 1 if (y, x) in pixels else 0
            if streak >= run:
                blocks = (step - run + 1) / length
                break
        side = "right" if (ck > 0).mean() > 0.5 else "left"
        cx, cy = int(np.median(xs)) * k, int(np.median(ys)) * k
        rad = max(35, min(80, int(max(np.ptp(xs), np.ptp(ys)) * k / 2) + 10))
        close = img.crop((cx - rad, cy - rad, cx + rad, cy + rad)).resize((300, 300))
        found.append((side, float(ak.min()), float(ak.max()), float(np.abs(ck).min()), close, blocks))
    return sorted(found, key=lambda p: (p[5] is None, p[3]))


def _centre(box) -> tuple[float, float]:
    return ((box.left + box.right) / 2, (box.top + box.bottom) / 2)


class JevMinimap:
    def __init__(self, cfg: MinimapConfig, client=None) -> None:
        self.cfg = cfg
        if client is None:
            from typesafe_sdk import AsyncTypeSafeClient

            # Reads TYPESAFE_API_KEY.
            client = AsyncTypeSafeClient(timeout=cfg.timeout_seconds)
        self._client = client

    @staticmethod
    def available() -> str | None:
        """Why Jev can't run here, or None if it can."""
        try:
            import typesafe_sdk  # noqa: F401
        except ImportError:
            return "the typesafe-sdk package isn't installed (pip install -e \".[jev]\")"
        if not os.environ.get("TYPESAFE_API_KEY"):
            return "TYPESAFE_API_KEY isn't set"
        return None

    async def _ask(self, model: str, state: dict, questions: dict):
        return (await self._client.system_one(state=state, questions=questions, model=model,
                                              extra_headers=IMAGES_HEADER)).answers

    async def read(self, img: Image.Image, to_pin: float | None = None) -> MinimapRead:
        """`to_pin` (yards) sets the map's scale for the bunker search; without it there's none."""
        from typesafe_sdk import Box, Choice, Image as JevImage, Noul

        started = time.perf_counter()
        img = fit(img)
        whole = _jpeg(img)
        result = MinimapRead(threshold=self.cfg.threshold, sand_threshold=self.cfg.sand_threshold,
                             tree_threshold=self.cfg.tree_threshold,
                             choice_confidence=self.cfg.choice_confidence, to_pin_hint=to_pin)

        def number_questions() -> dict:
            qs = {}
            for key, what in NUMBERS.items():
                qs[f"{key}_shown"] = Noul(instructions=f"{MAP} Is there {what} on `map`?")
                qs[f"{key}_h"] = Choice(instructions=f"{MAP} Consider {what}. What is its hundreds digit?",
                                        criteria=HUNDREDS)
                qs[f"{key}_r"] = Choice(instructions=f"{MAP} Consider {what}. What are its last two digits?",
                                        criteria=LAST_TWO)
            return qs

        whole_qs = {
            "water_ahead": Noul(instructions=f"{MAP} Does the shot have to carry over water between the ball and the pin?"),
            "island": Noul(instructions=f"{MAP} Is the green an island green, surrounded by water?"),
            **number_questions(),
        }
        boxes = {"ball": Box(instructions="The small solid dot marking the golf ball near the bottom middle of `map` (usually "
                                             "red, but it can be any colour; a dot, not a line)"),
                 "pin": Box(instructions=PIN_BOX)}
        # The boxes and the whole-map questions are independent, so they go out together; the side
        # questions need the boxes, so they follow.
        found, answers = await asyncio.gather(
            self._ask(self.cfg.box_model, {"map": JevImage(whole)}, boxes),
            self._ask(self.cfg.model, {"map": JevImage(whole)}, whole_qs))
        result.seen.update({k: a.noul for k, a in answers.items() if getattr(a, "type", "") == "noul"})
        for key in NUMBERS:
            h, r = answers[f"{key}_h"], answers[f"{key}_r"]
            if min(h.confidence, r.confidence) >= self.cfg.number_confidence:
                result.numbers[key] = int(h.choice) * 100 + int(r.choice)

        ball, pin = found["ball"], found["pin"]
        b, p = _centre(ball.box), _centre(pin.box)
        # GSPro centres the map on the ball, so a slightly unsure ball box in the middle is still the ball.
        ball_ok = ball.confidence >= self.cfg.box_confidence or (
            ball.confidence >= self.cfg.box_confidence - 0.15 and abs(b[0] - 0.5) <= 0.06)
        if ball_ok and pin.confidence >= self.cfg.box_confidence:
            if p[1] < b[1]:   # the pin must be up the map from the ball, or the split means nothing
                result.lines = await asyncio.to_thread(boundary_lines, img, b, p)
                result.past = await asyncio.to_thread(past_trouble, img, b, p)
                out = await asyncio.to_thread(out_of_play, img, b, p)
                inside = _greyed(img, out)
                left, right = half(inside, b, p, "left"), half(inside, b, p, "right")
                patches, leafy, dots = [], [], []
                if to_pin and to_pin > 0:
                    search = min(to_pin, AIM_SEARCH_YARDS) if to_pin >= AIM_CHECK_MIN_YARDS else to_pin
                    patches, leafy, dots = await asyncio.gather(
                        asyncio.to_thread(sand_patches, img, b, p, SAND_CORRIDOR_YARDS / search, out),
                        asyncio.to_thread(tree_patches, img, b, p, search, out),
                        asyncio.to_thread(aim_dots, img, b, p) if to_pin >= DOGLEG_MIN_YARDS else asyncio.sleep(0, []))
                    patches, leafy = patches[:MAX_SAND_CHECKS], leafy[:MAX_TREE_CHECKS]
                state = {"left": JevImage(_jpeg(left)), "right": JevImage(_jpeg(right))}
                side_qs = {}
                for i, patch in enumerate(patches):
                    state[f"patch{i}"] = JevImage(_jpeg(patch[4]))
                    side_qs[f"sand{i}"] = Noul(instructions=SAND_Q.format(k=f"patch{i}"))
                for i, patch in enumerate(leafy):
                    state[f"leafy{i}"] = JevImage(_jpeg(patch[4]))
                    side_qs[f"tree{i}"] = Noul(instructions=TREE_Q.format(k=f"leafy{i}"))
                    side_qs[f"one{i}"] = Noul(instructions=SINGLE_Q.format(k=f"leafy{i}"))
                aimed = img.convert("RGB")
                w, h = aimed.size
                ImageDraw.Draw(aimed).line([(b[0] * w, b[1] * h), (p[0] * w, p[1] * h)], fill=(0, 255, 255),
                                           width=max(2, w // 150))
                state["aimed"] = JevImage(_jpeg(aimed))
                side_qs["fairway"] = Choice(instructions=DOGLEG_Q, criteria=FAIRWAY)
                if to_pin and to_pin / max(1e-6, abs(b[1] - p[1])) <= PIN_SIDE_MAX_MAP_YARDS:
                    # About 30 yards around the pin, on the map's own scale.
                    rad = max(20, round(h * abs(b[1] - p[1]) * 30 / to_pin))
                    green = img.crop((round(p[0] * w) - rad, round(p[1] * h) - rad,
                                      round(p[0] * w) + rad, round(p[1] * h) + rad)).resize((320, 320))
                    state["green"] = JevImage(_jpeg(green))
                    side_qs["pin_side"] = Choice(
                        instructions=f"{GREEN} Where is the pin left to right, as the golfer looks at it?",
                        criteria=PIN_SIDE)
                for side in ("left", "right"):
                    intro = HALF.format(side=side)
                    side_qs[f"water_{side}"] = Noul(instructions=f"{intro} {WATER_Q.format(side=side)}")
                    side_qs[f"bunker_{side}"] = Noul(instructions=f"{intro} Is there any bunker or sandy area in `{side}`?")
                spot_qs, spot_state = {}, {}
                dots = dots[:MAX_AIM_CHECKS]
                if dots:
                    rd = round(w * 0.12)
                    for i, (x, y) in enumerate(dots):
                        spot_state[f"dot{i}"] = JevImage(_jpeg(img.crop(
                            (round(x * w) - rd, round(y * h) - rd, round(x * w) + rd, round(y * h) + rd)).resize((320, 320))))
                        spot_qs[f"dot{i}"] = Noul(instructions=AIM_DOT_Q.format(k=f"dot{i}"))
                if to_pin and to_pin >= AIM_CHECK_MIN_YARDS:
                    # Read the number beside the dot, in case it's the aim point rather than the pin. A
                    # call of its own: alongside the other images Jev reads their numbers into this one.
                    r = round(w * 0.16)
                    spot = img.crop((round(p[0] * w) - r, round(p[1] * h) - r,
                                     round(p[0] * w) + r, round(p[1] * h) + r)).resize((320, 320))
                    spot_state["spot"] = JevImage(_jpeg(spot))
                    what = f"{SPOT} Consider the white number printed right beside the centre dot."
                    spot_qs.update({
                        "aim_label": Noul(instructions=f"{SPOT} Is a white yardage number printed right beside that "
                                                       "centre dot?"),
                        "aim_h": Choice(instructions=f"{what} What is its hundreds digit?", criteria=HUNDREDS),
                        "aim_r": Choice(instructions=f"{what} What are its last two digits?", criteria=LAST_TWO)})
                if spot_qs:
                    answers, spot_answers = await asyncio.gather(self._ask(self.cfg.model, state, side_qs),
                                                                 self._ask(self.cfg.model, spot_state, spot_qs))
                    if "aim_h" in spot_answers and spot_answers["aim_label"].noul >= self.cfg.threshold and \
                            min(spot_answers["aim_h"].confidence, spot_answers["aim_r"].confidence) >= AIM_CONFIDENCE:
                        ah, ar = spot_answers["aim_h"], spot_answers["aim_r"]
                        n = int(ah.choice) * 100 + int(ar.choice)
                        if 0 < n < AIM_SHORT * to_pin:
                            result.aim = n
                            log.info("Minimap: the pin is off the map; measuring to the %d-yard aim point", n)
                    # The confirmed aim dot nearest the ball is the tee shot's.
                    if aim_dot := next((d for i, d in enumerate(dots)
                                        if spot_answers[f"dot{i}"].noul >= AIM_DOT_SURE), None):
                        result.aim_turn = _turn(b, p, aim_dot, w, h)
                else:
                    answers = await self._ask(self.cfg.model, state, side_qs)
                result.seen.update({k: a.noul for k, a in answers.items()
                                    if getattr(a, "type", "noul") == "noul"
                                    and not k.startswith(("sand", "tree", "one"))})
                result.choices.update({k: (answers[k].choice, answers[k].confidence)
                                       for k in ("fairway", "pin_side") if k in answers})
                result.sand = [Sand(side, near, far, answers[f"sand{i}"].noul, off=off, wide=wide)
                               for i, (side, near, far, off, _, wide) in enumerate(patches)]
                result.trees = [Tree(side, near, far, answers[f"tree{i}"].noul, off=off, blocks=blocks,
                                     single=answers[f"one{i}"].noul)
                                for i, (side, near, far, off, _, blocks) in enumerate(leafy)]
                result.sides = True
        else:
            log.info("Minimap: ball/pin not found confidently (%.2f/%.2f); no side reads",
                     ball.confidence, pin.confidence)
        result.seconds = time.perf_counter() - started
        return result
