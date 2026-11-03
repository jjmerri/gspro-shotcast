"""The booth's memory of a side's earlier rounds, as the facts a producer would hand it: when and where
they last played and how it went, the one thing about that round that stood out against their own
usual, their record on this course, and a pattern on the hole they're about to play.

Pure functions over Store.side_rounds rows (newest first). Each fact clears a bar before it's offered,
so most rounds and most holes get none; the engine decides when, and how often, to pass them on."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from .round import to_par

# How far from a side's own usual a last-round rate has to be to stand out, and the sample behind each.
PUTTS_PER_HOLE_GAP = 0.25
FAIRWAY_GAP = 0.25
GREEN_GAP = 0.2
BASELINE_ROUNDS = 3        # earlier rounds needed before "best" or "usual" means anything
BLOW_UP = 3                # strokes over par on one hole that make it the story of a round

_OVER = {3: "triple bogey", 4: "quadruple bogey"}


@dataclass(frozen=True)
class Standout:
    """The one thing about a side's last round worth a line, and the moment it fits: 'score' (the
    round as a whole, for the opener), 'putt', 'tee' (a par 4 or 5's tee shot) or 'approach'."""
    moment: str
    text: str


def _day(row: dict) -> date | None:
    try:
        return date.fromisoformat(row["date"])
    except (TypeError, ValueError):
        return None


def when(row: dict, today: date) -> str:
    d = _day(row)
    if d is None:
        return "recently"
    days = (today - d).days
    return ("earlier today" if days <= 0 else "yesterday" if days == 1
            else f"{days} days ago" if days < 14 else f"on {d:%B} {d.day}, {days // 7} weeks ago")


def _holes(row: dict) -> dict[int, tuple[int, int]]:
    """Hole number -> (strokes, par), for the holes with both."""
    try:
        scores, pars = json.loads(row.get("scores") or "{}"), json.loads(row.get("hole_pars") or "{}")
    except ValueError:
        return {}
    return {int(h): (s, pars[h]) for h, s in scores.items() if s and pars.get(h)}


def _over(d: int) -> str:
    return _OVER.get(d, f"{d} over par")


def _a(n: int) -> str:
    return f"an {n}" if n in (8, 11, 18) or 80 <= n < 90 else f"a {n}"


def _finished(row: dict) -> bool:
    """A round played to the end with every par known, so its gross and to_par9 describe the same holes
    (an abandoned round's to_par9 is the nine it finished)."""
    return row.get("to_par9") is not None and bool(row.get("complete")) and row.get("played") == row.get("holes") \
        and _full_par(row)


def _full_par(row: dict) -> bool:
    """Whether every played hole has a score and a par, so gross minus par is the round to par."""
    return bool(row.get("par") and row.get("played") and len(_holes(row)) == row["played"])


def last_round_fact(who: str, rows: list[dict], today: date) -> str | None:
    """When, where and what: "Dan's last round: yesterday, 78 (6 over par) for 18 holes at Bethpage"."""
    if not rows:
        return None
    last = rows[0]
    score = str(last["gross"])
    if _full_par(last):
        score += f" ({to_par(last['gross'] - last['par'])})"
    kind = " in the league" if last.get("is_league") else ""
    return (f"{who}'s last round{kind}: {when(last, today)}, {score} for {last['played']} holes"
            + (f" at {last['course']}" if last.get("course") else "") + ".")


def _rate(rows: list[dict], hit: str, tries: str) -> tuple[float | None, int]:
    known = [r for r in rows if r.get(hit) is not None and r.get(tries)]
    t = sum(r[tries] for r in known)
    return (sum(r[hit] for r in known) / t if t else None), t


def standout(who: str, rows: list[dict]) -> Standout | None:
    """The one thing about the last round furthest from the side's own usual, or None when nothing
    is: the season's best or worst score, a blow-up hole, or putting, fairways or greens well off
    their norm. Each scored by how far past its bar it is, so the clearest story wins."""
    if not rows:
        return None
    last, before = rows[0], rows[1:]
    found: list[tuple[float, Standout]] = []
    scored = [r["to_par9"] for r in before if _finished(r)]
    if _finished(last) and len(scored) >= BASELINE_ROUNDS:
        if last["to_par9"] < min(scored):
            found.append((2.0, Standout("score", f"That was {who}'s best round of the season.")))
        elif last["to_par9"] > max(scored):
            found.append((1.5, Standout("score", f"That was {who}'s highest-scoring round of the season.")))
    holes = _holes(last)
    if holes and (worst := max(holes, key=lambda h: holes[h][0] - holes[h][1])):
        s, p = holes[worst]
        if s - p >= BLOW_UP:
            found.append((1.0 + 0.2 * (s - p - BLOW_UP),
                          Standout("score", f"In that round {who} made {_a(s)} on the par-{p} hole {worst}, "
                                            f"a {_over(s - p)}.")))
    if len(before) >= BASELINE_ROUNDS:
        if (last.get("putt_holes") or 0) >= 9 and last.get("putts") is not None:
            usual, n = _rate(before, "putts", "putt_holes")
            mine = last["putts"] / last["putt_holes"]
            if usual is not None and n >= 27 and abs(mine - usual) >= PUTTS_PER_HOLE_GAP:
                found.append((abs(mine - usual) / PUTTS_PER_HOLE_GAP, Standout(
                    "putt", f"{who} took {last['putts']} putts over {last['putt_holes']} holes last round, "
                            f"{mine:.1f} a hole against {usual:.1f} usually.")))
        if (last.get("fir_tries") or 0) >= 5 and last.get("fir_hit") is not None:
            usual, n = _rate(before, "fir_hit", "fir_tries")
            mine = last["fir_hit"] / last["fir_tries"]
            if usual is not None and n >= 15 and abs(mine - usual) >= FAIRWAY_GAP:
                found.append((abs(mine - usual) / FAIRWAY_GAP, Standout(
                    "tee", f"{who} hit {last['fir_hit']} of {last['fir_tries']} fairways last round, "
                           f"against {round(100 * usual)}% usually.")))
        if (last.get("gir_tries") or 0) >= 9 and last.get("gir_hit") is not None:
            usual, n = _rate(before, "gir_hit", "gir_tries")
            mine = last["gir_hit"] / last["gir_tries"]
            if usual is not None and n >= 27 and abs(mine - usual) >= GREEN_GAP:
                found.append((abs(mine - usual) / GREEN_GAP, Standout(
                    "approach", f"{who} hit {last['gir_hit']} of {last['gir_tries']} greens in regulation "
                                f"last round, against {round(100 * usual)}% usually.")))
    return max(found, key=lambda f: f[0])[1] if found else None


def course_fact(who: str, rows: list[dict], course: str | None, today: date) -> str | None:
    """Their record here: how often, when last, and their best; or a first visit, once they've played
    enough elsewhere for that to mean something."""
    if not course:
        return None
    here = [r for r in rows if (r.get("course") or "").casefold() == course.casefold()]
    if not here:
        return f"This is {who}'s first recorded round at {course}." if len(rows) >= BASELINE_ROUNDS else None
    times = f"{len(here)} time{'s' if len(here) != 1 else ''}"
    fact = f"{who} has played {here[0]['course']} {times} before, most recently {when(here[0], today)}"
    known = [r for r in here if _finished(r)]
    if len(here) > 1 and known:
        best = min(known, key=lambda r: r["to_par9"])
        fact += f"; best: {best['gross']} ({to_par(best['gross'] - best['par'])}) for {best['played']} holes"
    return fact + "."


def hole_fact(who: str, rows: list[dict], course: str | None, hole: int | None) -> str | None:
    """A pattern on this hole across their earlier rounds here, when there's a clear one: bogey or
    worse every time, birdie or better every time, never a dropped shot in three or more, or a blow-up
    last time. None for most holes."""
    if not course or hole is None:
        return None
    plays = [h[hole] for r in rows if (r.get("course") or "").casefold() == course.casefold()
             and hole in (h := _holes(r))]
    if not plays:
        return None
    diffs = [s - p for s, p in plays]
    n = len(diffs)
    every = "both times" if n == 2 else f"all {n} times"
    if n >= 2 and all(d >= 1 for d in diffs):
        return f"{who} has made bogey or worse on this hole {every} they've played it before."
    if n >= 2 and all(d <= -1 for d in diffs):
        return f"{who} has made birdie or better on this hole {every} they've played it before."
    if n >= 3 and all(d <= 0 for d in diffs):
        return f"{who} has never dropped a shot on this hole in {n} rounds here."
    if diffs[0] >= BLOW_UP:
        return f"Last time here {who} made {_a(plays[0][0])} on this hole, a {_over(diffs[0])}."
    return None
