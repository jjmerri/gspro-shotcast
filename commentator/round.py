"""A league round: who's in it, the format, handicaps, hole scores and the numbers behind the story.

Everything is computed here so the booth never has to count: the model is told "hit 3 of 6
fairways", never handed a list of shots to tally.
"""

import difflib
import itertools
import logging
import re
from dataclasses import dataclass, field

from . import handicap
from .facts import to_yards
from .hud import Scorecard
from .league import League
from .store import COUNTS_AS_ROUND_HOLES

log = logging.getLogger(__name__)

FORMATS = {
    "scramble": "Scramble: both players hit, the team picks the better ball and both play the next shot from "
                "there. One team score per hole.",
    "best_ball": "Best ball: each player plays their own ball, and on every hole the team takes the better of "
                 "the two net scores.",
    "aggregate": "Team sum: each player plays their own ball, and the team score is both players' net scores "
                 "added together, so every stroke counts.",
    "alternate": "Alternate shot: one ball per team. The partners take turns on the tee hole by hole, then "
                 "alternate every shot until the ball is holed. GSPro shows the ball under one player's name, "
                 "so who hit any given shot isn't known.",
}
FORMAT_NAMES = {"scramble": "scramble", "best_ball": "best ball", "aggregate": "team sum", "alternate": "alternate shot"}
_ALIASES = {
    "scramble": {"scramble", "twopersonscramble", "twomanscramble"},
    "best_ball": {"bestball", "fourball", "betterball"},
    "aggregate": {"aggregate", "teamsum", "sum", "total", "combined", "teamtotal", "teamaggregate"},
    "alternate": {"alternate", "alternateshot", "altshot", "alt", "foursomes"},
}
# One ball per team: the side that owns a score is the team.
TEAM_BALL = ("scramble", "alternate")

# Allowances as fractions of each player's course handicap. One-ball formats give the team
# lowest x first + next x second. These match the Shed Sim League's Squabbit settings; the
# league file can override them under "handicap_allowances".
DEFAULT_ALLOWANCES = {"aggregate": [1.0], "best_ball": [0.75], "scramble": [0.25, 0.20], "alternate": [0.25, 0.20]}

RESULT_NAMES = {-3: "albatross", -2: "eagle", -1: "birdie", 0: "par", 1: "bogey", 2: "double bogey"}


def normalize_format(text: str | None) -> str | None:
    if not text:
        return None
    key = re.sub(r"[^a-z]", "", str(text).lower())
    return next((fmt for fmt, words in _ALIASES.items() if key in words), None)


def result_name(diff: int) -> str:
    if diff <= -3:
        return "albatross"
    return RESULT_NAMES.get(diff, "triple bogey or worse")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def to_par(n: int) -> str:
    return "even par" if n == 0 else (f"{n} over par" if n > 0 else f"{-n} under par")


@dataclass
class Side:
    """Whoever owns a score: one player, or a team playing one ball."""

    key: str
    label: str
    players: list[str]          # roster names behind it
    gspro: list[str]            # GSPro names whose shots and scorecard rows belong to it
    team: str | None = None
    strokes: int | None = None  # handicap strokes for this round, after the format's allowance
    # Per player, for best ball: strokes received on each hole of the round.
    hole_strokes: dict[int, int] = field(default_factory=dict)


@dataclass
class SideStats:
    holes_scored: int = 0
    gross: int = 0
    par: int = 0
    scores: dict[int, int] = field(default_factory=dict)  # hole of the round -> strokes
    fir: tuple[int, int] = (0, 0)  # hit, chances
    gir: tuple[int, int] = (0, 0)
    scramble: tuple[int, int] = (0, 0)  # up-and-downs made, greens missed (the chances)
    sand: tuple[int, int] = (0, 0)      # sand saves made, greenside bunker holes (the chances)
    putts: int = 0
    putt_holes: int = 0
    one_putts: int = 0
    three_putts: int = 0
    penalties: int = 0
    water_holes: list[int] = field(default_factory=list)   # holes of the round where a ball found the water
    longest_drive: float | None = None
    source: str = "scorecard"


