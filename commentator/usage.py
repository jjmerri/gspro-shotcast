import json
import time
from collections import defaultdict
from pathlib import Path

from .llm import LLMClient, LLMResult

# USD per million tokens: (input, output). Cache reads bill at 0.1x input, writes at 1.25x.
# Check current pricing before relying on these.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}
# OpenAI chat models, USD per million tokens: (input, output, cached input). OpenAI's prompt_tokens
# already include the cached ones (Anthropic's input_tokens don't). Estimates; check current pricing.
OPENAI_PRICES = {
    "gpt-6-luna": (0.10, 0.50, 0.01),
}


# Cloud TTS (gpt-4o-mini-tts) is billed by generated audio time; OpenAI's own estimate is about this.
# The tts-1 models bill by input characters instead. Check current pricing before relying on these.
TTS_USD_PER_MINUTE = 0.015
# Google Chirp 3 HD bills input characters ($30/M), with the first 1M characters each month free
# (not reflected here).
TTS_USD_PER_MILLION_CHARS = {"tts-1-hd": 30.0, "tts-1": 15.0, "google": 30.0}


def audio_cost(seconds: float) -> float:
    """Estimated USD for `seconds` of synthesized cloud TTS audio."""
    return seconds / 60.0 * TTS_USD_PER_MINUTE


def tts_cost(model: str, chars: int, audio_seconds: float) -> float:
    """Estimated USD for one TTS request (0 for a local or console voice). A request that never
    answered is counted as free: whether OpenAI bills a stalled request is unknown."""
    if model in ("kokoro", "console"):
        return 0.0
    per_char = next((p for prefix, p in TTS_USD_PER_MILLION_CHARS.items() if model.startswith(prefix)), None)
    if per_char is not None:
        return chars * per_char / 1_000_000 if audio_seconds else 0.0
    return audio_cost(audio_seconds)


def audio_summary(stats: dict) -> str:
    """One-line session TTS tally: what was synthesized vs what actually aired, and the estimated $.
    The gap is the waste: branches voiced ahead for every outcome (tts.prevoice), and clips cut off
    or lost to a retry."""
    synth_s, spoken_s = stats.get("synth_seconds", 0.0), stats.get("spoken_seconds", 0.0)
    pct = (1 - spoken_s / synth_s) * 100 if synth_s else 0.0
    return (
        f"Audio this session: synthesized {stats.get('synth_clips', 0)} clips / "
        f"{stats.get('synth_chars', 0):,} chars / {synth_s / 60:.1f} min (est ${audio_cost(synth_s):.2f}); "
        f"spoken {stats.get('spoken_clips', 0)} clips / {spoken_s / 60:.1f} min (est ${audio_cost(spoken_s):.2f}); "
        f"{pct:.0f}% of synthesized audio never aired (pre-voiced branches, cut-off or retried clips)"
    )


def estimate_cost(model: str, r: dict) -> float:
    oa = next((p for prefix, p in OPENAI_PRICES.items() if model.startswith(prefix)), None)
    if oa:
        inp, out, cached_price = oa
        cached = r.get("cache_read_tokens") or 0
        return (
            max((r.get("input_tokens") or 0) - cached, 0) * inp
            + cached * cached_price
            + (r.get("output_tokens") or 0) * out
        ) / 1_000_000
    price = next((p for prefix, p in PRICES.items() if model.startswith(prefix)), None)
    if not price:
        return 0.0
    inp, out = price
    return (
        (r.get("input_tokens") or 0) * inp
        + (r.get("cache_read_tokens") or 0) * inp * 0.1
        + (r.get("cache_write_tokens") or 0) * inp * 1.25
        + (r.get("output_tokens") or 0) * out
    ) / 1_000_000


