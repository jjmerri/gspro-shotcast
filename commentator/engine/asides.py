"""What the booth says between shots: news, stories, course trivia and live scores.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import datetime
import json
import time

from .common import NEWS_TRIGGER_TAGS, PreparedNews, log
from ..facts import course_listing_facts
from ..llm import LLMClient
from ..models import Line
from ..prompts import (
    NEWS_TRIGGERS, aside_room, news_prompt, news_schema, output_schema, parse_lines, retry_prompt, scores_prompt,
    trivia_prompt, trivia_schema, word_limit,
)
from ..trivia import looks_like_a_course


class AsidesMixin:
    """What the booth says between shots: news, stories, course trivia and live scores."""

    async def _league_page_midnight(self) -> None:
        """Re-write the league cards just after each midnight, from the board already read: a week whose
        deadline has passed is final, and its points count, without a new Squabbit read."""
        while True:
            now = datetime.datetime.now()
            nxt = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(0, 1))
            await asyncio.sleep((nxt - now).total_seconds())
            self._publish_league_page()

    async def _bookings_ready(self, timeout: float = 5.0, stale_after: float = 600.0) -> None:
        """Let a fetch in flight land before a round is classified, so it decides on fresh bookings.
        With no fetch for a while (no setup screen seen, e.g. preround off), fetch now."""
        if self.bookings is not None and time.monotonic() - self._bookings_fetched > stale_after:
            self._fetch_bookings("round start")
        if (task := self._bookings_task) and not task.done():
            await asyncio.wait({task}, timeout=timeout)

    def _weather_facts(self, rate: float = 1.0) -> list[str]:
        """The weather outside, when it's configured, fresh, and (between holes) this exchange's turn."""
        if self.weather is None or (rate < 1.0 and self.rng.random() >= rate):
            return []
        return self.weather.facts()

    async def _news_loop(self) -> None:
        """Keep the golf headlines fresh: fetch at startup, then re-fetch on a slow cadence."""
        while True:
            try:
                await self.news.refresh()
            except Exception:
                log.exception("Golf news refresh failed")
            await asyncio.sleep(max(self.cfg.news.refresh_hours, 0.1) * 3600)

    def _aside_slots(self, holes: int) -> int:
        """How many asides a round of this length has airtime for. _news_pick plays at most one every
        three holes, never on the first hole or the last two: five across an 18, two across a nine."""
        return max(1, len(range(2, max(holes - 1, 3), 3)))

    def _news_wanted(self, r) -> int:
        """News's own ceiling for a round: its configured share, never more than the airtime."""
        nines = max(1, -(-r.holes // 9))
        return max(1, min(self.cfg.news.per_nine * nines, self._aside_slots(r.holes)))

    def _nine_slots(self, r, nine: int) -> int:
        """The asides this nine (0 = the round's first) still has airtime for from the current hole:
        _aside_slots' one-every-three-holes rhythm, counted from here to the nine's end."""
        lo, hi = max(9 * nine + 1, r.seq), 9 * nine + 9
        return sum(1 for h in range(2, max(r.holes - 1, 3), 3) if lo <= h <= hi)

    def _split_asides(self, r, room: int) -> tuple[int, int]:
        """(trivia, news) to write for a nine into the airtime live scores left: taken in turn, news
        first; news up to its per-nine share, trivia up to what's left of its per-round share. Every
        aside is written with the news model, and a nine has airtime for only so many (see
        _aside_slots), so one written past that is money never heard."""
        caps = [max(0, self.cfg.trivia.per_round - self._trivia_planned) if self.trivia is not None else 0,
                self.cfg.news.per_nine if self.news is not None else 0]
        got = [0, 0]
        while room > 0 and (got[0] < caps[0] or got[1] < caps[1]):
            for i in (1, 0):
                if room > 0 and got[i] < caps[i]:
                    got[i] += 1
                    room -= 1
        return got[0], got[1]

    def _maybe_prep_asides(self, r) -> None:
        """At the second hole played of each nine, start writing that nine's asides. Written then,
        not at the round start, a false start or a round quit early pays for none (or only the nine
        it reached): these are the news model's, among the dearest calls the booth makes, and no aside
        can play on a nine's first hole anyway."""
        if not self._asides_on() or r.ended or r.seq < 2:
            return
        nine = (r.seq - 1) // 9
        if nine in self._aside_nines or r.seq < 9 * nine + 2:
            return
        self._aside_nines.add(nine)
        self._aside_task(self._news_prep(r, nine), "aside-prep")

    async def _news_prep(self, r, nine: int = 0) -> None:
        """Plan and write one nine's asides. Live scores claim their check-ins first (they're only
        planned when something is on), then news and trivia share what's left of the nine's airtime,
        less any aside from earlier in the round that's still waiting to air."""
        now = time.monotonic()
        # Still to air from earlier: written asides, score check-ins planned (a deferred one can land
        # in this nine), and check-ins being written right now.
        waiting = (sum(1 for b in self._news_bits if b.expires is None or b.expires > now)
                   + len(self._score_holes)
                   + sum(1 for t in self._aside_tasks if t.get_name() == "scores-prep" and not t.done()))
        slots = self._nine_slots(r, nine)
        room = slots - waiting
        if room > 0 and self.scores is not None:
            room -= await self._plan_scores(r, room, last_hole=9 * nine + 9)
        trivia, news = self._split_asides(r, max(0, room))
        self._trivia_planned += trivia
        log.info("Asides for nine %d (from round hole %s): %d slot(s), %d still waiting; %d trivia, %d news",
                 nine + 1, r.seq, slots, waiting, trivia, news)
        if self.round is not r or r.ended:
            return
        if trivia:
            self._aside_task(self._prepare_trivia(r, trivia), "trivia-prep")
        if news and self.news is not None:
            await self._prepare_news(r, news)

    async def _prepare_news(self, r, n: int | None = None) -> None:
        """Write and voice this round's golf-world asides up front, with the round's richest model, so
        each plays instantly and whole when its moment comes (a live one was thin, and its reply could
        miss its voice deadline and leave the opener hanging)."""
        if not self.news.headlines:
            await self.news.refresh()
        n = self._news_wanted(r) if n is None else n
        # Nobody hears the same story twice: skip any that someone in this round already heard.
        listeners = self._listeners(r)
        heard = self.store.news_heard(listeners) if listeners else self._news_used
        picks = self.news.pick_many(self.rng, n, heard)
        # One at a time, each shown the openings already written, so they don't all start "did you see".
        for i, h in enumerate(picks):
            if self.round is not r or r.ended:   # replaced or quit mid-batch: don't pay for another
                log.info("News prep stopped after %d of %d asides: the round is over", i, len(picks))
                return
            try:
                bit = await self._prepare_story(h, [b.lines[0].text for b in self._news_bits])
            except Exception as e:
                log.warning("News aside on %r failed: %r", h.title, e)
                continue
            if bit is not None and self.round is r:
                self._news_bits.append(bit)
        log.info("News: %d of %d asides ready for this round: %s", len(self._news_bits), len(picks),
                 "; ".join(f"{b.title!r} ({b.trigger or 'any quiet moment'})" for b in self._news_bits))

    async def _write_aside(self, label: str, user: str, facts_text: str, schema: dict, kind: str = "news",
                           llm: LLMClient | None = None, declined=None, check=None,
                           cap: int | None = None) -> tuple[list[Line], dict]:
        """A two-line exchange from the news model (or `llm`), checked against its facts; ([], {}) if
        none. `declined(data)`: the model chose to write nothing, so don't retry. `check(lines)`: any
        further problems with the two lines, to retry on. `cap`: each line's word limit, if not
        commentary.aside_max_words."""
        cap = cap or self.cfg.commentary.aside_max_words
        # The system prompt's shorter per-line limit is for live calls; an aside gets its own.
        user = user + aside_room(cap)
        prompt, data = user, {}
        for _ in range(2):
            raw = await self._complete(kind, llm or self.news_llm, prompt, [], schema=schema, system=self.system)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            if declined is not None and isinstance(data, dict) and declined(data):
                log.info("%s: the model chose not to air it", label)
                return [], {}
            lines = parse_lines(raw, self.speakers, cap, self._persona_names)
            good, problems = self._grounded(lines, facts_text, mode="news")
            # An over-long line is cut back to its last full sentence, which can leave just a teaser
            # ("Interesting bit of news this morning, Duke.") with the story itself gone.
            items = data.get("lines") if isinstance(data, dict) else None
            if long := [i + 1 for i, item in enumerate(items if isinstance(items, list) else [])
                        if isinstance(item, dict) and len(str(item.get("text", "")).split()) > word_limit(cap)]:
                problems = problems + [f"line {', '.join(map(str, long))} ran over {cap} words: say each line, "
                                       f"story included, in at most {cap} words"]
            if len(lines) != 2 or len(good) != 2 or good[0].speaker == good[1].speaker:
                problems = problems + ["write exactly two lines, one from each commentator"]
            elif len(good[1].text.split()) < 6:
                problems = problems + ["the reply must give a take of its own, in at least one full sentence"]
            elif check is not None:
                problems = problems + check(good)
            if not problems:
                return good, data
            self.stats["retries"] += 1
            prompt = retry_prompt(user, problems)
        log.info("%s dropped: no usable exchange (%s)", label, "; ".join(problems))
        return [], {}

    def _aside_openings(self) -> list[str]:
        return [b.lines[0].text for b in self._news_bits]

    async def _prepare_story(self, h, openings: list[str] = ()) -> PreparedNews | None:
        user, facts_text = news_prompt(topic=h.as_fact(), openings=openings)
        good, data = await self._write_aside(f"News aside on {h.title!r}", user, facts_text,
                                             news_schema(self.speakers))
        if not good:
            return None
        trigger = data.get("trigger") if data.get("trigger") in NEWS_TRIGGERS else None
        tie = None
        if trigger and (text := " ".join(str(data.get("tie_in") or "").split())):
            ok, _ = self._grounded([Line(speaker=good[0].speaker, text=text)], facts_text, mode="news")
            tie = ok[0] if ok else None
        kept, audio = await self._voice_ahead(good)
        if len(kept) != 2:
            log.info("News aside on %r dropped: it couldn't be voiced", h.title)
            return None
        tie_audio = None
        if tie is not None:
            voiced, clips = await self._voice_ahead([tie])
            tie_audio = clips[0] if voiced else None
            tie = tie if voiced else None
        if tie is None:
            trigger = None   # no bridge to play, so it airs on any quiet moment instead
        return PreparedNews(h.title, trigger, kept, audio, tie, tie_audio)


    def _asides_on(self) -> bool:
        return self.news is not None or self.scores is not None or self.trivia is not None

    def _aside_task(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._aside_tasks.add(task)
        task.add_done_callback(lambda t: (self._aside_tasks.discard(t), t.cancelled() or t.exception()))

    def _start_round_asides(self, r) -> None:
        """Cancel the last round's aside work; _maybe_prep_asides starts this round's, nine by nine."""
        for task in list(self._aside_tasks):
            task.cancel()
        self._score_holes = []
        self._aside_nines, self._trivia_planned = set(), 0

    def _course_names(self, r) -> list[str]:
        """What the round's course goes by, best first: GSPro's own display name and course id (from its
        log, when it's on) bracket the scorecard's name, which is OCR and can be screen text."""
        display = id_ = None
        if self._log_truth and self.gspro.mode == "course":
            display, id_ = self.gspro.course_display, self.gspro.course
        return [n for n in (display, r.course or self.current_course, id_) if n]

    async def _prepare_trivia(self, r, n: int | None = None) -> None:
        """Write and voice this round's course-trivia asides up front, from the real course's article,
        steering clear of what earlier rounds here already told."""
        names = self._course_names(r)
        course = next((n for n in names if looks_like_a_course(n)), None) or r.course or self.current_course
        found = await self.trivia.article(*names)
        if not found or self.round is not r:
            return
        title, article = found
        told = self.store.recent_asides("trivia", title)
        listing = course_listing_facts(r.course_card)   # GSPro's notes too
        made = 0
        for _ in range(max(0, self.cfg.trivia.per_round if n is None else n)):
            user, facts_text = trivia_prompt(course=course, title=title, article=article, told=told,
                                             openings=self._aside_openings(), listing=listing)
            try:
                good, data = await self._write_aside(f"Trivia on {title!r}", user, facts_text,
                                                     trivia_schema(self.speakers))
            except Exception as e:
                log.warning("Trivia on %r failed: %r", title, e)
                continue
            if not good:
                continue
            told = told + [" ".join(l.text for l in good)]
            kept, audio = await self._voice_ahead(good)
            if len(kept) != 2 or self.round is not r:
                continue
            try:
                hole = int(data.get("hole") or 0)
            except (TypeError, ValueError):
                hole = 0
            self._news_bits.append(PreparedNews(title, None, kept, audio, kind="trivia",
                                                hole=hole if 1 <= hole <= 18 else None, topic=title))
            made += 1
        log.info("Trivia: %d asides ready on %r: %s", made, title,
                 "; ".join(f"hole {b.hole}" if b.hole else "any quiet moment"
                           for b in self._news_bits if b.kind == "trivia"))

    async def _plan_scores(self, r, room: int | None = None, last_hole: int | None = None) -> int:
        """Pick the holes a live score is written for, if anything's on tonight, at most `room` of
        them, from the next hole up to `last_hole` (a nine's end), and return how many. Only the plan
        is made now: each score is fetched as its hole starts, so it isn't stale by the time it airs."""
        items = await self.scores.fetch()
        if not items or self.round is not r:
            log.info("Scores: nothing live or on tonight; no score asides this round")
            return 0
        n = max(0, self.cfg.scores.per_nine) * (1 if last_hole else max(1, -(-r.holes // 9)))
        if room is not None:
            n = min(n, room)
        # The asides' window: not the first hole or the last two, and not this hole, already started.
        first, last = max(2, r.seq + 1), min(r.holes - 2, last_hole or r.holes)
        if n <= 0 or last < first:
            return 0
        # One hole from each equal stretch of the window, so two check-ins aren't back to back.
        span = (last - first + 1) / n
        holes = []
        for i in range(n):
            lo, hi = int(first + i * span), int(first + (i + 1) * span) - 1
            if i < n - 1:
                hi -= 2       # room before the next stretch's hole
            hole = self.rng.randint(lo, max(lo, hi))
            if not holes or hole - holes[-1] >= 3:
                holes.append(hole)
        self._score_holes = sorted(self._score_holes + holes)
        log.info("Scores: %d on tonight (%s); a check-in on round hole(s) %s", len(items),
                 "; ".join(i.key for i in items), holes)
        return len(holes)

    def _score_hole_started(self, r) -> None:
        due = [h for h in self._score_holes if h <= r.seq]
        if not due or self.scores is None:
            return
        self._score_holes = [h for h in self._score_holes if h > r.seq]
        # An aside aired too recently for this one to play on this hole: move it to the first hole it
        # can, rather than write up a score that will go stale waiting.
        if self._news_aired and r.seq - self._news_aired[-1] < 3:
            later = self._news_aired[-1] + 3
            if later <= r.holes - 2 and later not in self._score_holes:
                self._score_holes = sorted(self._score_holes + [later])
                log.info("Scores: check-in moved to round hole %s (an aside just aired)", later)
            return
        self._aside_task(self._prepare_score(r), "scores-prep")

    async def _prepare_score(self, r) -> None:
        """Fetch the scores now, as the hole starts, and write and voice a check-in on the best one."""
        items = await self.scores.fetch()
        now = time.monotonic()
        pending = {b.topic for b in self._news_bits if b.kind == "score" and (b.expires or now + 1) > now}
        # Nothing already waiting to air, and nothing unchanged since it was last written up.
        fresh = [i for i in items if i.key not in pending and f"{i.key}@{i.version}" not in self._score_written]
        # Something the booth hasn't touched tonight first; then an update on one that's moved.
        fresh.sort(key=lambda i: (i.key in self._score_said, i.rank))
        if not fresh or self.round is not r:
            log.info("Scores: nothing new to check in on at hole %s", r.seq)
            return
        item = fresh[0]
        earlier = self._score_said.get(item.key, [])
        user, facts_text = scores_prompt(item=item.fact, earlier=earlier, openings=self._aside_openings())
        good, _ = await self._write_aside(f"Score check-in on {item.key}", user, facts_text,
                                          output_schema(self.speakers))
        if not good:
            return
        kept, audio = await self._voice_ahead(good)
        if len(kept) != 2 or self.round is not r:
            return
        self._score_written.add(f"{item.key}@{item.version}")
        self._news_bits.append(PreparedNews(item.key, None, kept, audio, kind="score", topic=item.key,
                                            expires=time.monotonic() + self.cfg.scores.max_age_minutes * 60))
        log.info("Scores: check-in on %s ready for hole %s: %s", item.key, r.seq, item.fact)

    def _round_moment_tags(self, r) -> set[str]:
        """Tags for what the ROUND just did, as opposed to what the ball did: a birdie or a blow-up on
        the hole just finished, and the turn. Golf news is mostly about scoring and the state of a
        round rather than ball flight, so without these an aside has almost nothing to land on."""
        tags: set[str] = set()
        if r is None:
            return tags
        if r.holes >= 18 and r.seq == r.holes // 2 + 1:
            tags.add("at_the_turn")
        last = r.seq - 1
        if last < 1 or not (par := r.par.get(last)):
            return tags
        for side in r.sides:
            if (score := r.card_score(side, last)) is None:
                continue
            if score - par <= -1:
                tags.add("birdie_posted")
            elif score - par >= 2:
                tags.add("blow_up_posted")
        return tags

    def _news_pick(self, moment: dict) -> tuple[PreparedNews, bool] | None:
        """The aside to play after this shot, if any, and whether it airs on its trigger. News, trivia
        and live scores are one bucket: on an ordinary shot, one picked at random from those ready. At
        most one every three holes, never on the first hole or the last two. A news aside held for a
        kind of shot (or for the state of the round) plays on that moment, and waits for it until the
        round is well along. A trivia story about one hole is only ready on that hole, wherever it
        falls (two holes after the last aside at least)."""
        r = self.round
        if not self._news_bits or r is None or r.ended or not r.seq:
            return None
        now = time.monotonic()
        self._news_bits = [b for b in self._news_bits if b.expires is None or b.expires > now]
        gap = r.seq - self._news_aired[-1] if self._news_aired else r.seq + 99
        open_hole = r.seq >= 2 and r.holes - r.seq >= 2 and gap >= 3
        tags = set(moment["tags"])
        if open_hole:
            for bit in self._news_bits:
                if bit.trigger and tags & NEWS_TRIGGER_TAGS[bit.trigger]:
                    return bit, True
        if not moment["routine"]:
            return None
        late = r.seq >= -(-r.holes * 3 // 5)
        pool = [b for b in self._news_bits
                if (b.hole is not None and b.hole == self.hole_number and gap >= 2)
                or (b.hole is None and open_hole and (b.trigger is None or late))]
        if not pool or self.rng.random() > self.cfg.news.banter_rate:
            return None
        return self.rng.choice(pool), False

    async def _news_after_shot(self, moment: dict) -> bool:
        """Play a prepared aside once this shot's call is done, if one suits the moment. Returns
        whether it played."""
        if (pick := self._news_pick(moment)) is None:
            return False
        bit, on_trigger = pick
        r, seq, hole = self.round, self.round.seq, self.hole_number
        deadline = time.monotonic() + 8.0
        while not self.speech.is_idle() and time.monotonic() < deadline and self._shot_seq == moment["seq"]:
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.8)   # a beat after the call, so the aside doesn't run straight on from it
        if not self.speech.is_idle() or self._shot_seq != moment["seq"] or bit not in self._news_bits:
            return False
        # The hole can end in the wait (a conceded putt, the round's last hole) without another shot.
        if not self.commentary_on or self.speech.muted:
            return False    # off air: keep it, since nobody would hear it
        if self.round is not r or r.ended or r.seq != seq or self.hole_number != hole \
                or (bit.expires is not None and bit.expires <= time.monotonic()):
            return False
        # The roster can change mid-round (a guest named, a scramble regrouped): recheck who's listening.
        if bit.kind == "news":
            listeners = self._listeners(r)
            if bit.title in (self.store.news_heard(listeners) if listeners else self._news_used):
                self._news_bits.remove(bit)
                return False
        first = (bit.tie, bit.tie_audio) if on_trigger else (bit.lines[0], bit.audio[0])
        items = [first, (bit.lines[1], bit.audio[1])]
        self._news_bits.remove(bit)
        self._news_aired.append(self.round.seq)
        lines = [line for line, _ in items]
        self.state.record_lines(lines)
        self._air_exchange(items, banter=True)
        self._write_transcript(lines, "banter")
        if bit.kind == "trivia":
            self.store.record_aside("trivia", bit.topic, " ".join(l.text for l in lines))
            log.info("Trivia aside%s: %r", f" on hole {bit.hole}" if bit.hole else "", bit.title)
        elif bit.kind == "score":
            self._score_said.setdefault(bit.topic, []).append(" ".join(l.text for l in lines))
            self.store.record_aside("score", bit.topic, " ".join(l.text for l in lines))
            log.info("Score check-in: %r", bit.title)
        else:
            self._news_used.add(bit.title)
            self.store.record_news_heard(self._listeners(self.round), bit.title)
            log.info("News aside%s: %r", f" on a {bit.trigger.replace('_', ' ')}" if on_trigger else "", bit.title)
        return True

    @staticmethod
    def _listeners(r) -> list[str]:
        """Everyone playing this round (both partners on a one-ball team), by roster name."""
        return sorted({p for side in getattr(r, "sides", None) or [] for p in side.players})
