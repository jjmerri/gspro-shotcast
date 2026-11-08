"""Posts a league round to Squabbit as it's played, when the players say yes on the sim.

Each league team has a scorecard in its week's Squabbit event, made ahead with a blank round per
player. As each hole finishes, its score goes on the team's rounds: the team's ball on every
teammate's (scramble, alternate shot), each player's own otherwise. A tee shot on the green on a
closest-to-the-pin hole goes in as that player's mark, alongside anyone else's; Squabbit keeps the
closest.

Nothing goes in over a person's entry. The booth signs in as the league organiser, so Squabbit
can't say who wrote what; the booth remembers what it wrote (in `path`) and treats anything else on
the team's card as someone's. A card with any such entry isn't offered, and one turning up
mid-round stops the posting. Each post reads the whole card and writes it in one go, only if
none of it changed since the read, so an entry made in between is never overwritten either.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INCHES_PER_YARD = 36


@dataclass
class Target:
    """One team's card in its week's Squabbit event."""

    team: str
    week: int
    tournament_id: str
    tournament_round_id: str
    scorecard_id: str
    start_hole: int                       # the course's hole number of the round's first hole
    num_holes: int
    rounds: dict[str, str]                # Squabbit user id -> their round document id
    handicaps: dict[str, float | None] = field(default_factory=dict)   # the event's, per user id
    ctp: dict[int, str] = field(default_factory=dict)                  # course hole -> competition id

    def hole(self, seq: int) -> int:
        """The course's hole number Squabbit keys a score by, for a hole of the round."""
        return self.start_hole + seq - 1


class Blocked(Exception):
    """Someone else has entered something on this card: the booth leaves it to them."""


class Busy(Exception):
    """The card kept changing under the booth: nothing was written this time."""


