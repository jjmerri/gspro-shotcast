"""Send this PC's rounds to Shotcast Cloud, once paired (see shotcast-cloud's docs/dev/thin-pc.md and
rounds-server.md).

Paired, the booth runs in the pairing's own cloud profile (cloud.locate), whose season file is bound
to the pairing as it's made (`bind`): every round record in it is that pairing's and goes up,
and nothing goes up from a file bound to another pairing, or to none. Each record (a round, or a
practice stretch) goes as a body of its rows once it has settled: finished or abandoned, and
CLOSER_SECONDS past its end, with no league (the PC is a capture device: the cloud decides a round's
league). A record that changes after it was sent goes up again as the next revision, found by its
change counter (round_records.gen).

A revision's bytes are built once and kept (committed before they're sent) until the cloud
acknowledges them, so a retry sends the same bytes. It counts as sent only on the cloud's own answer
for that revision: JSON, `stored` (201) or `duplicate` (200) with the revision sent. Anything else,
a proxy's page say, is tried again later. The cloud having a revision this PC didn't send (a
`duplicate` or `superseded` answer for a later one, or a `revision_conflict`: other bytes under that
number) is never overwritten from here: the record is held as a conflict, its snapshot kept, for
someone to reconcile (`cloud status` counts them). A 409 never counts as sent. Nothing is deleted here: the rounds stay on this PC.

The queue (cloud_uploads) is kept in the profile's season.db, made the first time it's used.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import time
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


def body(db: sqlite3.Connection, uuid: str) -> tuple[dict[str, Any], int] | None:
    """A record's v1 body and the gen it was read at, or None if the record is gone. Local integer ids
    become uids or are dropped. No league: the PC is a capture device (the cloud decides)."""
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
        "shots": [_without(s, "id", "session_id", "record_uuid") | {"is_putt": bool(s["is_putt"])} for s in shots],
        "shot_tracks": [_without(t, "shot_id") | {"shot_uid": uid_of[t["shot_id"]]} for t in tracks],
        "lines": [_without(ln, "id", "session_id", "record_uuid", "shot_id") | {"shot_uid": uid_of.get(ln["shot_id"])}
                  for ln in lines],
        "scorecards": [_without(c, "id", "session_id", "round_uuid") for c in cards],
        "player_rounds": [_without(p, "id", "session_id", "round_uuid") for p in players],
        "week_results": [_without(w, "id", "round_id") for w in weeks],
        "edit": {"excluded": any(c["excluded"] for c in cards), "mark": mark,
                 "left_out": [s["uid"] for s in shots if s["left_out"]]},
    }, rec["gen"]


def _json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def _content(b: dict[str, Any]) -> str:
    """What a revision says, apart from the starting edit values only revision 1 carries."""
    return hashlib.sha256(_json(_without(b, "edit"))).hexdigest()


def encode(b: dict[str, Any], revision: int) -> bytes:
    """The bytes sent for a revision: gzip with no timestamp, so building it again gives the same bytes."""
    out = dict(b)
    if revision != 1:
        out.pop("edit", None)
    return gzip.compress(_json(out), mtime=0)


# Draining the queue


def _due(db: sqlite3.Connection, now: float, settle: float) -> list[sqlite3.Row]:
    """Settled records to send (new, changed, or a revision waiting to be acknowledged), oldest first.
    Held (conflict) and refused (parked) ones aren't."""
    return db.execute(
        """SELECT r.uuid FROM round_records r
           LEFT JOIN cloud_uploads u ON u.uuid = r.uuid
           WHERE r.state != 'in_progress' AND COALESCE(r.ended_at, r.started_at, 0) <= ?
             AND (u.uuid IS NULL OR (u.status = 'queued' AND u.next_try <= ?
                                     AND (u.frozen_body IS NOT NULL OR u.acked_gen IS NOT r.gen)))
           ORDER BY r.started_at, r.uuid""",
        (now - settle, now)).fetchall()


