"""Builds the league file and league history from the league's yearly spreadsheets.

Each workbook has Schedule, Teams, Results and Standings sheets. Standings hold cumulative
points per week, so each week's points are the difference, and the place follows from the
points table (a tie splits the points of the places it covers). Emails are never read.

Re-running keeps what was added by hand to the league file: handicaps, notes, GSPro names,
pronouns, nicknames, storylines, handicap allowances and pronunciations.
"""

import datetime
import json
import re
from pathlib import Path
from typing import Any

from .round import normalize_format

KEEP_PLAYER = ("handicap", "notes", "gspro_name", "pronouns", "nickname")
KEEP_TEAM = ("nickname", "notes")
KEEP_LEAGUE = ("league_name", "storylines", "handicap_allowances", "notes", "pronunciations", "nicknames")


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _rows(ws) -> list[list[Any]]:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
        return float(v)
    return None


def _date(v) -> str | None:
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()
    return None


def _holes(text: str | None) -> tuple[int, int] | None:
    """(holes, starting hole) from "Front 9", "Back 9" or "18 Holes"."""
    t = (text or "").lower()
    if "back" in t:
        return 9, 10
    if "front" in t:
        return 9, 1
    if "18" in t:
        return 18, 1
    return None


def _split_course(raw: str) -> tuple[str, str | None]:
    """"Wentworth by the Sea (Balsams Panorama)" -> ("Wentworth by the Sea", "Balsams Panorama"). The
    parenthetical is GSPro's own course name, shown on the setup and load screens when it differs from
    the league's; the booth matches the schedule on either name and says the league's."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", raw.strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else (raw.strip(), None)


def _ctp(v) -> list[int]:
    if isinstance(v, (int, float)):
        return [int(v)]
    return [int(n) for n in re.findall(r"\d+", str(v or ""))]


def read_workbook(path: str | Path) -> dict[str, Any]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    out: dict[str, Any] = {"schedule": [], "teams": {}, "standings": {}, "points_table": [], "results": [],
                           "settings": [], "payouts": []}

    rows = _rows(wb["Schedule"])
    header = [str(h or "").strip().lower() for h in rows[0]]
    col = {name: i for i, name in enumerate(header)}
    tee_col = next((i for n, i in col.items() if "tee" in n), None)
    for r in rows[1:]:
        week = _num(r[col["week"]]) if "week" in col else None
        if week is not None and r[col["course"]]:
            span = _holes(r[col["holes"]])
            course_name, gspro_course = _split_course(str(r[col["course"]]))
            out["schedule"].append({
                "week": int(week), "course": course_name, "gspro_course": gspro_course,
                "format": str(r[col["format"]]).strip(), "holes": str(r[col["holes"]]).strip(),
                "hole_count": span[0] if span else None, "start_hole": span[1] if span else None,
                "tees": str(r[tee_col]).strip() if tee_col is not None and r[tee_col] else None,
                "start_date": _date(r[col["start date"]]) if "start date" in col else None,
                "end_date": _date(r[col["end date"]]) if "end date" in col else None,
                "ctp_holes": _ctp(r[col["ctp"]]) if "ctp" in col else [],
            })
        else:
            for v in r:
                if isinstance(v, str) and ":" in v and not v.lower().startswith(("booking", "all weeks")):
                    out["settings"] += [line.strip() for line in v.splitlines() if line.strip()]

    for r in _rows(wb["Teams"])[1:]:
        team, player = (str(r[0] or "").strip(), str(r[1] or "").strip()) if len(r) > 1 else ("", "")
        if re.fullmatch(r"Team \d+", team) and player:
            out["teams"].setdefault(team, []).append(" ".join(player.split()))

    for r in _rows(wb["Standings"]):
        label = str(r[0] or "").strip()
        if "/" in label and len(r) > 1:
            weeks = [_num(v) for v in r[1:13]]
            out["standings"][label] = {"cumulative": weeks, "adjusted": _num(r[13]) if len(r) > 13 else None}
        elif re.fullmatch(r"\d+(st|nd|rd|th)", label) and _num(r[1]) is not None:
            out["points_table"].append(_num(r[1]))
        elif re.match(r"(1st|2nd|3rd|week winners|ctp)\s*-\s*\$", label, re.I):
            out["payouts"].append(label)

    rows = _rows(wb["Results"])
    header = [str(h or "").strip().lower() for h in rows[0]]
    ctp_col = header.index("ctp") if "ctp" in header else None
    win_col = header.index("week winner") if "week winner" in header else None
    for r in rows[1:]:
        week, player = _num(r[0]), str(r[1] or "").strip()
        if week is None or not player:
            continue
        out["results"].append({
            "week": int(week), "player": player,
            "ctp": _num(r[ctp_col]) if ctp_col is not None else None,
            "week_winner": _num(r[win_col]) if win_col is not None else None,
        })
    dates = [e["end_date"] for e in out["schedule"] if e["end_date"]]
    out["year"] = int(min(dates)[:4]) if dates else None
    return out


def _first(name: str) -> str:
    return name.split()[0]


def _short_names(players: list[str]) -> dict[str, str]:
    """First names, with a last initial where two players share a first name ("Kyle M")."""
    firsts: dict[str, int] = {}
    for p in players:
        firsts[_first(p)] = firsts.get(_first(p), 0) + 1
    return {p: _first(p) if firsts[_first(p)] == 1 else f"{_first(p)} {p.split()[-1][0]}" for p in players}


def _match_label(label: str, teams: dict[str, list[str]]) -> str | None:
    """Maps a standings label ("Kyle M/Mike O") to a team number by its players."""
    parts = [p.strip() for p in label.split("/")]
    for number, players in teams.items():
        def fits(part: str) -> bool:
            words = part.split()
            return any(_first(p).lower() == words[0].lower()
                       and (len(words) == 1 or p.split()[-1].lower().startswith(words[1].lower())) for p in players)
        if len(parts) == len(players) and all(fits(part) for part in parts):
            return number
    return None


def place_for(points: float, table: list[float]) -> tuple[int, bool] | None:
    """(place, tied) for one week's points: a tie over places i..j shares their average."""
    for span in range(1, len(table) + 1):
        for start in range(len(table) - span + 1):
            share = sum(table[start:start + span]) / span
            if abs(share - points) < 0.01:
                return start + 1, span > 1
    return None


