import difflib
import json
import re
from pathlib import Path
from typing import Any

import yaml


def _chat_keys(name: str | None) -> list[str]:
    """A name as a live-chat viewer might type it: the whole of it, then its first word, lowercase
    without punctuation ("Dan M!" -> "dan m", "dan")."""
    words = re.sub(r"[^\w' -]+", " ", (name or "").lower()).split()
    return list(dict.fromkeys([" ".join(words), *words[:1]])) if words else []


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
        # A name or its first word (lowercase) -> every player it could be, for live-chat names.
        self._chat_names: dict[str, set[str]] = {}
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
        # Name (lowercase) -> what the booth calls them on air: a roster player's `nickname`, or the
        # top-level `nicknames` map's, keyed by a guest's GSPro name (or a roster name).
        self._nicknames: dict[str, str] = {str(k).casefold(): str(v).strip()
                                           for k, v in (data.get("nicknames") or {}).items() if k and v and str(v).strip()}
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
                if (nick := str(p.get("nickname") or "").strip()):
                    self._nicknames[name.casefold()] = nick
                for alias in [name, *p.get("aliases", []), p.get("gspro_name")]:
                    if alias:
                        self._aliases[alias.lower()] = name
                        for key in _chat_keys(alias):
                            self._chat_names.setdefault(key, set()).add(name)

    @property
    def _roster_cf(self) -> set[str]:
        return {p.casefold() for p in self.player_team}

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

    def exact_player(self, text: str | None) -> str | None:
        """The roster name `text` is one of a player's names exactly (no OCR tolerance)."""
        return self._aliases.get(text.lower().strip()) if text else None

    def names_of(self, player: str) -> list[str]:
        """Every name a roster player goes by (lowercased)."""
        return [a for a, p in self._aliases.items() if p == player]

    def chat_member(self, name: str | None) -> str | None:
        """The league player a live-chat name is most likely: the whole name or its first word is one of
        a player's names, or a name's first word, exactly ("dan", "Dan M", "Dan!") and nobody else's.
        Never fuzzy: a viewer typed it, OCR didn't misread it, and "Danny" may be anyone."""
        for candidate in _chat_keys(name):
            players = self._chat_names.get(candidate, ()) if len(candidate) >= 3 else ()
            if len(players) == 1:
                return next(iter(players))
            if players:
                return None   # shared by two players: either one
        return None

    def nickname(self, name: str | None) -> str | None:
        """What the booth calls this player (roster name) or guest (GSPro name) on air, if not their name."""
        return self._nicknames.get(name.casefold()) if name else None

    def nicknamed(self, guests: list[str] = ()) -> dict[str, str]:
        """Every name with a nickname, spelled as the booth writes it (the roster's, a guest's as GSPro
        has it) -> the nickname."""
        out = {str(k): str(v).strip() for k, v in (self.data.get("nicknames") or {}).items()
               if k and v and str(v).strip() and str(k).casefold() not in self._roster_cf}
        out.update({p: n for p in self.players if (n := self.nickname(p))})
        for g in guests:
            if (n := self.nickname(g)) and g.casefold() not in self._roster_cf:
                out = {k: v for k, v in out.items() if k.casefold() != g.casefold()}
                out[g] = n
        return out

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

    def schedule_entry(self, date: str, course: str | None = None, fits=None) -> dict[str, Any] | None:
        """This round's week of the schedule. Teams play weeks early and two in one visit, so the
        course GSPro shows decides it; an entry with tonight's "date" (YYYY-MM-DD or M/D/YYYY) is
        the fallback. Of two weeks on the same course (one naming it exactly first), the one whose
        deadline comes first. `fits`
        (entry -> bool) leaves out weeks surely on another course before the course decides."""
        import datetime

        entries = self.data.get("schedule") or []
        if course:
            # "gspro_course" is GSPro's own name for a course, when it differs from the schedule's.
            scored = [(max(course_match_score(e.get("course", ""), course),
                           course_match_score(e.get("gspro_course") or "", course)), e)
                      for e in entries if fits is None or fits(e)]
            good = [(sc, e) for sc, e in scored if sc >= 0.6]
            # A week naming the course exactly beats one merely as like it ("Pinehurst" is the week
            # whose alias it is, not Pinehurst No. 4's)
            plain = " ".join(re.findall(r"[a-z0-9]+", course.lower()))
            exact = [(sc, e) for sc, e in good if plain in {" ".join(re.findall(r"[a-z0-9]+", (n or "").lower()))
                                                             for n in (e.get("course"), e.get("gspro_course"))}]
            good = exact or good
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

    # With team_players, team_name and week_entry, what a shotcast_scoring Board reads of its league
    # (LeagueView). This league has no flights: every team plays in one field.
    @property
    def flights(self) -> dict[str, tuple[str, str]]:
        """The teams' flights (none)."""
        return {}

    def flight(self, team: str | None) -> tuple[str, str] | None:
        """`team`'s flight (id, name): None, this league has no flights."""
        return None

    @property
    def schedule(self) -> list[dict[str, Any]]:
        """The season's weeks (the league file's `schedule`)."""
        return self.data.get("schedule") or []

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