def settle_idle(db: sqlite3.Connection, now: float) -> int:
    """End the practice stretches left in progress with nothing struck or said for
    PRACTICE_IDLE_SECONDS, so a session on the range that just stops goes up without waiting for the
    next shot, round or restart. It's what the booth does itself at that next row
    (round_record.Live._practice: the stretch is over, at its last row), done here once it's due; the
    booth, finding it ended, starts the next stretch as it would have. One write transaction, the
    last row read inside it: a stretch that just got a shot isn't ended. Returns how many were."""
    from .round_record import PRACTICE_IDLE_SECONDS

    ended = 0
    with _txn(db, "IMMEDIATE"):
        for (u,) in db.execute("SELECT uuid FROM round_records WHERE kind = 'practice' AND state = 'in_progress'"
                               " AND assigned = 'live'").fetchall():
            last = db.execute("SELECT MAX(at) FROM (SELECT MAX(ts) AS at FROM shots WHERE record_uuid = ?1"
                              " UNION ALL SELECT MAX(ts) FROM lines WHERE record_uuid = ?1)", (u,)).fetchone()[0]
            if last is None or now - last <= PRACTICE_IDLE_SECONDS:   # (no row yet: the booth's to drop)
                continue
            ended += db.execute("UPDATE round_records SET state = 'finished', ended_at = MAX(?, COALESCE(started_at, ?))"
                                " WHERE uuid = ? AND state = 'in_progress'", (last, last, u)).rowcount
    return ended


def _freeze(db: sqlite3.Connection, uuid: str) -> bool:
    """Build the record's next revision and keep it until acknowledged. False if there's nothing new
    to send (it says what was last acknowledged, or the record is gone)."""
    got = body(db, uuid)
    if got is None:
        return False
    b, gen = got
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
    db.execute("UPDATE cloud_uploads SET tries = tries + 1, next_try = ?, error = ? WHERE uuid = ?",
               (now + min(RETRY_MAX_S, RETRY_BASE_S * 2 ** tries), error, uuid))


def _park(db: sqlite3.Connection, uuid: str, error: str) -> None:
    log.warning("Shotcast Cloud won't take round %s: %s", uuid, error)
    db.execute("UPDATE cloud_uploads SET status = 'parked', error = ?, frozen_body = NULL, frozen_revision = NULL "
               "WHERE uuid = ?", (error, uuid))


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


def _revision(answer: dict[str, Any]) -> int | None:
    rev = answer.get("revision")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) else None


def _sent(db: sqlite3.Connection, uuid: str, n: int, out: Drained) -> None:
    db.execute("UPDATE cloud_uploads SET revision = ?, acked_gen = frozen_gen, acked_content = frozen_content,"
               " frozen_revision = NULL, frozen_body = NULL, frozen_gen = NULL, frozen_content = NULL,"
               " tries = 0, next_try = 0, error = NULL WHERE uuid = ? AND frozen_revision = ?", (n, uuid, n))
    out.sent.append(uuid)


def _send(cl: Any, db: sqlite3.Connection, uuid: str, now: float, out: Drained) -> bool:
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
    status, top = (answer or {}).get("status"), _revision(answer or {})
    if answer is not None and top is not None and r.status_code in (200, 201):
        # The cloud's own word for this revision. Its `duplicate` is this PC's own revision, sent before
        # (a lost answer, or a crash before it was noted): the same bytes, so the same revision.
        if (r.status_code, status, top) in ((201, "stored", n), (200, "duplicate", n)):
            _sent(db, uuid, n, out)
            return True
        if r.status_code == 200 and status in ("duplicate", "superseded") and top > n:
            _hold(db, uuid, f"Shotcast Cloud has revision {top}, after this PC's {n} ({status})")
            out.conflicts.append(uuid)
            return True
    error = (answer or {}).get("error")
    if r.status_code == 409 and error == "revision_conflict":
        # Never counted as sent, even naming these bytes: the API answers its own copy of them as a
        # `duplicate`, so a conflict that matches is an inconsistency, kept for someone to look at.
        sha = (answer or {}).get("sha256")
        same = top == n and sha == hashlib.sha256(frozen).hexdigest()
        _hold(db, uuid, f"Shotcast Cloud answered a conflict naming this PC's own revision {n} (inconsistent)"
              if same else f"Shotcast Cloud has other content as revision {top} (this PC sent {n})")
        out.conflicts.append(uuid)
        return True
    if r.status_code == 409 and error == "not_bay_writer":
        raise Blocked("another PC now writes this bay's rounds on Shotcast Cloud: pair this PC again to send its rounds")
    if r.status_code == 409 and error == "round_owned_elsewhere":
        _park(db, uuid, "the round belongs to another bay on Shotcast Cloud")
        out.parked.append(uuid)
        return True
    if r.status_code == 403 and answer is not None:
        raise Blocked(f"Shotcast Cloud isn't taking rounds from this PC: {_message(r)}")
    if r.status_code in (400, 404, 409, 413, 415, 422) and answer is not None:
        _park(db, uuid, f"HTTP {r.status_code}: {_message(r)}")
        out.parked.append(uuid)
        return True
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
    settle_idle(db, now)
    for row in _due(db, now, settle):
        uuid = row["uuid"]
        u = db.execute("SELECT frozen_body FROM cloud_uploads WHERE uuid = ?", (uuid,)).fetchone()
        if (u is None or u["frozen_body"] is None) and not _freeze(db, uuid):
            out.unchanged += 1
            continue
        if not _send(cl, db, uuid, now, out):
            break
    return out


