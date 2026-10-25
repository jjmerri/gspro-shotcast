import difflib
import json
import re
from pathlib import Path
from typing import Any

import yaml


class League:
    """League context from a JSON or YAML file.

    Only `teams[].players[].name` is required (it drives player matching).
    Everything else is free-form and handed to the commentators as-is, so
    add whatever storylines, nicknames or stats you want them to know.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.name = data.get("league_name", "the league")
        # The league's Squabbit invite code, if handicaps are pulled from there.
        self.squabbit_invite = data.get("squabbit_invite")
        # alias (lowercase) -> canonical player name
        self._aliases: dict[str, str] = {}
        self.player_team: dict[str, str] = {}
        self.team_players: dict[str, list[str]] = {}
        self.player_info: dict[str, dict[str, Any]] = {}
        # Squabbit user id -> roster name, so a live handicap maps to the right player
        # despite several players sharing a surname.
        self.squabbit_ids: dict[str, str] = {}
        # Word -> how to spell it for the voice ("Szabo": "Sah-bo"). Spoken text only; the
        # commentary model and the transcript keep the real spelling.
        self.pronunciations: dict[str, str] = dict(data.get("pronunciations") or {})
        self.week_cards: dict[int, dict[str, Any]] = {}
        # Our own handicap index per player (handicap_index), for whoever has no handicap here.
        self.own_indexes: dict[str, float] = {}
        for team in data.get("teams", []):
            team_name = team.get("name", "")
            self.team_players[team_name] = []
            for p in team.get("players", []):
                name = p["name"]
                self.player_team[name] = team_name
                self.team_players[team_name].append(name)
                self.player_info[name] = p
                if p.get("squabbit_id"):
                    self.squabbit_ids[p["squabbit_id"]] = name
                for alias in [name, *p.get("aliases", []), p.get("gspro_name")]:
                    if alias:
                        self._aliases[alias.lower()] = name

    @classmethod
    def load(cls, path: str | Path | None) -> "League":
        """No path means no league: every round is practice and players go by their GSPro names.
        A path that doesn't exist is an error (most likely a typo in config.yaml), not a silent no-league."""
        if not path:
            return cls({})
        # utf-8-sig: Notepad and Windows PowerShell save UTF-8 with a byte-order mark.
        text = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
        league = cls(data or {})
        league.load_week_cards(cards_path(path))
        return league

    def load_week_cards(self, path: Path) -> None:
        """Squabbit's card for each week (`squabbit --cards`), if saved for this league's invite: the
        tee's rating and slope, its stroke index, and the format's handicap settings."""
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if self.squabbit_invite and saved.get("invite") not in (None, self.squabbit_invite):
            return   # another season's cards
        self.week_cards = {c["week"]: c for c in saved.get("weeks") or [] if c.get("week") is not None}

    def week_card(self, week: Any) -> dict[str, Any] | None:
        try:
            return self.week_cards.get(int(week))
        except (TypeError, ValueError):
            return None

    @property
    def players(self) -> list[str]:
        return list(self.player_team)

    def match_player(self, text: str | None) -> str | None:
        """Map OCR'd or hinted text to a roster name, tolerating OCR noise."""
        if not text:
            return None
        lowered = text.lower().strip()
        if lowered in self._aliases:
            return self._aliases[lowered]
        # The active player's name comes first; other players can follow in the same text.
        # At the same spot the longest name wins ("Daniel" over "Dan").
        found = [(lowered.find(alias), -len(alias), name) for alias, name in self._aliases.items()
                 if len(alias) >= 3 and alias in lowered]
        if found:
            return min(found)[2]
        close = difflib.get_close_matches(lowered, list(self._aliases), n=1, cutoff=0.75)
        return self._aliases[close[0]] if close else None

    def names_for(self, player: str) -> list[str]:
        """Every name a player goes by: roster name, aliases and GSPro name."""
        return sorted({alias for alias, name in self._aliases.items() if name == player} | {player.lower()})

    def match_team(self, text: str | None) -> str | None:
        """A GSPro player named after the team, as a one-ball team format may be set up."""
        if not text:
            return None
        lowered = text.lower().strip()
        teams = {t.lower(): t for t in self.team_players if t}
        for team in self.data.get("teams", []):
            for alias in team.get("aliases", []) + [team.get("nickname")]:
                if alias and team.get("name"):
                    teams[str(alias).lower()] = team["name"]
        if lowered in teams:
            return teams[lowered]
        close = difflib.get_close_matches(lowered, list(teams), n=1, cutoff=0.8)
        return teams[close[0]] if close else None

    def team_name(self, team: str) -> str:
        """The team's nickname if it has one."""
        entry = next((t for t in self.data.get("teams", []) if t.get("name") == team), {})
        return entry.get("nickname") or team

    def handicap(self, player: str) -> float | None:
        """The league's handicap (its file's, or Squabbit's live one), else our own index."""
        value = self.player_info.get(player, {}).get("handicap")
        if isinstance(value, (int, float)):
            return float(value)
        return self.own_indexes.get(player)

    def apply_handicaps(self, by_id: dict[str, float | None]) -> list[str]:
        """Overwrite roster handicaps with live values keyed by Squabbit user id. A missing or
        null value leaves the file's handicap in place. Returns the names it updated."""
        updated = []
        for sid, value in by_id.items():
            name = self.squabbit_ids.get(sid)
            if name and isinstance(value, (int, float)):
                self.player_info[name]["handicap"] = float(value)
                updated.append(name)
        return updated

    def schedule_entry(self, date: str, course: str | None = None) -> dict[str, Any] | None:
        """This round's week of the schedule. Teams play weeks early and two in one visit, so the
        course GSPro shows decides it; an entry with tonight's "date" (YYYY-MM-DD or M/D/YYYY) is
        the fallback. Of two weeks on the same course, the one whose deadline comes first."""
        import datetime

        entries = self.data.get("schedule") or []
        if course:
            # "gspro_course" is GSPro's own name for a course, when it differs from the schedule's.
            scored = [(max(course_match_score(e.get("course", ""), course),
                           course_match_score(e.get("gspro_course") or "", course)), e) for e in entries]
            good = [(sc, e) for sc, e in scored if sc >= 0.6]
            if good:
                best = max(sc for sc, _ in good)
                today = datetime.date.fromisoformat(date).isoformat()
                tied = [e for sc, e in good if sc == best]
                open_ = [e for e in tied if (e.get("end_date") or "9999") >= today]
                return min(open_ or tied, key=lambda e: e.get("end_date") or "9999")
        today = datetime.date.fromisoformat(date)
        for entry in entries:
            raw = str(entry.get("date", "")).strip()
            try:
                when = (datetime.date.fromisoformat(raw) if "-" in raw
                        else datetime.datetime.strptime(raw, "%m/%d/%Y").date())
            except ValueError:
                continue
            if when == today:
                return entry
        return None

    def spoken(self, text: str) -> str:
        """Text respelled for the voice, per the league file's pronunciations (plurals and
        possessives included: "the Szabos", "Szabo's")."""
        for word, say in self.pronunciations.items():
            text = re.sub(rf"\b{re.escape(word)}(?=(?:e?s)?\b)", say, text)
        return text

    def week_entry(self, week: int) -> dict[str, Any] | None:
        return next((e for e in self.data.get("schedule") or [] if e.get("week") == week), None)

    def prompt_text(self) -> str:
        # The whole season's schedule is noise to the booth; tonight's entry reaches it with the round.
        notes = {k: v for k, v in self.data.items() if k not in ("schedule", "pronunciations")}
        if not notes:
            return "(none)"
        return yaml.safe_dump(notes, sort_keys=False, allow_unicode=True)


