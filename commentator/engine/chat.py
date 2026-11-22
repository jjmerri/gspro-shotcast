"""Live chat: what the booth reads from it and answers on air.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import time
from collections.abc import Iterable

from .common import PreparedChat, log
from ..chat import ChatMessage, chat_hazard_denial_problems, chat_lead_in_problems, cursor_at, screened_out
from ..facts import PUTT_FOR, to_yards, wrong_green_yards
from ..hud import Hud
from ..prompts import chat_prompt, chat_schema


class ChatMixin:
    """Live chat: what the booth reads from it and answers on air."""

    def _chat_open(self) -> bool:
        """Whether the page's chat is open: the booth on air (not muted) in a round."""
        r = self.round
        return bool(self.commentary_on and self.speech is not None and not self.speech.muted
                    and r is not None and not r.ended)

    async def _chat_loop(self) -> None:
        failing = False
        while True:
            try:
                await self._chat_tick()
                if failing:
                    log.info("Chat: reaching the chat table again")
                failing = False
            except Exception as e:
                if not failing:   # once per outage, not every few seconds
                    log.warning("Chat: couldn't reach the chat table (chat.table %r): %s", self.cfg.chat.table, e)
                failing = True
            await asyncio.sleep(self.cfg.chat.poll_seconds)

    CHAT_REWRITE = 15.0     # seconds a ready take is kept before newer messages get it rewritten

    async def _chat_tick(self) -> None:
        """Tell the table whether the booth is on air (on a change, and as a heartbeat), and send the
        "we're live" alert (with or without the chat). The messages themselves are read only when a
        shot gives the booth a gap it could talk about them in (_chat_glance)."""
        on, now = self._chat_open(), time.monotonic()
        if self.chat is None:   # alerts without the chat
            if on:
                self._alert_live()
            return
        if on and self._chat_round != self.round.id:
            # A round's chat is its own: the page shows nothing from before this round's opened.
            self._chat_round, self._chat_since = self.round.id, cursor_at(time.time())
            self._chat_read_at = now
        if on:
            self._alert_live()
        since = self._chat_since if on else None
        if on != self._chat_live or since != self._chat_sent_since or now - self._chat_beat >= self.CHAT_HEARTBEAT:
            await asyncio.to_thread(self.chat.set_on_air, on, since)
            if on != self._chat_live:
                log.info("Chat: %s", "open on the stats page" if on else "closed")
            self._chat_live, self._chat_beat, self._chat_sent_since = on, now, since
        if not on:
            # Off air nothing is read, and nothing posted meanwhile is talked about later.
            self._chat_cursor = cursor_at(time.time())
            self._chat_log, self._chat_ready, self._chat_read_at = [], None, now
            self._chat_talked = self._chat_written = self._chat_cursor
            if self._chat_task and not self._chat_task.done():
                self._chat_task.cancel()

    def _alert_topics(self) -> list[str]:
        """The kinds of alert this booth sends, which the page offers: "we're live" only with the stream
        going to the page (_start_live's mirror), none about a league without one set up, and the recap
        only when the booth makes them (_recap_soon). "league" isn't a kind: it tells the page there's
        a league, for how it words the rest."""
        p, league = self.cfg.publish, bool(self.league.team_players)
        live = self.cfg.live.enabled and bool(p.bucket) and self.cfg.source.type != "replay"
        recaps = (league and p.enabled and p.bucket and p.recaps and self.cfg.squabbit.enabled
                  and self.league.squabbit_invite and self.cfg.source.type != "replay" and self.speech is not None
                  and not self.commentary_unavailable and getattr(self.live_llm, "provider", None) != "off")
        return ((["live"] + (["liveleague"] if league else []) if live else []) + ["round", "record"]
                + (["recap"] if recaps else []) + (["league"] if league else []))

    def _alert_live(self) -> None:
        """On air in a round with the published stream running: the page's "we're live" alert, once a
        round, and again if it turns out to be a tournament (Alerts.live's gap keeps the any-round
        phones from hearing twice)."""
        m = self.live_mirror
        if self.alerts is None or self.round is None or m is None or not m.on_page:
            return
        league = bool(self.round.is_league or self.round.is_tournament)
        if self._alerted_live in ((self.round.id, league), (self.round.id, True)):
            return
        self._alerted_live = (self.round.id, league)
        who = " · ".join(s.label for s in self.round.sides) or "A round"
        t = asyncio.create_task(asyncio.to_thread(self.alerts.live, who, self.round.course, league), name="alert-live")
        t.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _chat_glance_on_shot(self) -> None:
        """A shot was struck: if the booth could talk about the chat once its call is done, read the
        chat now (a quick query) and write a take while the ball flies and the call plays."""
        if (self.chat is None or not self._chat_live or not self._chat_open()
                or not self._chat_may_air(time.monotonic(), soon=True)
                or (self._chat_glancing is not None and not self._chat_glancing.done())):
            return
        self._chat_glancing = t = asyncio.create_task(self._chat_glance(), name="chat-read")
        t.add_done_callback(lambda t: t.cancelled() or t.exception())

    CHAT_STREAM_DELAY = 20.0   # seconds the stream runs behind, for a viewer whose player can't say
    CHAT_EDGE_LAG = 6.0        # the stream's live edge behind the PC: a segment's length and its upload
    CHAT_SHOT_WINDOW = 600.0   # a shot watched longer ago than this before a message isn't what it's about

    CHAT_RECORD_WAIT = 10.0    # seconds a take waits for a shot struck before a message to be recorded

    def _chat_watched_at(self, m: ChatMessage, posted: float) -> float:
        """The moment on the viewer's screen (the PC's clock) as they sent `m`, posted at `posted`. The
        page says where in the stream they were (its segment, which the mirror knows the recording time
        of, or how far behind the live edge); failing that the stream's usual delay is taken off."""
        at = posted
        if live := [x for x in self._live_mirrors() if x.session]:
            at = posted - self.CHAT_STREAM_DELAY
            for x in live:
                if (t := x.watched_at(m.sess, m.seg, m.off)) is not None:
                    at = t
                    break
                if m.sess == x.session and m.behind is not None:
                    at = posted - m.behind - self.CHAT_EDGE_LAG
                    break
        return at

    def _chat_shot_watched(self, at: float) -> int | None:
        """The shot on a viewer's screen at `at`: the last one struck before it."""
        try:
            return self.store.shot_struck_before(at, self.CHAT_SHOT_WINDOW)
        except Exception as e:
            log.debug("Chat: couldn't look up the shot a message was sent on: %s", e)
            return None

    @staticmethod
    def _setup_brief(hud: Hud, who: str, up: bool = False, hole: int | None = None, par: int | None = None) -> str:
        """'Frankie setting up shot 2 on hole 4 (par 4), 150 yards to the pin, from the fairway, with 7I'
        (`up`: 'Frankie is up, shot 2...'), and what holing it (a putt) or getting up and down (inside
        60 yards) would make."""
        if up:
            shot = f"{who} is up, shot {hud.shot_number}" if hud.shot_number else f"{who} is up"
        else:
            shot = f"{who} setting up shot {hud.shot_number}" if hud.shot_number else f"{who} setting up a shot"
        if hole:
            shot += f" on hole {hole}" + (f" (par {par})" if par else "")
        pin = to_yards(hud.to_pin)
        putting = (hud.lie or "").lower() == "green" and wrong_green_yards(hud.lie, hud.to_pin) is None
        if putting and pin is not None:
            shot += f", a {max(1, round(pin * 3))}-foot putt"
        else:
            if hud.to_pin:
                shot += f", {hud.to_pin} to the pin"
            if hud.lie:
                shot += f", from the {hud.lie.lower()}"
            if hud.club:
                shot += f", with {hud.club}"
        diff = hud.shot_number - par if hud.shot_number and par else None
        if diff is not None and putting and (name := PUTT_FOR.get(diff)):
            shot += f", for {name}"
        elif diff is not None and pin is not None and pin < 60 and (name := PUTT_FOR.get(diff + 1)):
            shot += f", up and down for {name}"
        return shot

    def _chat_upcoming(self, at: float, shot_id: int | None) -> str | None:
        """Who was over the ball on a viewer's screen at `at`, after the shot they'd watched (`shot_id`),
        and what that shot did, once it's been played: a warning or a prediction is about this one."""
        after = -1.0
        if shot_id is not None:
            row, _ = self.store.shot_brief(shot_id)
            after = row["ts"] if row is not None and row["ts"] is not None else after
        setup = next(((t, brief) for t, brief in reversed(self._setups_seen)
                      if after < t <= at and at - t <= self.CHAT_SHOT_WINDOW), None)
        if setup is None:
            return None
        try:
            next_id = self.store.shot_struck_after(setup[0], self.CHAT_SHOT_WINDOW)
            row = self.store.shot_brief(next_id)[0] if next_id is not None else None
            played = self._chat_shot_brief(next_id)
        except Exception as e:
            log.debug("Chat: couldn't look up the shot a viewer saw set up: %s", e)
            row = played = None
        # One struck earlier, but not recorded yet, is the one set up, not the one recorded after it.
        unrecorded = min((t for t in self._shots_unrecorded.values() if t > setup[0]), default=None)
        if unrecorded is not None and (row is None or row["ts"] is None or unrecorded < row["ts"]):
            return setup[1] + " (just played, result not in yet)"
        if played:
            # A mulligan's still what came of the shot they saw set up; the re-hit has a setup of its own.
            return setup[1] + f" (since played: {played}" + ("; then taken back, a mulligan)" if row["mulligan"] else ")")
        return setup[1] + " (not played yet)"

    def _chat_shot_brief(self, shot_id: int | None) -> str | None:
        """'Frankie's shot 2 on hole 4 (7I), from the fairway to the green; the booth called it: "..."'."""
        if shot_id is None:
            return None
        row, said = self.store.shot_brief(shot_id)
        if row is None:
            return None
        hole = (row["hole"] or "").split(",")[0].strip()
        # A shared ball (alternate shot) can't say which partner hit: no name then.
        who = f"{row['player']}'s " if row["attributed"] and row["player"] and row["player"] != "unknown" else ""
        text = who + ("putt" if row["is_putt"] else f"shot {row['shot_number']}" if row["shot_number"] else "shot")
        if hole:
            text += f" on {hole.lower()}" if hole.lower().startswith("hole") else f" on hole {hole}"
        if row["club"] and not row["is_putt"]:
            text += f" ({row['club']})"
        if row["is_putt"] and row["outcome"] in ("holed", "missed"):
            text += f", {row['outcome']}"
        elif row["lie_before"] and row["lie_after"]:
            text += f", from the {row['lie_before'].lower()} to the {row['lie_after'].lower()}"
        if said:
            text += "; the booth called it: " + " / ".join(f'"{t}"' for t in said)
        return text

    def _chat_writing(self) -> bool:
        """Whether a read, or a take on it, is under way."""
        return any(t is not None and not t.done() for t in (self._chat_glancing, self._chat_task))

    async def _chat_glance(self) -> None:
        """Read the new messages and, when the booth could talk about the chat soon, write its take."""
        if not self._chat_open() or self._chat_round != (r := self.round).id:
            return
        cursor = self._chat_cursor
        try:
            new = await asyncio.to_thread(self.chat.since, cursor)
        except Exception as e:
            log.info("Chat: couldn't read the chat (chat.table %r): %s", self.cfg.chat.table, e)
            return
        now, wall = time.monotonic(), time.time()
        if not self._chat_open() or self.round is not r or self._chat_cursor != cursor:
            return   # closed, or reopened, while it read
        if new:
            self._chat_cursor = new[-1].id
            # Aged on the table's clock when it said what that was (the PC's may be off), else the PC's;
            # never older than the last read either way: they weren't there then.
            since_read = max(0.0, now - self._chat_read_at)
            for m in new:
                if why := screened_out(m.text, self.cfg.chat.moderation):
                    log.info("Chat: %s's message left out (%s)", m.name, why)
                else:
                    age = min(m.age if m.age is not None else max(0.0, wall - m.at), since_read)
                    self._chat_log.append((m, now - age))
                    m.watched = self._chat_watched_at(m, wall - age)
        self._chat_read_at = now
        self._chat_log = [(m, at) for m, at in self._chat_log if now - at < self.cfg.chat.max_age_seconds]
        fresh = [m for m, _ in self._chat_log if m.id > self._chat_talked]
        ready = self._chat_ready
        if ready is not None and ready.expires <= now:
            # Part of what it's about aged out: what's left is written up afresh.
            self._chat_ready = ready = None
            self._chat_written = self._chat_talked
        if (not fresh or fresh[-1].id <= self._chat_written or not self._chat_may_air(now, soon=True)
                or (ready is not None and now - ready.written < self.CHAT_REWRITE)
                or (self._chat_task and not self._chat_task.done())):
            return
        self._chat_written = fresh[-1].id
        self._chat_task = asyncio.create_task(self._prepare_chat(), name="chat-write")
        self._chat_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _chat_hole(self) -> tuple | None:
        r = self.round
        return (id(r), r.seq) if r is not None else None

    def _chat_may_air(self, now: float, soon: bool = False) -> bool:
        """Whether the booth can talk about the chat now (or, `soon`, within the time a take is written):
        once a hole at most (chat.once_per_hole), and chat.min_gap_seconds after the last time."""
        c = self.cfg.chat
        if c.once_per_hole and self._chat_aired_hole is not None and self._chat_aired_hole == self._chat_hole():
            return False
        return now - self._chat_aired_at >= c.min_gap_seconds - (20.0 if soon else 0.0)

    def _chat_member(self, name: str) -> str | None:
        """A note on a chat name that's a league player's: most likely them, watching from home if they
        aren't in this round, or typing between shots if they are. None for any other name, and with nobody
        from the league playing (the booth leaves the league out of it)."""
        r = self.round
        player = self.league.chat_member(name)
        if not player or not r or r.ended or self._no_members(r):
            return None
        if player in self._playing(r):
            return f"a league player's name: most likely {player}, who's playing this round, typing between shots"
        team = self.league.player_team.get(player)
        return (f"a league player's name: most likely {player}"
                + (f" of {self.league.team_name(team)}" if team else "") + ", not playing this round, watching")

    def _chat_context(self, watching: Iterable[str] = ()) -> list[str]:
        """What the booth knows to talk about the chat with: the round, the card, who's up and the
        last few things it said. `watching`: league players most likely in the chat."""
        r = self.round
        out = [*self._roster_facts(watching)]
        if course := (r.course if r is not None else None) or self.current_course:
            out.append(f"Course: {course}")
        if self.hole_number:
            out.append(f"They're on hole {self.hole_number}" + (f", a par {self.current_par}" if self.current_par else ""))
        if r is not None and (sides := [s.label for s in r.sides]):
            out.append(("Playing as teams: " if any(len(s.players) > 1 for s in r.sides) else "Playing: ")
                       + "; ".join(sides))
        if r is not None and (standing := r.standing_fact(r.seq - 1)):
            out.append(standing)
        if self._last_setup is not None and self._last_setup[1] == self._shot_seq:
            hud = self._last_setup[0]
            if hud.player_text and (who := self._player(hud.player_text)):
                out.append(self._setup_brief(hud, who, up=True, hole=self.hole_number, par=self.current_par))
        if said := [ln.text for ln in list(self.state.recent_lines)[-4:]]:
            out.append("The booth just said: " + " / ".join(said))
        return out

    async def _prepare_chat(self) -> None:
        r, talked = self.round, self._chat_talked
        log_ = list(self._chat_log)
        new = [(m, at) for m, at in log_ if m.id > talked]
        if not new:
            return
        # A shot struck before what a viewer was watching may not be recorded yet (its call is still
        # being read, or GSPro saved it only once the ball stopped), nor one struck since, which is
        # what came of the shot they saw set up: wait for them, briefly.
        deadline = time.monotonic() + self.CHAT_RECORD_WAIT
        while (any(m.watched is not None for m, _ in new) and time.monotonic() < deadline
               and self._shots_unrecorded):
            await asyncio.sleep(0.1)
        pending = min(self._shots_unrecorded.values(), default=None)
        for m, _ in log_:
            # Still not recorded: no shot for it this time (the last one recorded isn't it), a later take may.
            if m.shot is None and m.watched is not None and (pending is None or m.watched < pending):
                m.shot = self._chat_shot_watched(m.watched)
            if m.watched is not None and (pending is None or m.watched < pending):
                m.upcoming = self._chat_upcoming(m.watched, m.shot)
        members = {m.name: self._chat_member(m.name) for m, _ in log_}
        user, facts_text = chat_prompt(messages=[(m.name, m.text, m.id > talked, self._chat_shot_brief(m.shot),
                                                  m.upcoming, members[m.name]) for m, _ in log_],
                                       context=self._chat_context(self.league.chat_member(n)
                                                                  for n, note in members.items() if note),
                                       moderation=self.cfg.chat.moderation,
                                       commentators=self._persona_names, openers=self._chat_openers[-4:])
        try:
            good, _ = await self._write_aside("Chat", user, facts_text, chat_schema(self.speakers), kind="chat",
                                              llm=self.chat_llm, cap=self.cfg.chat.max_words,
                                              declined=lambda data: data.get("air") is False,
                                              # English words only: another language relies on the prompt.
                                              check=(lambda ls: chat_lead_in_problems(ls) + chat_hazard_denial_problems(ls))
                                              if self.cfg.commentary.language == "en"
                                              else None)
        except Exception as e:
            log.warning("Chat: couldn't write a take on the chat: %r", e)
            return
        if good and self._chat_talked == talked:
            kept, audio = await self._voice_ahead(good)
        if self._chat_talked != talked:
            # The chat went on air while this was written: what it hasn't covered is written up afresh.
            self._chat_written = self._chat_talked
            return
        if not good or len(kept) != 2 or self.round is not r or not self._chat_open():
            return
        self._chat_ready = PreparedChat([m for m, _ in new], list(zip(kept, audio)), time.monotonic(),
                                        min(at for _, at in new) + self.cfg.chat.max_age_seconds)
        log.info("Chat: a take on %d new message(s) is ready for the next gap", len(new))

    async def _chat_after_shot(self, seq: int) -> bool:
        """Air the take on the chat that's ready once this shot's call is done (once a hole, at most,
        and chat.min_gap_seconds apart). Returns whether it played."""
        if (self._chat_ready is None and not self._chat_writing()) or not self._chat_may_air(time.monotonic()):
            return False
        # The take is written while the ball flies and the call plays: one still being written gets
        # until the call's done, and a little after, to be ready.
        deadline = time.monotonic() + 8.0
        while ((not self.speech.is_idle() or (self._chat_ready is None and self._chat_writing()))
               and time.monotonic() < deadline and self._shot_seq == seq):
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.8)   # a beat after the call
        bit = self._chat_ready
        if bit is None or not self.speech.is_idle() or self._shot_seq != seq or self._chat_ready is not bit or not self._chat_open():
            return False
        if bit.expires <= time.monotonic():
            self._chat_ready, self._chat_written = None, self._chat_talked
            return False
        self._chat_ready, self._chat_aired_at, self._chat_aired_hole = None, time.monotonic(), self._chat_hole()
        self._chat_talked = max(self._chat_talked, bit.new[-1].id)
        self._chat_openers.append(bit.items[0][0].text)
        lines = [line for line, _ in bit.items]
        self.state.record_lines(lines)
        # Viewers hear their chat talked about whole: a shot struck meanwhile waits its turn.
        self._air_exchange(bit.items, banter=True, hold=True)
        self._write_transcript(lines, "chat")
        log.info("Chat: talked about %d new message(s) on air", len(bit.new))
        for m in bit.new:
            self._bg(self._chat_mark_aired(m.id))
        return True

    async def _chat_mark_aired(self, message_id: str) -> None:
        try:
            await asyncio.to_thread(self.chat.mark_aired, message_id)
        except Exception as e:
            log.info("Chat: couldn't mark a message answered: %s", e)

    async def _drain_speech(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.speech.is_idle():
            if deadline is not None and time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.2)

    async def _drain_shot_tasks(self, timeout: float) -> None:
        """Wait (bounded) for in-flight shot handlers to finish, so a hole's last reaction is queued
        before the wrap is, instead of the two talking over each other."""
        pending = {t for t in self._shot_tasks if not t.done() and t is not asyncio.current_task()}
        if pending:
            await asyncio.wait(pending, timeout=timeout)
