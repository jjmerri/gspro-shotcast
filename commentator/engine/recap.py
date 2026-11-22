"""The league week's recap (commentator/recap.py).

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import hashlib
import time

from .common import RECAP_POLL_SECONDS, log
from ..board import Board
from ..models import Line
from ..prompts import retry_prompt


class RecapMixin:
    """The league week's recap (commentator/recap.py)."""

    def _recap_soon(self) -> None:
        """See whether the latest final league week needs its recap built (or rebuilt: its results or
        footage changed), once the booth's quiet. One build at a time; a call during one looks again
        after it."""
        p = self.cfg.publish
        if not (p.enabled and p.bucket and p.recaps and self._league_page_on and self.commentary_on
                and self.speech is not None) or self.cfg.source.type == "replay":
            return
        if self._recap_task and not self._recap_task.done():
            self._recap_again = True
            return
        self._recap_task = asyncio.create_task(self._recap_when_quiet(), name="recap")

    def _recap_busy(self) -> bool:
        """A round on, or its stats page publish (which cuts its reel) still to come."""
        r = self.round
        return bool(self._round_handling or (r is not None and not r.ended)
                    or (self._publish_task and not self._publish_task.done()))

    async def _recap_when_quiet(self) -> None:
        while True:
            self._recap_again = False
            while self._recap_busy():
                await asyncio.sleep(RECAP_POLL_SECONDS)
            try:
                await self._build_recap()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("League recap failed")
            if not self._recap_again:
                return

    async def _build_recap(self) -> None:
        from .. import recap
        from ..stats_web import round_groups, season_start

        board = self._league_board()
        today = time.strftime("%Y-%m-%d")
        if board is None:
            return
        season = season_start(self.cfg.publish.season_start)
        weeks = [w for w in board.weeks if board.final(w, today)
                 and (not season or str((self.league.week_entry(w) or {}).get("end_date") or "") >= season)]
        if not weeks:
            return
        week = max(weeks)
        facts = recap.week_facts(board, week, today)
        if not facts["rows"]:
            return
        facts["holes"] = await self._week_holes(board, week)
        key = recap.key_of(facts)
        _, of_row = round_groups(self.store.db)
        rows = self.store.week_rounds(self.league.data.get("schedule") or []).get(week, {})
        team_reel = {t: str(of_row[rid]) for t, rid in rows.items() if rid in of_row}
        p = self.cfg.publish
        client = self._s3()
        reels, index = await asyncio.to_thread(recap.load, client, p.bucket, p.prefix)
        found = recap.footage(reels, team_reel, self.league.team_players)
        story = recap.race(facts)
        turns = recap.turn_moments(story, self._turn_finder(team_reel)) if story else {}
        digest = recap.digest(facts, found, turns)
        if (index.get(key) or {}).get("hash") == digest or self._recap_tried == (key, digest):
            return
        self._recap_tried = (key, digest)   # a build that fails isn't paid for again till something changes
        if turns:   # (cut from the streams: one no longer kept shows nothing, the booth catching up over the next clip)
            from .. import highlights

            got = await asyncio.to_thread(highlights.cut_alone, client, p.bucket, p.prefix,
                                          [m for ms in turns.values() for m in ms])
            for t, ms in turns.items():
                f = found.setdefault(t, {"key": team_reel[t], "moments": [], "ats": []})
                turned = {m["id"] for m in ms}
                have = {m.get("id") for m in f["moments"]}
                # (one its reel already has plays from the reel, picked first as a turn)
                f["moments"] = [{**m, "turn": True} if m.get("id") in turned else m for m in f["moments"]]
                f["moments"] += [{**m, "at": got[m["id"]][1], "parts": got[m["id"]][0], "src": "s"}
                                 for m in ms if m["id"] in got and m["id"] not in have]
            found = {t: f for t, f in found.items() if f["moments"] or f.get("finish")}
        clips = recap.choose(facts, found)
        if not clips:
            log.info("League recap of week %s: no footage of any team's round", week)
            return
        lines = await self._write_recap(facts, clips)
        if lines is None:
            return
        # Every line voiced: (where, Line), where = ("intro", n) / ("clip", i, n) / ("outro", n).
        said = [(("intro", n), l) for n, l in enumerate(lines["intro"])]
        said += [(("clip", i, n), l) for i, ls in sorted(lines["clips"].items()) for n, l in enumerate(ls)]
        said += [(("outro", n), l) for n, l in enumerate(lines["outro"])]
        voiced = await asyncio.gather(*(self.speech._synthesize(Line(speaker=s, text=t)) for _, (s, t) in said))
        voices, audio = {}, []
        for (where, _), (ok, a) in zip(said, voiced):
            samples = getattr(a, "samples", None) if ok else None
            if samples is None or not len(samples):
                continue
            # Named for what's in it: a rebuild's new audio is under new names, the files being cached for good.
            data = recap.wav(samples, a.sample_rate)
            voices[where] = (len(samples) / a.sample_rate, f"a/{hashlib.sha1(data).hexdigest()[:16]}.wav")
            audio.append((voices[where][1], data))
        if not any(w[0] == "intro" for w in voices):
            log.info("League recap of week %s: its lines couldn't be voiced", week)
            return
        playlists = {}
        for c in clips:
            pk = (c["key"], bool(c.get("finish")))
            if pk not in playlists and not c.get("parts"):
                playlists[pk] = await asyncio.to_thread(recap.reel_playlist, client, p.bucket, p.prefix, *pk)
        cuts = []
        for i, c in enumerate(clips):
            spoken = sum(voices[w][0] + recap.LINE_GAP for w in voices if w[:2] == ("clip", i))
            alone = c.get("finish") or c.get("parts")   # (its own playlist: nothing else in it)
            ats = [c["at"]] if alone else found[c["team"]]["ats"]
            cuts.append(recap.cut(c, c.get("parts") or playlists[(c["key"], bool(c.get("finish")))], ats, spoken))
        body, video, sources = recap.timeline(facts, clips, lines, voices, cuts)
        if not body["clips"]:
            log.info("League recap of week %s: none of its clips are in their reels", week)
            return
        video, sources, cards = await asyncio.to_thread(recap.burn_cards, client, p.bucket, p.prefix, key, body, video, sources)
        video, made = await asyncio.to_thread(recap.burn_boards, client, p.bucket, p.prefix, key, body, video, sources)
        made.update(cards)
        entry = await asyncio.to_thread(recap.publish, client, p.bucket, p.prefix, key, body, video, sources, audio,
                                        digest, season, made=made)
        log.info("League recap of week %s up: %s clips, %.0fs of video", week, entry["clips"], entry["duration"])
        if self.alerts is not None:
            await asyncio.to_thread(self.alerts.recap, key, week, entry.get("course"))

    def _turn_finder(self, team_reel: dict[str, str]):
        """recap.turn_moments' `find`: a team's shot on a hole of its week's round (highlights.turn_shot),
        of its players' when it shared the round with another team, and none of another team's."""
        from .. import highlights
        from ..stats_web import _late_gimmes, round_groups, shot_rounds

        # (a putt conceded after its round was recorded is that round's, finishing its last hole)
        of_shot = _late_gimmes(self.store.db, round_groups(self.store.db)[0], shot_rounds(self.store.db))
        wanted = set(team_reel.values())
        by_round: dict[str, list[dict]] = {}
        for r in self.store.db.execute("SELECT * FROM shots ORDER BY ts, id"):
            if (g := of_shot.get(r["id"])) is not None and str(g) in wanted:
                by_round.setdefault(str(g), []).append(dict(r))
        players = self.league.team_players
        shared = {k for k in team_reel.values() if list(team_reel.values()).count(k) > 1}

        def find(team: str, hole: int, want: str) -> dict | None:
            if (key := team_reel.get(team)) is None:
                return None
            names = {n.casefold() for n in players.get(team, [])}
            others = {n.casefold() for t, ps in players.items() if t != team for n in ps} - names
            shots = [s for s in by_round.get(key, []) if (who := str(s["player"] or "").casefold()) not in others
                     and (key not in shared or who in names)]
            return highlights.turn_shot(shots, hole, want)
        return find

    async def _week_holes(self, board: Board, week: int) -> dict[str, dict[int, int]]:
        """Each posted team's week hole by hole, for the recap's story (recap.race): the sim's card for
        a net it counted, the players' Squabbit rounds for one posted there (read once per team and
        net). Best-effort: a team without one is left out of the story."""
        sim = self.store.week_holes(self.league.data.get("schedule") or []).get(week, {})
        posted = board.posted(week)
        out = {e.team: sim[e.team] for e in posted if e.sim and e.team in sim}
        theirs = {e.team: e.net for e in posted if not e.sim}
        if any(self._recap_holes.get((week, t), (None,))[0] != n for t, n in theirs.items()):
            invite = self.league.squabbit_invite

            def fetch() -> dict[str, dict[int, int]]:
                from ..board import squabbit_holes, week_of
                from ..handicap import week_card
                from ..squabbit import SquabbitClient

                with SquabbitClient() as client:
                    league = client.league_by_invite(invite)
                    t = next((t for t in client.tournaments(league["_id"]) if week_of(t) == week), None) if league else None
                    if t is None:
                        return {}
                    card = self.league.week_card(week)
                    if not card:
                        rounds = client.tournament_rounds(t["_id"])
                        card = week_card(t, rounds[0]) if rounds else None
                    return squabbit_holes(t, client.league_rounds(league["_id"]), card, self.league) if card else {}

            try:
                got = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=self.cfg.squabbit.timeout_seconds)
                for t, n in theirs.items():   # one Squabbit can't score is kept as none, not asked again
                    self._recap_holes[(week, t)] = (n, got.get(t) or {})
            except Exception as e:
                log.warning("League recap: Squabbit hole scores fetch failed (%s); the story leaves those teams out", e)
        out.update({t: holes for t, n in theirs.items()
                    if (cached := self._recap_holes.get((week, t))) and cached[0] == n and (holes := cached[1])})
        return out

    async def _write_recap(self, facts: dict, clips: list[dict]) -> dict | None:
        """The recap's lines from the closer's model, checked against the week's facts and retried
        once: {intro, clips: {i: lines}, outro} of (speaker, text), with any line that still fails
        its check left out. None when there's no intro left to air."""
        from .. import recap

        user, facts_text = recap.prompt(facts, clips, dict(zip(self.speakers, self._persona_names)))
        ask, lines = user, None
        for attempt in range(2):
            raw = await self._complete("recap", self.closer_llm, ask, [], schema=recap.schema(self.speakers),
                                       system=self._system_league)
            lines, problems = recap.script(raw, clips, self.speakers)
            checked = {}
            for part, ls in [("intro", lines["intro"]), ("outro", lines["outro"])] + [
                    (i, ls) for i, ls in lines["clips"].items()]:
                good, bad = self._grounded([Line(speaker=s, text=t) for s, t in ls], facts_text, mode="news")
                checked[part] = [(l.speaker, l.text) for l in good]
                problems += bad
            lines = {"intro": checked.get("intro", []), "outro": checked.get("outro", []),
                     "clips": {i: v for i, v in checked.items() if isinstance(i, int)}}
            if not problems:
                return lines
            if attempt == 0:
                self.stats["retries"] += 1
                ask = retry_prompt(user, problems)
        if not lines or not lines["intro"]:
            log.info("League recap of week %s dropped: no usable lines (%s)", facts["week"], "; ".join(problems))
            return None
        return lines