def cards_path(league_file: str | Path | None) -> Path:
    """Where `squabbit --cards` saves the season's week cards: beside the league file."""
    return Path(league_file or "data/league.json").with_name("squabbit_cards.json")


_COURSE_FILLER = {"the", "golf", "club", "course", "cc", "country", "gc", "at", "and", "of", "dpc", "resort"}


def _course_words(name: str) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _COURSE_FILLER}


def course_match_score(a: str, b: str) -> float:
    """How likely two course names are the same course (GSPro's name vs the schedule's):
    the share of the shorter name's distinctive words found in the longer one, or a close spelling."""
    wa, wb = _course_words(a), _course_words(b)
    if not wa or not wb:
        return 0.0
    overlap = len(wa & wb) / min(len(wa), len(wb))
    # Only a near-identical spelling counts, an OCR slip or two: "Sand Valley" and "Pine Valley" are
    # 0.73 alike, and "Alpine Valley" is 0.92 like "Pine Valley" but two letters away, a different course.
    sa, sb = " ".join(sorted(wa)), " ".join(sorted(wb))
    spelling = difflib.SequenceMatcher(None, sa, sb).ratio()
    close = spelling >= 0.85 and _edits(sa, sb) <= max(1, min(len(sa), len(sb)) // 10)
    return max(overlap, spelling if close else 0.0)


def _edits(a: str, b: str) -> int:
    """Levenshtein distance: the single-character inserts, deletes, and swaps between two strings."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
