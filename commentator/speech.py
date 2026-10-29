import asyncio
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field

from .config import Config
from .models import Line
from .tts import TTS, Audio, StreamedAudio
from .usage import tts_cost

log = logging.getLogger(__name__)

WORDS_PER_SECOND = 2.7
# The audio device powers down when idle; the first clip after a gap loses its opening moments
# while it wakes (the opener, minutes into silence, was starting mid-word). Lead silence after a
# gap absorbs the wake-up instead of the speech.
_IDLE_GAP_SECONDS = 5.0
_WAKE_LEAD_SECONDS = 0.6
_last_play_end = 0.0
# Requests per line, all told (hedges and replacements for errored ones): caps a fast-failing error
# (bad key, outage) at a handful of calls.
_MAX_REQUESTS = 5
# A streamed clip that goes this long without another block after it started is cut off there.
_STREAM_GAP_SECONDS = 3.0
# A live line that takes longer than this from being queued to starting to play is logged.
_SLOW_LINE_SECONDS = 1.5


async def play(audio: Audio) -> None:
    import numpy as np
    import sounddevice as sd

    global _last_play_end
    lead = _WAKE_LEAD_SECONDS if time.monotonic() - _last_play_end > _IDLE_GAP_SECONDS else 0.0
    if isinstance(audio, StreamedAudio):
        await _play_stream(audio, lead)
        _last_play_end = time.monotonic()
        return
    samples = audio.samples
    if lead:
        pad = np.zeros(int(audio.sample_rate * lead), dtype=samples.dtype)
        samples = np.concatenate([pad, samples])

    def _play() -> None:
        sd.play(samples, audio.sample_rate)
        sd.wait()

    await asyncio.to_thread(_play)
    _last_play_end = time.monotonic()


