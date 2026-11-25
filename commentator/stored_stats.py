"""A recorded round's stats, worked out again from the shots season.db keeps for it.

The rounds and player_rounds rows are written once, as the round ends, from the booth's own records
of its shots. The shots table is the better record: GSPro's round file corrects where a ball came to
rest (the screen's read can be wrong), after the booth has used the read. This rebuilds a round from
what the store has (its sides, scores and pars as stored, its shots as corrected) and runs the round's
own stats over it, for a repair of rows written from the reads."""

from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

from .round import Round, Side, SideStats

# The columns the shots' lies and distances decide. Scores, gross, net and the rest come from the card
# and stand as they are.
SIDE_COLUMNS = ("fir_hit", "fir_tries", "gir_hit", "gir_tries", "scramble_hit", "scramble_tries",
                "sand_hit", "sand_tries", "putts", "putt_holes", "penalties", "longest_drive")
PLAYER_COLUMNS = SIDE_COLUMNS[:-2]

_HOLE = re.compile(r"Hole (\d+)(?:, par (\d+))?")


def _hole(text: str | None) -> tuple[int | None, int | None]:
    m = _HOLE.match(text or "")
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else (None, None)


def _numbers(raw: str | None) -> dict[int, int]:
    try:
        return {int(h): int(v) for h, v in (json.loads(raw) or {}).items() if v is not None}
    except (TypeError, ValueError, AttributeError):
        return {}


class StoredRound(Round):
    """A round as the store recorded it: its sides' and players' scores as stored stand (nothing is
    re-read from the shots for a hole without one). A hole was picked up when its last stroke stopped
    short with no tap-in conceded after it, and the card has it past the next stroke (or at double par,
    GSPro's usual maximum): one more is the tap-in from before the store kept them."""

    def __init__(self, fmt: str | None, holes: int, sides: list[Side], side_scores: dict[str, dict[int, int]],
                 player_scores: dict[str, dict[int, int]], par: dict[int, int], source: str | None,
                 roster: dict[str, str], missed_last: dict[tuple[str, int], int]) -> None:
        self.id = 0
        self.format = fmt
        self.holes = holes
        self.stats_through = None
        self.sides = sides
        self.side_scores = side_scores
        self.player_scores = player_scores
        self.par = par
        self.source = source or "scorecard"
        self.roster = roster
        self.missed_last = missed_last
        self.picked_up = set()

    def playing(self, g: str, seq: int) -> bool:
        return False   # (a hole without a stored score had none: it isn't scored from the shots now)

    def card_score(self, side: Side, seq: int) -> int | None:
        return self.side_scores.get(side.key, {}).get(seq)

    def score_of(self, gspro: str, seq: int) -> int | None:
        return self.player_scores.get(self.roster.get(gspro, gspro), {}).get(seq)

    def _score_source(self) -> str:
        return self.source

    def was_picked_up(self, names: list[str], seq: int) -> bool:
        for n in names:
            if (last := self.missed_last.get((n, seq))) is None:
                continue
            side = self.side_for(n)
            score = self.score_of(n, seq) if self.format not in ("scramble", "alternate") or not side \
                else self.card_score(side, seq)
            if score is None and side:
                score = self.card_score(side, seq)
            if score is not None and (score >= last + 2 or (self.par.get(seq) and score >= 2 * self.par[seq])):
                return True
        return False


def _record(row: sqlite3.Row, seq: int, par: int | None, conceded: bool) -> SimpleNamespace:
    """A stored shot as the round's stats read a ShotRecord, its rest and drive the store's (as
    Engine._sync_rests gives a live one). `conceded`: a full shot that left a tap-in, which the store
    keeps on the tap-in's own row (its gimme), and the live record as the shot's outcome."""
    return SimpleNamespace(
        round_id=0, mulligan=bool(row["mulligan"]), player=row["player"], hole_seq=seq, par=par,
        shot=SimpleNamespace(club=row["club"], is_putt=bool(row["is_putt"]),
                             carry=None if row["left_out"] else row["carry_raw"],
                             units=row["units"], timestamp=row["ts"]),
        shot_number=row["shot_number"], lie_before=row["lie_before"], to_pin_before=row["to_pin"],
        hud=SimpleNamespace(to_pin=row["to_pin"], lie=row["lie_after"], tiles={}),
        tags=json.loads(row["tags"] or "[]"),
        outcome="conceded" if conceded and row["outcome"] is None and not row["is_putt"] else row["outcome"],
        picked=None if row["picked"] is None else bool(row["picked"]),
        rest_lie=row["lie_after"], drive_yards=None if row["left_out"] else row["drive_yards"], synced=True)


