"""Famous holes on GSPro's replicas of real courses, so the booth knows when the group stands on one.

Keyed by trivia.course_key() of GSPro's course id or name, like trivia.KNOWN_PAGES, then by hole
number. Each note is a short, well-documented fact about the real hole: the booth can mention it on
the tee, but it's told to stick to what the note says, so keep notes to things that are on the record.
"""

from .trivia import course_key

_SAWGRASS = {
    16: "TPC Sawgrass's par-5 16th, reachable in two with water guarding the green, the start of the "
        "famous finishing stretch at The Players Championship.",
    17: "TPC Sawgrass's 17th, the island-green par 3: the green is ringed by water, and at The Players "
        "Championship balls go in the water there every year.",
    18: "TPC Sawgrass's 18th, the par-4 closer at The Players Championship, with water all the way down "
        "the left.",
}
_PEBBLE = {
    7: "Pebble Beach's 7th, the short downhill par 3 out on a point with the Pacific behind and beside "
       "the green, one of the most photographed holes in golf.",
    8: "Pebble Beach's 8th, where the approach is played across an ocean chasm to the green.",
    17: "Pebble Beach's par-3 17th, out by the ocean: Jack Nicklaus hit the flagstick with a 1-iron "
        "here in the 1972 U.S. Open, and Tom Watson chipped in for birdie here on the way to winning "
        "the 1982 U.S. Open.",
    18: "Pebble Beach's 18th, the par 5 that curves along the sea wall and the Pacific all the way "
        "down the left.",
}
_OLD_COURSE = {
    1: "the Old Course's 1st, sharing its huge fairway with the 18th, with the Swilcan Burn crossing "
       "in front of the green.",
    11: "the Old Course's par-3 11th, the High In, with Hill and Strath bunkers guarding the green.",
    14: "the Old Course's par-5 14th, home of the huge Hell Bunker.",
    17: "the Old Course's Road Hole 17th: the drive goes over the hotel, and the Road Hole Bunker and "
        "the road itself sit by the green.",
    18: "the Old Course's 18th, the Tom Morris hole: over the Swilcan Bridge, with the Valley of Sin in "
        "front of the green.",
}
_BETHPAGE = {
    1: "the 1st at Bethpage Black, behind whose tee stands the famous sign warning that the Black is an "
       "extremely difficult course recommended only for highly skilled golfers.",
}
_TORREY_SOUTH = {
    18: "Torrey Pines South's par-5 18th, with a pond in front of the green, where Tiger Woods holed a "
        "birdie putt to force a playoff at the 2008 U.S. Open, which he went on to win.",
}

SIGNATURE_HOLES: dict[str, dict[int, str]] = {
    "dpc sodgrass": _SAWGRASS,
    "sodgrass": _SAWGRASS,
    "dpc pebble": _PEBBLE,
    "pebblebeach": _PEBBLE,
    "pebble beach": _PEBBLE,
    "the ancient course": _OLD_COURSE,
    "bethpage black": _BETHPAGE,
    "torrey pines south": _TORREY_SOUTH,
}


def signature_hole(names, hole: int | None) -> str | None:
    """The note for this hole on the first of `names` (course ids or names) the table knows, if any."""
    if not hole:
        return None
    for name in names:
        if (holes := SIGNATURE_HOLES.get(course_key(name))) is not None:
            return holes.get(hole)
    return None