def _resolve_player(name: str, roster: list[str]) -> str | None:
    if name in roster:
        return name
    matches = [p for p in roster if _first(p).lower() == name.split()[0].lower()]
    return matches[0] if len(matches) == 1 else None


def season_history(book: dict[str, Any]) -> dict[str, Any]:
    teams = book["teams"]
    roster = [p for players in teams.values() for p in players]
    table = book["points_table"]
    weeks_info = {e["week"]: e for e in book["schedule"]}
    season: dict[str, Any] = {"year": book["year"], "teams": [], "awards": [], "points_table": table,
                              "payouts": book["payouts"], "team_count": len(teams)}
    for label, st in book["standings"].items():
        number = _match_label(label, teams)
        players = teams.get(number, [])
        weeks, prev = [], 0.0
        for i, cum in enumerate(st["cumulative"], 1):
            if cum is None:
                break
            pts = cum - prev
            prev = cum
            place = place_for(pts, table)
            info = weeks_info.get(i, {})
            weeks.append({"week": i, "points": pts, "place": place[0] if place else None,
                          "tied": place[1] if place else False, "course": info.get("course"),
                          "format": normalize_format(info.get("format"))})
        season["teams"].append({"label": label, "players": players, "weeks": weeks,
                                "total": prev if weeks else 0.0, "adjusted": st["adjusted"]})
    played = [t for t in season["teams"] if t["weeks"]]
    finished = all(len(t["weeks"]) == len(book["schedule"]) for t in played) and played
    key = (lambda t: t["adjusted"] if finished and t["adjusted"] is not None else t["total"])
    for team in played:
        team["rank"] = 1 + sum(key(other) > key(team) for other in played)
        team["rank_tied"] = sum(key(other) == key(team) for other in played) > 1
    season["complete"] = bool(finished)
    for r in book["results"]:
        player = _resolve_player(r["player"], roster)
        if not player:
            continue
        if r["ctp"]:
            season["awards"].append({"week": r["week"], "player": player, "kind": "ctp", "amount": r["ctp"]})
        if r["week_winner"]:
            season["awards"].append({"week": r["week"], "player": player, "kind": "week_win", "amount": r["week_winner"]})
    return season


