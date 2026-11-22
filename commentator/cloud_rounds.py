"""Send this PC's rounds to Shotcast Cloud, once paired (see shotcast-cloud's docs/dev/thin-pc.md and
rounds-server.md).

Paired, the booth runs in the pairing's own cloud profile (cloud.locate), whose season file is bound
to the pairing as it's made (`bind`): every round record in it is that pairing's and goes up,
and nothing goes up from a file bound to another pairing, or to none. Each round goes as a body of
its rows once it has settled: finished or abandoned, and
CLOSER_SECONDS past its end, with no league (the PC is a capture device: the cloud decides a round's
league). A record that changes after it was sent goes up again as the next revision, found by its
change counter (round_records.gen).

A revision's bytes are built once and kept (committed before they're sent) until the cloud
acknowledges them, so a retry sends the same bytes. It counts as sent only on the cloud's own answer
for that revision: JSON, `stored` (201) or `duplicate` (200) with the revision sent. Anything else,
a proxy's page say, is tried again later. The cloud having a revision this PC didn't send (a
`duplicate` or `superseded` answer for a later one, or a `revision_conflict`: other bytes under that
number) is never overwritten from here: the record is held as a conflict, its snapshot kept, for
someone to reconcile (`cloud status` counts them). A 409 never counts as sent. A record the cloud
refuses outright (too big, or not a round body) is parked, and goes again once it changes, or when
asked (`cloud upload --refused`). Nothing is deleted here: the rounds stay on this PC.

Only rounds go up. A practice stretch (what the booth stored outside any round: shots on a course
with no round on, and what it said) stays on this PC: it is never sent, never counted as waiting, and
never holds up an unpair. (The booth can still move a stretch's rows into the round that follows, so
a stretch isn't settled the way a round is; range sessions may be sent one day, as their own thing.)

The queue (cloud_uploads) is kept in the profile's season.db, made the first time it's used.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import time
from time import monotonic as _monotonic
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import CLOSER_SECONDS

log = logging.getLogger(__name__)

FORMAT = 1                 # the body's format (shotcast-cloud's contract/round-body-v1.schema.json)
RETRY_BASE_S = 30.0
RETRY_MAX_S = 3600.0

TABLES = """
CREATE TABLE IF NOT EXISTS cloud_uploads (
    uuid TEXT PRIMARY KEY,                 -- the round record's
    revision INTEGER NOT NULL DEFAULT 0,   -- the newest revision the cloud has acknowledged (0: none)
    acked_gen INTEGER,                     -- the record's gen that revision was built from
    acked_content TEXT,                    -- and a hash of what it said
    frozen_revision INTEGER,               -- a revision built and not yet acknowledged: sent as is till it is
    frozen_body BLOB,
    frozen_gen INTEGER,
    frozen_content TEXT,
    status TEXT NOT NULL DEFAULT 'queued', -- queued / conflict (held: see error) / parked (refused: see error)
    error TEXT,
    tries INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0
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


THROTTLE = """
CREATE TABLE IF NOT EXISTS cloud_throttle (  -- the API said to wait (429): nothing is sent from this PC till then
    one INTEGER PRIMARY KEY CHECK (one = 1),
    not_before REAL NOT NULL
)"""
THROTTLE_DEFAULT_S = 300.0   # a 429 with no Retry-After
THROTTLE_MAX_S = 86400.0     # (the API's longest allowance is a day's)


# The API's upload refusals (its docs/dev/device-api.md): REFUSED are for good, as the revision is
# (`action: fix`); the rest of KNOWN are handled each in its own way in _send.
REFUSED = frozenset({"not_gzip", "body_too_large", "invalid_body", "round_id_mismatch", "invalid_request"})
KNOWN = REFUSED | {"unsupported_format", "revision_conflict", "not_bay_writer", "round_owned_elsewhere",
                   "league_suspended", "round_league_suspended", "rate_limited", "busy", "client_too_old",
                   "access_token_invalid", "device_not_paired"}


class Blocked(Exception):
    """The cloud refuses every upload from this PC for now; the message says why."""


class NotThisPairing(Exception):
    """The season file isn't bound to this pairing: nothing is sent from it."""


@dataclass
class Drained:
    sent: list[str] = field(default_factory=list)       # record uuids acknowledged
    unchanged: int = 0                                   # changed counters, same body: nothing sent
    parked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # held: the cloud has a revision this PC didn't send
    waiting: int = 0                                     # left for a later try (network, server busy, odd answer)


def connect(db_path: str) -> sqlite3.Connection:
    """The season file (which must exist), with the cloud's tables. Autocommit, each commit synced to
    disk: a revision is kept for good before it's sent."""
    db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous = FULL")
    db.executescript(TABLES)
    db.execute(PAIRING)
    db.execute(THROTTLE)
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


def _rows(db: sqlite3.Connection, query: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(query, args)]


def _without(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in keys}


# The cloud's limits (its roundbody.py and rounds.py): a track of more points, or more tags, isn't
# sent; a round over either size goes without its heaviest optional parts (encode).
MAX_POINTS = 5_000
MAX_TAGS = 50
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
            if isinstance(k, str) and k.isdigit() and k == str(int(k)) and isinstance(v, int) and not isinstance(v, bool)}


