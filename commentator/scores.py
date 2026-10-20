"""Live scores for the booth's asides: a tour leaderboard and the home teams' games, from ESPN's public
scoreboard JSON (no key). Read at the start of the hole an aside is written for, so what's said on air
is minutes old, never a stale score from round start. A board that fails is skipped, never fatal.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

log = logging.getLogger(__name__)

_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_UA = "gspro-shotcast/1.0 (booth scores reader)"
TOURS = {"pga": "PGA Tour", "lpga": "LPGA Tour", "eur": "DP World Tour", "champions-tour": "PGA Tour Champions"}
LEAGUES = {"nhl": "hockey/nhl", "nba": "basketball/nba", "mlb": "baseball/mlb", "nfl": "football/nfl",
           "wnba": "basketball/wnba", "mls": "soccer/usa.1"}
# How long a team's result stays worth a mention, and how far ahead a game tonight is.
FINAL_HOURS = 18
UPCOMING_HOURS = 5
GAME_HOURS = 3     # kickoff to final, roughly, for dating a result


@dataclass
class ScoreItem:
    key: str      # the event, so the round doesn't come back to it
    kind: str     # "tour" or "team"
    state: str    # "in" (live), "post" (just finished), "pre" (later tonight)
    fact: str     # how the booth is told it
    version: str = ""   # changes when the score does, so a later hole can give an update

    @property
    def rank(self) -> int:
        return {"in": 0, "post": 1, "pre": 2}.get(self.state, 3)


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def _when(dt: datetime, now: datetime) -> str:
    """"this afternoon", "last night", "Sunday": when a game or event finished, said the way people do."""
    part = "morning" if dt.hour < 12 else "afternoon" if dt.hour < 17 else "evening"
    if dt.date() == now.date():
        return "tonight" if part == "evening" else f"this {part}"
    if dt.date() == now.date() - timedelta(days=1):
        return "last night" if part == "evening" else f"yesterday {part}"
    return dt.strftime("%A")


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_par(text) -> int | None:
    text = str(text or "").strip()
    if text in ("E", "0"):
        return 0
    try:
        return int(text)
    except ValueError:
        return None


def _par_text(n: int) -> str:
    return "even" if n == 0 else f"{n:+d}"


def parse_tour(body: dict, tour: str, now: datetime, top: int = 5) -> list[ScoreItem]:
    """A tournament in progress (between rounds counts: the leaderboard after two rounds is current),
    or one that finished in the last day, as its leaderboard's top few and their day."""
    out = []
    for event in body.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        status = (comp.get("status") or event.get("status") or {})
        state = (status.get("type") or {}).get("state")
        if state not in ("in", "post"):
            continue
        end = _parse_time(event.get("endDate"))
        # ESPN dates an event's end as midnight Eastern starting its last day: a date, not a time.
        last_day = (end - timedelta(hours=4)).date() if end else None
        if state == "post" and (last_day is None or not 0 <= (now.date() - last_day).days <= 1):
            continue
        period = status.get("period") or 0
        label = (status.get("type") or {}).get("detail") or ""
        # A playoff is its own "period" of a hole or three: the day's numbers are the last full round's.
        playoff = "playoff" in label.lower()
        rows = []
        for c in comp.get("competitors") or []:
            score = _to_par(c.get("score"))
            name = ((c.get("athlete") or {}).get("displayName") or "").strip()
            if score is None or not name:
                continue
            rounds = c.get("linescores") or []
            if state == "post" or playoff:
                full = [ls for ls in rounds if (ls.get("value") or 0) >= 50]   # strokes for a whole round
                today, holes = (full[-1], 18) if full else ({}, 0)
            else:
                today = next((ls for ls in rounds if ls.get("period") == period), {})
                holes = sum(1 for h in today.get("linescores") or [] if h.get("value") is not None)
            rows.append((score, c.get("order") or 999, name, holes, today.get("displayValue")))
        if not rows:
            continue
        rows.sort(key=lambda r: (r[0], r[1]))
        leaders = []
        for score, _o, name, holes, day in rows:
            if len(leaders) >= top and score != leaders[-1][0]:
                break
            leaders.append((score, name, holes, day))
        # Finished level at the top means a playoff, and ESPN's order has its winner first.
        won_playoff = state == "post" and len(rows) > 1 and rows[0][0] == rows[1][0]
        parts = []
        for i, (score, name, holes, day) in enumerate(leaders):
            pos = 1 + sum(1 for r in rows if r[0] < score)
            tied = sum(1 for r in rows if r[0] == score) > 1
            detail = []
            if won_playoff and score == rows[0][0]:
                pos, tied = (1, False) if i == 0 else (2, sum(1 for r in rows if r[0] == score) > 2)
                detail.append("won the playoff" if i == 0 else "lost in the playoff")
            if state == "in" and not playoff and 0 < holes < 18:
                detail.append(f"thru {holes}")
            if day and holes:
                detail.append(f"{day} in the final round" if state == "post"
                              else f"{day} in their last round" if playoff else f"{day} today")
            parts.append(f"{'T' if tied else ''}{pos}. {name} {_par_text(score)}"
                         + (f" ({', '.join(detail)})" if detail else ""))
        name = event.get("name") or "the tournament"
        if state == "in":
            head = f"{TOURS.get(tour, tour)}, live: the {name}, {label}." if label else \
                f"{TOURS.get(tour, tour)}, live: the {name}."
        else:
            when = "today" if last_day == now.date() else "yesterday"
            head = f"{TOURS.get(tour, tour)}: the {name} finished {when}. Final leaderboard:"
        fact = f"{head} " + (f"Leaderboard as of {_clock(now)}: " if state == "in" else "") + "; ".join(parts) + "."
        version = f"{state}:{label}:" + ",".join(f"{n}{s}{h}" for s, n, h, _d in leaders[:3])
        out.append(ScoreItem(f"{tour}:{event.get('id')}", "tour", state, fact, version))
    return out