class Round:
    _ids = itertools.count(1)

    def __init__(
        self, *, league: League, gspro_players: list[str], course: str | None, scheduled_format: str | None,
        holes: int = 9, start_hole: int | None = None, schedule: dict | None = None, is_league: bool = True,
        slot_names: list[str] | None = None,
    ) -> None:
        self.id = next(Round._ids)
        self.league = league
        self.course = course
        # GSPro's setup-screen slots in order (every named slot, league player or not), captured only
        # when the round was built from that screen. GSPro indexes players by slot, so this is the only
        # trustworthy source of a name -> GSPro index map; a tee-time roster is in honour order instead.
        self.slot_names = slot_names
        # A real league match (scheduled course, in date, correct team and player count) versus a
        # practice round the booth still calls, minus the league framing (points, standings, handicap).
        self.is_league = is_league
        self.holes = holes
        # Rounds start on 1 or 10, and a lone "1" in the hole box rarely OCRs, so an unread start is
        # a placeholder until the first scorecard shows which hole got the first score.
        self.start_hole = start_hole or 1
        self.start_hole_assumed = start_hole is None
        # False while the count is only a default (GSPro's setup HOLES tab unread, no schedule): the
        # opener mustn't announce it.
        self.holes_confirmed = True
        self.schedule = schedule or {}
        self.seq = 1  # which hole of the round is being played
        self.par: dict[int, int] = {}
        self.index: dict[int, int] = {}
        # The course's static card (par, stroke index per hole), read from GSPro's files; authoritative.
        self.course_card = None
        self.card_scores: dict[str, dict[int, int]] = {}  # GSPro name -> hole of the round -> strokes
        # The same, from GSPro's log: its own count of each finished hole, which beats a screen read (a
        # scorecard cell OCR can miss). The on-screen card fills any hole the log didn't settle.
        self.log_scores: dict[str, dict[int, int]] = {}
        self.scorecard_seen = False
        self.ended = False
        # GSPro's log said every hole in the round is holed out ("Round Is Complete"). With the log
        # followed, this (not the guessed hole count) is what says the round is over.
        self.log_complete = False
        # A round quit early is scored only through its last finished hole (the one in progress, with no
        # card score yet, would otherwise be guessed from its last putt).
        self.stats_through: int | None = None
        # (player, hole of the round) the player picked up on: GSPro capped the score at its maximum,
        # so the strokes on the card past the last one played are the cap, not penalties or putts.
        self.picked_up: set[tuple[str, int]] = set()
        # The setup screen showed a shamble or alternate shot: GSPro copies balls between teammates in
        # those too, so its log mustn't turn the round into a scramble.
        self.no_scramble = False
        self.ctp_holes = [int(h) for h in self.schedule.get("ctp_holes") or []]
        # Squabbit's card for this league week (`squabbit --cards`): its stroke index, tee rating and
        # slope, and the format's handicap settings are what the league's net scores use.
        self.week_card = league.week_card(self.schedule.get("week")) if is_league else None
        # Hole of the round -> who -> how far their tee shot finished from the hole.
        self.ctp_results: dict[int, dict[str, str]] = {}
        # The season store's last shot id when the round started: the week's earlier CTP tee shots
        # are the ones at or below it (this round's come from ctp_results instead).
        self.shot_floor: int | None = None

        self.roster = {g: league.match_player(g) for g in gspro_players}
        teams: dict[str, list[str]] = {}
        for g, player in self.roster.items():
            team = league.player_team.get(player) if player else league.match_team(g)
            teams.setdefault(team or g, []).append(g)
        self.teams = teams
        self.format, self.format_warning = self._resolve_format(normalize_format(scheduled_format))
        self.sides = self._build_sides()
        if self.week_card:
            self._allocate_hole_strokes()

    # ---- setup ----------------------------------------------------------------------------

    def _resolve_format(self, scheduled: str | None) -> tuple[str | None, str | None]:
        """One GSPro player per two-person team means one ball per team; the schedule says which kind."""
        # A practice round isn't a league team format: take only an explicit format, never infer one
        # from the ball count. A lone league player practising is not playing alternate shot.
        if not self.is_league:
            return scheduled, None
        one_ball = [t for t, gs in self.teams.items() if len(gs) == 1 and len(self.league.team_players.get(t, [])) >= 2]
        if self.teams and len(one_ball) == len(self.teams):
            fmt = scheduled if scheduled in TEAM_BALL else "alternate"
            warning = (f"schedule says {FORMAT_NAMES[scheduled]} but each team has one ball in GSPro; "
                       "treating it as alternate shot") if scheduled and scheduled not in TEAM_BALL else None
            return fmt, warning
        if scheduled == "alternate":
            return None, "schedule says alternate shot but both partners are in GSPro; format not used"
        return scheduled, None

    def play_scramble(self, groups: list[list[str]]) -> bool:
        """GSPro's log showed a scramble (it copies the picked ball onto teammates). That's the format
        whatever the schedule said: the league scores its own format in an app and leaves GSPro on
        stroke play, so a scramble in GSPro is deliberate. `groups` are the GSPro names the log tied
        together as teammates (or the setup screen's two players); a league round keeps its league
        teams. True if anything changed."""
        teams = None
        if not self.is_league and groups:
            teams = {" & ".join(g): list(g) for g in groups}
            for g in self.roster:
                if not any(g in grp for grp in groups):
                    teams[g] = [g]
        if self.format == "scramble" and (teams is None or teams == self.teams):
            return False
        if teams:
            self.teams = teams
        self.format, self.format_warning = "scramble", None
        self.sides = self._build_sides()
        if self.course_card:
            self.apply_course_card(self.course_card)
        elif self.week_card:
            self._allocate_hole_strokes()
        return True

    def _build_sides(self) -> list[Side]:
        sides = []
        if self.format in TEAM_BALL:
            for team, gspro in self.teams.items():
                players = self.league.team_players.get(team) or [self.roster.get(g) or g for g in gspro]
                sides.append(Side(key=team, label=team, players=players, gspro=gspro, team=team))
        else:
            for g, player in self.roster.items():
                name = player or g
                team = self.league.player_team.get(player) if player else None
                sides.append(Side(key=name, label=name, players=[name], gspro=[g], team=team))
        self._assign_strokes(sides)
        return sides

    def _allowances(self) -> list[float]:
        custom = (self.league.data.get("handicap_allowances") or {})
        for key, value in custom.items():
            if normalize_format(key) == self.format and isinstance(value, list):
                return [float(v) for v in value]
        return DEFAULT_ALLOWANCES.get(self.format or "", [1.0])

    def _assign_strokes(self, sides: list[Side]) -> None:
        """Handicap strokes the way Squabbit gives them (see handicap.py). With the week's Squabbit
        card: its settings (a USGA course handicap from the tee's slope and rating where the format
        uses one, the max index) and its allowances. Without it: the index scaled to the holes played
        (an 18-hole index over 9 holes is half), then the format's allowance.
        Practice rounds get no strokes - they're gross-only, and the opener says as much, so leaving
        them None keeps the "receiving N handicap strokes" line from contradicting that."""
        if not self.is_league:
            return
        card = self.week_card
        if card:
            card = {**card, "handicap": {**card["handicap"],
                                         "allowances": card["handicap"].get("allowances") or self._allowances()}}
        holes = [self.start_hole + seq - 1 for seq in range(1, self.holes + 1)]
        if card and not all(str(h) in card["par"] for h in holes):
            card = None
        allow = self._allowances()
        for side in sides:
            indexes = [self.league.handicap(p) for p in side.players]
            if not indexes or any(i is None for i in indexes):
                continue
            team_ball = self.format in TEAM_BALL
            if card:
                side.strokes = handicap.strokes_for(card, indexes if team_ball else indexes[:1], holes, team_ball)
            else:
                course = [handicap.course_handicap(i, self.holes) for i in indexes]
                side.strokes = handicap.side_strokes(course, allow, team_ball)

    def _apply_week_card(self) -> None:
        """Squabbit's par and stroke index for the week's tee win over GSPro's card: Squabbit scores
        the league, and a GSPro card's index can differ."""
        for seq in range(1, self.holes + 1):
            hole = str(self.start_hole + seq - 1)
            if self.week_card["par"].get(hole):
                self.par[seq] = int(self.week_card["par"][hole])
            if self.week_card["index"].get(hole):
                self.index[seq] = int(self.week_card["index"][hole])

    def anonymous_players(self) -> list[str]:
        """Players who share a ball nobody can attribute: their names mustn't go on a shot."""
        if self.format not in TEAM_BALL:
            return []
        return [p for side in self.sides if len(side.gspro) == 1 and len(side.players) > 1 for p in side.players]

    def side_for(self, player: str | None) -> Side | None:
        for side in self.sides:
            if player and (player in side.players or player in side.gspro or player == side.key):
                return side
        return None

    def roster_position(self, name: str | None) -> int | None:
        """This name's 0-based GSPro slot index (GSPro names its players by setup slot). Uses the
        captured slot order when present so an unresolved or guest slot doesn't shift later players'
        indices; falls back to roster insertion order otherwise. None when the name isn't found. (The
        caller excludes alternate shot, and only trusts this in multiplayer when slot_names is set.)"""
        if not name:
            return None
        order = self.slot_names if self.slot_names else list(self.roster)
        for i, g in enumerate(order):
            if g and (name == g or name == self.league.match_player(g)):   # g may be None (empty slot)
                return i
        return None

    def hole_number(self, seq: int) -> int | None:
        """The course's hole number, once the starting hole is known (front or back nine)."""
        return None if self.start_hole_assumed else self.start_hole + seq - 1

    def apply_course_card(self, card) -> None:
        """Seed par and stroke index per round-hole from the course's static card. Uses the
        current start hole (the assumed one until the scorecard confirms it), so it's re-applied
        if the start hole is later corrected."""
        self.course_card = card
        for seq in range(1, self.holes + 1):
            hole = self.start_hole + seq - 1
            if hole in card.par:
                self.par[seq] = card.par[hole]
            if hole in card.index:
                self.index[seq] = card.index[hole]
        self._trim_to_holes()
        self._allocate_hole_strokes()

    def _trim_to_holes(self) -> None:
        # A card first applied for 18 holes from hole 1, then cut to a 3-hole round from 16, left
        # seqs 4-18 behind, which were stored as par for "holes 19-33".
        for seq in [q for q in self.par if not 1 <= q <= self.holes]:
            del self.par[seq]
        for seq in [q for q in self.index if not 1 <= q <= self.holes]:
            del self.index[seq]

    def set_holes(self, holes: int, confirmed: bool | None = None) -> None:
        """Correct the round's length (the configured count is a guess until GSPro says otherwise).
        `confirmed` marks the new count as read rather than guessed (None keeps the current mark)."""
        self.holes = holes
        if confirmed is not None:
            self.holes_confirmed = confirmed
        self._trim_to_holes()
        if self.course_card:
            self.apply_course_card(self.course_card)
        elif self.week_card:
            self._allocate_hole_strokes()

    def holes_phrase(self, seqs: list[int]) -> str:
        """"hole 12", "holes 11, 14 and 15", or by position when the starting hole isn't known."""
        if self.start_hole_assumed:
            return _join([self.hole_name(s) for s in seqs])
        numbers = [str(self.hole_number(s)) for s in seqs]
        return ("hole " if len(numbers) == 1 else "holes ") + _join(numbers)

    def hole_name(self, seq: int) -> str:
        number = self.hole_number(seq)
        return f"hole {number}" if number else f"the {_ordinal(seq)} hole of the round"

    # ---- the scorecard --------------------------------------------------------------------

    def apply_scorecard(self, card: Scorecard) -> bool:
        """Takes the scorecard's numbers as the truth. Returns whether it held any of our players."""
        rows = {}
        names = list(card.scores)
        for g, player in self.roster.items():
            wanted = {_norm(g)} | {_norm(a) for a in (self.league.names_for(player) if player else [])}
            match = next((n for n in names if _norm(n) in wanted), None)
            if match is None:
                close = difflib.get_close_matches(_norm(g), [_norm(n) for n in names], n=1, cutoff=0.8)
                match = next((n for n in names if close and _norm(n) == close[0]), None)
            if match:
                rows[g] = card.scores[match]
        if not rows:
            return False
        played = sorted({h for row in rows.values() for h in row})
        if self.start_hole_assumed and played:
            self.start_hole, self.start_hole_assumed = played[0], False
            log.info("Round started on hole %s (from the scorecard)", self.start_hole)
            if self.course_card:  # re-map the static card to the now-known holes
                self.apply_course_card(self.course_card)
            elif self.week_card:
                self._allocate_hole_strokes()
        for g, row in rows.items():
            for hole, strokes in row.items():
                seq = hole - self.start_hole + 1
                if 1 <= seq <= self.holes:
                    self.card_scores.setdefault(g, {})[seq] = strokes
                    logged = self.log_scores.get(g, {}).get(seq)
                    if logged is not None and logged != strokes:
                        log.warning("Round %s: the scorecard read %s on hole %s for %s, GSPro's log says %s; "
                                    "keeping the log's", self.id, strokes, hole, g, logged)
        # The static course card is authoritative; the on-screen card only fills gaps.
        for seq in range(1, self.holes + 1):
            hole = self.hole_number(seq)
            if hole in card.par:
                self.par.setdefault(seq, card.par[hole])
            if hole in card.index:
                self.index.setdefault(seq, card.index[hole])
        self.scorecard_seen = True
        self._allocate_hole_strokes()
        return True

    def _allocate_hole_strokes(self) -> None:
        """Strokes go to the hardest holes of the round first (lowest stroke index); a plus handicap
        gives them back from the easiest. With a Squabbit week card, its par and index (and the strokes,
        which can depend on the par of the holes played) are re-applied first, for the current start hole."""
        if self.week_card:
            self._apply_week_card()
            self._assign_strokes(self.sides)
        order = sorted(range(1, self.holes + 1), key=lambda s: (self.index.get(s, 99), s))
        for side in self.sides:
            if side.strokes is None:
                continue
            side.hole_strokes = handicap.allocate(side.strokes, order)

    def set_log_scores(self, gspro: str, scores: dict[int, int]) -> None:
        """The holes GSPro's log has this player finished, by hole of the round: the whole set, so a hole
        reopened (a holed putt taken back as a mulligan) drops out again."""
        scores = {seq: v for seq, v in scores.items() if 1 <= seq <= self.holes}
        had = self.log_scores.get(gspro, {})
        if scores == had:
            return
        for seq in sorted(set(had) | set(scores)):
            if seq not in scores:   # a card read of it is stale too, the partner's row in a shared ball
                side = self.side_for(gspro) if self.format in TEAM_BALL else None
                for g in (side.gspro if side else [gspro]):
                    self.card_scores.get(g, {}).pop(seq, None)
            if had.get(seq) != scores.get(seq):
                log.info("Round %s: %s %s on %s (GSPro's log)", self.id, gspro,
                         f"scored {scores[seq]}" if seq in scores else "reopened", self.hole_name(seq))
        self.log_scores[gspro] = scores

    def score_of(self, gspro: str, seq: int) -> int | None:
        """One GSPro player's score on a hole of the round: GSPro's log, else the on-screen card."""
        logged = self.log_scores.get(gspro, {}).get(seq)
        return logged if logged is not None else self.card_scores.get(gspro, {}).get(seq)

    def _score_source(self) -> str:
        return "log" if self.log_scores else "scorecard" if self.scorecard_seen else "shots"

    def card_score(self, side: Side, seq: int) -> int | None:
        # The log first across the whole side: in a scramble only the partner whose line followed the
        # ball dropping has a logged score, and the other's card read mustn't undercut it.
        for source in (self.log_scores, self.card_scores):
            values = [source[g][seq] for g in side.gspro if seq in source.get(g, {})]
            if values:
                return min(values) if self.format in TEAM_BALL else values[0]
        return None

    def was_picked_up(self, names: list[str], seq: int) -> bool:
        return any((n, seq) in self.picked_up for n in names)

    def complete(self) -> bool:
        return all(self.card_score(side, seq) is not None for side in self.sides for seq in range(1, self.holes + 1))

    # ---- stats ----------------------------------------------------------------------------

    def side_stats(self, side: Side, records: list, final_override: int | None = None) -> SideStats:
        mine = [r for r in records if r.round_id == self.id and not r.mulligan
                and (r.player in side.players or r.player in side.gspro)]
        stats = SideStats(source=self._score_source())
        fir, fir_tries, gir, gir_tries = 0, 0, 0, 0
        scr, scr_tries, sand, sand_tries = 0, 0, 0, 0
        for seq in range(1, (self.stats_through or self.holes) + 1):
            recs = sorted((r for r in mine if r.hole_seq == seq), key=lambda r: r.shot.timestamp or 0)
            par = self.par.get(seq) or next((r.par for r in recs if r.par), None)
            numbers = sorted({r.shot_number for r in recs if r.shot_number})
            green = [r for r in recs if r.lie_before == "green"]
            score = self.card_score(side, seq)
            picked = self.was_picked_up([*side.gspro, *side.players], seq)
            if score is None:
                score = _score_from_shots(recs, numbers, green)
            # The closer is pre-built before the final putt drops, branched by the score this side
            # might finish the last hole on; `final_override` supplies that hypothetical last-hole
            # score so the branch's whole-round totals are right for it.
            if final_override is not None and seq == self.holes:
                score = final_override
            if score is not None:
                stats.scores[seq] = score
                stats.gross += score
                stats.holes_scored += 1
                if par:
                    stats.par += par
            # Penalties and the longest drive come from full shots already on the card, so count them
            # even for the overridden last hole (the closer is pre-built with the ball on the green and
            # those shots already struck); otherwise a pre-built closer under-reports both.
            if recs:
                stats.penalties += sum(b - a - 1 for a, b in zip(numbers, numbers[1:]) if b - a > 1)
                if any("in_water" in r.tags for r in recs):
                    stats.water_holes.append(seq)
                for r in recs:
                    if (r.shot.club or "").upper() == "DR" and not r.shot.is_putt and (
                            r.shot_number == 1 or (r.lie_before or "").lower() == "tee"
                            or (r.shot_number is None and not r.lie_before)):
                        drive = _drive_yards(r)
                        if drive and (stats.longest_drive is None or drive > stats.longest_drive):
                            stats.longest_drive = drive
            if not recs or not par:
                continue
            if final_override is not None and seq == self.holes:
                # The closer is pre-built with the ball already on the last green, so that hole's tee
                # shot and green are known; only the putts are still to come, and the branch's score says
                # how many. Leaving the hole out made a closer call one fairway "all night" when the
                # last drive was the second.
                tee = [r for r in recs if r.shot_number == 1 and r.hud.lie]
                if par >= 4 and tee:
                    fir_tries += 1
                    fir += any(r.hud.lie == "fairway" for r in tee)
                if (reached := _green_reached_at(recs)) is not None and score is not None:
                    hole_gir = reached <= par - 1
                    gir_tries, gir = gir_tries + 1, gir + hole_gir
                    if not hole_gir:
                        scr_tries, scr = scr_tries + 1, scr + (score <= par)
                    if (putts := score - reached + 1) > 0 and self.format != "scramble":
                        stats.putts += putts
                        stats.putt_holes += 1
                        stats.one_putts += putts == 1
                        stats.three_putts += putts >= 3
                if self.format != "scramble" and (saved := _sand_save(recs, score)) is not None:
                    sand_tries, sand = sand_tries + 1, sand + saved
                continue
            tee = [r for r in recs if r.shot_number == 1 and r.hud.lie]
            if par >= 4 and tee:
                fir_tries += 1
                fir += any(r.hud.lie == "fairway" for r in tee)
            # GIR. The authoritative "settled on the green" signal is a shot PLAYED from the green (a
            # putt; lie_before == green), not a shot's own landing lie (that reads the touchdown spot).
            first_green = min((r.shot_number for r in green if r.shot_number), default=None)
            hole_gir = None   # unknown when the reads can't place it
            if first_green:
                hole_gir = first_green <= par - 1
                gir_tries += 1
                gir += hole_gir
            elif green:
                pass  # a putt was played but its shot number didn't OCR; can't place the GIR, so skip it
            elif score is not None and score <= par - 2 and all(r.lie_before for r in recs):
                hole_gir = True
                gir_tries, gir = gir_tries + 1, gir + 1  # holed from off the green
            elif score is not None and all(r.lie_before for r in recs):
                # No putt was played, so the putt signal can't see it. A conceded STIFF (an approach
                # that settled on the green in regulation, then the short putt conceded) is still a GIR,
                # so check the regulation shot's settled finish lie: a non-putt that stopped on the
                # green by the regulation stroke (<= par - 2) hit it; otherwise it's a miss (up-and-down
                # from off the green, or the green reached late).
                reached = any((r.hud.lie or "").lower() == "green" for r in recs
                              if not r.shot.is_putt and r.shot_number and r.shot_number <= par - 2)
                hole_gir = reached
                gir_tries += 1
                gir += reached
            # Scrambling as the PGA Tour counts it: every green missed in regulation is a chance, saved
            # by par or better, whatever the next stroke was (a putt from the fringe included). When the
            # reads couldn't place the GIR, a non-putt played from off the green at or past the
            # regulation stroke (par - 1) still says the green was missed.
            recoveries = [r for r in recs if not r.shot.is_putt and r.lie_before not in (None, "green")
                          and r.shot_number and r.shot_number >= par - 1]
            if score is not None and (hole_gir is False or (hole_gir is None and recoveries)):
                scr_tries += 1
                scr += score <= par
            # Sand saves (skipped for scramble, where the pooled two balls can't say which the team played).
            if self.format != "scramble" and (saved := _sand_save(recs, score)) is not None:
                sand_tries += 1
                sand += saved
            # Putts as the PGA Tour counts them: every stroke played from the green, whatever the club
            # (a wedge chipped off the green is a putt), and none from off it (a putt from the fringe
            # isn't). A putter stroke whose lie wasn't read is given the benefit of the doubt. Strokes the
            # card has past the last one recorded are the conceded tap-in, counted once the ball was on
            # the green (a putt was struck, or the last shot finished there).
            putt_recs = [r for r in recs if r.lie_before == "green" or (r.shot.is_putt and r.lie_before is None)]
            last = max((r for r in recs if r.shot_number), key=lambda r: r.shot_number, default=None)
            on_green = bool(putt_recs) or any(r.shot.is_putt for r in recs) or (last is not None and (last.hud.lie or "").lower() == "green")
            conceded = score - numbers[-1] if score is not None and numbers and score > numbers[-1] \
                and not picked else 0
            putts = len(putt_recs) + (conceded if on_green else 0)
            # Holed from off the green (a chip-in, an ace): the card ends on a tracked full shot.
            holed_off = not putt_recs and bool(numbers) and score == numbers[-1] and all(r.lie_before for r in recs)
            if (putts or holed_off) and self.format != "scramble":
                stats.putts += putts
                stats.putt_holes += 1
                stats.one_putts += putts == 1
                stats.three_putts += putts >= 3
        stats.fir, stats.gir = (fir, fir_tries), (gir, gir_tries)
        stats.scramble, stats.sand = (scr, scr_tries), (sand, sand_tries)
        return stats

    def player_stats(self, player: str, records: list) -> SideStats:
        """Per-individual-player version of side_stats: FIR/GIR/scrambling/sand saves from one player's
        OWN shots, for per-player season stats. The hole score comes from the player's own scorecard row
        (card_scores is keyed by GSPro name) - the authoritative source side_stats uses, which counts
        conceded tap-ins and holes that ended with no putt that the shot records alone miss; it falls
        back to the last stroke seen only when there is no card row. GIR, scrambling and sand are read
        from one's own ball but are only meaningful in an individual-ball format (in a scramble the
        partners share the selected ball, so the green/score aren't a single player's), so the caller
        gates all three to those formats; FIR (one's own tee shot) is genuine per-player in any format.
        The hole loop mirrors side_stats."""
        mine = [r for r in records if r.round_id == self.id and not r.mulligan and r.player == player]
        stats = SideStats(source=self._score_source())
        # card_scores is keyed by the raw GSPro name; ShotRecord.player is the ROSTER name (match_player),
        # or the raw GSPro text on a match miss. Map through self.roster {gspro: roster} to this player's
        # GSPro name(s), so the card row is actually found (g == player covers the match-miss fallback).
        gspro = [g for g, p in self.roster.items() if g == player or p == player]
        fir, fir_tries, gir, gir_tries = 0, 0, 0, 0
        scr, scr_tries, sand, sand_tries = 0, 0, 0, 0
        for seq in range(1, (self.stats_through or self.holes) + 1):
            recs = sorted((r for r in mine if r.hole_seq == seq), key=lambda r: r.shot.timestamp or 0)
            par = self.par.get(seq) or next((r.par for r in recs if r.par), None)
            numbers = sorted({r.shot_number for r in recs if r.shot_number})
            green = [r for r in recs if r.lie_before == "green"]
            score = next((v for g in gspro if (v := self.score_of(g, seq)) is not None), None)
            if self.format in TEAM_BALL and (side := self.side_for(player)):
                score = self.card_score(side, seq)   # a shared ball: everyone's score is the team's
            picked = self.was_picked_up([*gspro, player], seq)
            if score is None:
                score = _score_from_shots(recs, numbers, green)
            if score is not None:
                stats.scores[seq] = score
                stats.gross += score
                stats.holes_scored += 1
                if par:
                    stats.par += par
            if not recs or not par:
                continue
            tee = [r for r in recs if r.shot_number == 1 and r.hud.lie]
            if par >= 4 and tee:
                fir_tries += 1
                fir += any(r.hud.lie == "fairway" for r in tee)
            first_green = min((r.shot_number for r in green if r.shot_number), default=None)
            hole_gir = None   # unknown when the reads can't place it
            if first_green:
                hole_gir = first_green <= par - 1
                gir_tries += 1
                gir += hole_gir
            elif green:
                pass  # a putt was played but its shot number didn't OCR; can't place the GIR, so skip it
            elif score is not None and score <= par - 2 and all(r.lie_before for r in recs):
                hole_gir = True
                gir_tries, gir = gir_tries + 1, gir + 1  # holed from off the green
            elif score is not None and all(r.lie_before for r in recs):
                # No green-putt record because the approach finished inside the gimme and the tap-in was
                # conceded. One stroke short of the card = a single conceded putt, so the last recorded
                # shot is the one that reached the green: a GIR if it was at or before regulation.
                hole_gir = bool(numbers) and score == numbers[-1] + 1 and numbers[-1] <= par - 2
                gir_tries += 1
                gir += hole_gir
            # Scrambling as the PGA Tour counts it (see side_stats): every missed green is a chance.
            recoveries = [r for r in recs if not r.shot.is_putt and r.lie_before not in (None, "green")
                          and r.shot_number and r.shot_number >= par - 1]
            if score is not None and (hole_gir is False or (hole_gir is None and recoveries)):
                scr_tries += 1
                scr += score <= par
            # Sand saves (skipped for scramble, where the pooled two balls can't say which the team played).
            if self.format != "scramble" and (saved := _sand_save(recs, score)) is not None:
                sand_tries += 1
                sand += saved
            # Putts per hole, from this player's own green shots. Skipped in a scramble, where both
            # partners putt the one selected ball, so the count isn't a single player's (matches
            # side_stats; the hole-score season stats are gated to individual-ball formats anyway).
            # Putts as the PGA Tour counts them: every stroke played from the green, whatever the club
            # (a wedge chipped off the green is a putt), and none from off it (a putt from the fringe
            # isn't). A putter stroke whose lie wasn't read is given the benefit of the doubt. Strokes the
            # card has past the last one recorded are the conceded tap-in, counted once the ball was on
            # the green (a putt was struck, or the last shot finished there).
            putt_recs = [r for r in recs if r.lie_before == "green" or (r.shot.is_putt and r.lie_before is None)]
            last = max((r for r in recs if r.shot_number), key=lambda r: r.shot_number, default=None)
            on_green = bool(putt_recs) or any(r.shot.is_putt for r in recs) or (last is not None and (last.hud.lie or "").lower() == "green")
            conceded = score - numbers[-1] if score is not None and numbers and score > numbers[-1] \
                and not picked else 0
            putts = len(putt_recs) + (conceded if on_green else 0)
            # Holed from off the green (a chip-in, an ace): the card ends on a tracked full shot.
            holed_off = not putt_recs and bool(numbers) and score == numbers[-1] and all(r.lie_before for r in recs)
            if (putts or holed_off) and self.format != "scramble":
                stats.putts += putts
                stats.putt_holes += 1
                stats.one_putts += putts == 1
                stats.three_putts += putts >= 3
        stats.fir, stats.gir = (fir, fir_tries), (gir, gir_tries)
        stats.scramble, stats.sand = (scr, scr_tries), (sand, sand_tries)
        return stats

    # ---- facts for the booth --------------------------------------------------------------

    def describe_side(self, side: Side) -> str:
        if self.format in TEAM_BALL:
            return f"{side.label} ({' and '.join(side.players)}, one ball)"
        return side.label + (f" ({side.team})" if side.team and side.team != side.label else "")

    def format_facts(self) -> list[str]:
        if not self.format:
            return []
        facts = [f"This round's format: {FORMAT_NAMES[self.format]}. {FORMATS[self.format]}"]
        if self.anonymous_players():
            facts.append("Never credit a shot to one player by name; talk about the team, or say \"they\".")
        return facts

    def opening_facts(self, season: dict[str, dict], first_up: str | None, hole: str | None) -> list[str]:
        facts = []
        if not self.is_league:
            facts.append("This is a practice round, not a league match: no points, standings, handicap strokes "
                         "or closest-to-the-pin in this round, just the golf. Say it's practice once, in passing, "
                         "not repeatedly. League history and past results are still fair game for color.")
        if self.course:
            facts.append(f"Course: {self.course}.")
        if hole:
            facts.append(f"First hole: {hole}.")
        if self.holes_confirmed:
            facts.append(f"Holes in this round: {self.holes}.")
        else:
            facts.append("How many holes this round is isn't known yet: don't say a number of holes (no "
                         "\"eighteen holes\" or \"nine holes\").")
        if self.schedule.get("week"):
            total = self.league.data.get("total_weeks")
            week = f"League week {self.schedule['week']}" + (f" of {total}" if total else "")
            if self.schedule.get("holes"):
                week += f": {self.schedule['holes']}"
            if self.schedule.get("tees"):
                week += f", {self.schedule['tees']} tees"
            if self.schedule.get("end_date"):
                week += f", to be played by {self.schedule['end_date']}"
            facts.append(week + ".")
        if self.ctp_holes:
            facts.append("Closest-to-the-pin holes this week: " + " and ".join(str(h) for h in self.ctp_holes) + ".")
        facts += self.format_facts()
        if self.is_league:
            facts.append("This is a handicap league: a round is judged on the net score (gross minus handicap "
                         "strokes), so a big gross number can still be a good round.")
        for side in self.sides:
            line = f"Playing: {self.describe_side(side)}"
            if side.strokes is not None:
                line += f", receiving {side.strokes} handicap strokes over these {self.holes} holes"
            facts.append(line + ".")
            facts += _season_facts(side.label, season.get(side.key), self.holes, self.is_league)
        if first_up:
            facts.append(f"First on the tee: {first_up}.")
        return facts

    def closing_facts(self, records: list, season: dict[str, dict],
                      final_overrides: dict[str, int] | None = None,
                      week_ctp: dict[int, tuple[str, str, float]] | None = None) -> list[str]:
        """`week_ctp`: the week's best earlier tee shot per closest-to-the-pin hole (who, distance text,
        yards), so tonight's best can be put against it; None when the week can't be counted."""
        facts = self.format_facts()
        if self.course:
            facts.append(f"Course: {self.course}.")
        if self.stats_through and self.stats_through < self.holes:
            facts.append(f"The round was ended early, after {self.stats_through} of {self.holes} holes. "
                         f"Everything here covers only those {self.stats_through}, and there is no finished "
                         f"{self.holes}-hole score: sign off on the holes that were played and don't call it "
                         "a completed round.")
        ov = final_overrides or {}
        all_stats = {side.key: self.side_stats(side, records, ov.get(side.key)) for side in self.sides}
        if any(s.source == "shots" for s in all_stats.values()):
            facts.append("Scores were counted from the shots seen; the scorecard wasn't read.")
        for side in self.sides:
            facts += self._side_facts(side, all_stats[side.key], season.get(side.key))
        facts += self._team_facts(all_stats)
        for seq, results in sorted(self.ctp_results.items()):
            who, distance = min(results.items(), key=lambda kv: to_yards(kv[1]) or 999)
            fact = f"Closest to the pin today on {self.hole_name(seq)}: {who}, {distance} from the hole"
            mine = to_yards(distance)
            if week_ctp is None or mine is None:
                facts.append(fact + " (it's up against the rest of the league's tee shots this week).")
                continue
            prior = week_ctp.get(self.hole_number(seq))
            if prior is None:
                facts.append(fact + ". That's the closest on this hole this week so far.")
            elif mine < prior[2]:
                facts.append(fact + f". That's the closest on this hole this week so far, inside {prior[0]}'s "
                             f"{prior[1]} from an earlier round.")
            else:
                facts.append(fact + f". {prior[0]}'s {prior[1]} from an earlier round this week is still closer.")
        return facts

    def net_to_par(self, side: Side, stats: SideStats) -> int | None:
        if side.strokes is None or stats.holes_scored < self.holes or not stats.par:
            return None
        return stats.gross - side.strokes - stats.par

    def net_to_par_played(self, side: Side, stats: SideStats) -> int | None:
        """Net to par over just the holes scored (a round quit early): only the handicap strokes that
        fall on those holes count. None without strokes or a par for every scored hole."""
        if side.strokes is None or not stats.scores or (side.strokes and not side.hole_strokes):
            return None
        pars = [self.par.get(seq) for seq in stats.scores]
        if not all(pars):
            return None
        return sum(stats.scores.values()) - sum(side.hole_strokes.get(seq, 0) for seq in stats.scores) - sum(pars)

    def first_nine(self, stats: SideStats, hole_strokes: dict[int, int] | None = None) \
            -> tuple[int, int | None] | None:
        """(gross to par, net to par) over the first nine holes played, in round order. The season
        ranks rounds per nine; a round quit at eleven holes would otherwise be prorated to a score
        nobody shot (four over through eleven stored as 3.3), so an early exit contributes the nine
        it actually finished instead. None when fewer than nine holes were scored, or a par is
        missing from them. Net is None without `hole_strokes` (see `hole_strokes_for`)."""
        seqs = sorted(stats.scores)[:COUNTS_AS_ROUND_HOLES]
        if len(seqs) < COUNTS_AS_ROUND_HOLES:
            return None
        pars = [self.par.get(seq) for seq in seqs]
        if not all(pars):
            return None
        gross = sum(stats.scores[seq] for seq in seqs) - sum(pars)
        if hole_strokes is None:
            return gross, None
        return gross, gross - sum(hole_strokes.get(seq, 0) for seq in seqs)

    @staticmethod
    def hole_strokes_for(side: Side) -> dict[int, int] | None:
        """A side's per-hole handicap strokes, or None when no net score can be worked out: no
        handicap at all, or strokes owed with no per-hole allocation. Same rule as net_to_par_played."""
        if side.strokes is None or (side.strokes and not side.hole_strokes):
            return None
        return side.hole_strokes or {}

    def team_net(self, team: str) -> tuple[int, int] | None:
        """A league team's net to par so far, the way Squabbit's leaderboard scores it: (net to par,
        holes counted). Scramble and alternate: the team's ball. Best ball: the better net each hole.
        Aggregate: both nets. A format's max hole score caps a blow-up hole. Counts the holes every one
        of the team's sides has scored. None without strokes, or with a partner missing from the round
        (the leaderboard's team score needs both)."""
        if self.format is None or not self.is_league:
            return None
        sides = [s for s in self.sides if (s.team or s.key) == team]
        if not sides or any(s.strokes is None or (s.strokes and not s.hole_strokes) for s in sides):
            return None
        if self.format not in TEAM_BALL:
            members = set(self.league.team_players.get(team) or [])
            if members and {p for s in sides for p in s.players} != members:
                return None
        seqs = [q for q in range(1, self.holes + 1)
                if self.par.get(q) and all(self.card_score(s, q) is not None for s in sides)]
        if not seqs:
            return None
        total = 0
        for q in seqs:
            par = self.par[q]
            nets = [self._capped_score(s, q) - s.hole_strokes.get(q, 0) - par for s in sides]
            total += min(nets) if self.format == "best_ball" else sum(nets)
        return total, len(seqs)

    def team_gross(self, team: str) -> tuple[int, int, list[tuple[Side, int]]] | None:
        """A best-ball team's running GROSS score to par: (team to par, holes counted, each partner's own
        to par over those holes). Counts holes every partner in the round has finished, so it sits
        alongside GSPro's per-player HUD score, which is the individual's. None outside best ball or
        without at least two partners in the round."""
        if self.format != "best_ball":
            return None
        sides = [s for s in self.sides if s.team == team]
        if len(sides) < 2:
            return None
        seqs = [q for q in range(1, self.holes + 1)
                if self.par.get(q) and all(self.card_score(s, q) is not None for s in sides)]
        if not seqs:
            return 0, 0, [(s, 0) for s in sides]
        team_total = sum(min(self.card_score(s, q) for s in sides) - self.par[q] for q in seqs)
        own = [(s, sum(self.card_score(s, q) - self.par[q] for q in seqs)) for s in sides]
        return team_total, len(seqs), own

    def _capped_score(self, side: Side, seq: int) -> int:
        score = self.card_score(side, seq)
        if self.week_card:
            cap = handicap.hole_cap(self.week_card, self.par[seq], side.hole_strokes.get(seq, 0))
            if cap is not None:
                return min(score, cap)
        return score

    def _practice_form_fact(self, side: Side, s: SideStats) -> str | None:
        """A practice round has no net scoring, but a solo player's handicap still says whether they
        played well: their gross against what the index predicts for the holes played."""
        if self.is_league or len(side.players) != 1 or s.holes_scored < self.holes or not s.par:
            return None
        index = self.league.handicap(side.players[0])
        if index is None:
            return None
        expected = index * self.holes / 18
        diff = (s.gross - s.par) - expected
        verdict = ("well better than" if diff <= -2 else "a little better than" if diff < -0.5
                   else "right around" if diff <= 0.5 else "a little worse than" if diff < 2 else "well worse than")
        return (f"{side.label} plays off a {index:g} handicap index, which predicts about "
                f"{to_par(round(expected))} over {self.holes} holes; {to_par(s.gross - s.par)} is {verdict} that, "
                f"a gauge of form. There's no net score or match in this round, so don't announce one.")

    def _side_facts(self, side: Side, s: SideStats, season: dict | None) -> list[str]:
        who = side.label
        if not s.holes_scored:
            return [f"{who}: no scores recorded."]
        facts = []
        line = f"{who}: gross {s.gross} over {s.holes_scored} holes"
        if s.par:
            line += f" ({to_par(s.gross - s.par)}, par {s.par})"
        net = self.net_to_par(side, s)
        if net is not None:
            line += (f"; with {side.strokes} handicap strokes that's a net {s.gross - side.strokes}, "
                     f"net {to_par(net)}")
        facts.append(line + ".")
        if net is None and (form := self._practice_form_fact(side, s)):
            facts.append(form)
        elif net is None:
            # A league player may have no handicap yet: judge against par rather than announcing a
            # missing handicap.
            facts.append(f"{who}'s score is gross only: judge it against par, and don't mention handicaps "
                         f"or net scores.")
        if net is not None:
            verdict = ("better than their handicap" if net < 0 else "right on their handicap" if net == 0
                       else "worse than their handicap")
            facts.append(f"Against the handicap, {who} played {verdict}.")
        counts: dict[str, list[int]] = {}
        for seq, strokes in s.scores.items():
            if par := self.par.get(seq):
                counts.setdefault(result_name(strokes - par), []).append(seq)
        if counts:
            order = ["albatross", "eagle", "birdie", "par", "bogey", "double bogey", "triple bogey or worse"]
            parts = []
            for name in order:
                if name in counts:
                    holes = counts[name]
                    label = name if len(holes) == 1 else name.replace("bogey", "bogeys").replace("birdie", "birdies") \
                        .replace("eagle", "eagles").replace("par", "pars")
                    where = None if name == "par" else self.holes_phrase(holes)
                    parts.append(f"{len(holes)} {label}" + (f" ({where})" if where else ""))
            facts.append(f"{possessive(who)} card: " + ", ".join(parts) + ".")
        hit, tries = s.fir
        if tries:
            facts.append(f"{who} hit {hit} of {tries} fairways off the tee on the par 4s and 5s.")
        hit, tries = s.gir
        if tries:
            facts.append(f"{who} hit {hit} of {tries} greens in regulation.")
        hit, tries = s.scramble
        if tries:
            facts.append(f"{who} got up and down {hit} of {tries} times after missing the green.")
        hit, tries = s.sand
        if tries:
            facts.append(f"{who} got up and down from a greenside bunker {hit} of {tries} times.")
        if s.putt_holes >= max(3, self.holes // 2):
            line = f"{who} took {s.putts} putts over {s.putt_holes} holes"
            extras = [f"{s.three_putts} three-putts" if s.three_putts > 1 else "one three-putt" if s.three_putts else "no three-putts"]
            if s.one_putts:
                extras.append(f"{s.one_putts} one-putts" if s.one_putts > 1 else "one one-putt")
            facts.append(line + ", with " + " and ".join(extras) + ".")
        if s.penalties:
            facts.append(f"{who} took {s.penalties} penalty stroke{'s' if s.penalties > 1 else ''}.")
        if s.water_holes:
            facts.append(f"{who} found the water on {self.holes_phrase(s.water_holes)}.")
        picked = sorted(seq for seq in s.scores if self.was_picked_up([*side.gspro, *side.players], seq))
        if picked:
            facts.append(f"{who} picked up on {self.holes_phrase(picked)}, scored at the maximum the game allows.")
        if s.longest_drive:
            facts.append(f"{possessive(who)} longest drive went {s.longest_drive:.0f} yards.")
        scale = self.holes / 9   # season numbers are per nine holes; put them on this round's length
        if season and season.get("net_rounds") and net is not None and self.is_league:
            avg, best = season.get("avg_net_to_par"), season.get("best_net_to_par")
            if best is not None and net < best * scale:
                facts.append(f"That's {possessive(who)} best net league round of the season so far.")
            elif avg is not None:
                facts.append(f"{possessive(who)} average in {season['net_rounds']} earlier league rounds"
                             f"{_scaled_note(self.holes)}: net {to_par(round(avg * scale))}.")
        elif (season and season.get("gross_rounds") and not self.is_league and s.par
              and s.holes_scored >= self.holes and all(seq in self.par for seq in s.scores)):
            gross, avg, best = s.gross - s.par, season.get("avg_gross_to_par"), season.get("best_gross_to_par")
            if best is not None and gross < best * scale:
                facts.append(f"That's {possessive(who)} best practice round of the season so far, against par.")
            elif avg is not None:
                facts.append(f"{possessive(who)} average in {season['gross_rounds']} earlier practice rounds"
                             f"{_scaled_note(self.holes)}: {to_par(round(avg * scale))}.")
        return facts

    def _team_facts(self, all_stats: dict[str, SideStats]) -> list[str]:
        if self.format in TEAM_BALL or self.format is None:
            return []
        facts = []
        by_team: dict[str, list[Side]] = {}
        for side in self.sides:
            if side.team:
                by_team.setdefault(side.team, []).append(side)
        for team, sides in by_team.items():
            if len(sides) < 2 or any(s.strokes is None for s in sides):
                continue
            stats = [all_stats[s.key] for s in sides]
            if any(st.holes_scored < self.holes for st in stats):
                continue
            par = stats[0].par
            # Squabbit's count (with any max hole score) when the scorecard has every hole.
            tn = self.team_net(team)
            official = tn[0] if tn and tn[1] == self.holes else None
            if self.format == "aggregate":
                net = 2 * par + official if official is not None else sum(st.gross - s.strokes for s, st in zip(sides, stats))
                facts.append(f"Team {team} (team sum): combined net {net}, net {to_par(net - 2 * par)} "
                             f"against a combined par of {2 * par}.")
            elif self.format == "best_ball":
                if official is not None:
                    net = par + official
                else:
                    net = 0
                    for seq in range(1, self.holes + 1):
                        net += min(st.scores[seq] - s.hole_strokes.get(seq, 0) for s, st in zip(sides, stats))
                facts.append(f"Team {team} (best ball): net {net}, net {to_par(net - par)}.")
        return facts


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def possessive(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _scaled_note(holes: int) -> str:
    return "" if holes == 9 else f" (scaled to {holes} holes)"


def _best_note(best: float, label: str = "") -> str:
    """A season best is one real round, not a rate: it is never scaled to this round's length, or a
    five-under nine gets announced as "ten under". Always stated on the per-nine basis it is stored on."""
    return f"best{label}: {to_par(round(best))} over nine holes"


def _season_facts(who: str, season: dict | None, holes: int = 9, league: bool = True) -> list[str]:
    """The opener's season line for a side, from season_rounds sliced to this round's kind: league
    rounds (judged net) for a league round, practice rounds (judged gross) for a practice one."""
    kind = "league" if league else "practice"
    if not season or not season.get("rounds"):
        return [f"{who} has no earlier {kind} rounds recorded this season."]
    facts = [f"{possessive(who)} earlier {kind} rounds this season: {season['rounds']}."]
    scale = holes / 9   # season numbers are per nine holes
    if league and season.get("avg_net_to_par") is not None:
        n = season.get("net_rounds") or season["rounds"]
        facts.append(f"{possessive(who)} average net score in {'them' if n == season['rounds'] else f'the {n} with a handicap'}"
                     f"{_scaled_note(holes)}: {to_par(round(season['avg_net_to_par'] * scale))}; "
                     f"{_best_note(season['best_net_to_par'], ' net')}.")
    elif not league and season.get("avg_gross_to_par") is not None:
        n = season.get("gross_rounds") or season["rounds"]
        facts.append(f"{possessive(who)} average gross score to par in {'them' if n == season['rounds'] else f'the {n} with every par known'}"
                     f"{_scaled_note(holes)}: "
                     f"{to_par(round(season['avg_gross_to_par'] * scale))}; "
                     f"{_best_note(season['best_gross_to_par'])}.")
    return facts


def _drive_yards(rec) -> float | None:
    """How far a drive went: GSPro's total (carry and roll) where its tile was read, else the carry."""
    value = re.search(r"\d+(?:\.\d+)?", rec.hud.tiles.get("total length", "") if rec.hud else "")
    if value:
        metric = (rec.hud.to_pin or "").endswith("metres")
        return float(value.group(0)) * (1.0936 if metric else 1)
    return _carry_yards(rec)


def _score_from_shots(recs: list, numbers: list, green: list) -> int | None:
    """A hole's score with no card or log number for it, from the strokes seen. The last one's outcome
    (from GSPro's log) settles it: holed, or a conceded tap-in after it, which GSPro counts but nobody
    hits. Without one, the last putt seen is taken as holed."""
    if not numbers:
        return None
    last = max((r for r in recs if r.shot_number), key=lambda r: r.shot_number)
    outcome = getattr(last, "outcome", None)
    if outcome == "conceded":
        return numbers[-1] + 1
    if outcome == "holed" or green:
        return numbers[-1]
    return None


def _green_reached_at(recs: list) -> int | None:
    """The stroke first played from the green (a full shot that finished there makes the next one), or
    None if the ball isn't known to have reached it."""
    on = [r.shot_number for r in recs if r.shot_number and r.lie_before == "green"]
    on += [r.shot_number + 1 for r in recs if r.shot_number and not r.shot.is_putt
           and r.lie_before != "green" and (r.hud.lie or "").lower() == "green"]
    return min(on, default=None)


def _sand_save(recs: list, score: int | None) -> bool | None:
    """A sand save as the PGA Tour counts it: once in a greenside bunker, up and down - holed in two
    strokes or fewer counting the bunker shot, whatever the score. Judged from the hole's first
    greenside bunker shot (one left in the bunker fails). None when there was no chance (or no score)."""
    if score is None:
        return None
    first = min((r.shot_number for r in recs if not r.shot.is_putt and r.shot_number
                 and _greenside_sand(r, recs)), default=None)
    return None if first is None else score <= first + 1


def _greenside_sand(rec, recs: list) -> bool:
    """A shot from a greenside bunker, not a fairway one: within about 40 yards of the pin (a 30-yard
    cut-off missed real greenside splashes), or, with the distance unread, the next shot on the hole
    was a putt (the ball got out onto the green)."""
    if rec.lie_before not in ("sand", "bunker"):
        return False
    if (tp := to_yards(rec.hud.to_pin)) is not None:
        return tp <= 40
    nxt = min((r for r in recs if r.shot_number and rec.shot_number and r.shot_number > rec.shot_number),
              key=lambda r: r.shot_number, default=None)
    return nxt is not None and (nxt.shot.is_putt or nxt.lie_before == "green")


def _carry_yards(rec) -> float | None:
    value = re.search(r"\d+(?:\.\d+)?", rec.hud.tiles.get("carry (game)", "") if rec.hud else "")
    metric = (rec.hud.to_pin or "").endswith("metres") if rec.hud else False
    if value:
        return float(value.group(0)) * (1.0936 if metric else 1)
    if rec.shot.carry:
        return rec.shot.carry * (1 if (rec.shot.units or "yards").lower().startswith("y") else 1.0936)
    return None