class LiveSync:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: dict[str, Any] = {"answers": {}, "scores": {}, "ctp": {}}
        try:
            self.state.update(json.loads(path.read_text()))
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("Couldn't read %s; starting it over", path, exc_info=True)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.path)

    # ---- what the players said -------------------------------------------------------------

    def answer(self, target: Target) -> bool | None:
        """Yes or no for this card, if already asked (a booth restarted mid-round doesn't ask again)."""
        return self.state["answers"].get(target.scorecard_id)

    def set_answer(self, target: Target, yes: bool) -> None:
        self.state["answers"][target.scorecard_id] = yes
        self._save()

    def posted(self, target: Target) -> dict[str, set[int]]:
        """The holes the booth has on this card, per user id, by the round's hole order: a booth that
        restarted mid-round still takes back a hole GSPro reopened."""
        out: dict[str, set[int]] = {}
        for uid, rid in target.rounds.items():
            seqs = {int(h) - target.start_hole + 1 for h in self.state["scores"].get(rid, {})}
            if seqs:
                out[uid] = seqs
        return out

    # ---- reading Squabbit -------------------------------------------------------------------

    @staticmethod
    def find(client, league_id: str, week: int, team: str, uids: list[str]) -> Target | None:
        """The team's card in the week's event: the scorecard whose rounds are all its players'."""
        from .board import week_of

        tournament = next((t for t in client.tournaments(league_id) if week_of(t) == week), None)
        if tournament is None:
            log.info("Squabbit live: no week %s event", week)
            return None
        cards: dict[str, dict[str, str]] = {}
        for rd in client.player_rounds(tournament["_id"]):
            if rd.get("scorecardId") and rd.get("userId"):
                cards.setdefault(rd["scorecardId"], {})[rd["userId"]] = rd["_id"]
        found = [(sc, rounds) for sc, rounds in cards.items() if set(uids) <= set(rounds)]
        if len(found) != 1:
            log.info("Squabbit live: %s scorecards in week %s hold all of %s's players", len(found), week, team)
            return None
        scorecard, rounds = found[0]
        trounds = client.tournament_rounds(tournament["_id"])
        wanted = (client.document(f"scorecards/{scorecard}") or {}).get("tournamentRoundId")
        tround = next((t for t in trounds if t["_id"] == wanted), trounds[0] if len(trounds) == 1 else None)
        if tround is None:
            log.info("Squabbit live: can't tell which of week %s's rounds %s's card is in", week, team)
            return None
        start = tround.get("startingHole")
        start = start.get("holeNumber") if isinstance(start, dict) else start
        users = tournament.get("users") or {}
        return Target(
            team=team, week=week, tournament_id=tournament["_id"], tournament_round_id=tround["_id"],
            scorecard_id=scorecard, start_hole=int(start or 1), num_holes=int(tround.get("numHoles") or 18),
            rounds=rounds,
            handicaps={uid: (users.get(uid) or {}).get("tournamentHandicap") for uid in rounds},
            ctp={int(c["holeNumber"]): cid for cid, c in (tround.get("holeCompetitions") or {}).items()
                 if c.get("type") == "ClosestToThePin" and c.get("holeNumber") is not None},
        )

    def others(self, client, target: Target) -> list[str]:
        """What's on the card that the booth didn't put there, e.g. "Kyle hole 3: 4"."""
        found = []
        for uid, rid in target.rounds.items():
            doc = client.document(f"rounds/{rid}") or {}
            found += self._foreign(rid, doc)
        return found

    def _foreign(self, rid: str, doc: dict[str, Any]) -> list[str]:
        mine = self.state["scores"].get(rid, {})
        theirs = doc.get("holeScoresV2") or {}
        return ([f"round {rid} hole {h}: {v}" for h, v in sorted(theirs.items()) if mine.get(str(h)) != v]
                + [f"round {rid} hole {h}: cleared" for h in sorted(mine) if h not in theirs])

    # ---- writing ----------------------------------------------------------------------------

    def push(self, client, target: Target, scores: dict[str, dict[int, int | None]],
             marks: dict[tuple[int, str], float]) -> list[str]:
        """Posts what Squabbit doesn't have yet: each player's finished holes (Squabbit user id -> hole
        of the round -> strokes; None takes back a score the booth posted, for a hole GSPro reopened)
        and closest-to-the-pin marks ((hole of the round, user id) -> yards; None takes back the
        booth's mark for a tee shot taken back).
        The whole card is read and written as one: anyone else's entry on any of the team's rounds
        raises Blocked, and a change between the read and the write (someone entering just then) makes
        the write fail and the booth look again. Returns what was written, for the log."""
        marks = {k: y for k, y in marks.items() if target.hole(k[0]) in target.ctp}
        tround = f"tournamentsV2/{target.tournament_id}/tournamentRounds/{target.tournament_round_id}"
        for _ in range(3):
            docs = {}
            for rid in target.rounds.values():
                if (got := client.document_at(f"rounds/{rid}")) is None:
                    raise Blocked(f"round {rid} is gone")
                docs[rid] = got
            if (foreign := [x for rid, (doc, _) in docs.items() for x in self._foreign(rid, doc)]):
                raise Blocked(", ".join(foreign))
            updates, verify, done = [], [], []
            new_scores: dict[str, dict[str, int]] = {}
            for uid, rid in target.rounds.items():
                doc, at = docs[rid]
                values, new = self._score_values(target, uid, doc, scores.get(uid) or {})
                if values:
                    updates.append((f"rounds/{rid}", values, at))
                    new_scores[rid] = new
                    done.append(f"{uid} " + " ".join(f"{h}:{'-' if v is None else v}"
                                                     for h, v in sorted(new.items(), key=lambda x: int(x[0]))))
                else:
                    verify.append((f"rounds/{rid}", at))
            new_ctp: dict[str, float | None] = {}
            if marks and (got := client.document_at(tround)) is not None:
                values, new_ctp, lines = self._ctp_values(target, got[0], marks, client.user_id())
                if values:
                    updates.append((tround, values, got[1]))
                    done += lines
            if not updates:
                return []
            if client.commit(updates, verify):
                for rid, new in new_scores.items():
                    mine = self.state["scores"].setdefault(rid, {})
                    for h, v in new.items():
                        if v is None:
                            mine.pop(h, None)
                        else:
                            mine[h] = v
                for key, inches in new_ctp.items():
                    if inches is None:
                        self.state["ctp"].pop(key, None)
                    else:
                        self.state["ctp"][key] = inches
                self._save()
                return done
        raise Busy(f"week {target.week}'s card for {target.team} kept changing")

    @staticmethod
    def _score_values(target: Target, uid: str, doc: dict[str, Any],
                      by_seq: dict[int, int | None]) -> tuple[dict[tuple[str, ...], Any], dict[str, int | None]]:
        from .squabbit import DELETE

        have = {str(k): v for k, v in (doc.get("holeScoresV2") or {}).items()}
        new = {str(target.hole(seq)): v for seq, v in by_seq.items()
               if 1 <= seq <= target.num_holes and have.get(str(target.hole(seq))) != v}
        if not new:
            return {}, {}
        merged = {h: v for h, v in {**have, **new}.items() if v is not None}
        values: dict[tuple[str, ...], Any] = {("holeScoresV2", h): DELETE if v is None else v for h, v in new.items()}
        stats = doc.get("holeStats") or {}
        values.update({("holeStats", h): {"holeEvents": {}} for h, v in new.items() if v is not None and h not in stats})
        values[("score",)] = sum(merged.values()) if merged else None
        if target.handicaps.get(uid) is not None and doc.get("userHandicap") is None:
            values[("userHandicap",)] = float(target.handicaps[uid])
        if len(merged) >= target.num_holes:
            if not doc.get("finishedDate"):
                values[("finishedDate",)] = datetime.now(timezone.utc).replace(microsecond=0)
        elif doc.get("finishedDate"):
            values[("finishedDate",)] = None   # a reopened last hole: not finished after all
        return values, new

    def _ctp_values(self, target: Target, tround: dict[str, Any], marks: dict[tuple[int, str], float | None],
                    me: str | None) -> tuple[dict[tuple[str, ...], Any], dict[str, float | None], list[str]]:
        """The event's CTP results to set: the booth's marks, alongside anyone else's. A player's mark
        someone else entered stays, and only the booth's own mark is taken back."""
        from .squabbit import DELETE

        comps = tround.get("holeCompetitions") or {}
        values: dict[tuple[str, ...], Any] = {}
        new: dict[str, float | None] = {}
        lines = []
        for (seq, uid), yards in sorted(marks.items()):
            cid = target.ctp[target.hole(seq)]
            have = ((comps.get(cid) or {}).get("results") or {}).get(uid)
            key = f"{target.tournament_round_id}/{cid}/{uid}"
            if yards is None:
                if have is not None and have == self.state["ctp"].get(key):
                    values[("holeCompetitions", cid, "results", uid)] = DELETE
                    values[("holeCompetitions", cid, "enteredByUserIds", uid)] = DELETE
                    new[key] = None
                    lines.append(f"{uid} CTP hole {target.hole(seq)} taken back")
                continue
            inches = float(round(yards * INCHES_PER_YARD))
            if have is None and key in self.state["ctp"]:
                continue   # the booth's mark, cleared by someone on Squabbit: theirs to put back
            if have is not None and have != self.state["ctp"].get(key):
                log.info("Squabbit live: %s already has a mark on hole %s (%s in); leaving it", uid,
                         target.hole(seq), have)
                continue
            if have == inches:
                continue
            values[("holeCompetitions", cid, "results", uid)] = inches
            if me:
                values[("holeCompetitions", cid, "enteredByUserIds", uid)] = me
            new[key] = inches
            lines.append(f"{uid} CTP hole {target.hole(seq)} {inches:g} in")
        return values, new, lines


