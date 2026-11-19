"""Run the booth's captured closer prompts (logs/closer-prompts.jsonl) on other models, to compare
sign-offs written from the identical prompt. Prints each model's lines side by side with latency,
output tokens and estimated cost. Calls the real APIs (keys from --env); nothing goes on air.

    python tools/closer_ab.py --prompts logs/closer-prompts.jsonl --last 3 \\
        --model anthropic:claude-opus-5-5+low --model openai:gpt-6.2-sol+low+default

A model is provider:model[+effort[+service_tier]] (the model name may hold a colon, as Ollama tags
do); the rest of its settings come from the config's closer model, less its endpoint. Model output isn't deterministic: compare several rounds, not one.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commentator.config import load_config  # noqa: E402
from commentator.dotenv import load_env  # noqa: E402
from commentator.llm import build_llm  # noqa: E402
from commentator.usage import estimate_cost  # noqa: E402


def model_config(base, spec: str):
    """The config's closer model with provider, model and optionally effort and service tier swapped."""
    provider, rest = spec.split(":", 1)
    model, *rest = rest.split("+")
    # Another provider's endpoint would get this provider's key: always its default one.
    update = {"provider": provider, "model": model, "base_url": None}
    if rest and rest[0]:
        update["effort"] = rest[0]
    if len(rest) > 1 and rest[1]:
        update["service_tier"] = rest[1]
    return base.model_copy(update=update)


def load_prompts(path: Path, last: int | None) -> list[dict]:
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return entries[-last:] if last else entries


def lines_of(text: str) -> list[str]:
    try:
        return [f"{l.get('speaker', '?')}: {l.get('text', '')}" for l in json.loads(text).get("lines", [])]
    except (ValueError, AttributeError):
        return [text]


async def run(entries: list[dict], llms: dict) -> None:
    for i, e in enumerate(entries, 1):
        print(f"\n=== {i}. closer at {e['ts']} ===")
        # Captured before grounding: the booth may have retried or trimmed what aired.
        print(f"\n-- captured reply {e['provider']}:{e['model']} "
              f"({e.get('latency_s')}s, {e.get('output_tokens')} out)")
        print("\n".join(lines_of(e["text"])))
        for spec, llm in llms.items():
            try:
                r = await llm.complete_json(e["system"], e["user"], e["schema"], [], cache=False)
            except Exception as ex:
                print(f"\n-- {spec}: failed: {ex}")
                continue
            cost = estimate_cost(llm.model, {
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "cache_read_tokens": r.cache_read_tokens, "cache_write_tokens": r.cache_write_tokens,
                "service_tier": llm.cfg.service_tier if llm.provider == "openai" else None})
            price = f"~${cost:.4f}" if cost or llm.provider == "ollama" else "cost unknown"
            print(f"\n-- {spec} ({r.latency_seconds:.1f}s, {r.output_tokens} out, {price})")
            print("\n".join(lines_of(r.text)))


def positive(value: str) -> int:
    if int(value) < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return int(value)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompts", default="logs/closer-prompts.jsonl")
    ap.add_argument("--model", action="append", required=True, help="provider:model[+effort[+tier]]")
    ap.add_argument("--last", type=positive, help="only the last N closers")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()
    load_env(Path(args.env))
    base = load_config(args.config).closer_llm
    llms = {spec: build_llm(model_config(base, spec)) for spec in args.model}
    asyncio.run(run(load_prompts(Path(args.prompts), args.last), llms))


if __name__ == "__main__":
    main()