def _list(text: Any, most: int = MAX_POINTS) -> list | None:
    data = _loads(text)
    return data if isinstance(data, list) and len(data) <= most else None


def body(db: sqlite3.Connection, uuid: str) -> tuple[dict[str, Any], int] | None:
    """A record's v1 body and the gen it was read at, or None if the record is gone. Local integer ids
    become uids or are dropped. No league: the PC is a capture device (the cloud decides).

    A player is named in one place, their slot (erasing an account in the cloud takes the name from
    its slot). So a shot and a card say whose they are by `slot_id` alone, with no `player`, and a
    scorecard doesn't list its side's `players`. Not sent either: what only this PC's own pages use
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
    flags = cards + shots
    mark = ("tournament" if any(r.get("is_tournament") for r in flags)
            else "league" if any(r.get("is_league") for r in flags)
            else "practice" if rec["kind"] == "practice" else None)
    return {
        "format": FORMAT,
        "dataset_id": dataset,
        "round": _without(rec, "uuid", "session_id", "gen", "finishes_gen")
        | {"id": uuid, "session": dict(session) if session else None, "league_id": None, "league": None},
        "slots": [_without(s, "round_uuid") for s in slots],
        "shots": [_wire(s, "id", "session_id", "record_uuid", "player", "raw", "left_out", "excluded",
                        flags=_SHOT_FLAGS, lists=("tags",), most=MAX_TAGS) for s in shots],
        "shot_tracks": [_wire(t, "shot_id", "was", lists=("flight", "roll")) | {"shot_uid": uid_of[t["shot_id"]]}
                        for t in tracks],
        "lines": [_without(ln, "id", "session_id", "record_uuid", "shot_id", "excluded")
                  | {"shot_uid": uid_of.get(ln["shot_id"])} for ln in lines],
        "scorecards": [_wire(c, "id", "session_id", "round_uuid", "players", "excluded", flags=_CARD_FLAGS,
                             holes=("scores", "hole_pars"), dates=("date",)) for c in cards],
        "player_rounds": [_wire(p, "id", "session_id", "round_uuid", "player", "excluded", flags=_CARD_FLAGS,
                                holes=("scores", "hole_pars")) for p in players],
        "week_results": [_wire(w, "id", "round_id", holes=("holes",)) for w in weeks],
        "edit": {"excluded": any(c["excluded"] for c in cards), "mark": mark,
                 "left_out": [s["uid"] for s in shots if s["left_out"]]},
    }, rec["gen"]


def _json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def _content(b: dict[str, Any]) -> str:
    """What a revision says, apart from the starting edit values only revision 1 carries."""
    return hashlib.sha256(_json(_without(b, "edit"))).hexdigest()


def encode(b: dict[str, Any], revision: int) -> bytes:
    """The bytes sent for a revision: gzip with no timestamp, so building it again gives the same bytes.

    A round too big for the cloud (MAX_BODY compressed, MAX_INFLATED as JSON) goes without its
    heaviest optional parts, in the order the cloud's API gives, till it fits: the tracks' flight and
    roll paths, then the tracks, then the commentary. The scores, cards and shots always go; one
    still too big is sent whole, and the cloud says so (413)."""
    out = dict(b)
    if revision != 1:
        out.pop("edit", None)
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


def _due(db: sqlite3.Connection, now: float, settle: float) -> list[sqlite3.Row]:
    """Settled rounds to send (new, changed, or a revision waiting to be acknowledged), oldest first.
    Held (conflict) ones aren't, nor refused (parked) ones till the record changes (_unpark)."""
    return db.execute(
        """SELECT r.uuid FROM round_records r
           LEFT JOIN cloud_uploads u ON u.uuid = r.uuid
           WHERE r.kind = 'round' AND r.state != 'in_progress' AND COALESCE(r.ended_at, r.started_at, 0) <= ?
             AND (u.uuid IS NULL OR (u.status = 'queued' AND u.next_try <= ?
                                     AND (u.frozen_body IS NOT NULL OR u.acked_gen IS NOT r.gen))
                  OR (u.status = 'parked' AND u.frozen_gen IS NOT r.gen))
           ORDER BY r.started_at, r.uuid""",
        (now - settle, now)).fetchall()


