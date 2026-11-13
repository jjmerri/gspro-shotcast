"""Several cloud voices, one per round: the first with enough free allowance left this month for the
round about to start (docs/booth.md, "Voice fallbacks")."""
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import TTSConfig
from .azure_tts import QuotaExhausted
from .base import TTS, Audio

log = logging.getLogger(__name__)

# A provider's choice of (voice, instructions) for a line, resolved when its request is sent.
Resolver = Callable[[str], tuple[str, str | None]]

MIN_ROUNDS_FOR_ESTIMATE = 5


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def billing_month(provider: str, ts: float) -> str:
    """The provider's billing month a request at `ts` falls in. Google's free allowance resets at
    midnight US Pacific time; Azure's is taken as UTC. US daylight time is worked out here (Windows
    Python has no time zone data without the tzdata package)."""
    utc = datetime.fromtimestamp(ts, timezone.utc)
    if provider != "google":
        return utc.strftime("%Y-%m")
    # PDT from 2 am PST on the second Sunday of March to 2 am PDT on the first Sunday of November.
    dst = (_nth_sunday(utc.year, 3, 2) + timedelta(hours=10) <= utc
           < _nth_sunday(utc.year, 11, 1) + timedelta(hours=9))
    return (utc - timedelta(hours=7 if dst else 8)).strftime("%Y-%m")


def _p90(values: list[float]) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(0.9 * len(v)))]


