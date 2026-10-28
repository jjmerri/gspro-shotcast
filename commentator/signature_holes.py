"""Famous holes on GSPro's replicas of real courses, so the booth knows when the group stands on one.

Keyed by trivia.course_key() of GSPro's course id or name, like trivia.KNOWN_PAGES, then by hole
number. Each note is a short, well-documented fact about the real hole: the booth can mention it on
the tee, but it's told to stick to what the note says, so keep notes to things that are on the record.
Courses are GSPro's names for them as listed on simulatorgolftour.com/courses (renamed replicas such
as "Georgia Golf Club" and "DPC Royal noorT" were matched to the real course by location and card).
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
    4: "the par-5 4th at Bethpage Black, climbing past huge bunkers cut into the hillside.",
}
_TORREY_SOUTH = {
    18: "Torrey Pines South's par-5 18th, with a pond in front of the green, where Tiger Woods holed a "
        "birdie putt to force a playoff at the 2008 U.S. Open, which he went on to win.",
}

_AUGUSTA = {
    11: "Augusta National's 11th, White Dogwood, the start of Amen Corner, with a pond guarding the left "
        "of the green; Larry Mize chipped in from off the green here to win the 1987 Masters playoff.",
    12: "Augusta National's par-3 12th, Golden Bell, the heart of Amen Corner: a shallow green across "
        "Rae's Creek, where the swirling wind makes club choice famously hard.",
    13: "Augusta National's par-5 13th, Azalea, the end of Amen Corner, reachable in two with a "
        "tributary of Rae's Creek running in front of the green.",
    16: "Augusta National's par-3 16th, Redbud, played over water; Tiger Woods's chip-in here in 2005 "
        "hung on the edge of the cup before it dropped.",
}
_CARNOUSTIE = {
    6: "Carnoustie's par-5 6th, Hogan's Alley, named for Ben Hogan, who won the 1953 Open here driving "
       "down the narrow strip between the fairway bunkers and the out of bounds.",
    18: "Carnoustie's 18th, with the Barry Burn crossing it twice; Jean van de Velde stood on this tee "
        "with a three-shot lead at the 1999 Open, made a triple-bogey seven and lost the playoff.",
}
_TROON = {
    8: "Royal Troon's 8th, the Postage Stamp, the shortest hole on the Open rota; Gene Sarazen made a "
       "hole-in-one here at the 1973 Open, aged 71.",
}
_PRESTWICK = {
    17: "Prestwick's 17th, the Alps, a blind approach over a huge sandhill to a green guarded by the "
        "Sahara bunker; Prestwick hosted the first Open Championship, in 1860.",
}
_PORTRUSH = {
    16: "Royal Portrush's par-3 16th, Calamity Corner, played across a deep ravine; Shane Lowry won the "
        "2019 Open on this course.",
}
_PINEHURST = {
    18: "Pinehurst No. 2's 18th, where Payne Stewart holed a 15-foot par putt to win the 1999 U.S. Open; "
        "his statue stands behind the green.",
}
_MERION = {
    11: "Merion's 11th, where Bobby Jones completed golf's Grand Slam in 1930.",
    18: "Merion's 18th, where Ben Hogan hit his famous 1-iron to the green at the 1950 U.S. Open; a plaque "
        "in the fairway marks the spot.",
}
_PINE_VALLEY = {
    7: "Pine Valley's 7th, home of Hell's Half Acre, a huge expanse of sand the second shot has to carry.",
    10: "Pine Valley's par-3 10th, with a tiny, notoriously deep pot bunker in front of the green.",
}
_KIAWAH = {
    17: "the Ocean Course's par-3 17th at Kiawah Island, played over water.",
    18: "the Ocean Course's 18th at Kiawah Island, where Bernhard Langer missed a six-foot putt on the "
        "final green that decided the 1991 Ryder Cup, the War by the Shore.",
}
_QUAIL_HOLLOW = {
    16: "Quail Hollow's 16th, the start of the Green Mile, the hard closing stretch of 16, 17 and 18.",
    17: "Quail Hollow's par-3 17th, over water, the middle of the Green Mile.",
    18: "Quail Hollow's 18th, the end of the Green Mile, with a creek all the way down the left.",
}
_CYPRESS = {
    15: "Cypress Point's par-3 15th, a short shot across an ocean inlet.",
    16: "Cypress Point's par-3 16th, a long carry over the Pacific to a green on a rocky point, one of "
        "the most famous holes in golf.",
}
_RIVIERA = {
    6: "Riviera's par-3 6th, famous for the bunker in the middle of the green.",
    10: "Riviera's 10th, the short, drivable par 4 that tour players argue over: go for it or lay up.",
}
_COLONIAL = {n: f"Colonial's {n}th, part of the Horrible Horseshoe, the tough stretch of holes 3, 4 and 5."
             for n in (4, 5)} | {3: "Colonial's 3rd, the start of the Horrible Horseshoe, the tough stretch "
                                    "of holes 3, 4 and 5."}

SIGNATURE_HOLES: dict[str, dict[int, str]] = {
    # TPC Sawgrass, Pebble Beach, the Old Course ("The Ancient Course" is its Mars-themed replica),
    # Bethpage Black, Torrey Pines South
    "dpc sodgrass": _SAWGRASS,
    "sodgrass": _SAWGRASS,
    "dpc pebble": _PEBBLE,
    "pebblebeach": _PEBBLE,
    "pebble beach": _PEBBLE,
    "the ancient course": _OLD_COURSE,
    "bethpage black": _BETHPAGE,
    "torrey pines south": _TORREY_SOUTH,
    "torrey south": _TORREY_SOUTH,
    # Augusta National
    "georgia golf club": _AUGUSTA,
    "carnoustie": _CARNOUSTIE,
    # Royal Troon
    "dpc royal noort": _TROON,
    "prestwick golf club": _PRESTWICK,
    "prestwick golf club tournament": _PRESTWICK,
    "north berwick west links": {
        15: "North Berwick's 15th, the original Redan, the par 3 whose angled green has been copied by "
            "architects all over the world."},
    "royal dornoch": {
        14: "Royal Dornoch's 14th, Foxy, famous for having no bunkers at all, just a raised green and the "
            "natural ground."},
    "dpc portrush": _PORTRUSH,
    "dpc county down": {
        9: "Royal County Down's 9th, where the drive goes blind over a dune toward the Mountains of Mourne, "
           "one of the most photographed views in golf."},
    "royal st georges": {
        4: "Royal St George's 4th, home of the Himalaya, one of the biggest bunkers in championship golf, "
           "beside the fairway."},
    "royal birkdale golf club": {
        18: "Royal Birkdale's 18th, where at the 1969 Ryder Cup Jack Nicklaus conceded Tony Jacklin's short "
            "putt to halve their match, the famous Concession."},
    "le golf national": {
        18: "Le Golf National's 18th, with water short and left of the green; the course hosted the 2018 "
            "Ryder Cup."},
    "pinehurst 2": _PINEHURST,
    "oakmont cc": {
        3: "Oakmont's 3rd, alongside the Church Pews, the famous bunker with rows of grass ridges between "
           "the 3rd and 4th fairways."},
    "merion golf club": _MERION,
    # Winged Foot West
    "wing da'foot west course": {
        18: "Winged Foot West's 18th, where Phil Mickelson needed a par to win the 2006 U.S. Open and made "
            "double bogey."},
    "pine valley": _PINE_VALLEY,
    "pinevalley": _PINE_VALLEY,
    "muirfield village golf club": {
        16: "Muirfield Village's par-3 16th, over water; Tiger Woods holed a flop shot from behind the green "
            "here on his way to winning the 2012 Memorial."},
    "harbour town golf links at sea pines": {
        18: "Harbour Town's 18th, along Calibogue Sound with the red-and-white lighthouse behind the green."},
    "kiawah ocean course": _KIAWAH,
    "bay hill": {
        18: "Bay Hill's 18th, with water short and right of the green, the closing hole of Arnold Palmer's "
            "tournament."},
    "blue monster at doral": {18: "Doral's Blue Monster 18th, with water down the entire left side."},
    "riviera": _RIVIERA,
    "cypress point club": _CYPRESS,
    "colonial country club": _COLONIAL,
    "quail hollow club": _QUAIL_HOLLOW,
    "quail hollow club championship edition": _QUAIL_HOLLOW,
    # A fantasy course, but its 16th is TPC Scottsdale's stadium hole
    "dpc scottsdale": {
        16: "a stadium par 3 like TPC Scottsdale's 16th, the Phoenix Open hole ringed by grandstands, where "
            "Tiger Woods made a hole-in-one in 1997."},
    "kapalua - plantation course": {
        18: "Kapalua Plantation's 18th, a par 5 of over 660 yards that plays steeply downhill toward the "
            "ocean."},
    "firestone country club": {16: "Firestone South's 16th, the long par 5 nicknamed the Monster."},
    "cape kidnappers": {
        15: "Cape Kidnappers' par-5 15th, Pirate's Plank, running out along a narrow ridge with cliffs "
            "falling away on both sides."},
    "cabot cliffs": {
        16: "Cabot Cliffs' par-3 16th, played across a chasm in the cliffs to a green above the Gulf of "
            "St. Lawrence."},
    "cabotcliffs": {
        16: "Cabot Cliffs' par-3 16th, played across a chasm in the cliffs to a green above the Gulf of "
            "St. Lawrence."},
    # One par 3 played 18 times: say it once, on the first
    "paynes valley 19th": {
        1: "Payne's Valley's 19th hole, the Big Rock, a bonus par 3 played down to a green set among rock "
           "walls and water."},
}


def signature_hole(names, hole: int | None) -> str | None:
    """The note for this hole on the first of `names` (course ids or names) the table knows, if any."""
    if not hole:
        return None
    for name in names:
        if (holes := SIGNATURE_HOLES.get(course_key(name))) is not None:
            return holes.get(hole)
    return None
