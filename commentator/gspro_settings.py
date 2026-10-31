"""GSPro's settings (Settings.vgs, next to its Unity log): a .NET BinaryFormatter (MS-NRBF) stream.

Only the game options are read for a problem report; the file also holds the players' names and
emails, which are left out."""
from __future__ import annotations

import struct
from pathlib import Path

# GameData members worth seeing in a problem report: what GSPro shows, hides and how it plays.
# (Hide UI on shot isn't saved in the file, nor anywhere else found.)
REPORTED = (
    "BallReadyHideInterface", "units", "LMCarry", "PlayMode", "ShotBoost", "ShootBoostType", "BallCurvature",
    "CameraControlMethod", "FollowCamDelay", "AutoFlyover", "HoleIntroOn", "ShowAimIndicator", "ShowBallIndicator",
    "ShowUpNextMessage", "ClubSelectorHideOption", "HideBannerOnGreen", "MiniMapLocation", "ControlStyle",
    "DistanceCountOption", "scorecardAfterHole", "ScorecardStyle", "PlayerPanelAlternativeLayout",
    "MatchSettingsCondensed", "NarrowUIMode", "NarrowUIModeMultiplier", "NarrowUIModeMultiplierScorecard",
    "ShowSGTLeaderboard", "TrailSize", "temperature", "altitude", "GhostPlayEnabled",
)

# Settings.vgs holds ~1,000 player slots; a corrupt length mustn't run memory up.
MAX_ITEMS = 1_000_000

_PRIMITIVE = {1: "<?", 2: "<B", 6: "<d", 7: "<h", 8: "<i", 9: "<q", 10: "<b", 11: "<f", 12: "<q", 13: "<q",
              14: "<H", 15: "<I", 16: "<Q"}