class UsageLog:
    """logs/usage.jsonl: one row per model call ("kind" is the call's role), per TTS request
    (kind "tts") and per line aired or dropped (kind "line")."""

    def __init__(self, log_dir: str) -> None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(log_dir) / "usage.jsonl"

    def write(self, row: dict) -> None:
        row = {"ts": time.time(), "date": time.strftime("%Y-%m-%d"), **row}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def record(self, kind: str, llm: LLMClient, result: LLMResult) -> None:
        row = {
            "kind": kind,
            "provider": llm.provider,
            "model": llm.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cache_write_tokens": result.cache_write_tokens,
            "latency_s": round(result.latency_seconds, 2),
        }
        row["est_cost_usd"] = round(estimate_cost(llm.model, row), 6)
        self.write(row)


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else 0.0


def summarize(log_dir: str, weeks: int = 12, as_model: str | None = None) -> str:
    """Per-night totals: model calls and their cost by provider, the voice's cost and the audio
    voiced vs aired, how often a voice request stalled, and how long lines waited to start.
    `as_model` prices every model call at that model's rates, which turns a local night's token
    counts into a rough cloud estimate (tokenizers differ, so treat it as ballpark). Costs are
    recomputed from the token counts, so older rows pick up prices added since."""
    path = Path(log_dir) / "usage.jsonl"
    if not path.exists():
        return f"No usage log at {path}"
    nights: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "latency": 0.0, "llm": defaultdict(float), "models": set(),
        "tts_reqs": 0, "tts_unanswered": 0, "tts_cost": 0.0, "tts_audio": 0.0, "first": [],
        "aired": 0.0, "waits": [], "dropped": 0,
    })
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        d = nights[r.get("date", "?")]
        kind = r.get("kind")
        if kind == "tts":
            d["tts_reqs"] += 1
            d["tts_unanswered"] += r.get("outcome") == "unanswered"
            d["tts_cost"] += r.get("est_cost_usd") or 0
            d["tts_audio"] += r.get("audio_s") or 0
            if r.get("first_audio_s") is not None:
                d["first"].append(r["first_audio_s"])
        elif kind == "line":
            if r.get("outcome") == "dropped":
                d["dropped"] += 1
            else:
                d["aired"] += r.get("audio_s") or 0
                if r.get("wait_s") is not None:
                    d["waits"].append(r["wait_s"])
        else:
            d["calls"] += 1
            d["latency"] += r.get("latency_s") or 0
            d["llm"][r.get("provider", "?")] += estimate_cost(as_model or r.get("model", ""), r)
            d["models"].add(f'{r.get("provider")}:{r.get("model")}')
    out = ["date        calls  avg_lat  llm_cost (by provider)            voice_cost  voiced/aired min"
           "  stalls  first_audio p50/p90  line_wait p50/p90  dropped"]
    total = {}
    for date, d in sorted(nights.items()):
        llm = sum(d["llm"].values())
        total[date] = llm + d["tts_cost"]
        by = ", ".join(f"{p} ${c:.2f}" for p, c in sorted(d["llm"].items())) or "-"
        stalls = f'{d["tts_unanswered"]}/{d["tts_reqs"]}' if d["tts_reqs"] else "-"
        first = f'{_pct(d["first"], .5):.2f}/{_pct(d["first"], .9):.2f}s' if d["first"] else "-"
        wait = f'{_pct(d["waits"], .5):.2f}/{_pct(d["waits"], .9):.2f}s' if d["waits"] else "-"
        avg = d["latency"] / d["calls"] if d["calls"] else 0.0
        out.append(
            f'{date}  {d["calls"]:>5}  {avg:>6.2f}s  ${llm:>6.2f} ({by})'.ljust(54)
            + f'  ${d["tts_cost"]:>8.2f}  {d["tts_audio"] / 60:>6.1f}/{d["aired"] / 60:<6.1f}'
            + f'  {stalls:>7}  {first:>19}  {wait:>17}  {d["dropped"]:>7}'
        )
    priced = f" (model calls priced as {as_model})" if as_model else ""
    out.append("\nCosts are estimates from list prices; voice requests that never answered are counted as free.")
    out.append(f"Last night x {weeks} weeks{priced}: ${total[max(total)] * weeks:.2f}")
    return "\n".join(out)
