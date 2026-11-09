"""What the stats page's alerts say, and when (docs/tracking.md#alerts).

    live    the booth is on air in a round with the stream on (once a few hours at most)
    liveleague  the same, for a league or tournament round only (a phone picks one or the other)
    round   a league or tournament round posted to the stats page
    record  a new league record (the Records tab's all-time ones, not a player's own), an eagle or an ace
    recap   the week's league recap is up

Rounds, records, eagles and aces come from comparing each published build (its summary-all.json)
with what was alerted already, kept in log_dir/ALERTS_STATE. The first build only takes note of what's
there: nothing from before alerts were set up goes out.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import push

log = logging.getLogger(__name__)

ALERTS_STATE = "alerts-state.json"   # under log_dir
LIVE_GAP = 3 * 3600   # seconds: one "we're live" a night, not one per round
# A round or record shot older than the last check (less this) isn't news: a round put back in the
# stats, or a record that fell back to an older shot when the record was left out.
NEWS_MARGIN = 3600
KEEP_SEEN = 2000


def _to_par(v: int | None) -> str:
    return "" if v is None else " (E)" if v == 0 else f" ({v:+d})"


def _feet(yards: float) -> str:
    ft = yards * 3
    return f"{ft:.1f} ft" if ft < 10 else f"{round(ft)} ft"


def _where(shot: dict) -> str:
    bits = [shot.get("course"), f"hole {shot['hole']}" if shot.get("hole") else None]
    return ", ".join(b for b in bits if b)


def record_holders(summary: dict) -> dict[str, dict]:
    """The all-time records in a summary (summary-league.json: the league's): category -> the shot holding it."""
    rec = (summary.get("records") or {}).get("all") or {}
    out = {}
    if rec.get("longest_drive"):
        out["drive"] = rec["longest_drive"]
    if rec.get("longest_putt"):
        out["putt"] = rec["longest_putt"]
    for band in rec.get("approaches") or []:
        if band.get("best"):
            out[f"approach-{band['from']}-{band['to']}"] = {**band["best"], "band": (band["from"], band["to"])}
    return out


def _beats(cat: str, shot: dict, old: dict) -> bool:
    """Whether `shot` is a better record than `old`, as the stats rank them: longer for drives and
    putts, closer for an approach."""
    v, was = shot.get("v") or 0, old.get("v") or 0
    return v > was if cat in ("drive", "putt") else v < was


def _record_alert(cat: str, shot: dict, old: dict | None) -> push.Alert:
    who = shot.get("player") or "Someone"
    if cat == "drive":
        what, value, was = "Longest drive", f"{round(shot['v'])} yd", old and f"{round(old['v'])} yd"
    elif cat == "putt":
        what, value, was = "Longest putt holed", _feet(shot["v"]), old and _feet(old["v"])
    else:
        lo, hi = shot["band"]
        what = f"Closest approach from {lo}–{hi} yd"
        value = "holed" if shot.get("outcome") == "holed" else _feet(shot["v"])
        was = old and ("holed" if old.get("outcome") == "holed" else _feet(old["v"]))
    body = f"{who}: {value}" + (f", {_where(shot)}" if _where(shot) else "") + (f" (was {was})" if was else "")
    # The page plays the shot (its record clip, or the round's video) if there's one, else shows the records.
    watch = f"&watch=1&shot={shot['id']}&ts={shot['ts']}" if shot.get("id") and shot.get("ts") else ""
    return push.Alert("record", f"🏆 New league record: {what}", body, "#highlights?player=&ctx=league&period=all" + watch,
                      tag=f"record-{cat}")


def _round_alerts(rows: list[dict]) -> list[push.Alert]:
    """One round's sides (summary rounds rows sharing a `round`): the round posted, and its eagles and aces."""
    first = rows[0]
    rnd, course = first["round"], first.get("course") or "a course"
    out = []
    if any(r.get("league") or r.get("tournament") for r in rows):
        kind = "League round" if any(r.get("league") for r in rows) else "Tournament round"
        scores = sorted((r for r in rows if r.get("gross")), key=lambda r: (r.get("to_par") is None, r.get("to_par") or 0))
        body = " · ".join(f"{r.get('side') or ', '.join(r.get('players') or [])} {r['gross']}{_to_par(r.get('to_par'))}"
                          for r in scores) or "The scores are up."
        out.append(push.Alert("round", f"⛳ {kind} posted: {course}", body, f"#round?player=&r={rnd}", tag=f"round-{rnd}"))
    for r in rows:
        who = r.get("side") or ", ".join(r.get("players") or []) or "Someone"
        for h in r.get("card") or []:
            score, par = h.get("score"), h.get("par")
            if not score or not par or score > par - 2:
                continue
            name = "Hole in one" if score == 1 else "Albatross" if score == par - 3 else "Eagle" if score == par - 2 else "Condor"
            icon = "🎯" if score == 1 else "🦅"
            out.append(push.Alert("record", f"{icon} {name}! {who}", f"Hole {h['hole']} (par {par}) at {course}",
                                  f"#round?player=&id={r['id']}&hole={h['hole']}&watch=1", tag=f"eagle-{rnd}-{h['hole']}-{who}"))
    return out


def diff(summary: dict, state: dict, now: float | None = None,
         league: dict | None = None) -> tuple[list[push.Alert], dict]:
    """The alerts a new build calls for, and the state to keep after it: rounds, eagles and aces from
    its summary-all.json, records from its summary-league.json (`league`; none, no record alerts). A
    state without a check yet (alerts just set up) only takes note of the build."""
    now = time.time() if now is None else now
    rows = summary.get("rounds") or []
    holders = record_holders(league or {})
    new_state = {**state, "at": now,
                 "records": {k: {"id": s.get("id"), "v": s.get("v"), "outcome": s.get("outcome")} for k, s in holders.items()}}
    seen = list(state.get("rounds") or [])
    by_round: dict[int, list[dict]] = {}
    for r in rows:
        by_round.setdefault(r["round"], []).append(r)
    if "at" not in state:
        new_state["rounds"] = sorted(by_round)[-KEEP_SEEN:]
        return [], new_state
    since = state["at"] - NEWS_MARGIN
    alerts: list[push.Alert] = []
    known = set(seen)
    for rnd in sorted(by_round):
        group = by_round[rnd]
        if rnd in known:
            continue
        seen.append(rnd)
        if max((r.get("ended") or 0) for r in group) >= since:
            alerts += _round_alerts(group)
    new_state["rounds"] = seen[-KEEP_SEEN:]
    old = state.get("records") or {}
    for cat, shot in holders.items():
        before = old.get(cat)
        if before and (before.get("id") == shot.get("id") or not _beats(cat, shot, before)):
            continue   # the same record, or a lesser one left holding it (the better shot gone from the stats)
        if (shot.get("ts") or 0) < since:
            continue
        alerts.append(_record_alert(cat, shot, before))
    return alerts, new_state


class Alerts:
    """Sends the page's alerts through the chat table's subscriptions. Blocking: call it off the
    event loop. Never raises: an alert that can't go out is logged and dropped."""

    def __init__(self, log_dir: str | Path, table: Any, contact: str = push.CONTACT):
        self.log_dir, self.table, self.contact = Path(log_dir), table, contact
        self._key = None
        self._lock = threading.Lock()

    def _state(self) -> dict:
        try:
            return json.loads((self.log_dir / ALERTS_STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        path = self.log_dir / ALERTS_STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)

    def start(self, topics: list[str] | None = None) -> None:
        """Make the booth's key if it has none, and give the page its public half and the kinds of
        alert this booth sends (`topics`)."""
        try:
            self._key = push.load_key(self.log_dir)
            self.table.set_push_key(push.public_key(self._key), topics)
        except Exception as e:
            log.warning("Alerts: couldn't set up the push key: %s", e)

    def send(self, alerts: list[push.Alert]) -> None:
        if not alerts:
            return
        try:
            key = self._key or push.load_key(self.log_dir)
            subs = self.table.push_subscriptions()
        except Exception as e:
            log.warning("Alerts: couldn't read the subscriptions: %s", e)
            return
        gone: set[str] = set()
        for a in alerts:
            n, dead = push.send(a, subs, key, contact=self.contact)
            gone |= set(dead)
            log.info("Alerts: %r to %d phone%s", a.title, n, "" if n == 1 else "s")
        for sub_id in gone:
            try:
                self.table.drop_push_subscription(sub_id)
            except Exception as e:
                log.debug("Alerts: couldn't drop a gone subscription: %s", e)

    def after_publish(self, site: str | Path) -> None:
        """A build was published: alert what's new in it."""
        try:
            site = Path(site)
            version = json.loads((site / "latest.json").read_text(encoding="utf-8"))["version"]
            folder = site / "v" / version
            summary = json.loads((folder / "summary-all.json").read_text(encoding="utf-8"))
            league = json.loads((folder / "summary-league.json").read_text(encoding="utf-8"))
            with self._lock:
                alerts, state = diff(summary, self._state(), league=league)
                self._save(state)
        except Exception as e:
            log.warning("Alerts: couldn't check the published stats: %s", e)
            return
        self.send(alerts)

    def live(self, who: str, course: str | None, league: bool = False) -> None:
        """The booth went on air in a round with the stream on. `league`: a league or tournament round,
        which the phones that only want those hear about too. Each kind keeps its own gap, so a practice
        round's alert doesn't use up the league one's night."""
        now = time.time()
        with self._lock:
            state = self._state()
            topics = [t for t in ("live", "liveleague") if (t == "live" or league)
                      and now - (state.get(f"{t}_at") or 0) >= LIVE_GAP]
            if not topics:
                return
            for t in topics:
                state[f"{t}_at"] = now
            try:
                self._save(state)
            except OSError as e:
                log.debug("Alerts: couldn't save the state: %s", e)
        body = f"{who}" + (f" on {course}" if course else "") + ". Watch and chat on the stats page."
        self.send([push.Alert(t, "🔴 Live now", body.strip(), "#live?player=", tag="live", ttl=3600) for t in topics])

    def recap(self, key: str, week: Any, course: str | None) -> None:
        """A league recap was published (sent once per recap, however often it's rebuilt)."""
        with self._lock:
            state = self._state()
            sent = list(state.get("recaps") or [])
            if key in sent:
                return
            state["recaps"] = (sent + [key])[-200:]
            try:
                self._save(state)
            except OSError as e:
                log.debug("Alerts: couldn't save the state: %s", e)
        body = f"Week {week}" + (f" at {course}" if course else "") + ": the highlights and the booth's call."
        self.send([push.Alert("recap", "🎬 The league recap is up", body, f"#vods?player=&recap={quote(key, safe='')}", tag=f"recap-{key}")])