def _missing_between(a, b) -> bool:
    """A gap in a ball's stroke numbers that's a stroke the store lacks, not a penalty: either side of it
    is on the green (no penalty's drop is). Off the green the lies can't tell them apart: a re-tee, a
    drop where it lay, in the fairway (GSPro's out-of-bounds option) or by water are all penalties'."""
    return bool(a["is_putt"] or a["lie_after"] == "green" or b["is_putt"] or b["lie_before"] == "green")


def _rows(db: sqlite3.Connection, sql: str, args: tuple) -> list[sqlite3.Row]:
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, args).fetchall()


def recompute(db: sqlite3.Connection, uuid: str) -> tuple[dict[int, SideStats], dict[int, SideStats]] | None:
    """A recorded round's stats from its stored shots: {rounds row id: stats}, {player_rounds row id:
    stats}. None when the round can't be rebuilt (no row, or no hole numbers its scores are keyed by)."""
    sides = _rows(db, "SELECT id, side, team, format, holes, players, scores, hole_pars, source, complete "
                       "FROM rounds WHERE round_uuid = ? ORDER BY id", (uuid,))
    players = _rows(db, "SELECT id, player, slot_id, fmt, scores, hole_pars, complete FROM player_rounds "
                    "WHERE round_uuid = ? ORDER BY id", (uuid,))
    if not sides and not players:
        return None
    # (left_out: a shot the stats page took out of the stats; its column added by the page when first used)
    left_out = "left_out" if any(r[1] == "left_out" for r in db.execute("PRAGMA table_info(shots)")) else "0"
    shots = _rows(db, "SELECT id, ts, player, hole, shot_number, club, is_putt, carry_raw, units, lie_before, "
                       "lie_after, to_pin, tags, outcome, picked, mulligan, uid, derived_from, slot_id, "
                       f"{left_out} AS left_out, COALESCE(total_yards, carry_game_yards) AS drive_yards FROM shots "
                       "WHERE record_uuid = ? ORDER BY ts, id", (uuid,))
    slots = _rows(db, "SELECT slot_id, name, side, player FROM round_slots WHERE round_uuid = ?", (uuid,))
    # A player is their slot: one renamed mid-round (a guest's name read again) keeps it, so their shots
    # under the old name are theirs (else the strokes before would be missing from the stats). The name
    # is the one they ended the round under: their card's, else their last shot's.
    name_of = {r["slot_id"]: r["player"] for r in shots if r["slot_id"] and r["player"] not in (None, "unknown")}
    name_of.update({r["slot_id"]: r["player"] for r in players if r["slot_id"]})
    shots = [{**dict(r), "player": name_of.get(r["slot_id"]) or r["player"]} for r in shots]
    # The holes the round scored, in order (which comes first only matters to a nine's split, not kept
    # here): a hole it stopped on unfinished wasn't counted, so its shots aren't now.
    side_scores = {r["id"]: _numbers(r["scores"]) for r in sides}
    player_scores = {r["id"]: _numbers(r["scores"]) for r in players}
    numbers = sorted({h for s in [*side_scores.values(), *player_scores.values()] for h in s})
    if not numbers:
        return None
    # A stroke on a hole the round scored, or on one not read, that's nobody's (its match taken back, or
    # never made) leaves a gap in whoever's it was: whose isn't known, so the round is left as stored.
    if any(not r["mulligan"] and r["player"] in (None, "unknown")
           and (_hole(r["hole"])[0] is None or _hole(r["hole"])[0] in numbers) for r in shots):
        return None
    shots = [r for r in shots if r["player"] not in (None, "unknown")]
    seq_of = {h: i for i, h in enumerate(numbers, 1)}
    par: dict[int, int] = {}
    for r in [*sides, *players]:
        for h, p in _numbers(r["hole_pars"]).items():
            if h in seq_of:
                par.setdefault(seq_of[h], p)
    fmt = next((r["format"] for r in sides if r["format"]), None) \
        or next((r["fmt"] for r in players if r["fmt"]), None)
    roster = {s["name"]: s["player"] or s["name"] for s in slots if s["name"]}
    built: list[Side] = []
    for r in sides:
        names = json.loads(r["players"] or "[]")
        # (a slot named for the side itself: a team on one GSPro profile)
        gspro = [s["name"] for s in slots if s["name"] and (s["side"] == r["side"] or s["player"] in names
                                                            or s["name"] == r["side"])]
        names = names or sorted({roster[g] for g in gspro})
        built.append(Side(key=str(r["id"]), label=r["side"], players=names, gspro=gspro or list(names), team=r["team"]))
    # A row from before sides kept their players: the one side with none is whoever no other side has.
    unknown = [s for s in built if not s.players]
    if len(unknown) == 1:
        claimed = {n for s in built for n in [*s.players, *s.gspro]}
        others = sorted({row["player"] for row in shots} - claimed - {"unknown"})
        unknown[0].players = others
        unknown[0].gspro = [g for g, p in roster.items() if p in others] or list(others)
    built = [s for s in built if s.players]   # (more than one: whose shots are whose isn't known; left as stored)
    for p in players:
        roster.setdefault(p["player"], p["player"])
    # Each ball's strokes on each hole: a team's one ball in a scramble or alternate shot, else a player's.
    ball_of = {n: tuple(s.players) for s in built if s.players and len(s.players) > 1 and fmt in ("scramble", "alternate")
               for n in [*s.players, *s.gspro]}
    by_ball: dict[tuple[tuple, int], list] = {}
    records = []
    # A full shot that left a conceded tap-in: its gimme's parent. A gimme from before the store linked
    # them is the stroke before it, its player's on its hole; when that isn't one stroke, left as stored.
    # (a linked one whose parent isn't in the round's shots: the same)
    tapped: set[int] = set()
    for g in shots:
        if g["outcome"] == "gimme" and not g["mulligan"]:
            if g["derived_from"]:
                before = [r["id"] for r in shots if r["uid"] == g["derived_from"]
                          and not r["mulligan"] and r["outcome"] != "gimme"]
            else:
                before = [r["id"] for r in shots if g["shot_number"] and not r["mulligan"] and r["outcome"] != "gimme"
                          and r["player"] == g["player"] and _hole(r["hole"])[0] == _hole(g["hole"])[0]
                          and r["shot_number"] == g["shot_number"] - 1]
            if len(before) != 1:
                return None
            tapped.update(before)
    seen: set[tuple[str, int, int]] = set()
    outside: list[tuple[int, float, int | None]] = []   # strokes on a hole the card has no score for
    last_on_card = float("-inf")
    for row in shots:
        if row["mulligan"]:
            continue   # (taken back: no stat counts it, wherever it was)
        number, hole_par = _hole(row["hole"])
        # The hole is the screen's read of it: one it can't place, or misread (its par isn't the hole's,
        # or a player's stroke comes twice), would put the stats on the wrong hole. Left as stored.
        if number is None or (hole_par and par.get(seq_of.get(number), hole_par) != hole_par):
            return None
        if row["shot_number"] and row["outcome"] != "gimme":
            if (key := (row["player"], number, row["shot_number"])) in seen:
                return None
            seen.add(key)
        if number not in seq_of:
            outside.append((number, row["ts"], row["shot_number"]))
            continue
        if hole_par:   # (a hole the cards kept no par for: every ball's stats get it)
            par.setdefault(seq_of[number], hole_par)
        if row["outcome"] != "gimme":   # (a gimme's time is when it was noted, maybe after the next tee shot)
            last_on_card = max(last_on_card, row["ts"])
        by_ball.setdefault((ball_of.get(row["player"], (row["player"],)), seq_of[number]), []).append(row)
        if row["outcome"] != "gimme":   # (a conceded tap-in the booth added: never struck, no live record)
            records.append(_record(row, seq_of[number], hole_par, row["id"] in tapped))
    # A hole off the card is the one a round quit early stopped on unfinished: one hole, begun (its
    # first stroke's there) after the rest. Anything else (any in a finished round) is a misread hole,
    # its stroke lost to the hole it was on: left as stored.
    finished = any(r["complete"] for r in [*sides, *players])
    if outside and (finished or len({n for n, _, _ in outside}) > 1 or any(ts < last_on_card for _, ts, _ in outside)
                    or min(outside, key=lambda o: o[1])[2] != 1):
        return None
    # A hole that may have been picked up: the ball's last stroke (one the team played on, in a
    # scramble) stopped short (a putt missed, or a full shot not holed), with no tap-in conceded after
    # it (StoredRound.was_picked_up).
    missed_last: dict[tuple[str, int], int] = {}
    for (ball, seq), rows in by_ball.items():
        struck = [r for r in rows if not r["mulligan"] and r["shot_number"] and r["picked"] != 0]
        last = max((r["shot_number"] for r in struck), default=None)
        if last is not None and not any(r["outcome"] == "gimme" for r in rows) \
                and all(r["outcome"] == "missed" or (r["outcome"] is None and not r["is_putt"])
                        for r in struck if r["shot_number"] == last):
            missed_last.update(((name, seq), last) for name in ball)
    rnd = StoredRound(fmt, max([r["holes"] for r in sides] or [len(numbers)]), built,
                      {str(r["id"]): {seq_of[h]: v for h, v in side_scores[r["id"]].items()} for r in sides},
                      {p["player"]: {seq_of[h]: v for h, v in player_scores[p["id"]].items()} for p in players},
                      par, next((r["source"] for r in sides), None), roster, missed_last)
    rnd.holes = max(rnd.holes, len(numbers))
    # A stroke past its ball's score on the hole is one misread from another hole of the same par: left
    # as stored.
    for (ball, seq), rows in by_ball.items():
        side = rnd.side_for(ball[0])
        if len(ball) > 1 and side:
            score = rnd.card_score(side, seq)
        else:
            score = rnd.score_of(ball[0], seq) or (rnd.card_score(side, seq) if side and len(side.players) == 1 else None)
        if score and any(r["shot_number"] and r["shot_number"] > score for r in rows
                         if not r["mulligan"] and r["outcome"] != "gimme"):
            return None
    # A row with a scored hole none of its strokes are recorded on (shots the record never got) would
    # come out short, and one with no scores kept (an older row) can't be scored, nor one with a hole
    # whose par neither the cards nor the shots have (its hole would be left out): it's left as stored.
    # A ball whose numbered strokes on a hole don't start at 1 is missing its first (its tee shot, the
    # fairway's), one with a gap that's a stroke it lacks (not a penalty: _missing_between), and one with
    # a stroke whose number wasn't read too:
    # so is the row of whoever played it. In a scramble each player's own drive is theirs too.
    struck = {(r.player, r.hole_seq) for r in records if not r.mulligan}
    groups = {key: list(rows) for key, rows in by_ball.items()}
    if fmt == "scramble":
        mine: dict[tuple, list] = {}
        for (ball, seq), rows in by_ball.items():
            if len(ball) > 1:
                for r in rows:
                    mine.setdefault(((r["player"],), seq), []).append(r)
        groups.update(mine)
    unstarted = set()
    for key, rows in groups.items():
        played = sorted((r for r in rows if r["shot_number"] and r["outcome"] != "gimme"),
                        key=lambda r: r["shot_number"])
        unread = any(not r["shot_number"] and r["outcome"] != "gimme" for r in rows)   # (which stroke it was)
        if unread or played and (played[0]["shot_number"] != 1 or any(
                b["shot_number"] - a["shot_number"] > 1 and _missing_between(a, b)
                for a, b in zip(played, played[1:]))):
            unstarted.add(key)

    def whole(names, scores: dict[int, int]) -> bool:
        return bool(scores) and all(seq_of[h] in par and any((n, seq_of[h]) in struck for n in names)
                                    and not any(k in unstarted for n in names
                                                for k in ((ball_of.get(n, (n,)), seq_of[h]), ((n,), seq_of[h])))
                                    for h in scores)

    # A scramble team's row is each partner's drives too (its longest): one with a card for a hole none
    # of their strokes are on leaves it as stored. (A team on one GSPro profile: its strokes are its.)
    carded = {p["player"]: {seq_of[h] for h in player_scores[p["id"]]} for p in players}

    def partners(side: Side) -> bool:
        if fmt != "scramble" or any(r.player not in side.players for r in records
                                    if r.player in side.gspro or r.player == side.label):
            return True
        return all((n, seq) in struck for n in side.players for seq in carded.get(n, ()))

    out_sides = {int(s.key): rnd.side_stats(s, records) for s in built
                 if whole([*s.players, *s.gspro], side_scores[int(s.key)]) and partners(s)}
    out_players = {p["id"]: rnd.player_stats(p["player"], records) for p in players
                   if whole([p["player"]], player_scores[p["id"]])}
    return out_sides, out_players


def side_values(st: SideStats) -> tuple:
    """A side's recomputed stats in SIDE_COLUMNS' order, as record_round writes them."""
    return (*st.fir, *st.gir, *st.scramble, *st.sand, st.putts if st.putt_holes else None, st.putt_holes,
            st.penalties, st.longest_drive)


def player_values(st: SideStats) -> tuple:
    """A player's recomputed stats in PLAYER_COLUMNS' order, as record_player_round writes them."""
    return side_values(st)[:-2]