async def _play_stream(audio: StreamedAudio, lead: float) -> None:
    """Play a clip as it downloads, in small blocks so an interrupt cuts it off within ~50 ms.
    Playback waits for `audio.prebuffer` seconds of audio (or the whole clip, if shorter): the stream
    arrives in uneven bursts, and starting on its first block (often a few ms of sound) runs the
    sound card dry within a few tenths of a second on most lines."""
    import numpy as np
    import sounddevice as sd

    stop = threading.Event()
    block = max(1, audio.sample_rate // 20)
    need = int(audio.prebuffer * audio.sample_rate)
    underruns = 0

    def _run() -> None:
        nonlocal underruns
        blocks = audio.blocks(stop)
        held: list = []
        for chunk in blocks:
            held.append(chunk)
            if sum(len(c) for c in held) >= need:
                break
        if stop.is_set() or not held:
            return
        with sd.OutputStream(samplerate=audio.sample_rate, channels=1, dtype="float32") as out:
            if lead:
                out.write(np.zeros((int(audio.sample_rate * lead), 1), dtype="float32"))
            first = True
            for chunk in itertools.chain(held, blocks):
                for i in range(0, len(chunk), block):
                    if stop.is_set():
                        out.abort()
                        return
                    piece = chunk[i:i + block]
                    # write() reports whether the card ran dry since the last write (a gap in the voice).
                    if out.write(piece.reshape(-1, 1)) and not first:
                        underruns += 1
                    first = False
                    audio.played += len(piece)

    try:
        await asyncio.to_thread(_run)
    finally:
        stop.set()
        if underruns:
            log.info("Streamed clip ran dry %s time(s) while playing (%.1fs of %.1fs played)",
                     underruns, audio.played / audio.sample_rate, audio.seconds)


def _audio_seconds(audio: Audio | None) -> float:
    """Playback length of a synthesized clip, for the cost estimate (TTS is billed by audio time)."""
    if audio is None:
        return 0.0
    if isinstance(audio, StreamedAudio):
        return audio.seconds
    try:
        return len(audio.samples) / audio.sample_rate
    except Exception:
        return 0.0


def stop_playback() -> None:
    try:
        import sounddevice as sd

        sd.stop()
    except Exception:
        pass


def _aired_seconds(audio: Audio | None) -> float:
    """How much of a clip reached the speakers: a streamed clip cut off mid-line counts only the
    part played (it may have downloaded all of it)."""
    if isinstance(audio, StreamedAudio):
        return audio.played / audio.sample_rate
    return _audio_seconds(audio)


@dataclass
class _Queued:
    """A live line waiting its turn. On a network voice its synth starts as soon as it's queued, so a
    stalled line doesn't hold up voicing the ones behind it; playback still goes in queue order."""

    gen: int
    line: Line
    deadline: float | None  # time.monotonic() by which it must be playing, or None
    exchange: int  # lines from one say() call: lose one and the replies after it go too
    task: asyncio.Task | None = None
    queued: float = field(default_factory=time.monotonic)


def _discard(item: _Queued) -> None:
    """Stop a dropped line's voicing: cancel its synth, or if that already returned a clip that is
    still streaming, stop the download."""
    task = item.task
    if task is None:
        return
    if not task.done():
        task.cancel()
    elif not task.cancelled() and task.exception() is None:
        _, audio = task.result()
        if isinstance(audio, StreamedAudio):
            audio.cancel()


class SpeechQueue:
    """Synthesizes the next line while the current one plays, so the booth doesn't pause between speakers."""

    def __init__(self, cfg: Config, tts: TTS, player=play, stopper=stop_playback) -> None:
        self.cfg = cfg
        self.tts = tts
        # Respells names for the voice (League.spoken); the engine sets it from the league file.
        self.spoken = lambda text: text
        self._play = player
        self._stop = stopper
        # A line past its deadline is dropped, never played late.
        self._text_q: asyncio.Queue[_Queued] = asyncio.Queue()
        # (generation, line, audio, deadline, when a live line was queued or None for a prepared one)
        self._audio_q: asyncio.Queue[tuple[int, Line, Audio | None, float | None, float | None]] = asyncio.Queue(maxsize=1)
        # The synth the loop is waiting on; interrupt() cancels it (queued ones are cancelled as they're
        # drained) so a stalled request can't hold up the line that superseded it.
        self._synth_task: asyncio.Task | None = None
        self._exchange = 0
        self._lost_exchange: int | None = None
        # Commentary off: nothing is voiced or played.
        self.muted = False
        # Lines accepted but not yet finished playing, including the ones mid-synthesis or mid-playback.
        self._pending = 0
        # Bumped by interrupt(); lines from an older generation are skipped wherever they are.
        self._generation = 0
        self._playing: asyncio.Task | None = None
        # The pause before a line (tts.turn_gap_seconds / line_gap_seconds), cancelled by interrupt().
        self._gap: asyncio.Task | None = None
        # When the last line finished airing and who said it; a line after a longer silence gets no gap.
        self._last_end: float | None = None
        self._last_speaker: str | None = None
        # The generation of the prepared sequence (wrap/closer) currently feeding the audio queue, or
        # None when none is. The synth loop and the feeder both park on the maxsize-1 audio queue and
        # would otherwise alternate, splicing a concurrent say() (a late reaction) into the middle of
        # the exchange; while a sequence owns this the synth loop drops its output instead of
        # competing. It's the owning generation, not a bool, so a plain interrupt (which bumps the
        # generation but not this) is still cleared by the feeder, and an overlapping sequence keeps
        # ownership until it finishes.
        self._sequencing: int | None = None
        # The generation an aside (news, trivia, scores) is airing in: a new shot doesn't drop the rest
        # of it, and the engine holds a routine shot's calls until it's done (it's long, and paid for).
        self._banter_gen: int | None = None
        self._banter_lines: set[int] = set()   # id()s of its lines still to air
        # Per-session audio tally, for the cost estimate. "synth" counts every clip actually run
        # through TTS (including pre-voiced branches that never air); "spoken" counts only the clips
        # that reached the speakers. The gap is the pre-voicing waste. Non-blocking dict increments.
        self.audio_stats = {
            "synth_clips": 0, "synth_chars": 0, "synth_seconds": 0.0,
            "spoken_clips": 0, "spoken_chars": 0, "spoken_seconds": 0.0,
        }
        # Where per-request TTS rows and per-line air rows go (the engine's UsageLog), for the cost and
        # latency summary (`commentator cost`). None: not recorded.
        self.usage = None

    def _record(self, row: dict) -> None:
        if self.usage is None:
            return
        try:
            self.usage.write(row)
        except Exception as e:
            log.debug("Usage row not written: %s", e)

    def _record_tts(self, purpose: str, attempt: int, chars: int, outcome: str, started: float,
                    first: float | None, audio_s: float) -> None:
        model = self.cfg.tts.openai_model if self.cfg.tts.provider == "openai" else self.cfg.tts.provider
        self._record({
            "kind": "tts", "provider": self.cfg.tts.provider, "model": model, "purpose": purpose,
            "attempt": attempt, "chars": chars, "outcome": outcome,
            "first_audio_s": round(first, 3) if first is not None else None,
            "total_s": round(time.monotonic() - started, 3), "audio_s": round(audio_s, 2),
            "est_cost_usd": round(tts_cost(model, chars, audio_s), 6),
        })

    @property
    def _streaming(self) -> bool:
        return bool(self.cfg.tts.stream and getattr(self.tts, "remote", False) and hasattr(self.tts, "stream"))

    @property
    def prevoicing(self) -> bool:
        """Whether lines that might not air (setup branches, wrap/closer branches, asides) should be
        voiced ahead. Always on a local voice (free); on a cloud voice only with tts.prevoice."""
        return self.cfg.tts.prevoice or not getattr(self.tts, "remote", False)

    def _count_synth(self, text: str, audio: Audio | None) -> None:
        if audio is None:  # console/local-silent providers cost nothing to "synthesize"
            return
        self.audio_stats["synth_clips"] += 1
        self.audio_stats["synth_chars"] += len(text)
        self.audio_stats["synth_seconds"] += _audio_seconds(audio)

    def _count_spoken(self, text: str, audio: Audio | None) -> None:
        if audio is None:
            return
        self.audio_stats["spoken_clips"] += 1
        self.audio_stats["spoken_chars"] += len(text)
        self.audio_stats["spoken_seconds"] += _aired_seconds(audio)

    def say(self, lines: list[Line], interrupt: bool = False, deadline: float | None = None,
            banter: bool = False) -> None:
        """Queue lines to voice and play. `deadline` (a time.monotonic()) is when they stop being worth
        saying; each line is also dropped if its voice isn't ready within live_deadline_seconds."""
        if self.muted:
            return
        if interrupt:
            self.interrupt()
            if banter:
                self._banter_gen, self._banter_lines = self._generation, {id(line) for line in lines}
        else:
            # Commentary on a shot from two shots ago is worse than none.
            backlog = self._text_q.qsize() + self._audio_q.qsize()
            if backlog >= self.cfg.commentary.max_speech_backlog:
                log.info("Speech backlog %s; dropping %s queued lines", backlog, self._text_q.qsize())
                self._drain(self._text_q)
        self._exchange += 1
        eager = getattr(self.tts, "concurrent", False)  # a local CPU voice goes one line at a time
        for line in lines:
            item = _Queued(self._generation, line, deadline, self._exchange)
            if eager:
                self._start_synth(item)
            self._text_q.put_nowait(item)
            self._pending += 1

    def _start_synth(self, item: _Queued) -> None:
        # A live line waits at most live_deadline_seconds for its voice (or until its own deadline).
        until = time.monotonic() + self.cfg.tts.live_deadline_seconds
        if item.deadline is not None:
            until = min(until, item.deadline)
        item.task = asyncio.create_task(self._synthesize(item.line, until, live=True))

    def _voice_for(self, line: Line) -> tuple[str, str | None]:
        voices = self.cfg.tts.active_voices
        voice = voices.get(line.speaker) or next(iter(voices.values()))
        return voice, self.cfg.tts.active_instructions.get(line.speaker)

    async def _synthesize(self, line: Line, until: float | None = None, live: bool = False) -> tuple[bool, Audio | None]:
        """Voice a line, returning (ok, audio). A cloud voice's requests either answer in a second or
        two or stall for 30 s+, so a request still unanswered after hedge_after_seconds gets a second
        one alongside it (the first isn't killed; a slow one that would have worked still can) and
        whichever answers first wins. It keeps hedging, at most two requests in flight, until `until`
        (a time.monotonic()), which defaults to prepare_deadline_seconds for a line voiced ahead."""
        voice, instructions = self._voice_for(line)
        text = self.spoken(line.text)
        if not getattr(self.tts, "remote", False):
            try:
                audio = await self.tts.synthesize(text, voice, instructions)
            except Exception as e:
                log.warning("TTS failed (%s: %s): %s", type(e).__name__, e, line.text)
                return False, None
            self._count_synth(line.text, audio)
            return True, audio

        cfg = self.cfg.tts
        started = time.monotonic()
        if until is None:
            until = started + cfg.prepare_deadline_seconds
        if self._streaming:
            return await self._synthesize_streamed(line, text, voice, instructions, until, live)
        inflight: list[tuple[asyncio.Task, float]] = []  # (request, when it was sent), oldest first
        sent = 0
        purpose = "live" if live else "ahead"

        async def request(n: int) -> Audio | None:
            at, outcome, audio = time.monotonic(), "unanswered", None
            try:
                audio = await self.tts.synthesize(text, voice, instructions)
                outcome = "ok"
                return audio
            except asyncio.CancelledError:
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                self._record_tts(purpose, n, len(text), outcome, at,
                                 time.monotonic() - at if outcome == "ok" else None, _audio_seconds(audio))

        def send() -> None:
            nonlocal sent
            sent += 1
            inflight.append((asyncio.create_task(request(sent)), time.monotonic()))

        try:
            while True:
                now = time.monotonic()
                if now >= until - 0.05:
                    break
                if not inflight:
                    if sent >= _MAX_REQUESTS:
                        break
                    send()  # the first request, or a replacement for one that errored
                can_hedge = sent < _MAX_REQUESTS
                tick = min(inflight[-1][1] + cfg.hedge_after_seconds, until) if can_hedge else until
                done, _ = await asyncio.wait([t for t, _ in inflight], timeout=max(tick - time.monotonic(), 0),
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    inflight = [(t, at) for t, at in inflight if t is not task]
                    if (err := task.exception()) is None:
                        audio = task.result()
                        if sent > 1:
                            log.info("TTS answered in %.1fs after %s requests: %s",
                                     time.monotonic() - started, sent, line.text)
                        self._count_synth(line.text, audio)
                        return True, audio
                    log.warning("TTS request failed (%s: %s): %s", type(err).__name__, err, line.text)
                if done or not can_hedge or time.monotonic() >= until - 0.05:
                    continue
                if len(inflight) >= 2:  # keep two in flight: drop the oldest, long since stalled
                    old, _ = inflight.pop(0)
                    old.cancel()
                log.info("TTS slow (%.1fs); hedging with another request: %s", time.monotonic() - started, line.text)
                send()
        finally:
            for task, _ in inflight:
                task.cancel()
        log.warning("TTS gave up after %.1fs (%s requests): %s", time.monotonic() - started, sent, line.text)
        return False, None

    async def _synthesize_streamed(self, line: Line, text: str, voice: str, instructions: str | None,
                                   until: float, live: bool) -> tuple[bool, Audio | None]:
        """Stream a line. A request with no audio within first_audio_timeout_seconds (or one that
        errors) gets `retries` more sent alongside it; the first to start is kept and the rest are
        cancelled. A live line returns as soon as its audio starts (it plays while it downloads); a
        line voiced ahead waits for the whole clip. Gives up at `until`."""
        cfg = self.cfg.tts
        started = time.monotonic()
        attempts: list[StreamedAudio] = []
        last_sent = started
        purpose = "live" if live else "ahead"

        def send() -> None:
            nonlocal last_sent
            clip = StreamedAudio(self.tts.stream_sample_rate)
            clip.prebuffer = cfg.stream_prebuffer_seconds
            clip.task = asyncio.create_task(
                self._stream_into(clip, text, voice, instructions, len(attempts) + 1, purpose))
            attempts.append(clip)
            last_sent = time.monotonic()

        winner: StreamedAudio | None = None
        kept = False
        send()
        try:
            while True:
                winner = next((a for a in attempts if a.started.is_set()), None)
                now = time.monotonic()
                if winner is not None or now >= until:
                    break
                waiting = [a for a in attempts if not a.finished.is_set()]
                if len(attempts) <= cfg.retries and (
                        not waiting or now - last_sent >= cfg.first_audio_timeout_seconds):
                    log.info("TTS %s after %.1fs; retrying: %s", "no audio" if waiting else "failed",
                             now - started, line.text)
                    send()
                    continue
                if not waiting:
                    break   # every request failed and the retries are spent
                await asyncio.sleep(0.02)
            if winner is None:
                log.warning("TTS gave up after %.1fs (%s requests): %s", time.monotonic() - started,
                            len(attempts), line.text)
                return False, None
            if len(attempts) > 1:
                log.info("TTS started in %.1fs on request %s of %s: %s", time.monotonic() - started,
                         attempts.index(winner) + 1, len(attempts), line.text)
            for a in attempts:
                if a is not winner:
                    a.cancel()
            if live:
                kept = True
                return True, winner
            try:
                await asyncio.wait_for(winner.finished.wait(), max(until - time.monotonic(), 0.0))
            except TimeoutError:
                log.warning("TTS clip still downloading at its deadline (%.1fs): %s",
                            time.monotonic() - started, line.text)
                return False, None
            if not winner.ok:
                log.warning("TTS stream broke off while voicing ahead: %s", line.text)
                return False, None
            return True, Audio(samples=winner.samples, sample_rate=winner.sample_rate)
        finally:
            for a in attempts:
                if not (kept and a is winner):
                    a.cancel()

    async def _stream_into(self, clip: StreamedAudio, text: str, voice: str, instructions: str | None,
                           attempt: int, purpose: str) -> None:
        """Download one streamed request into `clip`, recording it as a usage row when it ends."""
        started = time.monotonic()
        first: float | None = None
        outcome = "error"
        stream = self.tts.stream(text, voice, instructions)
        try:
            while True:
                try:
                    async with asyncio.timeout(None if first is None else _STREAM_GAP_SECONDS):
                        block = await anext(stream)
                except StopAsyncIteration:
                    outcome = "ok"
                    break
                except TimeoutError:
                    outcome = "broke_off"
                    log.warning("TTS stream stalled mid-clip after %.1fs of audio: %s", clip.seconds, text)
                    break
                if first is None:
                    first = time.monotonic() - started
                clip.push(block)
        except asyncio.CancelledError:
            outcome = "cut" if first is not None else "unanswered"
            raise
        except Exception as e:
            log.warning("TTS request failed (%s: %s): %s", type(e).__name__, e, text)
        finally:
            clip.finish(outcome == "ok")
            await stream.aclose()
            if clip.parts:
                self.audio_stats["synth_clips"] += 1
                self.audio_stats["synth_chars"] += len(text)
                self.audio_stats["synth_seconds"] += clip.seconds
            self._record_tts(purpose, attempt, len(text), outcome, started, first, clip.seconds)

    async def prepare_exchange(self, lines: list[Line]) -> tuple[list[Line], list[Audio | None]]:
        """Voice a multi-line exchange ahead of time (a wrap, the closer): in parallel on a network
        voice, in order on a local one. It's cut at the first line that couldn't be voiced, since a
        reply to a line nobody hears makes no sense; empty when the first line failed."""
        if self.muted:
            return [], []
        if getattr(self.tts, "concurrent", False):
            results = await asyncio.gather(*(self._synthesize(l) for l in lines))
        else:
            results = [await self._synthesize(l) for l in lines]
        kept: list[Audio | None] = []
        for line, (ok, audio) in zip(lines, results):
            if not ok:
                log.info("TTS gave up preparing %r; cut the exchange to %s of %s lines", line.text, len(kept), len(lines))
                break
            kept.append(audio)
        return lines[:len(kept)], kept

    async def prepare(self, line: Line) -> Audio | None:
        """Voices a line now to play later (mid-flight calls are voiced before the swing)."""
        if self.muted:
            return None
        ok, audio = await self._synthesize(line)
        if not ok:
            log.info("TTS gave up preparing: %s", line.text)
        return audio

    def say_prepared(self, line: Line, audio: Audio | None) -> None:
        """Cuts off whatever is playing and plays already-voiced audio straight away."""
        if self.muted:
            return
        self.interrupt()
        self._audio_q.put_nowait((self._generation, line, audio, None, None))
        self._pending += 1

    def say_prepared_sequence(self, items: list[tuple[Line, Audio | None]], banter: bool = False) -> None:
        """Play several already-voiced lines in order. Cuts off whatever's playing once, then feeds
        the clips to the play loop one at a time (the audio queue holds only one, so a background task
        awaits each put, like the synth loop does). A later interrupt supersedes the rest of the
        sequence. Looping say_prepared instead would interrupt between every line and drop all but the
        last, so the whole exchange has to go through here."""
        if not items or self.muted:
            return
        self.interrupt()
        gen = self._generation
        self._sequencing = gen
        if banter:
            self._banter_gen, self._banter_lines = gen, {id(line) for line, _ in items}
        self._pending += len(items)

        async def feed() -> None:
            try:
                for i, (line, audio) in enumerate(items):
                    if gen != self._generation:   # a newer interrupt superseded this sequence
                        self._pending -= (len(items) - i)   # the rest never reach the play loop
                        return
                    await self._audio_q.put((gen, line, audio, None, None))
            finally:
                # Release the synth loop, but only if we still own it (a newer sequence that took over
                # owns the token now and will clear it itself). A plain interrupt bumps the generation
                # without touching the token, so this still clears and the booth doesn't go silent.
                if self._sequencing == gen:
                    self._sequencing = None

        task = asyncio.create_task(feed())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def interrupt(self) -> None:
        """Cut off what's playing and discard everything queued or mid-synthesis."""
        self._generation += 1
        dropped = self._text_q.qsize() + self._audio_q.qsize()
        self._drain(self._text_q)
        self._drain(self._audio_q)
        cut = bool(self._playing and not self._playing.done())
        if dropped or cut:
            log.info("Interrupted: %s cut off, %s queued lines dropped", "a line" if cut else "nothing", dropped)
        if self._playing and not self._playing.done():
            self._stop()
            self._playing.cancel()
        if self._gap and not self._gap.done():
            self._gap.cancel()
        self._last_end = None   # what cuts in starts at once, not after a conversational pause
        if self._synth_task and not self._synth_task.done():
            self._synth_task.cancel()

    def banter_airing(self) -> bool:
        """An aside is still on air (or queued to finish), and nothing has cut it off."""
        return self._banter_gen == self._generation and bool(self._banter_lines)

    async def wait_banter(self, limit: float) -> bool:
        """Wait (up to `limit`) for the aside to finish. False if something else cut it off and took
        the air (a hole wrap, a big moment), so what waited is now old news."""
        deadline = time.monotonic() + limit
        while self.banter_airing() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return self._banter_gen == self._generation

    def drop_queued(self) -> None:
        """Drop every line not yet on air (queued, mid-synthesis, or the rest of a prepared exchange)
        and let the one playing finish: a new shot was hit, so the last one's calls are old news. An
        aside on air is kept whole."""
        if self.banter_airing():
            log.info("New shot: the aside on air plays on")
            return
        dropped = self._text_q.qsize() + self._audio_q.qsize()
        self._generation += 1
        self._drain(self._text_q)
        self._drain(self._audio_q)
        if self._synth_task and not self._synth_task.done():
            self._synth_task.cancel()
            dropped += 1
        if self._gap and not self._gap.done():
            self._gap.cancel()
        if dropped:
            log.info("New shot: dropped %s lines still waiting to air", dropped)

    def _drain(self, q: asyncio.Queue) -> None:
        while not q.empty():
            item = q.get_nowait()
            if isinstance(item, _Queued):
                _discard(item)
            elif isinstance(item, tuple) and isinstance(item[2], StreamedAudio):
                item[2].cancel()
            self._pending -= 1

    def is_idle(self) -> bool:
        return self._pending == 0

    async def run(self) -> None:
        await asyncio.gather(self._synth_loop(), self._play_loop())

    async def _synth_loop(self) -> None:
        """Hands voiced lines to the play loop in queue order, waiting on each line's synth in turn."""
        while True:
            item = await self._text_q.get()
            if item.gen != self._generation or item.exchange == self._lost_exchange:
                _discard(item)
                self._banter_lines.discard(id(item.line))
                self._pending -= 1
                continue
            if item.task is None:
                self._start_synth(item)
            self._synth_task = item.task
            try:
                ok, audio = await item.task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                ok, audio = False, None  # interrupt() superseded it mid-synthesis
            finally:
                self._synth_task = None
            late = ok and item.deadline is not None and time.monotonic() > item.deadline
            if not ok or late:
                if isinstance(audio, StreamedAudio):
                    audio.cancel()
                if item.gen == self._generation:
                    if late:
                        log.info("Dropped a line voiced %.1fs past its deadline: %s",
                                 time.monotonic() - item.deadline, item.line.text)
                    else:
                        log.info("Dropped a line its voice wasn't ready for in time: %s", item.line.text)
                    self._record({"kind": "line", "outcome": "dropped", "reason": "late" if late else "no_voice",
                                  "chars": len(item.line.text)})
                    self._lost_exchange = item.exchange  # its replies go too
                self._banter_lines.discard(id(item.line))
                self._pending -= 1
                continue
            if item.gen != self._generation or self._sequencing is not None:
                # Interrupted while synthesizing, or a prepared wrap/closer is airing and this ordinary
                # line would splice into the middle of it; drop it rather than compete for the queue.
                if isinstance(audio, StreamedAudio):
                    audio.cancel()
                self._banter_lines.discard(id(item.line))
                self._pending -= 1
                continue
            await self._audio_q.put((item.gen, item.line, audio, item.deadline, item.queued))

    async def _pause_before(self, line: Line) -> None:
        """Leaves a beat between consecutive lines, longer when the other commentator takes over, so
        an exchange doesn't sound rapid-fire (streamed clips have almost no trailing silence). Only
        tops up the silence since the last line ended; a line after a longer silence starts at once."""
        if self._last_end is None:
            return
        tts = self.cfg.tts
        gap = tts.turn_gap_seconds if line.speaker != self._last_speaker else tts.line_gap_seconds
        remaining = gap - (time.monotonic() - self._last_end)
        if remaining <= 0:
            return
        self._gap = asyncio.create_task(asyncio.sleep(remaining))
        try:
            await self._gap
        except asyncio.CancelledError:
            # interrupt() cut the pause short; only a shutdown of this loop should propagate.
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._gap = None

    async def _play_loop(self) -> None:
        while True:
            gen, line, audio, deadline, queued = await self._audio_q.get()
            streamed = isinstance(audio, StreamedAudio)
            try:
                if gen != self._generation:
                    continue
                await self._pause_before(line)
                if gen != self._generation:
                    continue
                if deadline is not None and time.monotonic() > deadline:
                    log.info("Dropped a line that missed its moment by %.1fs: %s",
                             time.monotonic() - deadline, line.text)
                    self._record({"kind": "line", "outcome": "dropped", "reason": "missed",
                                  "chars": len(line.text)})
                    continue
                name = self.cfg.personas[line.speaker].name if line.speaker in self.cfg.personas else line.speaker
                print(f"  [{name}] {line.text}", flush=True)
                log.info("Speaking [%s]: %s", name, line.text)
                wait = time.monotonic() - queued if queued is not None else None
                if wait is not None and wait > _SLOW_LINE_SECONDS:
                    log.info("Line started %.1fs after it was queued: %s", wait, line.text)
                if audio is not None:
                    if not streamed:   # a streamed clip is counted once it has finished playing
                        self._count_spoken(line.text, audio)
                    self._playing = asyncio.create_task(self._play(audio))
                elif self.cfg.tts.console_simulate_duration:
                    self._playing = asyncio.create_task(asyncio.sleep(len(line.text.split()) / WORDS_PER_SECOND))
                else:
                    continue
                try:
                    await self._playing
                except asyncio.CancelledError:
                    # interrupt() cancelled the line; only a shutdown of this loop should propagate.
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    log.exception("Audio playback failed")
                if gen == self._generation:
                    self._last_end, self._last_speaker = time.monotonic(), line.speaker
                if streamed:
                    self._count_spoken(line.text, audio)
                if audio is not None:
                    self._record({"kind": "line", "outcome": "aired",
                                  "source": "live" if queued is not None else "prepared",
                                  "streamed": streamed, "chars": len(line.text),
                                  "wait_s": round(wait, 3) if wait is not None else None,
                                  "audio_s": round(_aired_seconds(audio), 2)})
            finally:
                if streamed:
                    audio.cancel()   # an interrupted clip stops downloading
                self._banter_lines.discard(id(line))
                self._pending -= 1