def team_scores(r, team: str, uid_of: dict[str, str]) -> dict[str, dict[int, int]]:
    """A round's finished holes for one team, per Squabbit user id: a shared ball's score on each
    teammate's round, each player's own otherwise."""
    out: dict[str, dict[int, int]] = {}
    for side in r.sides:
        if side.team != team:
            continue
        for seq in range(1, r.holes + 1):
            score = r.card_score(side, seq)
            if score is None:
                continue
            for p in side.players:
                if (uid := uid_of.get(p)):
                    out.setdefault(uid, {})[seq] = score
    return out


def team_ctp(r, team: str, uid_of: dict[str, str], match_player,
             finished: dict[tuple[int, str], float] | None = None) -> dict[tuple[int, str], float]:
    """This round's tee shots on the green on CTP holes for one team: (hole of the round, user id) ->
    yards. Only a player's own: a shared ball nobody can put a name to isn't anyone's mark. `finished`
    adds tee shots that ended the hole ((hole of the round, player) -> yards), which ctp_results misses."""
    from .facts import to_yards

    out = {}
    labels = {side.label for side in r.sides if len(side.players) > 1}   # a shared ball, not a player
    shared = set(r.anonymous_players())   # one profile's ball: whoever hit it, it's no one's mark
    found = [(seq, who, to_yards(dist)) for seq, by_who in r.ctp_results.items() for who, dist in by_who.items()]
    found += [(seq, who, yards) for (seq, who), yards in (finished or {}).items()]
    for seq, who, yards in found:
        player = None if who in labels else match_player(who)
        if player in shared:
            continue
        side = r.side_for(player) if player else None
        if side is None or side.team != team or not (uid := uid_of.get(player)) or yards is None:
            continue
        if (seq, uid) not in out or yards < out[(seq, uid)]:
            out[(seq, uid)] = yards
    return out