def _names(team: dict) -> set[str]:
    """Every way a team may be named in the config: "BOS", "Bruins", "Boston Bruins"."""
    return {str(team.get(k) or "").strip().lower() for k in ("abbreviation", "shortDisplayName", "name",
                                                              "displayName")} - {""}


def parse_games(body: dict, league: str, teams: list[str], now: datetime) -> list[ScoreItem]:
    """The home teams' games: live, finished in the last FINAL_HOURS, or starting within UPCOMING_HOURS."""
    wanted = {t.strip().lower() for t in teams if t.strip()}
    out = []
    for event in body.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        sides = comp.get("competitors") or []
        if not any(_names(s.get("team") or {}) & wanted for s in sides):
            continue
        status = event.get("status") or comp.get("status") or {}
        kind = status.get("type") or {}
        state = kind.get("state")
        start = _parse_time(event.get("date"))
        if start is None:
            continue
        start = start.astimezone(now.tzinfo)
        if state == "pre" and not timedelta(0) <= start - now <= timedelta(hours=UPCOMING_HOURS):
            continue
        if state == "post" and now - (start + timedelta(hours=GAME_HOURS)) > timedelta(hours=FINAL_HOURS):
            continue
        if state not in ("pre", "in", "post"):
            continue

        def team(s):
            t = s.get("team") or {}
            rec = next((r.get("summary") for r in s.get("records") or [] if r.get("summary")), None)
            return t.get("displayName") or t.get("name") or "?", t.get("shortDisplayName") or t.get("name"), rec

        away = next((s for s in sides if s.get("homeAway") == "away"), sides[-1] if sides else {})
        home = next((s for s in sides if s.get("homeAway") == "home"), sides[0] if sides else {})
        (an, ash, arec), (hn, hsh, hrec) = team(away), team(home)
        matchup = f"{an}{f' ({arec})' if arec else ''} at {hn}{f' ({hrec})' if hrec else ''}"
        sport = league.upper()
        if state == "pre":
            fact = f"{sport}, later tonight: {matchup}, starting at {_clock(start)}."
            version = "pre"
        else:
            score = f"{ash} {away.get('score', '?')}, {hsh} {home.get('score', '?')}"
            if state == "in":
                fact = f"{sport}, live as of {_clock(now)}: {matchup}. {score}, {kind.get('detail') or 'in progress'}."
            else:
                fact = f"{sport}, final, {_when(start, now)}: {matchup}. {score}" + (
                    f" ({kind.get('detail')})." if kind.get("detail") not in (None, "", "Final") else ".")
                recap = next((h for h in comp.get("headlines") or [] if h.get("description")), None)
                if recap:
                    # ESPN cuts the description off mid-sentence; keep only its whole sentences.
                    desc = recap["description"].lstrip("— ").strip()
                    desc = desc[: desc.rfind(". ") + 1] if not desc.endswith(".") else desc
                    fact += f" Recap: {(recap.get('shortLinkText') or '').rstrip('.')}." + (f" {desc}" if desc else "")
            version = f"{away.get('score')}-{home.get('score')}-{kind.get('detail')}"
        out.append(ScoreItem(f"{league}:{event.get('id')}", "team", state, fact, version))
    return out


class LiveScores:
    """Fetches every configured board at once, on demand. Never raises."""

    def __init__(self, tours: list[str], teams: list[str] | dict[str, list[str]], timeout: float = 8.0) -> None:
        self.tours = [t for t in tours if t]
        # A plain list of teams is looked for in the big four leagues; a dict names each team's league.
        if isinstance(teams, (list, tuple)):
            teams = {lg: list(teams) for lg in ("nhl", "nba", "mlb", "nfl")} if teams else {}
        self.teams = {lg: ts for lg, ts in (teams or {}).items() if ts and lg in LEAGUES}
        self.timeout = timeout

    async def fetch(self, now: datetime | None = None) -> list[ScoreItem]:
        now = now or datetime.now().astimezone()
        jobs = [(f"golf/{t}", {}, lambda b, t=t: parse_tour(b, t, now)) for t in self.tours]
        # The default board is today's slate only; last night's results are on yesterday's (ESPN takes
        # a single date here, not a range).
        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        jobs += [(LEAGUES[lg], q, lambda b, lg=lg, ts=ts: parse_games(b, lg, ts, now))
                 for lg, ts in self.teams.items() for q in ({}, {"dates": yesterday})]
        if not jobs:
            return []
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": _UA}) as client:
            async def one(path, params, parse):
                try:
                    resp = await client.get(f"{_BASE}/{path}/scoreboard", params=params)
                    resp.raise_for_status()
                    return parse(resp.json())
                except Exception as e:  # a down board must never take the booth off air
                    log.warning("Scoreboard %s failed: %s", path, e)
                    return []
            results = await asyncio.gather(*(one(p, q, f) for p, q, f in jobs))
        seen: set[str] = set()
        items = [i for r in results for i in r if not (i.key in seen or seen.add(i.key))]
        items.sort(key=lambda i: i.rank)
        return items