def build_league(book: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    old_players = {p["name"]: p for t in existing.get("teams", []) for p in t.get("players", [])}
    old_teams = {t.get("number"): t for t in existing.get("teams", [])}
    roster = [p for players in book["teams"].values() for p in players]
    short = _short_names(roster)
    labels = {_match_label(label, book["teams"]): label for label in book["standings"]}
    table = book["points_table"]
    league: dict[str, Any] = {
        "league_name": "the Sim League",
        "season": book["year"],
        "total_weeks": len(book["schedule"]),
        "format": (f"{len(book['teams'])} two-person teams. Every team plays every week's round on its own schedule "
                   "and earns points for where it places in the whole field (FedEx Cup style): "
                   + ", ".join(f"{ordinal(i)} {p:g}" for i, p in enumerate(table, 1))
                   + ". Ties split the points of the places they cover. Each team's two worst weeks are dropped "
                   "at the end of the season. Formats rotate: scramble, best ball, aggregate (team sum), "
                   "alternate shot. Scores are net of handicap."),
        "prizes": book["payouts"] + ["Weekly CTP winners and the weekly winning team are paid per person."],
        "rules": [
            "Two closest-to-the-pin holes each week; an unclaimed CTP carries over to the next week.",
            "A weekly tie for first is broken for the money by card matching from hole 1; points are still split.",
            "Season ties for 1st, 2nd or 3rd go to a best ball playoff.",
            "Mulligans only for misreads, never for a shot someone actually swung at.",
            "New players need three 18-hole handicap rounds before league play.",
        ],
        "gspro_settings": book["settings"],
    }
    for line in book["payouts"]:
        m = re.match(r"(ctp|week winners)\s*-\s*\$(\d+)", line, re.I)
        if m:
            league["ctp_prize" if m.group(1).lower() == "ctp" else "week_winner_prize"] = int(m.group(2))
    for key in KEEP_LEAGUE:
        if key in existing:
            league[key] = existing[key]
    league.setdefault("storylines", [])
    league["teams"] = []
    for number, players in sorted(book["teams"].items(), key=lambda kv: int(kv[0].split()[1])):
        team: dict[str, Any] = {"name": " and ".join(short[p] for p in players), "number": number,
                                "aliases": [a for a in (number, labels.get(number)) if a], "players": []}
        for key in KEEP_TEAM:
            if key in old_teams.get(number, {}):
                team[key] = old_teams[number][key]
        for p in players:
            # Only a first name nobody else in the league shares is a safe alias.
            entry: dict[str, Any] = {"name": p, "aliases": [short[p]], "handicap": None, "notes": ""}
            for key in KEEP_PLAYER:
                if key in old_players.get(p, {}):
                    entry[key] = old_players[p][key]
            team["players"].append(entry)
        league["teams"].append(team)
    old_weeks = {e.get("week"): e for e in existing.get("schedule", [])}
    for entry in book["schedule"]:
        if old_weeks.get(entry["week"], {}).get("gspro_course"):
            entry["gspro_course"] = old_weeks[entry["week"]]["gspro_course"]
    league["schedule"] = book["schedule"]
    return league


def run_import(current: str, history: list[str], league_out: str, history_out: str) -> tuple[dict, dict]:
    book = read_workbook(current)
    existing = json.loads(Path(league_out).read_text(encoding="utf-8-sig")) if Path(league_out).exists() else None
    league = build_league(book, existing)
    seasons = [season_history(book)] + [season_history(read_workbook(h)) for h in history]
    hist = {"current_year": book["year"], "seasons": sorted(seasons, key=lambda s: s["year"] or 0, reverse=True)}
    Path(league_out).write_text(json.dumps(league, indent=2) + "\n", encoding="utf-8")
    Path(history_out).write_text(json.dumps(hist, indent=2) + "\n", encoding="utf-8")
    return league, hist
