"""Every model call's prompt and reply, one JSON line each, in log_dir/prompts-YYYY-MM-DD.jsonl, so a
wrong line on air can be traced to what the model was actually told.

The system prompt and schema barely change across a round but are most of each prompt's bytes, so each
is written in full only the first time this process puts it in a day's file, as {"system": text,
"system_sha": sha}; later lines carry only the sha. Image bytes aren't kept, just their sizes. Files
older than KEEP_DAYS are deleted when the log opens. Writing never raises: it's a diagnostic record,
not worth a line of commentary."""

import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 7
PREFIX = "prompts-"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class PromptLog:
    def __init__(self, log_dir: str | Path) -> None:
        self.dir = Path(log_dir)
        self._written: dict[Path, set[str]] = {}   # file -> shas whose full text it has from this process
        self.prune()

    def prune(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - KEEP_DAYS * 86400
        for f in self.dir.glob(f"{PREFIX}[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].jsonl"):
            try:   # one file held open (Windows) mustn't keep the rest
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError as e:
                log.debug("Couldn't prune %s: %s", f.name, e)

    def record(self, kind: str, provider: str, model: str, system: str, user: str, schema: dict | None,
               images: list[bytes], *, text: str | None = None, outcome: str = "ok", error: str | None = None,
               latency_s: float | None = None, input_tokens: int | None = None,
               output_tokens: int | None = None) -> None:
        try:
            now = time.time()
            path = self.dir / f"{PREFIX}{time.strftime('%Y-%m-%d', time.localtime(now))}.jsonl"
            if path not in self._written:   # a new day, for a booth left running: drop the stale ones
                self.prune(now)
            written = self._written.setdefault(path, set())
            entry: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}",
                           "kind": kind, "provider": provider, "model": model, "outcome": outcome}
            new: set[str] = set()
            for key, value in (("system", system), ("schema", schema)):
                if value is None:
                    continue
                sha = _sha(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
                entry[f"{key}_sha"] = sha
                if sha not in written:
                    entry[key] = value
                    new.add(sha)
            entry["user"] = user
            entry["images"] = [len(i) for i in images]
            if text is not None:
                entry["text"] = text
            if error is not None:
                entry["error"] = error
            if latency_s is not None:
                entry["latency_s"] = round(latency_s, 2)
            if input_tokens is not None:
                entry["input_tokens"] = input_tokens
            if output_tokens is not None:
                entry["output_tokens"] = output_tokens
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            written |= new   # only once the full text is actually in the file
        except Exception as e:
            log.debug("Couldn't log the %s prompt: %s", kind, e)


def excerpt(path: Path, cap: int) -> bytes:
    """The newest whole lines of a prompt log within about `cap` bytes, for a problem report. A system
    prompt or schema whose full text was cut off with the older lines goes back into the first kept
    line that refers to it, so every line can still be read on its own terms. Reads only the tail,
    and the older part line by line as far as it needs, so a big day's log isn't held in memory."""
    size = path.stat().st_size
    if size <= cap:
        return path.read_bytes()
    with open(path, "rb") as f:
        f.seek(size - cap - 1)
        if f.read(1) != b"\n":
            f.readline()   # start on a whole line
        start = f.tell()
        kept = f.read().splitlines(keepends=True)
    entries: list = []
    need: dict[str, str] = {}   # sha -> key, referenced in the tail before its full text
    have: set[str] = set()
    for line in kept:
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        entries.append(entry)
        for key in ("system", "schema"):
            if isinstance(entry, dict) and (sha := entry.get(f"{key}_sha")) and sha not in have:
                have.add(sha)
                if key not in entry:
                    need[sha] = key
    full: dict[str, object] = {}
    left_out = 0
    with open(path, "rb") as f:
        for line in iter(f.readline, b""):
            if f.tell() > start:
                break
            left_out += 1
            if len(full) < len(need) and b'_sha"' in line:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for sha, key in need.items():
                    if entry.get(f"{key}_sha") == sha and key in entry:
                        full.setdefault(sha, entry[key])
    out = [(json.dumps({"note": "earlier prompts left out", "lines_left_out": left_out}) + "\n").encode("utf-8")]
    filled: set[str] = set()
    for line, entry in zip(kept, entries):
        changed = False
        for key in ("system", "schema"):
            sha = entry.get(f"{key}_sha") if isinstance(entry, dict) else None
            if sha in full and sha not in filled and key not in entry:
                entry[key] = full[sha]
                changed = True
            if sha in full:
                filled.add(sha)
        out.append((json.dumps(entry) + "\n").encode("utf-8") if changed else line)
    return b"".join(out)
