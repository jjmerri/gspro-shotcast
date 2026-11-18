"""Which club fields a player's launch monitor really measures, learned per club.

A monitor (or its connector) writes 0.0 for a club field it didn't measure, and what it measures
depends on the club: a GC3 or an EYE XO2 needs stickers or markers for some fields, and a player may
have them on some clubs and not others. So a field counts as measured for a player's club once it has
come through non-zero with that club; until then it's dropped (None), never read as a real zero. Each
club is learned on its own (a new set starts over) and never from another player.

What's been seen is the season's shots table itself (Store.club_fields_seen), read afresh for each
shot, so it survives a restart, and a shot moved to another player moves what it taught with it.
"""

import logging
from collections.abc import Callable
from dataclasses import replace

from .models import Shot

log = logging.getLogger(__name__)

# Shot attribute -> its column in the shots table.
COLUMNS = {
    "club_speed": "club_speed", "path": "club_path", "angle_of_attack": "angle_of_attack",
    "face_to_target": "face_to_target", "loft": "dynamic_loft",
    "horizontal_face_impact": "face_impact_h", "vertical_face_impact": "face_impact_v",
}
# Only stored, or read, when the monitor reported club data for the shot (Shot.club_measured).
DELIVERY = tuple(f for f in COLUMNS if f != "club_speed")


def apply(player: str | None, shot: Shot, seen: Callable[[str, str], set[str]]) -> Shot:
    """The shot with the club fields this player's monitor doesn't measure with this club as None.
    `seen(player, club)` gives the shots table's columns ever non-zero for them (Store.club_fields_seen);
    this shot's own non-zero fields count too. A shot where every field known for the club reads 0 (or
    is missing) is a misread: all its club fields are dropped, so it teaches nothing once recorded.
    With no player or club the shot is left as it is: there's nothing to learn it against."""
    if not player or player == "unknown" or not shot.club:
        return shot
    # A shot without club data has no delivery fields (whatever it says, they're never stored or used).
    fields = ("club_speed", *DELIVERY) if shot.club_measured else ("club_speed",)
    cols = seen(player, shot.club)
    known = {f for f, c in COLUMNS.items() if c in cols}
    if known and not any(getattr(shot, f) for f in known.intersection(fields)):
        log.info("Club data: every club field %s's %s is known to measure read 0 or missing: a misread, "
                 "its club data dropped", player, shot.club)
        return replace(shot, **{f: None for f in fields})
    if new := {f for f in fields if getattr(shot, f)} - known:
        log.info("Club data: %s's %s measures %s", player, shot.club, ", ".join(sorted(new)))
        known |= new
    return replace(shot, **{f: None for f in fields if f not in known})