class VoiceBudget:
    """Characters sent to each provider this billing month, counted from the usage log's TTS rows
    (every request, retries included, so it errs high) and kept up as lines are voiced."""

    def __init__(self, cfg: TTSConfig) -> None:
        self.cfg = cfg
        self.used: dict[tuple[str, str], int] = defaultdict(int)   # (provider, month) -> chars
        self.spent: set[tuple[str, str]] = set()   # (provider, month) the provider said is used up
        self.per_hole: list[float] = []             # chars per player-hole of past rounds

    def load(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open(encoding="utf-8") as f:
            for raw in f:
                if '"kind": "tts' not in raw:   # most rows are model calls and aired lines
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if r.get("kind") == "tts" and r.get("provider") and r.get("chars"):
                    self.add(r["provider"], r["chars"], r.get("ts"))
                elif r.get("kind") == "tts_quota" and r.get("provider") and r.get("ts"):
                    self.spent.add((r["provider"], billing_month(r["provider"], r["ts"])))
                elif r.get("kind") == "tts_round" and r.get("holes") and r.get("players"):
                    self.per_hole.append(r["chars"] / (r["holes"] * r["players"]))

    def add(self, provider: str, chars: int, ts: float | None = None) -> None:
        self.used[provider, billing_month(provider, ts or time.time())] += chars

    def mark_spent(self, provider: str) -> None:
        self.spent.add((provider, billing_month(provider, time.time())))

    def remaining(self, provider: str) -> float | None:
        """Free characters left this month inside the margin; None for a provider with no allowance."""
        allowance = self.cfg.free_chars.get(provider)
        if allowance is None:
            return None
        month = billing_month(provider, time.time())
        if (provider, month) in self.spent:
            return 0
        return allowance * (1 - self.cfg.free_margin) - self.used[provider, month]

    def chars_per_player_hole(self) -> float:
        if len(self.per_hole) >= MIN_ROUNDS_FOR_ESTIMATE:
            return _p90(self.per_hole)
        return self.cfg.chars_per_player_hole

    def choose(self, needed: float) -> str:
        """The first provider whose free allowance covers `needed` characters, else the paid one."""
        for provider in self.cfg.tiers:
            left = self.remaining(provider)
            if left is None or left >= needed:
                return provider
        return self.cfg.paid_provider


class TieredTTS(TTS):
    """The voice providers in tts.provider + tts.fallback (+ tts.paid), one in use at a time. The
    speech queue passes a resolver for the voice, so a line's voice is that of the provider its
    request goes to."""

    concurrent = True
    remote = True
    expressive = True
    resolves_voices = True

    def __init__(self, cfg: TTSConfig, backends: dict[str, TTS]) -> None:
        self.cfg = cfg
        self.backends = backends
        rates = {getattr(b, "stream_sample_rate", None) for b in backends.values()}
        if len(rates) != 1:
            raise ValueError(f"Voice providers stream at different rates: {rates}")
        self.stream_sample_rate = rates.pop()
        self.budget = VoiceBudget(cfg)
        self.usage = None
        self.provider = self.budget.choose(0)

    def load_usage(self, usage) -> None:
        """Count this month's characters from the usage log (a UsageLog), and note spent allowances in it."""
        self.usage = usage
        self.budget.load(usage.path)
        self.provider = self.budget.choose(0)
        log.info("Voice: %s (free this month: %s)", self.provider, self._left())

    def _left(self) -> str:
        parts = []
        for p in self.cfg.tiers:
            left = self.budget.remaining(p)
            parts.append(f"{p} unlimited" if left is None else f"{p} {max(left, 0):,.0f} chars")
        return ", ".join(parts)

    def start_round(self, holes: int, players: int) -> None:
        """Pick the voice for a round about to start; it holds until the next round starts."""
        per = self.budget.chars_per_player_hole()
        needed = holes * max(players, 1) * per
        chosen = self.budget.choose(needed)
        if chosen != self.provider:
            log.info("Voice switched %s -> %s for this round (needs ~%s chars: %s holes x %s players x %.0f; "
                     "free left: %s)", self.provider, chosen, f"{needed:,.0f}", holes, players, per, self._left())
        else:
            log.info("Voice: %s for this round (needs ~%s chars; free left: %s)",
                     chosen, f"{needed:,.0f}", self._left())
        self.provider = chosen

    def _spent(self, provider: str, err: Exception) -> bool:
        """The provider ran out mid-round: the paid voice takes over for the rest of it. True when
        there's somewhere else to go."""
        self.budget.mark_spent(provider)
        if self.usage is not None:   # so a restart this month doesn't pick it again
            try:
                self.usage.write({"kind": "tts_quota", "provider": provider})
            except Exception as e:
                log.debug("Usage row not written: %s", e)
        paid = self.cfg.paid_provider
        if provider == paid:
            log.warning("Voice %s says its allowance is used up (%s), and it's the paid voice too", provider, err)
            return False
        if self.provider == provider:
            log.warning("Voice %s says its allowance is used up (%s): %s for the rest of the round",
                        provider, err, paid)
            self.provider = paid
        return True

    async def synthesize(self, text: str, voice: Resolver, instructions: str | None = None,
                         excited: bool = False, served: dict | None = None) -> Audio:
        """`served` gets the provider that took the request ("provider"), for the usage count."""
        served = {} if served is None else served
        served["provider"] = provider = self.provider
        try:
            return await self._backend_synth(provider, text, voice, excited)
        except QuotaExhausted as e:
            if not self._spent(provider, e):
                raise
            served["provider"] = self.provider
            return await self._backend_synth(self.provider, text, voice, excited)

    async def _backend_synth(self, provider: str, text: str, voice: Resolver, excited: bool) -> Audio:
        name, instructions = voice(provider)
        backend = self.backends[provider]
        if getattr(backend, "expressive", False):
            return await backend.synthesize(text, name, instructions, excited=excited)
        return await backend.synthesize(text, name, instructions)

    async def stream(self, text: str, voice: Resolver, instructions: str | None = None, excited: bool = False,
                     served: dict | None = None):
        served = {} if served is None else served
        served["provider"] = provider = self.provider
        started = False
        try:
            async with aclosing(self._backend_stream(provider, text, voice, excited)) as blocks:
                async for block in blocks:
                    started = True
                    yield block
        except QuotaExhausted as e:
            if started or not self._spent(provider, e):
                raise
            served["provider"] = self.provider
            async with aclosing(self._backend_stream(self.provider, text, voice, excited)) as blocks:
                async for block in blocks:
                    yield block

    async def _backend_stream(self, provider: str, text: str, voice: Resolver, excited: bool):
        name, instructions = voice(provider)
        backend = self.backends[provider]
        kw = {"excited": excited} if getattr(backend, "expressive", False) else {}
        async with aclosing(backend.stream(text, name, instructions, **kw)) as blocks:
            async for block in blocks:
                yield block