class _Ref:
    def __init__(self, ref: int) -> None:
        self.ref = ref


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0
        self.objects: dict[int, object] = {}
        self.classes: dict[int, tuple[str, list[str], list[tuple[int, object]]]] = {}
        self.budget = MAX_ITEMS

    def items(self, n: int) -> int:
        """Charge an object of n items (array entries, members, names) to the budget: n + 1."""
        if n < 0 or n + 1 > self.budget:
            raise ValueError("Settings.vgs: too many objects or items")
        self.budget -= n + 1
        return n

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("Settings.vgs ended early")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def int32(self) -> int:
        return self.unpack("<i")

    def string(self) -> str:
        n = 0
        for shift in range(0, 35, 7):   # at most five bytes of length
            b = self.take(1)[0]
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                break
        else:
            raise ValueError("Settings.vgs: a string length too long")
        return self.take(n).decode("utf-8", errors="replace")

    def primitive(self, kind: int):
        if kind == 3:   # Char: one UTF-8 character
            first = self.data[self.pos]
            size = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
            return self.take(size).decode("utf-8", errors="replace")
        if kind in (5, 18):   # Decimal and String are length-prefixed strings
            return self.string()
        if kind == 17:
            return None
        return self.unpack(_PRIMITIVE[kind])

    def class_info(self) -> tuple[int, str, list[str]]:
        oid, name, count = self.int32(), self.string(), self.int32()
        return oid, name, [self.string() for _ in range(self.items(count))]

    def member_types(self, count: int) -> list[tuple[int, object]]:
        kinds = list(self.take(count))
        out = []
        for k in kinds:
            if k in (0, 7):
                out.append((k, self.take(1)[0]))
            elif k == 3:
                out.append((k, self.string()))
            elif k == 4:
                out.append((k, (self.string(), self.int32())))
            else:
                out.append((k, None))
        return out

    def members(self, oid: int, name: str, names: list[str], types: list[tuple[int, object]]) -> dict:
        self.items(len(names))
        obj: dict = {"__class__": name}
        self.objects[oid] = obj
        for member, (kind, extra) in zip(names, types):
            obj[member] = self.primitive(extra) if kind == 0 else self.record()
        return obj

    def nulls(self, rec: int) -> int:
        return self.take(1)[0] if rec == 13 else self.int32()

    def array_items(self, length: int) -> list:
        items: list = []
        while len(items) < length:
            rec = self.data[self.pos]
            if rec in (13, 14):
                self.pos += 1
                n = self.nulls(rec)
                if n > length - len(items):
                    raise ValueError("Settings.vgs: more nulls than the array holds")
                items += [None] * n
            else:
                items.append(self.record())
        return items

    def record(self):
        rec = self.take(1)[0]
        if rec == 12:   # BinaryLibrary, then the record it precedes
            self.int32(), self.string()
            return self.record()
        if rec in (4, 5):   # (System)ClassWithMembersAndTypes
            oid, name, names = self.class_info()
            types = self.member_types(len(names))
            if rec == 5:
                self.int32()
            self.classes[oid] = (name, names, types)
            return self.members(oid, name, names, types)
        if rec == 1:   # ClassWithId: the same shape as an earlier class
            oid, meta = self.int32(), self.int32()
            name, names, types = self.classes[meta]
            return self.members(oid, name, names, types)
        if rec == 6:
            oid = self.int32()
            self.items(0)
            self.objects[oid] = value = self.string()
            return value
        if rec == 9:
            return _Ref(self.int32())
        if rec == 10:
            return None
        if rec == 8:
            return self.primitive(self.take(1)[0])
        if rec == 15:   # ArraySinglePrimitive
            oid, length, kind = self.int32(), self.items(self.int32()), self.take(1)[0]
            self.objects[oid] = value = [self.primitive(kind) for _ in range(length)]
            return value
        if rec in (16, 17):   # ArraySingleObject / ArraySingleString
            oid, length = self.int32(), self.items(self.int32())
            self.objects[oid] = value = self.array_items(length)
            return value
        if rec == 7:   # BinaryArray
            oid, shape, rank = self.int32(), self.take(1)[0], self.int32()
            lengths = [self.int32() for _ in range(self.items(rank))]
            if shape in (3, 4, 5):
                [self.int32() for _ in range(rank)]
            kind, extra = self.member_types(1)[0]
            total = 1
            for n in lengths:
                if n < 0 or total * n > self.budget:
                    raise ValueError("Settings.vgs: too many objects or items")
                total *= n
            self.items(total)
            value = [self.primitive(extra) for _ in range(total)] if kind == 0 else self.array_items(total)
            self.objects[oid] = value
            return value
        raise ValueError(f"Settings.vgs: unknown record type {rec} at {self.pos - 1}")

    def read(self):
        if self.take(1)[0] != 0:
            raise ValueError("not a BinaryFormatter stream")
        self.take(16)   # header: root id, header id, version
        root = None
        while self.pos < len(self.data):
            if self.data[self.pos] == 11:   # MessageEnd
                break
            value = self.record()
            root = value if root is None else root
        return root


def _follow(value, objects: dict):
    seen: set[int] = set()
    while isinstance(value, _Ref) and value.ref not in seen:
        seen.add(value.ref)
        value = objects.get(value.ref)
    return None if isinstance(value, _Ref) else value


def _option(value, objects: dict):
    """A reported option's value: references followed, an enum its number. Nothing nested is
    expanded, so the players' details are never resolved."""
    value = _follow(value, objects)
    if isinstance(value, dict) and set(value) == {"__class__", "value__"}:
        value = _follow(value["value__"], objects)
    return value if value is None or isinstance(value, (bool, int, float, str)) else None


def read_game_settings(path: Path) -> dict:
    """The REPORTED game options in GSPro's Settings.vgs, as {name: value}."""
    r = _Reader(path.read_bytes())
    root = _follow(r.read(), r.objects)
    game = _follow(root.get("Game_s"), r.objects) if isinstance(root, dict) else None
    if not isinstance(game, dict):
        raise ValueError("no game settings in Settings.vgs")
    return {k: _option(game[k], r.objects) for k in REPORTED if k in game}