_OPEN_QUOTE, _CLOSE_QUOTE = "\"“'‘", "\"”'’"
# First names that are also everyday words: at the start of a sentence "Will" is as likely the verb
# ("Will this drop?") as the player, so only a mid-sentence one is taken for him.
_WORD_NAMES = {
    "will", "mark", "bill", "pat", "rob", "grant", "chase", "jack", "guy", "drew", "frank", "bob",
    "rich", "ray", "max", "miles", "lane", "wade", "chip", "rusty", "sandy", "dean", "don", "earl",
    "jay", "lee", "norm", "rocky", "gene", "may", "june", "april", "hope", "joy", "rose", "ruby",
    "faith", "penny", "sky", "dawn", "art", "bud", "duke", "hunter", "carter", "page", "summer",
}


def _sentence_start(text: str, at: int) -> bool:
    before = text[:at].rstrip(" \t(" + _OPEN_QUOTE)
    return not before or before[-1] in ".!?:\n-"


class OnAir:
    """Players who go by a nickname, as the booth should say them. A prompt names a nicknamed player
    in full as First "Nick" Last and by first name as the nickname, so the model never sees the first
    name alone; a line that says it anyway is a slip. A first name someone else in `others` shares
    is left alone: only the full name is sure to be them."""

    def __init__(self, nicknamed: dict[str, str], others: list[str], present: list[str] | None = None) -> None:
        """`nicknamed`: name as written -> nickname. `others`: everyone else the booth may name (the
        roster, guests, the commentators), whose full names are theirs. `present`: those of them a
        first name alone could mean (the round's players and the commentators; default `others`): a
        rostered Mike who isn't playing doesn't stop Mike being Ozzy. Names match as written,
        capitalised: "will" isn't Will."""
        self._full: list[tuple[re.Pattern, str, str]] = []           # (pattern, nickname, name)
        self._first: list[tuple[re.Pattern, str, bool, str]] = []    # (pattern, nickname, a word too, name)
        self._named: list[tuple[re.Pattern, str]] = []         # the nickname back to the name
        self._intros: list[re.Pattern] = []                    # First "Nick" Last, said right
        # Someone else's full name is theirs, even with a nicknamed first name in it: Dan Scott's "Scott".
        whole = sorted({o for o in others if len(o.split()) > 1 and o not in nicknamed}, key=len, reverse=True)
        self._others = re.compile(r"\b(?:" + "|".join(r"\s+".join(map(re.escape, o.split())) for o in whole)
                                  + r")\b") if whole else None
        # A nickname is what it says, even with a first name in it: "Big Mike" isn't a slip for Mike.
        nicks = sorted({n for n in nicknamed.values() if n.strip()}, key=len, reverse=True)
        self._nicks = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, nicks)) + r")(?!\w)") if nicks else None
        # A first name that's also someone else's name or nickname could be either of them.
        taken: dict[str, set[str]] = {}
        for o in [*(others if present is None else present), *nicknamed]:
            if o and o.split():
                taken.setdefault(o.split()[0].casefold(), set()).add(o.casefold())
        for n in nicknamed.values():
            for w in n.split():
                taken.setdefault(w.casefold(), set()).add(f"\x00{n}")
        for name, nick in nicknamed.items():
            words = name.split()
            if not words or nick.casefold() == words[0].casefold():
                continue
            quoted = rf"\s+[{_OPEN_QUOTE}]{re.escape(nick)}[{_CLOSE_QUOTE}]"
            rest = r"\s+".join(map(re.escape, words[1:]))
            self._intros.append(re.compile(rf"\b{re.escape(words[0])}{quoted}" + (rf"(?:\s+{rest}\b)?" if rest else "")))
            if len(words) > 1:
                self._full.append((re.compile(rf"\b({re.escape(words[0])})(?:{quoted})?\s+({rest})\b"), nick, name))
            if not taken.get(words[0].casefold(), set()) - {name.casefold(), f"\x00{nick}"}:
                self._first.append((re.compile(rf"\b{re.escape(words[0])}\b(?!{quoted})"), nick,
                                    words[0].casefold() in _WORD_NAMES, name))
            # The checks know players by name, so a line's nickname reads as the name to them - unless
            # someone else answers to it too.
            if not (taken.get(nick.split()[0].casefold(), set()) - {name.casefold(), f"\x00{nick}"}) \
                    and sum(n == nick for n in nicknamed.values()) == 1:
                self._named.append((re.compile(
                    (rf"(?:\b{re.escape(words[0])}\s+)?" if len(words) > 1 else "")
                    + rf"[{_OPEN_QUOTE}]?(?<!\w){re.escape(nick)}(?!\w)[{_CLOSE_QUOTE}]?(?!\w)"
                    + (rf"(?:\s+{rest}\b)?" if len(words) > 1 else "")), name))

    def __bool__(self) -> bool:
        return bool(self._full or self._first)

    def _firsts(self, pattern: re.Pattern, word: bool, text: str):
        return (m for m in pattern.finditer(text) if not (word and _sentence_start(text, m.start())))

    def spoken(self, text: str) -> str:
        held: list[str] = []

        def hold(s: str) -> str:
            held.append(s)
            return f"\x00{len(held) - 1}\x00"

        for pattern, nick, _ in self._full:
            text = pattern.sub(lambda m: hold(f'{m[1]} "{nick}" {m[2]}'), text)
        for protect in (self._others, self._nicks):
            if protect:
                text = protect.sub(lambda m: hold(m[0]), text)
        for pattern, nick, word, _ in self._first:
            for m in reversed(list(self._firsts(pattern, word, text))):
                text = text[:m.start()] + hold(nick) + text[m.end():]
        return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m[1])], text)

    def slip(self, line: str) -> tuple[str, str] | None:
        """(the first name said, the nickname) when a line calls a nicknamed player by first name."""
        def mask(m: re.Match) -> str:
            return "\x00" * len(m[0])

        for protect in (*self._intros, self._others, self._nicks):
            if protect:
                line = protect.sub(mask, line)
        for pattern, nick, word, name in self._first:
            # Another nicknamed player's full name is his, whatever it shares with this one's first name.
            theirs = line
            for full, _, whose in self._full:
                if whose != name:
                    theirs = full.sub(mask, theirs)
            if m := next(self._firsts(pattern, word, theirs), None):
                return m[0], nick
        return None

    def named(self, line: str) -> str:
        """The line with each nickname as the player's name, for checks that know players by name."""
        for pattern, name in self._named:
            line = pattern.sub(name, line)
        return line


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
