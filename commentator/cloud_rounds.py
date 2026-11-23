"""Send this PC's rounds to Shotcast Cloud, once paired (see shotcast-cloud's docs/dev/thin-pc.md and
rounds-server.md).

Paired, the booth runs in the pairing's own cloud profile (cloud.locate), whose season file is bound
to the pairing as it's made (`bind`): every round record in it is that pairing's and goes up,
and nothing goes up from a file bound to another pairing, or to none.

A round is sent ONCE, when it's done: finished or abandoned, and quiet (what it would send hasn't
changed for QUIET_S, or CEILING_S after it was first seen over, whatever), as one body of its rows, with no
league (the PC is a capture device: the cloud decides a round's league). From then on the round is
the cloud's: nothing that changes here afterwards (a startup repair, a round GSPro takes up again)
is sent, and corrections are made in the cloud.

The round's bytes are built once and kept (committed before they're sent) until the cloud
acknowledges them, so a retry, after a crash too, sends the same bytes. It counts as sent only on
the cloud's own answer: JSON, `stored` (201) or `duplicate` (200: it had these bytes' content
already). `round_final` (409: it has the round with other content, from an earlier build of the
body) also means there's nothing more to send. Anything else, a proxy's page say, is tried again
later. A round the cloud refuses outright (too big, or not a round body) is parked, and goes again
once its record changes, or when asked (`cloud upload --refused`). Nothing is deleted here: the
rounds stay on this PC.

Only rounds go up. A practice stretch (what the booth stored outside any round: shots on a course
with no round on, and what it said) stays on this PC: it is never sent, never counted as waiting, and
never holds up an unpair. (The booth can still move a stretch's rows into the round that follows, so
a stretch isn't settled the way a round is; range sessions may be sent one day, as their own thing.)

The queue (cloud_uploads) is kept in the profile's season.db, made the first time it's used.

No time of day is kept or compared here. Every wait (a round's quiet, the ceiling, a retry, a wait
the API asks for) is counted in elapsed seconds by the run that's uploading (`Pace`: the booth's
loop, or one `cloud upload`), so nothing depends on the PC's clock, or on it being put right.
A run that starts counts afresh: a round waits out its quiet again, and a retry is tried at once.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from time import monotonic as _monotonic
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

FORMAT = 1                 # the body's format (shotcast-cloud's contract/round-body-v1.schema.json)
RETRY_BASE_S = 30.0
RETRY_MAX_S = 3600.0

TABLES = """
CREATE TABLE IF NOT EXISTS cloud_uploads (
    uuid TEXT PRIMARY KEY,                 -- the round record's
    revision INTEGER NOT NULL DEFAULT 0,   -- 1 once the cloud has the round (a round is sent once), else 0
    acked_gen INTEGER,                     -- the record's gen it was built from
    acked_content TEXT,                    -- and a hash of what it said
    frozen_revision INTEGER,               -- (1 while a body is frozen; from when a round could be sent again)
    frozen_body BLOB,                      -- the round's bytes, built once and sent as they are till acknowledged
    frozen_gen INTEGER,
    frozen_content TEXT,
    frozen_ended REAL,                     -- the round's ended_at those bytes were built at (which ending they're of)
    status TEXT NOT NULL DEFAULT 'queued', -- queued / parked (refused: see error)
    error TEXT,
    tries INTEGER NOT NULL DEFAULT 0       -- failed tries so far (how long the next waits: Pace.retry)
);
CREATE TABLE IF NOT EXISTS cloud_claim_codes (   -- a player's code to claim a round with (claim_codes)
    uuid TEXT NOT NULL,                    -- the round record's
    slot_id TEXT NOT NULL,
    code TEXT NOT NULL,                    -- made once and kept: the round's body carries its hash
    PRIMARY KEY (uuid, slot_id)
);
"""
PAIRING = """
CREATE TABLE IF NOT EXISTS cloud_pairing (   -- the pairing this season file's rounds are for (bind)
    one INTEGER PRIMARY KEY CHECK (one = 1),
    api TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    bound_at REAL NOT NULL
)"""


THROTTLE_DEFAULT_S = 300.0   # a 429 with no Retry-After
THROTTLE_MAX_S = 86400.0     # (the API's longest allowance is a day's)


# The API's upload refusals (its docs/dev/device-api.md): REFUSED are for good, as the revision is
# (`action: fix`); the rest of KNOWN are handled each in its own way in _send.
REFUSED = frozenset({"not_gzip", "body_too_large", "invalid_body", "round_id_mismatch", "invalid_request"})
KNOWN = REFUSED | {"unsupported_format", "round_final", "not_bay_writer", "round_owned_elsewhere",
                   "league_suspended", "rate_limited", "busy", "client_too_old",
                   "access_token_invalid", "device_not_paired"}
# When a round is done, and so sent. A round goes to Shotcast Cloud once, and nothing that changes
# here afterwards follows it, so it waits till the booth has stopped adding to it: it has ended, and
# what would be sent hasn't changed for QUIET_S (no shot or line added, no shot's data changed),
# as the uploader has watched it (Pace: elapsed seconds, not the time of day).
# "What would be sent" is the body itself, less the two columns that say when a line was spoken
# (lines.aired_at, aired_seconds: filled as the sign-off airs, up to a minute and a half after the
# round, for this PC's own highlight cuts). A round goes once it's known what was said, not once
# it has been said: the claim cards are up the moment a round ends, so the round should be on the
# cloud soon after. (The record's own change counter, round_records.gen, can't be used: airing a
# line moves it.) Measured on a booth's logs (212 shots, 4 rounds): GSPro's round-file data for a shot
# lands within 20 s of it (median 7), a round is declared over 5 to 14 s after its last shot, and the
# closer is written about 7 s after that. CEILING_S after the uploader first saw it over (within
# cloud_booth.DRAIN_S of its end) a round goes whatever is still changing, so one can't be held back for good. What comes later stays on this PC: a closer that
# takes longer than QUIET_S to be written, the air times of the last lines, and what the round file
# gives after the ceiling (it can give a stroke up to store.FILE_END_DUE_S = 600 s after it: a
# pick-up on the last hole, say).
QUIET_S = 30.0
CEILING_S = 180.0


class Pace:
    """What one run of the uploader (the booth's loop, or one `cloud upload`) remembers between its
    looks at the queue, timed in elapsed seconds on its own clock (time.monotonic), never by the
    time of day: when it first saw each finished round, what that round would send and since when
    that has stood, when a failed round is next tried, and how long the API said to wait. None of
    it is kept on disk: another run starts afresh."""

    def __init__(self) -> None:
        self.seen: dict[str, float] = {}               # round -> when this run first saw it over
        self.ended: dict[str, Any] = {}                # round -> the ending that was (its ended_at, as a mark only)
        self.stood: dict[str, tuple[str, float]] = {}  # round -> (what it would send: _signature; since when)
        self.retry: dict[str, float] = {}              # round -> not tried again before
        self.hold: float | None = None                 # nothing at all is sent before (the API's 429)

    def clock(self) -> float:
        return _monotonic()

    def note(self, uuid: str, sig: str, ended: Any = None) -> None:
        """A look at a finished round: one first seen, or that would now send something else,
        stands from now. `ended`: which ending of the round this is (its ended_at, compared only
        with itself): a round taken up again and finished again, even unseen in between, is seen
        anew, ceiling and all."""
        now = self.clock()
        if uuid in self.seen and self.ended.get(uuid) != ended:
            del self.seen[uuid]
            self.stood.pop(uuid, None)
        self.ended[uuid] = ended
        self.seen.setdefault(uuid, now)
        if uuid not in self.stood or self.stood[uuid][0] != sig:
            self.stood[uuid] = (sig, now)

    def ready(self, uuid: str, settle: float) -> bool:
        """Done: quiet for QUIET_S as last looked at, or `settle` (the ceiling) since first seen over."""
        if uuid not in self.seen:
            return False
        now = self.clock()
        return now - self.seen[uuid] >= settle or now - self.stood[uuid][1] >= QUIET_S

    def held(self) -> float:
        """Seconds still to wait before anything is sent (0: none)."""
        return 0.0 if self.hold is None else max(0.0, self.hold - self.clock())

    def waits(self, uuid: str) -> bool:
        """Whether a round that failed is still waiting for its next try."""
        return uuid in self.retry and self.retry[uuid] > self.clock()

    def only(self, uuids: set[str]) -> None:
        """Forget every round but these (the ones over and not sent): one sent, gone, or taken up
        again is seen anew if it comes back."""
        for kept in (self.seen, self.ended, self.stood, self.retry):
            for uuid in set(kept) - uuids:
                del kept[uuid]



class Blocked(Exception):
    """The cloud refuses every upload from this PC for now; the message says why."""


class NotThisPairing(Exception):
    """The season file isn't bound to this pairing: nothing is sent from it."""


@dataclass
class Drained:
    sent: list[str] = field(default_factory=list)       # record uuids the cloud has (acknowledged now)
    settling: int = 0                                    # over, and not done yet (still changing, or just seen)
    unreadable: int = 0                                  # can't be built from the season file (logged; left unsent)
    unchanged: int = 0                                   # nothing to send after all (not a settled round any more)
    parked: list[str] = field(default_factory=list)
    waiting: int = 0                                     # left for a later try (network, server busy, odd answer)


def connect(db_path: str) -> sqlite3.Connection:
    """The season file (which must exist), with the cloud's tables. Autocommit, each commit synced to
    disk: a revision is kept for good before it's sent."""
    db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous = FULL")
    db.executescript(TABLES)
    if "frozen_ended" not in {c["name"] for c in db.execute("PRAGMA table_info(cloud_uploads)")}:
        db.execute("ALTER TABLE cloud_uploads ADD COLUMN frozen_ended REAL")   # (a queue from an earlier build)
    db.execute(PAIRING)
    return db


@contextmanager
def _txn(db: sqlite3.Connection, mode: str = "") -> Iterator[None]:
    db.execute(f"BEGIN {mode}")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def bind(db_path: str, key: tuple | list) -> None:
    """Bind a season file to a pairing (cloud.folder_key: its API, tenant and device), as its data
    folder is made. Once bound, it's bound for good."""
    db = connect(db_path)
    try:
        with _txn(db, "IMMEDIATE"):
            got = bound(db)
            if got is not None and got != tuple(key):
                raise NotThisPairing(f"{db_path} is already bound to another Shotcast Cloud pairing")
            db.execute("INSERT OR IGNORE INTO cloud_pairing (one, api, tenant_id, device_id, bound_at) "
                       "VALUES (1, ?, ?, ?, ?)", (*key, time.time()))
    finally:
        db.close()


def bound(db: sqlite3.Connection) -> tuple | None:
    """The pairing (API, tenant, device) a season file is bound to, or None."""
    row = db.execute("SELECT api, tenant_id, device_id FROM cloud_pairing WHERE one = 1").fetchone()
    return tuple(row) if row else None


# Bodies


def claim_codes(db: sqlite3.Connection, uuid: str) -> dict[str, str]:
    """A code for each of a round's players, by slot: what a customer at a venue claims the round with
    (shotcast-cloud's docs/dev/claims.md; the bay's cards, cloud_cards, show it as a QR). Made here,
    once, when first asked for, and kept with the round: its body carries each code's hash
    (`slots[].claim_hash`), and the cloud keeps the first hash it's sent, so a code must never change.
    Made for every round: a tenant that isn't a venue ignores the hashes. Not inside a transaction of
    the caller's."""
    with _txn(db, "IMMEDIATE"):
        for (slot,) in db.execute("SELECT slot_id FROM round_slots WHERE round_uuid = ?", (uuid,)).fetchall():
            db.execute("INSERT OR IGNORE INTO cloud_claim_codes (uuid, slot_id, code) VALUES (?, ?, ?)",
                       (uuid, slot, secrets.token_urlsafe(16)))
        return {r["slot_id"]: r["code"] for r in
                db.execute("SELECT slot_id, code FROM cloud_claim_codes WHERE uuid = ?", (uuid,))}


def claim_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _rows(db: sqlite3.Connection, query: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(query, args)]


def _without(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in keys}


# The cloud's limits (its roundbody.py and rounds.py): a track of more points, or more tags, isn't
# sent; a round over either size goes without its heaviest optional parts (encode).
MAX_POINTS = 5_000
MAX_TAGS = 50
MAX_TEXT = 4_000                   # a line's text (longer is cut: the scores must still go)
MAX_BODY = 4 * 1024 * 1024         # compressed
MAX_INFLATED = 32 * 1024 * 1024    # as JSON
_SHOT_FLAGS = ("is_putt", "attributed", "is_league", "is_tournament", "mulligan", "shared_ball", "picked")
_CARD_FLAGS = ("is_league", "is_tournament", "complete")


def _wire(row: dict[str, Any], *drop: str, flags: tuple[str, ...] = (), holes: tuple[str, ...] = (),
          lists: tuple[str, ...] = (), most: int = MAX_POINTS, dates: tuple[str, ...] = ()) -> dict[str, Any]:
    """A table's row as the cloud's format has it (its contract/round-body-v1.schema.json): without
    the `drop` columns, the 0/1 `flags` as true/false, the columns kept as JSON text as what they
    hold (`holes`: a hole number -> whole number map; `lists`: a list of at most `most`), and an
    empty `dates` column as not known. What can't be read is sent as not known (null), never as
    text, nor as a guess: a flag that's neither 0 nor 1 isn't made true."""
    out = _without(row, *drop)
    for k in flags:
        v = out.get(k)
        out[k] = None if isinstance(v, bool) or v not in (0, 1) else v == 1
    for k in holes:
        out[k] = _holes(out.get(k))
    for k in lists:
        out[k] = _list(out.get(k), most)
    for k in dates:
        out[k] = out.get(k) or None
    return out


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _holes(text: Any) -> dict[str, int] | None:
    data = _loads(text)
    if not isinstance(data, dict):
        return None
    # (a hole's number as it's written, "7": not "07" as well, which would be the same hole twice)
    return {k: v for k, v in data.items()
            if isinstance(k, str) and k.isascii() and k.isdigit() and k == str(int(k))
            and isinstance(v, int) and not isinstance(v, bool)}


def _list(text: Any, most: int = MAX_POINTS) -> list | None:
    data = _loads(text)
    return data if isinstance(data, list) and len(data) <= most else None


def _sides(slots: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """(each slot's side key, in order; side label -> its key). The players under one label are one
    side; a player with no label is a side of one. A side's key is made from its players' slot ids,
    so it's the same however often the body is built, and needs nothing kept in the season file. The
    cloud ties a side's card to its players by this key, never by the label (which is made of names)."""
    members: dict[Any, list[str]] = {}
    for s in slots:
        members.setdefault(s["side"] if s["side"] is not None else ("alone", s["slot_id"]), []).append(s["slot_id"])
    key = {group: hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:16] for group, ids in members.items()}
    keys = [key[s["side"] if s["side"] is not None else ("alone", s["slot_id"])] for s in slots]
    return keys, {group: k for group, k in key.items() if isinstance(group, str)}


def body(db: sqlite3.Connection, uuid: str) -> tuple[dict[str, Any], int] | None:
    """A record's v1 body and the gen it was read at, or None if the record is gone. Local integer ids
    become uids or are dropped. No league: the PC is a capture device (the cloud decides).

    A player is named in one place, their slot (erasing an account in the cloud takes the name from
    its slot). So a shot and a card say whose they are by `slot_id` alone, with no `player`, and a
    scorecard doesn't list its side's `players`: every slot and every scorecard carries its side's key
    (`side_id`, _sides). Not sent either: what only this PC's own pages use
    (a shot's raw launch-monitor message, what a track replaced, the row-level left-out and
    excluded marks, which `edit` carries, and the record's change counters)."""
    with _txn(db):   # (one snapshot: a record's rows and its gen read together)
        rec = db.execute("SELECT * FROM round_records WHERE uuid = ?", (uuid,)).fetchone()
        if rec is None:
            return None
        rec = dict(rec)
        dataset = db.execute("SELECT dataset_id FROM meta").fetchone()[0]
        session = db.execute("SELECT id, started_at, date FROM sessions WHERE id = ?",
                             (rec["session_id"],)).fetchone()
        shots = _rows(db, "SELECT * FROM shots WHERE record_uuid = ? ORDER BY ts, id", uuid)
        uid_of = {s["id"]: s["uid"] for s in shots}
        tracks = _rows(db, "SELECT t.* FROM shot_tracks t JOIN shots s ON s.id = t.shot_id "
                           "WHERE s.record_uuid = ? ORDER BY t.shot_id", uuid)
        lines = _rows(db, "SELECT * FROM lines WHERE record_uuid = ? ORDER BY ts, id", uuid)
        cards = _rows(db, "SELECT * FROM rounds WHERE round_uuid = ? ORDER BY id", uuid)
        weeks = _rows(db, "SELECT w.* FROM week_results w JOIN rounds r ON r.id = w.round_id "
                          "WHERE r.round_uuid = ? ORDER BY w.id", uuid)
        players = _rows(db, "SELECT * FROM player_rounds WHERE round_uuid = ? ORDER BY id", uuid)
        slots = _rows(db, "SELECT * FROM round_slots WHERE round_uuid = ? ORDER BY seat, slot_id", uuid)
        codes = {r["slot_id"]: r["code"] for r in
                 db.execute("SELECT slot_id, code FROM cloud_claim_codes WHERE uuid = ?", (uuid,))}
    keys, side_of = _sides(slots)
    card_side = {c["id"]: c["side"] for c in cards}
    # One card a side: its last (a booth that stopped between writing a round's cards and marking it
    # over writes them again when the round is finished; the cloud takes one card for a side).
    last = {c["side"]: c["id"] for c in cards}
    cards = [c for c in cards if last[c["side"]] == c["id"]]
    for c in cards:
        if c["side"] not in side_of:   # (a card none of the round's players is on: nobody's to file)
            log.warning("Shotcast Cloud: round %s: the card for %r has no player in the round; not sent", uuid, c["side"])
    flags = cards + shots
    mark = ("tournament" if any(r.get("is_tournament") for r in flags)
            else "league" if any(r.get("is_league") for r in flags)
            else "practice" if rec["kind"] == "practice" else None)
    return {
        "format": FORMAT,
        "dataset_id": dataset,
        "round": _without(rec, "uuid", "session_id", "gen", "finishes_gen")
        | {"id": uuid, "session": dict(session) if session else None, "league_id": None, "league": None},
        # (A slot's claim code goes as its hash; made before a finished round is looked at: _watch.)
        "slots": [_without(s, "round_uuid") | {"side_id": key}
                  | ({"claim_hash": claim_hash(codes[s["slot_id"]])} if s["slot_id"] in codes else {})
                  for s, key in zip(slots, keys)],
        "shots": [_wire(s, "id", "session_id", "record_uuid", "player", "raw", "left_out", "excluded",
                        flags=_SHOT_FLAGS, lists=("tags",), most=MAX_TAGS) for s in shots],
        "shot_tracks": [_wire(t, "shot_id", "was", lists=("flight", "roll")) | {"shot_uid": uid_of[t["shot_id"]]}
                        for t in tracks],
        "lines": [_without(ln, "id", "session_id", "record_uuid", "shot_id", "excluded")
                  | {"shot_uid": uid_of.get(ln["shot_id"])}
                  | ({"text": ln["text"][:MAX_TEXT]} if isinstance(ln["text"], str) else {}) for ln in lines],
        "scorecards": [_wire(c, "id", "session_id", "round_uuid", "players", "excluded", flags=_CARD_FLAGS,
                             holes=("scores", "hole_pars"), dates=("date",)) | {"side_id": side_of[c["side"]]}
                       for c in cards if c["side"] in side_of],
        "player_rounds": [_wire(p, "id", "session_id", "round_uuid", "player", "excluded", flags=_CARD_FLAGS,
                                holes=("scores", "hole_pars")) for p in players],
        "week_results": [_wire(w, "id", "round_id", holes=("holes",))
                         | ({"side_id": side_of[card_side[w["round_id"]]]} if card_side.get(w["round_id"]) in side_of
                            else {}) for w in weeks],
        "edit": {"excluded": any(c["excluded"] for c in cards), "mark": mark,
                 "left_out": [s["uid"] for s in shots if s["left_out"]]},
    }, rec["gen"]


def _json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def encode(b: dict[str, Any]) -> bytes:
    """The bytes sent for a round: gzip with no timestamp, so building it again gives the same bytes.

    A round too big for the cloud (MAX_BODY compressed, MAX_INFLATED as JSON) goes without its
    heaviest optional parts, in the order the cloud's API gives, till it fits: the tracks' flight and
    roll paths, then the tracks, then the commentary. The scores, cards and shots always go; one
    still too big is sent whole, and the cloud says so (413)."""
    out = dict(b)
    lighter = (
        lambda: out.update(shot_tracks=[t | {"flight": None, "roll": None} for t in out["shot_tracks"]]),
        lambda: out.update(shot_tracks=[]),
        lambda: out.update(lines=[]),
    )
    whole = None
    for drop in (None, *lighter):
        if drop is not None:
            drop()
        data = _json(out)
        packed = gzip.compress(data, mtime=0)
        whole = whole or packed
        if len(data) <= MAX_INFLATED and len(packed) <= MAX_BODY:
            if drop is not None:
                log.warning("Shotcast Cloud: round %s is too big whole; sent without some of its tracks or commentary",
                            out["round"].get("id"))
            return packed
    return whole


# Draining the queue


def _signature(b: dict[str, Any]) -> str:
    """A hash of what a round would send, less when its lines were spoken: what has to stand still for
    QUIET_S before the round goes (and what a refused round is known by)."""
    lines = [_without(ln, "aired_at", "aired_seconds") for ln in b["lines"]]
    return hashlib.sha256(_json(b | {"lines": lines})).hexdigest()


# Rounds that are over and not on the cloud, oldest first: what a drain looks at. One the cloud has
# is never sent again, whatever changes here afterwards.
_OVER = """SELECT r.uuid, u.status, u.frozen_body IS NOT NULL AS frozen, u.frozen_content
           FROM round_records r LEFT JOIN cloud_uploads u ON u.uuid = r.uuid
           WHERE r.kind = 'round' AND r.state != 'in_progress' AND COALESCE(u.revision, 0) = 0
           ORDER BY r.started_at, r.uuid"""


def _watch(db: sqlite3.Connection, pace: Pace, rows: list[sqlite3.Row]) -> set[str]:
    """Look at each finished round whose bytes aren't built yet, and note whether what it would send
    has changed since it was last looked at: what its quiet is measured from (Pace.ready). Returns
    the rounds that can't be built (left unsent, and shown as behind): one of those mustn't hold up
    the others."""
    pace.only({r["uuid"] for r in rows})
    bad = set()
    for r in rows:
        if r["frozen"]:
            continue
        try:
            claim_codes(db, r["uuid"])   # (before its body is first read: the hashes are part of it)
            got = body(db, r["uuid"])
            if got is not None:
                pace.note(r["uuid"], _signature(got[0]), got[0]["round"].get("ended_at"))
        except Exception:   # noqa: BLE001 - a damaged row, a value that can't be sent
            log.warning("Shotcast Cloud: round %s can't be read to send", r["uuid"], exc_info=True)
            bad.add(r["uuid"])
    return bad


def _freeze(db: sqlite3.Connection, uuid: str, pace: Pace, settle: float) -> bool:
    """Build the round's body and keep its bytes until the cloud acknowledges them: those same bytes
    are what's sent, however often, across restarts. False if there's nothing to send: the cloud has
    the round already, the record is gone, or it isn't a done round any more (checked on the very
    body that's built: a round the booth took up again since the queue was looked at isn't sent in
    progress, and one that says something else than when it was last looked at starts its quiet
    again, from now, unless the ceiling has passed)."""
    claim_codes(db, uuid)   # (for a player who joined since it was last looked at: every slot sent has its hash)
    got = body(db, uuid)
    if got is None:
        return False
    b, gen = got
    if any("claim_hash" not in s for s in b["slots"]):
        return False   # (a player who joined in the instant since the codes were made: built at the next look)
    sig = _signature(b)
    pace.note(uuid, sig, b["round"].get("ended_at"))
    if b["round"]["kind"] != "round" or b["round"]["state"] == "in_progress" or not pace.ready(uuid, settle):
        return False
    db.execute("INSERT OR IGNORE INTO cloud_uploads (uuid) VALUES (?)", (uuid,))
    if db.execute("SELECT revision FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()[0]:
        return False
    db.execute("UPDATE cloud_uploads SET frozen_revision = 1, frozen_body = ?, frozen_gen = ?, frozen_content = ?, "
               "frozen_ended = ? WHERE uuid = ?", (encode(b), gen, sig, b["round"].get("ended_at"), uuid))
    return True


def _backoff(db: sqlite3.Connection, uuid: str, pace: Pace, error: str) -> None:
    tries = db.execute("SELECT tries FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()[0]
    # (the exponent capped, not only the wait: a round that waits for weeks mustn't overflow)
    pace.retry[uuid] = pace.clock() + min(RETRY_MAX_S, RETRY_BASE_S * 2 ** min(tries, 30))
    db.execute("UPDATE cloud_uploads SET tries = tries + 1, error = ? WHERE uuid = ?", (error, uuid))


def _throttle(r: Any, pace: Pace) -> float:
    """The API's allowance for this PC is spent (429): its limits are the device's, not a round's, so
    this run sends nothing more for as long as it said (Retry-After), counted from this answer.
    Returns how long that is."""
    try:
        wait = float(r.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        wait = THROTTLE_DEFAULT_S
    wait = min(max(wait, 1.0), THROTTLE_MAX_S) if wait == wait else THROTTLE_DEFAULT_S   # (not a number: nan)
    pace.hold = pace.clock() + wait
    return wait


def _park(db: sqlite3.Connection, uuid: str, error: str) -> None:
    log.warning("Shotcast Cloud won't take round %s: %s", uuid, error)
    db.execute("UPDATE cloud_uploads SET status = 'parked', error = ?, frozen_body = NULL, frozen_revision = NULL "
               "WHERE uuid = ?", (error, uuid))


def _unpark(db: sqlite3.Connection, uuid: str) -> bool:
    """A refused record that has changed since: queued again if it now says something else than what
    was refused (True), else left refused, its change noted."""
    got = body(db, uuid)
    if got is None:
        return False
    b, gen = got
    if _signature(b) == db.execute("SELECT frozen_content FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()[0]:
        db.execute("UPDATE cloud_uploads SET frozen_gen = ? WHERE uuid = ?", (gen, uuid))
        return False
    db.execute("UPDATE cloud_uploads SET status = 'queued', error = NULL, tries = 0 WHERE uuid = ?", (uuid,))
    return True


def requeue(db_path: str) -> int:
    """Queue every refused (parked) record again, as it stands, for the next drain (`cloud upload
    --refused`: once what made Shotcast Cloud refuse them is put right there). Rounds it has stay
    held. Returns how many."""
    db = connect(db_path)
    try:
        return db.execute("UPDATE cloud_uploads SET status = 'queued', error = NULL, tries = 0 "
                          "WHERE status = 'parked'").rowcount
    finally:
        db.close()


def _answer(r: Any) -> dict[str, Any] | None:
    """The cloud's JSON answer, or None for anything else (a proxy's page, say)."""
    if not str(r.headers.get("content-type", "")).split(";")[0].strip().lower() == "application/json":
        return None
    try:
        got = r.json()
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _sent(db: sqlite3.Connection, uuid: str, out: Drained) -> None:
    """The cloud has the round: it's never sent again."""
    db.execute("UPDATE cloud_uploads SET revision = 1, acked_gen = frozen_gen, acked_content = frozen_content,"
               " frozen_revision = NULL, frozen_body = NULL, frozen_gen = NULL, frozen_content = NULL,"
               " frozen_ended = NULL, status = 'queued', tries = 0, error = NULL WHERE uuid = ?", (uuid,))
    out.sent.append(uuid)


def _send(cl: Any, db: sqlite3.Connection, uuid: str, pace: Pace, out: Drained) -> bool:
    """Send a round's frozen bytes and act on the answer. False to stop draining for now."""
    from .cloud import CloudError, Unreachable, _message

    frozen = db.execute("SELECT frozen_body FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()["frozen_body"]
    try:
        r = cl.request("PUT", f"/api/rounds/{uuid}", raw=frozen,
                       headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
    except Unreachable as e:   # (the network, or a refresh that got no answer)
        _backoff(db, uuid, pace, str(e))
        out.waiting += 1
        return False
    except CloudError:   # (not paired, or paired elsewhere meanwhile: nothing more is sent)
        raise
    except Exception as e:   # noqa: BLE001 - the network
        _backoff(db, uuid, pace, f"couldn't reach Shotcast Cloud: {e}")
        out.waiting += 1
        return False
    answer = _answer(r)
    status = (answer or {}).get("status")
    # The cloud's own word that it has the round: taken now, or here already with these same bytes'
    # content (a lost answer, or a crash before it was noted).
    if (r.status_code, status) in ((201, "stored"), (200, "duplicate")):
        _sent(db, uuid, out)
        return True
    error = (answer or {}).get("error")
    final = answer or {}
    if r.status_code == 409 and error == "round_final" and final.get("action") == "fix" \
            and isinstance(final.get("sha256"), str) and len(final["sha256"]) == 64 \
            and not set(final["sha256"]) - set("0123456789abcdef"):
        # (the API's whole word for it: its code, that it's for good, and the checksum of what it has)
        # The cloud has this round already, as it was first sent, and a round is sent once: there's
        # nothing more to send for it. (What's here differs from what went: the round was changed on
        # this PC between two builds of its body. It stays as it is in the cloud, where it's changed.)
        log.info("Shotcast Cloud already has round %s as it was first sent; nothing more is sent for it", uuid)
        _sent(db, uuid, out)
        return True
    if r.status_code == 409 and error == "not_bay_writer":
        raise Blocked("another PC now writes this bay's rounds on Shotcast Cloud: pair this PC again to send its rounds")
    if r.status_code == 409 and error == "round_owned_elsewhere":
        _park(db, uuid, "the round belongs to another bay on Shotcast Cloud")
        out.parked.append(uuid)
        return True
    if r.status_code == 403 and error == "league_suspended":
        raise Blocked(f"Shotcast Cloud isn't taking rounds from this PC: {_message(r)}")
    if r.status_code == 422 and error == "unsupported_format":
        # The API is behind this app (it reads a new format before any app writes it, so this
        # passes): every round's bytes are kept, as they are, and sent once it isn't.
        _backoff(db, uuid, pace, f"Shotcast Cloud: {_message(r)}")
        out.waiting += 1
        return False
    action = (answer or {}).get("action")
    if isinstance(error, str) and (error in REFUSED or (error not in KNOWN and action == "fix")):
        # The API's own word that it will never take these bytes (too big, not a round body: koi's
        # rounds.py; or a code this app doesn't know, whose `action` says so). The round is kept,
        # refused, till its record changes or it's sent again by hand. Anything else (a code or
        # action this app doesn't know, an answer without them, a proxy's page) is tried again below,
        # the bytes kept as they are, and shows red in the tray while it keeps failing.
        _park(db, uuid, f"HTTP {r.status_code}: {_message(r)}")
        out.parked.append(uuid)
        return True
    if r.status_code == 429:
        wait = _throttle(r, pace)
        db.execute("UPDATE cloud_uploads SET error = ? WHERE uuid = ?",
                   (f"Shotcast Cloud: this PC's upload allowance is spent; waiting {int(wait)}s", uuid))
        log.warning("Shotcast Cloud: this PC's upload allowance is spent; nothing sent for %ds", int(wait))
        out.waiting += 1
        return False
    what = f"an answer this PC doesn't understand (HTTP {r.status_code})" if r.status_code in (200, 201) \
        else f"HTTP {r.status_code}"
    _backoff(db, uuid, pace, f"Shotcast Cloud: {what}")
    out.waiting += 1
    return False


def lock_path(db_path: str) -> Path:
    return Path(db_path).with_name(Path(db_path).name + ".upload.lock")


def drain(cl: Any, db_path: str, pace: Pace | None = None, settle: float = CEILING_S,
          wait: float = 60.0) -> Drained:
    """Send every done round the cloud doesn't have yet. `cl`: a paired cloud.Cloud; `db_path`: the
    season file in its pairing's profile (cloud.locate). `pace`: what this run of the uploader
    remembers between drains (the same one each time; without one, only what needs no waiting goes:
    bytes built already, and everything when `settle` is 0). `settle`: the ceiling (shorter only to
    test). One drain at a time across processes: waits `wait` seconds for another to finish, then
    TimeoutError. Raises NotThisPairing when the file isn't bound to `cl`'s pairing, Blocked when the
    cloud refuses this PC, NotPaired / PairingChanged when the stored pairing isn't `cl`'s any more."""
    from .cloud import folder_identity, home_for
    from .locks import file_lock

    key = tuple(cl.identity[:3])
    home = home_for(key)
    if Path(db_path).resolve() != (home / "logs" / "season.db").resolve() or folder_identity(home) != key:
        raise NotThisPairing(f"{db_path} isn't this pairing's profile's season file; nothing sent")
    with file_lock(lock_path(db_path), wait, "another upload holds"):
        db = connect(db_path)
        try:
            if bound(db) != key:
                raise NotThisPairing(f"{db_path} isn't bound to this pairing; nothing sent")
            return _drain(cl, db, Pace() if pace is None else pace, settle)
        finally:
            db.close()


def _drain(cl: Any, db: sqlite3.Connection, pace: Pace, settle: float) -> Drained:
    out = Drained()
    # A round taken up again before the cloud had it (in progress now, or over again with another
    # ending than the one its bytes were built at, however long ago): those bytes aren't what it is
    # any more, so they're dropped, and it's built again once it's over and quiet. (If the cloud
    # did take those bytes and its answer was lost, the new ones get `round_final`: done either way.)
    # (Whatever the row's status: a refused round keeps no bytes, and a queue from an earlier build can
    # have kept some under a status this build doesn't write.)
    db.execute("""UPDATE cloud_uploads SET frozen_revision = NULL, frozen_body = NULL, frozen_gen = NULL,
                         frozen_content = NULL, frozen_ended = NULL
                  WHERE revision = 0 AND frozen_body IS NOT NULL
                    AND uuid IN (SELECT r.uuid FROM round_records r WHERE r.state = 'in_progress'
                                 OR r.ended_at IS NOT cloud_uploads.frozen_ended)""")
    rows = db.execute(_OVER).fetchall()
    bad = _watch(db, pace, rows)
    held = pace.held() > 0   # (the API said to wait: the whole queue does, every round's bytes kept)
    for row in rows:
        uuid = row["uuid"]
        if uuid in bad:
            out.unreadable += 1
            continue
        if row["status"] == "parked" and uuid in pace.stood and pace.stood[uuid][0] == row["frozen_content"]:
            out.unchanged += 1   # (refused, and it would send the same again)
            continue
        if not row["frozen"] and not pace.ready(uuid, settle):
            out.settling += 1
            continue
        if held:
            out.waiting += 1
            continue
        if pace.waits(uuid):
            continue
        # A refused round goes again only once what it would send has changed (whatever changed: a
        # row of the round, or something else its body reads).
        try:
            if row["status"] == "parked" and not _unpark(db, uuid):
                out.unchanged += 1
                continue
            built = row["frozen"] or _freeze(db, uuid, pace, settle)
        except Exception:   # noqa: BLE001 - it could be read a moment ago (_watch), and can't now
            log.warning("Shotcast Cloud: round %s can't be read to send", uuid, exc_info=True)
            out.unreadable += 1
            continue
        if not built:
            if uuid in pace.seen and not pace.ready(uuid, settle):
                out.settling += 1   # (it changed as it was built: its quiet starts again)
            else:
                out.unchanged += 1
            continue
        # (taken up again, over again or gone while others were being sent: not sent as it was; the
        # next drain drops those bytes)
        over = db.execute(
            """SELECT 1 FROM round_records r JOIN cloud_uploads u ON u.uuid = r.uuid
               WHERE r.uuid = ? AND r.kind = 'round' AND r.state != 'in_progress'
                 AND r.ended_at IS u.frozen_ended""", (uuid,)).fetchone()
        if over is None:
            again = db.execute("SELECT 1 FROM round_records WHERE uuid = ? AND kind = 'round' AND state != 'in_progress'",
                               (uuid,)).fetchone()
            if again is not None:
                out.settling += 1   # (over again, with another ending: it's watched anew from the next look)
            else:
                out.unchanged += 1
            continue
        if not _send(cl, db, uuid, pace, out):
            break
    return out


# How `cloud status` words each kind of record (status, outstanding).
STATUS_WORDS = (("in_progress", "in progress"), ("never", "not sent yet"),
                ("waiting", "waiting for an answer"), ("parked", "refused"), ("sent", "on Shotcast Cloud"))
_OUTSTANDING_WORDS = {"in_progress": "in progress", "never": "not sent yet",
                      "waiting": "sent, not yet acknowledged", "parked": "refused"}


def _kinds(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """(record uuid, kind) for every round (not practice stretches: those are never sent), from
    round_records joined to the queue. (A round that's over and not sent is "never", whether it's
    still settling or not: only the run that's uploading knows that, see Pace.)"""
    out = []
    for r in db.execute(
            """SELECT r.uuid, r.state, u.revision, u.frozen_body IS NOT NULL AS frozen, u.status
               FROM round_records r LEFT JOIN cloud_uploads u ON u.uuid = r.uuid
               WHERE r.kind = 'round' ORDER BY r.started_at, r.uuid"""):
        if r["revision"]:
            kind = "sent"   # (the cloud has it: for good, whatever has happened to it here since)
        elif r["state"] == "in_progress":
            kind = "in_progress"
        elif r["status"] == "parked":
            kind = "parked"
        elif r["frozen"]:
            kind = "waiting"
        else:
            kind = "never"
        out.append((r["uuid"], kind))
    return out


def status(db_path: str) -> dict[str, int]:
    """How this PC's rounds stand, counted from its round records (STATUS_WORDS' kinds)."""
    db = connect(db_path)
    try:
        counts = dict.fromkeys((k for k, _ in STATUS_WORDS), 0)
        for _, kind in _kinds(db):
            counts[kind] += 1
        return counts
    finally:
        db.close()


BEHIND = ("never", "waiting", "parked")   # over, and not on Shotcast Cloud


def behind(db_path: str) -> int:
    """How many finished rounds aren't on Shotcast Cloud, whatever the reason (just over, never
    sent, waiting for an answer, refused). The one measure of upload health: while it stays above 0
    for long, the tray is red (cloud_booth) and `cloud upload` fails."""
    db = connect(db_path)
    try:
        return sum(kind in BEHIND for _, kind in _kinds(db))
    finally:
        db.close()


def health(db_path: str) -> list[tuple[str, str]]:
    """What needs someone, for the tray, from what the season file keeps (so it holds till it's
    resolved): a failure ('fail') for rounds refused. Rounds that
    simply aren't going up are `behind`'s."""
    db = connect(db_path)
    try:
        # (of the rounds there are: a refused one whose record is gone is nobody's to see to)
        got = dict(db.execute("SELECT u.status, COUNT(*) FROM cloud_uploads u JOIN round_records r ON r.uuid = u.uuid "
                              "WHERE r.kind = 'round' GROUP BY u.status").fetchall())
    finally:
        db.close()
    out = []
    if n := got.get("parked"):
        out.append(("fail", f"Shotcast Cloud: {n} round(s) refused (see cloud status)"))
    return out


def outstanding(db_path: str) -> list[tuple[str, str]]:
    """(uuid, what) for each round record that isn't on Shotcast Cloud as it stands here."""
    db = connect(db_path)
    try:
        return [(uuid, _OUTSTANDING_WORDS[kind]) for uuid, kind in _kinds(db) if kind != "sent"]
    finally:
        db.close()