# How `cloud status` words each kind of record (status, outstanding).
STATUS_WORDS = (("in_progress", "in progress"), ("settling", "settling"), ("never", "never sent"),
                ("changed", "changed since sent"), ("waiting", "waiting for an answer"),
                ("conflict", "held as conflicts"), ("parked", "refused"), ("sent", "on Shotcast Cloud"))
_OUTSTANDING_WORDS = {"in_progress": "in progress", "settling": "settling (just ended)",
                      "never": "never sent", "changed": "changed since it was sent",
                      "waiting": "sent, not yet acknowledged", "conflict": "conflict", "parked": "refused"}


def _kinds(db: sqlite3.Connection, now: float, settle: float) -> list[tuple[str, str]]:
    """(record uuid, kind) for every round record, from round_records joined to the queue."""
    out = []
    for r in db.execute(
            """SELECT r.uuid, r.state, COALESCE(r.ended_at, r.started_at, 0) AS ended, r.gen,
                      u.uuid AS queued, u.revision, u.acked_gen, u.frozen_body IS NOT NULL AS frozen, u.status
               FROM round_records r LEFT JOIN cloud_uploads u ON u.uuid = r.uuid
               ORDER BY r.started_at, r.uuid"""):
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
        out.append((r["uuid"], kind))
    return out


def status(db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS) -> dict[str, int]:
    """How this PC's rounds stand, counted from its round records (STATUS_WORDS' kinds)."""
    db = connect(db_path)
    try:
        counts = dict.fromkeys((k for k, _ in STATUS_WORDS), 0)
        for _, kind in _kinds(db, time.time() if now is None else now, settle):
            counts[kind] += 1
        return counts
    finally:
        db.close()


def health(db_path: str, now: float | None = None, retries: int = 1) -> list[tuple[str, str]]:
    """How uploads stand for the tray, from what the season file keeps (so it holds till it's resolved,
    whether or not a drain just tried): (level, message), each a failure ('fail': the tray goes red),
    for rounds held as conflicts or refused, and for rounds that failed `retries` times or more and
    wait to be tried again (through every wait between tries, till they're on Shotcast Cloud)."""
    db = connect(db_path)
    try:
        got = dict(db.execute(
            """SELECT CASE WHEN status IN ('conflict', 'parked') THEN status
                           WHEN tries >= ? THEN 'failing' END AS kind, COUNT(*)
               FROM cloud_uploads GROUP BY kind""", (retries,)).fetchall())
    finally:
        db.close()
    out = []
    if n := got.get("conflict"):
        out.append(("fail", f"Shotcast Cloud: {n} round(s) held as conflicts, not sent (see cloud status)"))
    if n := got.get("parked"):
        out.append(("fail", f"Shotcast Cloud: {n} round(s) refused (see cloud status)"))
    if n := got.get("failing"):
        out.append(("fail", f"Shotcast Cloud: {n} round(s) aren't going up (the network, or Shotcast Cloud is "
                            "busy); still retrying (see cloud status)"))
    return out


def outstanding(db_path: str, now: float | None = None, settle: float = CLOSER_SECONDS) -> list[tuple[str, str]]:
    """(uuid, what) for each round record that isn't on Shotcast Cloud as it stands here."""
    db = connect(db_path)
    try:
        return [(uuid, _OUTSTANDING_WORDS[kind]) for uuid, kind in _kinds(db, time.time() if now is None else now,
                                                                          settle) if kind != "sent"]
    finally:
        db.close()