def _settled(rec: dict[str, Any], now: float, settle: float) -> bool:
    """Whether a record, as read, is a round that's over and past its closer: what may be sent."""
    ended = rec.get("ended_at") if rec.get("ended_at") is not None else rec.get("started_at") or 0
    return rec.get("kind") == "round" and rec.get("state") != "in_progress" and ended <= now - settle


def _freeze(db: sqlite3.Connection, uuid: str, now: float, settle: float) -> bool:
    """Build the record's next revision and keep it until acknowledged. False if there's nothing new
    to send: it says what was last acknowledged, the record is gone, or it isn't a settled round any
    more (checked in the very read the revision is built from: a round the booth took up again
    since the queue was looked at, while earlier ones were being sent, isn't sent in progress)."""
    got = body(db, uuid)
    if got is None:
        return False
    b, gen = got
    if not _settled(b["round"], now, settle):
        return False
    db.execute("INSERT OR IGNORE INTO cloud_uploads (uuid) VALUES (?)", (uuid,))
    u = db.execute("SELECT * FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()
    content = _content(b)
    if u["revision"] and u["acked_content"] == content:
        db.execute("UPDATE cloud_uploads SET acked_gen = ? WHERE uuid = ?", (gen, uuid))
        return False
    n = u["revision"] + 1
    db.execute("UPDATE cloud_uploads SET frozen_revision = ?, frozen_body = ?, frozen_gen = ?, frozen_content = ? "
               "WHERE uuid = ?", (n, encode(b, n), gen, content, uuid))
    return True


def _backoff(db: sqlite3.Connection, uuid: str, now: float, error: str) -> None:
    tries = db.execute("SELECT tries FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()[0]
    # (the exponent capped, not only the wait: a round that waits for weeks mustn't overflow)
    db.execute("UPDATE cloud_uploads SET tries = tries + 1, next_try = ?, error = ? WHERE uuid = ?",
               (now + min(RETRY_MAX_S, RETRY_BASE_S * 2 ** min(tries, 30)), error, uuid))


def _throttle(db: sqlite3.Connection, r: Any, now: float) -> float:
    """The API's allowance for this PC is spent (429): its limits are the device's, not a round's, so
    nothing more is sent, by the booth or the command line, till the time it gave (Retry-After).
    Returns how long that is."""
    try:
        wait = float(r.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        wait = THROTTLE_DEFAULT_S
    wait = min(max(wait, 1.0), THROTTLE_MAX_S)
    db.execute("INSERT OR REPLACE INTO cloud_throttle (one, not_before) VALUES (1, ?)", (now + wait,))
    return wait


def throttled(db: sqlite3.Connection, now: float) -> float:
    """Seconds still to wait before anything is sent (0: none)."""
    row = db.execute("SELECT not_before FROM cloud_throttle WHERE one = 1").fetchone()
    return max(0.0, min(row[0] - now, THROTTLE_MAX_S)) if row else 0.0


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
    if _content(b) == db.execute("SELECT frozen_content FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()[0]:
        db.execute("UPDATE cloud_uploads SET frozen_gen = ? WHERE uuid = ?", (gen, uuid))
        return False
    db.execute("UPDATE cloud_uploads SET status = 'queued', error = NULL, tries = 0, next_try = 0 WHERE uuid = ?",
               (uuid,))
    return True


def requeue(db_path: str) -> int:
    """Queue every refused (parked) record again, as it stands, for the next drain (`cloud upload
    --refused`: once what made Shotcast Cloud refuse them is put right there). Held conflicts stay
    held. Returns how many."""
    db = connect(db_path)
    try:
        return db.execute("UPDATE cloud_uploads SET status = 'queued', error = NULL, tries = 0, next_try = 0 "
                          "WHERE status = 'parked'").rowcount
    finally:
        db.close()


def _hold(db: sqlite3.Connection, uuid: str, error: str) -> None:
    """The cloud has a revision of this round that this PC didn't send: keep the local snapshot (the
    frozen revision and the rows), send nothing more of it, and leave it to be reconciled."""
    log.warning("Shotcast Cloud round %s held as a conflict, nothing more sent of it: %s", uuid, error)
    db.execute("UPDATE cloud_uploads SET status = 'conflict', error = ? WHERE uuid = ?", (error, uuid))


def _answer(r: Any) -> dict[str, Any] | None:
    """The cloud's JSON answer, or None for anything else (a proxy's page, say)."""
    if not str(r.headers.get("content-type", "")).split(";")[0].strip().lower() == "application/json":
        return None
    try:
        got = r.json()
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _revision(answer: dict[str, Any], key: str = "revision") -> int | None:
    rev = answer.get(key)
    return rev if isinstance(rev, int) and not isinstance(rev, bool) else None


def content_sha(frozen: bytes) -> str:
    """A frozen revision's checksum as the cloud keeps it: of the JSON itself, not its gzip bytes."""
    return hashlib.sha256(gzip.decompress(frozen)).hexdigest()


def _sent(db: sqlite3.Connection, uuid: str, n: int, out: Drained) -> None:
    db.execute("UPDATE cloud_uploads SET revision = ?, acked_gen = frozen_gen, acked_content = frozen_content,"
               " frozen_revision = NULL, frozen_body = NULL, frozen_gen = NULL, frozen_content = NULL,"
               " tries = 0, next_try = 0, error = NULL WHERE uuid = ? AND frozen_revision = ?", (n, uuid, n))
    out.sent.append(uuid)


def _send(cl: Any, db: sqlite3.Connection, uuid: str, now: float, out: Drained,
          begun: float | None = None) -> bool:
    """Send a record's frozen revision and act on the answer. False to stop draining for now."""
    from .cloud import CloudError, Unreachable, _message

    u = db.execute("SELECT frozen_revision, frozen_body FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()
    n, frozen = u["frozen_revision"], u["frozen_body"]
    try:
        r = cl.request("PUT", f"/api/rounds/{uuid}/revisions/{n}", raw=frozen,
                       headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
    except Unreachable as e:   # (the network, or a refresh that got no answer)
        _backoff(db, uuid, now, str(e))
        out.waiting += 1
        return False
    except CloudError:   # (not paired, or paired elsewhere meanwhile: nothing more is sent)
        raise
    except Exception as e:   # noqa: BLE001 - the network
        _backoff(db, uuid, now, f"couldn't reach Shotcast Cloud: {e}")
        out.waiting += 1
        return False
    answer = _answer(r)
    status = (answer or {}).get("status")
    # The cloud's receipt: `revision`, the one this answer is about (the one sent), and
    # `current_revision`, the round's latest there.
    mine, top = _revision(answer or {}), _revision(answer or {}, "current_revision")
    if answer is not None and mine == n and top is not None and r.status_code in (200, 201):
        # The cloud's own word for this revision. Its `duplicate` is this PC's own revision, sent before
        # (a lost answer, or a crash before it was noted): the same content, so the same revision.
        if (r.status_code, status, top) in ((201, "stored", n), (200, "duplicate", n)):
            _sent(db, uuid, n, out)
            return True
        if r.status_code == 200 and status in ("duplicate", "superseded") and top > n:
            _hold(db, uuid, f"Shotcast Cloud has revision {top}, after this PC's {n} ({status})")
            out.conflicts.append(uuid)
            return True
    error = (answer or {}).get("error")
    if r.status_code == 409 and error == "revision_conflict":
        # Never counted as sent, even naming this content: the API answers its own copy of it as a
        # `duplicate`, so a conflict that matches is an inconsistency, kept for someone to look at.
        sha = (answer or {}).get("sha256")
        same = mine == n and sha == content_sha(frozen)
        _hold(db, uuid, f"Shotcast Cloud answered a conflict naming this PC's own revision {n} (inconsistent)"
              if same else f"Shotcast Cloud has other content as revision {n} (its latest is {top})")
        out.conflicts.append(uuid)
        return True
    if r.status_code == 409 and error == "not_bay_writer":
        raise Blocked("another PC now writes this bay's rounds on Shotcast Cloud: pair this PC again to send its rounds")
    if r.status_code == 409 and error == "round_owned_elsewhere":
        _park(db, uuid, "the round belongs to another bay on Shotcast Cloud")
        out.parked.append(uuid)
        return True
    if r.status_code == 403 and error == "league_suspended":
        raise Blocked(f"Shotcast Cloud isn't taking rounds from this PC: {_message(r)}")
    if r.status_code == 403 and error == "round_league_suspended":
        # This round's league only: it waits (kept, tried again later) and the rest of the queue goes.
        _backoff(db, uuid, now, f"Shotcast Cloud: {_message(r)}")
        out.waiting += 1
        return True
    if r.status_code == 422 and error == "unsupported_format":
        # The API is behind this app (it reads a new format before any app writes it, so this
        # passes): every revision is kept, as it is, and sent once it isn't.
        _backoff(db, uuid, now, f"Shotcast Cloud: {_message(r)}")
        out.waiting += 1
        return False
    action = (answer or {}).get("action")
    if isinstance(error, str) and (error in REFUSED or (error not in KNOWN and action == "fix")):
        # The API's own word that it will never take this revision as it is (too big, not a round
        # body: koi's rounds.py; or a code this app doesn't know, whose `action` says so). (It
        # never refuses one for what it says of who played what: it owns that from the round's
        # first revision, keeps its own, and says so in the receipt's `kept`.) The round is kept, refused, till it changes or is sent again by hand.
        # Anything else (a code or action this app doesn't know, an answer without them, a proxy's
        # page) is tried again below, the revision kept as it is, and shows red in the tray while
        # it keeps failing.
        _park(db, uuid, f"HTTP {r.status_code}: {_message(r)}")
        out.parked.append(uuid)
        return True
    if r.status_code == 429:
        wait = _throttle(db, r, now + (0.0 if begun is None else max(0.0, _monotonic() - begun)))
        db.execute("UPDATE cloud_uploads SET error = ? WHERE uuid = ?",
                   (f"Shotcast Cloud: this PC's upload allowance is spent; waiting {int(wait)}s", uuid))
        log.warning("Shotcast Cloud: this PC's upload allowance is spent; nothing sent for %ds", int(wait))
        out.waiting += 1
        return False
    what = f"an answer this PC doesn't understand (HTTP {r.status_code})" if r.status_code in (200, 201) \
        else f"HTTP {r.status_code}"
    _backoff(db, uuid, now, f"Shotcast Cloud: {what}")
    out.waiting += 1
    return False


def lock_path(db_path: str) -> Path:
    return Path(db_path).with_name(Path(db_path).name + ".upload.lock")


def drain(cl: Any, db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS,
          wait: float = 60.0) -> Drained:
    """Send every settled record that's new or changed. `cl`: a paired cloud.Cloud; `db_path`: the
    season file in its pairing's profile (cloud.locate). `settle`: how long past its end a
    record waits (shorter only to test). One drain at a time across processes: waits `wait` seconds
    for another to finish, then TimeoutError. Raises NotThisPairing when the file isn't bound to `cl`'s
    pairing, Blocked when the cloud refuses this PC, NotPaired / PairingChanged when the stored
    pairing isn't `cl`'s any more."""
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
            return _drain(cl, db, time.time() if now is None else now, settle)
        finally:
            db.close()


def _drain(cl: Any, db: sqlite3.Connection, now: float, settle: float) -> Drained:
    out = Drained()
    begun = _monotonic()
    if throttled(db, now):   # (the API said to wait: the whole queue does, every revision kept)
        out.waiting = len(_due(db, now, settle))
        return out
    for row in _due(db, now, settle):
        uuid = row["uuid"]
        u = db.execute("SELECT frozen_body, status FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()
        if u is not None and u["status"] == "parked" and not _unpark(db, uuid):
            out.unchanged += 1
            continue
        if (u is None or u["frozen_body"] is None) and not _freeze(db, uuid, now, settle):
            out.unchanged += 1
            continue
        # (`now` is when the batch began: what's settled is judged as of then. An answer is timed
        # from when it came, so a wait the API gives late in a long batch isn't cut short.)
        if not _send(cl, db, uuid, now, out, begun):
            break
    return out


# How `cloud status` words each kind of record (status, outstanding).
STATUS_WORDS = (("in_progress", "in progress"), ("settling", "settling"), ("never", "never sent"),
                ("changed", "changed since sent"), ("waiting", "waiting for an answer"),
                ("conflict", "held as conflicts"), ("parked", "refused"), ("sent", "on Shotcast Cloud"))
_OUTSTANDING_WORDS = {"in_progress": "in progress", "settling": "settling (just ended)",
                      "never": "never sent", "changed": "changed since it was sent",
                      "waiting": "sent, not yet acknowledged", "conflict": "conflict", "parked": "refused"}


def _kinds(db: sqlite3.Connection, now: float, settle: float) -> list[tuple[str, str, float]]:
    """(record uuid, kind, when it ended) for every round (not practice stretches: those are never
    sent), from round_records joined to the queue."""
    out = []
    for r in db.execute(
            """SELECT r.uuid, r.state, COALESCE(r.ended_at, r.started_at, 0) AS ended, r.gen,
                      u.uuid AS queued, u.revision, u.acked_gen, u.frozen_body IS NOT NULL AS frozen, u.status
               FROM round_records r LEFT JOIN cloud_uploads u ON u.uuid = r.uuid
               WHERE r.kind = 'round' ORDER BY r.started_at, r.uuid"""):
        if r["state"] == "in_progress":
            kind = "in_progress"
        elif r["status"] == "conflict":
            kind = "conflict"
        elif r["status"] == "parked":
            kind = "parked"
        elif r["frozen"]:
            kind = "waiting"
        elif r["ended"] > now - settle:
            kind = "settling"
        elif r["queued"] is None or not r["revision"]:
            kind = "never"
        elif r["acked_gen"] != r["gen"]:
            kind = "changed"
        else:
            kind = "sent"
        out.append((r["uuid"], kind, r["ended"]))
    return out


def status(db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS) -> dict[str, int]:
    """How this PC's rounds stand, counted from its round records (STATUS_WORDS' kinds)."""
    db = connect(db_path)
    try:
        counts = dict.fromkeys((k for k, _ in STATUS_WORDS), 0)
        for _, kind, _ended in _kinds(db, time.time() if now is None else now, settle):
            counts[kind] += 1
        return counts
    finally:
        db.close()


BEHIND = ("never", "changed", "waiting", "conflict", "parked")   # settled, and not on Shotcast Cloud as it stands


def behind(db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS) -> int:
    """How many settled round records aren't on Shotcast Cloud as they stand here, whatever the reason
    (never sent, changed, waiting for an answer, held, refused). The one measure of upload health:
    while it stays above 0 for long, the tray is red (cloud_booth) and `cloud upload` fails."""
    db = connect(db_path)
    try:
        return sum(kind in BEHIND for _, kind, _ended in _kinds(db, time.time() if now is None else now, settle))
    finally:
        db.close()


def behind_since(db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS) -> float | None:
    """When the oldest round that isn't on Shotcast Cloud settled (None: none is behind): how long the
    queue has already been behind, for a booth that's just starting."""
    db = connect(db_path)
    try:
        late = [ended + settle for _, kind, ended in _kinds(db, time.time() if now is None else now, settle)
                if kind in BEHIND]
        return min(late) if late else None
    finally:
        db.close()


def health(db_path: str) -> list[tuple[str, str]]:
    """What needs someone, for the tray, from what the season file keeps (so it holds till it's
    resolved): a failure ('fail') for rounds held as conflicts and for rounds refused. Rounds that
    simply aren't going up are `behind`'s."""
    db = connect(db_path)
    try:
        got = dict(db.execute("SELECT status, COUNT(*) FROM cloud_uploads GROUP BY status").fetchall())
    finally:
        db.close()
    out = []
    if n := got.get("conflict"):
        out.append(("fail", f"Shotcast Cloud: {n} round(s) held as conflicts, not sent (see cloud status)"))
    if n := got.get("parked"):
        out.append(("fail", f"Shotcast Cloud: {n} round(s) refused (see cloud status)"))
    return out


def outstanding(db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS) -> list[tuple[str, str]]:
    """(uuid, what) for each round record that isn't on Shotcast Cloud as it stands here."""
    db = connect(db_path)
    try:
        return [(uuid, _OUTSTANDING_WORDS[kind]) for uuid, kind, _ended in _kinds(
            db, time.time() if now is None else now, settle) if kind != "sent"]
    finally:
        db.close()
