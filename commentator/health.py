"""Booth health: is the foundation (GSPro's log lines, the HUD the booth reads by OCR) still what the
booth was built against?

A GSPro update can reword its log, move the HUD, or rename a data tile; a monitor change or a re-picked
tile can do the same. None of that crashes the booth: the reads just quietly come back empty or wrong.
So deterministic checks run at startup, at every round start (the booth is left running for days), and
after every round, and anything off is raised here. The ON AIR badge shows a warning chip while any
issue is open (hover for the list), and every change is logged and saved to `log_dir/health.json`,
with each round's report card appended to `log_dir/health-history.jsonl`.

An issue is keyed (a re-check updates or clears it in place) and is either a `warn` (worth a look) or a
`fail` (a read the booth depends on is broken). A `verify` issue ("GSPro was updated") isn't broken
yet, only unproven: the next round whose checks all pass clears it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

LEVELS = ("warn", "fail")
PROVEN_SCREENS = "screens_proven"   # the GSPro view sizes a clean round was read at
MAX_PROVEN_SCREENS = 20
SCREEN_FLAGGED = "screen_flagged"   # a view size was ever flagged: from then on every unproven one is


@dataclass
class Issue:
    key: str
    level: str          # "warn" or "fail"
    message: str
    since: str          # local time it was first raised
    verify: bool = False


class Health:
    """A thread-safe registry of open issues (the overlay reads it from its own thread), plus a few
    remembered facts (the GSPro version and screen size the booth last ran against)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._issues: dict[str, Issue] = {}
        self._facts: dict[str, object] = {}
        self._dismissed: frozenset = frozenset()
        self.last_round: dict | None = None
        self._listeners: list[Callable[[str, Issue | None], None]] = []
        self._load()

    # ---- persistence ------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._facts = dict(data.get("facts") or {})
            self._issues = {i["key"]: Issue(**i) for i in data.get("issues") or []}
            self.last_round = data.get("last_round")
        except Exception:
            log.exception("Couldn't read %s; starting with a clean health state", self.path)
            self._facts, self._issues = {}, {}

    def _save(self) -> None:
        if not self.path:
            return
        data = {"updated": _now(), "facts": self._facts, "issues": [asdict(i) for i in self._issues.values()],
                "last_round": self.last_round}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log.exception("Couldn't save %s", self.path)

    # ---- issues -----------------------------------------------------------------------------

    def add_listener(self, fn: Callable[[str, Issue | None], None]) -> None:
        """`fn(key, issue)` on every change: the issue when raised or changed, None when cleared. Called
        on whichever thread made the change; keep it quick (a phone push hands off to its own thread)."""
        self._listeners.append(fn)

    def _notify(self, key: str, issue: Issue | None) -> None:
        for fn in list(self._listeners):
            try:
                fn(key, issue)
            except Exception:
                log.exception("Health listener failed")

    def raise_issue(self, key: str, level: str, message: str, verify: bool = False) -> None:
        assert level in LEVELS, level
        with self._lock:
            old = self._issues.get(key)
            if old and (old.level, old.message, old.verify) == (level, message, verify):
                return
            issue = Issue(key, level, message, old.since if old else _now(), verify)
            self._issues[key] = issue
            self._save()
        (log.error if level == "fail" else log.warning)("HEALTH %s [%s]: %s", level.upper(), key, message)
        self._notify(key, issue)

    def clear(self, key: str, why: str = "") -> None:
        with self._lock:
            issue = self._issues.pop(key, None)
            if issue is None:
                return
            self._save()
        log.info("HEALTH cleared [%s]%s: %s", key, f" ({why})" if why else "", issue.message)
        self._notify(key, None)

    def clear_prefix(self, prefix: str, keep: set[str] = frozenset(), why: str = "") -> None:
        for key in [k for k in self.keys() if k.startswith(prefix) and k not in keep]:
            self.clear(key, why)

    def clear_verified(self, screen: str | None = None) -> None:
        """A round's checks all passed: whatever was waiting to be proven by one is proven. `screen` is
        GSPro's view size at the round's end: it's proven too when it's the one checked at its start. A
        size that changed mid-round proves neither, and its warning stays up."""
        with self._lock:
            proved = bool(screen) and screen == self._facts.get("screen")
            if proved:
                proven = self._proven_screens()
                if screen not in proven:
                    self._facts[PROVEN_SCREENS] = (proven + [screen])[-MAX_PROVEN_SCREENS:]
                    self._save()
        for issue in self.issues():
            if issue.verify and (proved or issue.key != "changed.screen"):
                self.clear(issue.key, "a clean round since")

    def _proven_screens(self) -> list[str]:
        """The proven view sizes (the caller holds the lock). Once, from before sizes were remembered:
        the last size, with no change waiting on it, was read clean."""
        proven = self._facts.get(PROVEN_SCREENS)
        if proven is None:
            old, pending = self._facts.get("screen"), "changed.screen" in self._issues
            proven = self._facts[PROVEN_SCREENS] = [old] if old and not pending else []
            if pending:
                self._facts[SCREEN_FLAGGED] = True
        return list(proven)

    def note_screen(self, size: str | None) -> tuple[str | None, list[str], bool]:
        """Record GSPro's view size (None: just settle the proven sizes), returning the size before, the
        proven sizes and whether a size was ever flagged, in one step so concurrent checks can't take
        each other's size as proven."""
        with self._lock:
            proven, old = self._proven_screens(), self._facts.get("screen")
            if size:
                self._facts["screen"] = size
            self._save()
            return old, proven, bool(self._facts.get(SCREEN_FLAGGED))

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._issues)

    def issues(self) -> list[Issue]:
        with self._lock:
            return sorted(self._issues.values(), key=lambda i: (i.level != "fail", i.key))

    def get(self, key: str) -> Issue | None:
        with self._lock:
            return self._issues.get(key)

    # ---- the badge ----------------------------------------------------------------------------

    def _signature(self) -> frozenset:
        return frozenset((i.key, i.level, i.message) for i in self._issues.values())

    def status(self) -> tuple[str | None, list[str]]:
        """(worst level, one line per issue) for the badge. The level is None when nothing is open or
        the open set was dismissed (a new or changed issue brings the chip back)."""
        with self._lock:
            if not self._issues or self._signature() == self._dismissed:
                return None, []
            items = sorted(self._issues.values(), key=lambda i: (i.level != "fail", i.key))
        level = "fail" if any(i.level == "fail" for i in items) else "warn"
        return level, [f"{'FAIL' if i.level == 'fail' else 'WARN'}: {i.message}" for i in items]

    def dismiss(self) -> None:
        with self._lock:
            self._dismissed = self._signature()
        log.info("HEALTH chip dismissed until the issues change")

    # ---- facts and reports --------------------------------------------------------------------

    def fact(self, name: str) -> object:
        with self._lock:
            return self._facts.get(name)

    def set_fact(self, name: str, value: object) -> object:
        """Remember a fact, returning what it was before (None the first time)."""
        with self._lock:
            old = self._facts.get(name)
            if old != value:
                self._facts[name] = value
                self._save()
            return old

    def record_round(self, report: dict) -> None:
        with self._lock:
            self.last_round = report
            self._save()
        if self.path:
            try:
                with open(self.path.with_name("health-history.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(report) + "\n")
            except Exception:
                log.exception("Couldn't append the round's health report")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---- checks ---------------------------------------------------------------------------------------
# Pure functions over what the engine saw, so they can be tested without a booth.

def check_files(health: Health, paths: dict[str, str]) -> None:
    """Each named file the booth reads must exist (a GSPro reinstall or update can move one)."""
    for name, path in paths.items():
        key = f"file.{name}"
        if path and not os.path.exists(path):
            health.raise_issue(key, "fail", f"{name.replace('_', ' ')} not found at {path}")
        else:
            health.clear(key, "found")


def check_changed(health: Health, fact: str, value: object, what: str) -> None:
    """A fact the booth was proven against (GSPro's version, the screen size) changed: flag it until a
    clean round proves the booth still reads everything."""
    if value is None:
        return
    old = health.set_fact(fact, value)
    if old is not None and old != value:
        health.raise_issue(f"changed.{fact}", "warn",
                           f"{what} changed from {old} to {value}: the next round's checks will confirm "
                           f"the booth still reads it", verify=True)


def check_screen(health: Health, size: str | None) -> None:
    """GSPro's view size (`WxH`): a projector or a windowed GSPro can switch between a few sizes, so
    only one no clean round has been read at is flagged, until a clean round proves it. None (GSPro's
    view isn't up, so there's nothing it'd be read at) checks nothing."""
    if not size:
        return
    old, proven, flagged = health.note_screen(size)
    if size in proven:
        health.clear("changed.screen", "read clean at this size before")
    elif proven or flagged or (old is not None and old != size):
        # Only a booth's very first size goes unflagged; once any was, every unproven one is.
        reads = f"it has read rounds at {', '.join(proven)}" if proven else "no size is proven by a round yet"
        health.set_fact(SCREEN_FLAGGED, True)
        health.raise_issue("changed.screen", "warn",
                           f"GSPro's view is {size}, a size the booth hasn't read a round at ({reads}): the "
                           f"next round's checks will confirm it reads it", verify=True)


@dataclass
class RoundCounters:
    """What the booth saw during one round, for the post-round checkup."""
    fires_at_start: int = 0
    kinds_at_start: dict = field(default_factory=dict)
    shots: int = 0                   # shots the booth handled
    setups: int = 0                  # pre-shot HUD reads
    hud_shot_missing: int = 0
    hud_to_pin_missing: int = 0
    hud_lie_missing: int = 0
    full_shots: int = 0              # non-putts whose final reading came back
    tiles_missing: dict = field(default_factory=dict)   # required tile -> full shots without it
    tiles_seen: set = field(default_factory=set)


# A rate check only means something over enough samples; below these it's skipped.
MIN_SETUPS = 8
MIN_FULL_SHOTS = 6
MIN_SHOTS = 6
MISSING_RATE = 0.3         # more than this share of reads missing is a fault, not OCR noise
# Event kinds a round always logs when GSPro's log is read right: its score lines, setups (distance to
# the pin), player selection, and each shot.
REQUIRED_KINDS = ("state", "tdist", "select", "shot_fired")


def evaluate_round(c: RoundCounters, fires: int, kinds: dict, unparsed: list[str], required_tiles: list[str],
                   log_truth: bool, screen: bool) -> tuple[list[tuple[str, str, str]], dict]:
    """The post-round findings [(key, level, message)] and the report card. `fires`, `kinds` and
    `unparsed` are the GSPro log's running totals now (the counters hold them at the round's start)."""
    found: list[tuple[str, str, str]] = []
    report: dict = {"shots": c.shots}
    if log_truth:
        d_fires = fires - c.fires_at_start
        d_kinds = {k: kinds.get(k, 0) - c.kinds_at_start.get(k, 0) for k in kinds}
        d_kinds = {k: v for k, v in d_kinds.items() if v}
        report.update(gspro_shots=d_fires, log_events=d_kinds)
        if c.shots >= MIN_SHOTS:
            if d_fires == 0:
                found.append(("round.log_silent", "fail",
                              f"GSPro's log logged no shots while the booth saw {c.shots}: is it the right file?"))
            else:
                if missing := [k for k in REQUIRED_KINDS if not d_kinds.get(k)]:
                    found.append(("round.log_kinds", "fail",
                                  f"GSPro's log had no {', '.join(missing)} lines this round: its format may have changed"))
                lo, hi = sorted((c.shots, d_fires))
                if lo < hi * 0.75:
                    found.append(("round.shot_count", "warn",
                                  f"the booth saw {c.shots} shots but GSPro logged {d_fires}"))
        new_unparsed = d_kinds.get("unparsed", 0)
        if new_unparsed:
            found.append(("round.log_unparsed", "warn",
                          f"{new_unparsed} GSPro log line(s) changed wording, e.g. {unparsed[-1][:90]!r}"
                          if unparsed else f"{new_unparsed} GSPro log line(s) changed wording"))
        if wrong := d_kinds.get("wrong_hole", 0):
            # GSPro's own fix-up, so the round's scores may need a look against its scorecard.
            found.append(("round.log_wrong_hole", "warn",
                          f"GSPro corrected {wrong} shot(s) counted on the wrong hole: check the round's scores"))
    if screen:
        report.update(setups=c.setups, hud_missing={"shot": c.hud_shot_missing, "to_pin": c.hud_to_pin_missing,
                                                    "lie": c.hud_lie_missing},
                      full_shots=c.full_shots, tiles_missing=dict(c.tiles_missing))
        if c.setups >= MIN_SETUPS:
            for what, n in (("shot number", c.hud_shot_missing), ("distance to the pin", c.hud_to_pin_missing),
                            ("lie", c.hud_lie_missing)):
                if n > c.setups * MISSING_RATE:
                    found.append((f"round.hud_{what.split()[0]}", "warn",
                                  f"the HUD's {what} wasn't read at {n} of {c.setups} setups: has the HUD moved?"))
        if c.full_shots >= MIN_FULL_SHOTS:
            for tile in required_tiles:
                n = c.tiles_missing.get(tile, 0)
                if n > c.full_shots * MISSING_RATE:
                    found.append((f"round.tile.{tile}", "warn",
                                  f"the {tile!r} tile wasn't read on {n} of {c.full_shots} full shots"))
    report["checked"] = c.shots >= MIN_SHOTS
    report["findings"] = [f"{lvl}: {msg}" for _, lvl, msg in found]
    return found, report


def report_line(report: dict) -> str:
    """The round's report card on one line, for the booth log."""
    bits = [f"{report.get('shots', 0)} shots"]
    if "gspro_shots" in report:
        bits.append(f"GSPro logged {report['gspro_shots']}")
    if "setups" in report:
        m = report["hud_missing"]
        bits.append(f"{report['setups']} setups (missing: shot {m['shot']}, to pin {m['to_pin']}, lie {m['lie']})")
        bits.append(f"{report['full_shots']} full shots (tiles missing: "
                    f"{', '.join(f'{k} {v}' for k, v in report['tiles_missing'].items()) or 'none'})")
    verdict = ("; ".join(report["findings"]) if report["findings"]
               else "all clear" if report.get("checked") else "too short to judge")
    return f"{', '.join(bits)} -> {verdict}"
