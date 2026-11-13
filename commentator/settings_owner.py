"""Who owns each setting, for when a hosted service manages part of the config.

- "web": the league's choices (personas, voices, commentary, news, chat): edited on a website and
  synced down to the PC.
- "pc": this machine's own (GSPro paths, screen regions, overlay, audio, backups): always local.
- "service": set by the hosting plan (models, the bucket, how long video is kept).

Nothing reads this yet: it's the contract a hosted service would build on. Self-hosted, every setting
stays in config.yaml exactly as before. A new top-level setting must be given an owner here (a test
checks), so the split can't silently fall behind the config."""
from __future__ import annotations

from typing import Any, Literal

from .config import Config

Owner = Literal["web", "pc", "service"]
OWNERS: tuple[Owner, ...] = ("web", "pc", "service")

# Each top-level setting's owner.
SECTIONS: dict[str, Owner] = {
    "league_file": "web",
    "history_file": "pc",
    "personas": "web",
    "llm": "service",
    "tts": "web",
    "screen": "pc",
    "source": "pc",
    "squabbit": "pc",
    "booking": "pc",
    "commentary": "web",
    "news": "web",
    "weather": "web",
    "scores": "web",
    "trivia": "web",
    "overlay": "pc",
    "health": "pc",
    "notify": "pc",
    "minimap": "service",
    "putt_read": "web",
    "publish": "service",
    "live": "pc",
    "chat": "web",
    "backup": "pc",
    "report": "pc",
    "log_dir": "pc",
    "db_path": "pc",
    "process_priority": "pc",
    "max_cpu_cores": "pc",
}

# Settings inside a section whose owner differs from the section's.
FIELDS: dict[str, Owner] = {
    "tts.provider": "service",
    "tts.openai_model": "service",
    "tts.fallback": "service",
    "tts.paid": "service",
    "tts.free_chars": "service",
    "tts.free_margin": "service",
    "tts.azure_region": "service",
    "tts.kokoro_model": "pc",
    "tts.kokoro_voices": "pc",
    "publish.site_name": "web",
    "publish.more_tab": "web",
    "publish.season_start": "web",
    "publish.theme": "web",
    "publish.recaps": "web",
    "live.enabled": "web",
    "live.with_rounds": "web",
    "live.keep_days": "service",
    "chat.table": "service",
}


def owner(path: str) -> Owner:
    """The owner of a setting by its dotted path ("tts.provider", "commentary")."""
    if path in FIELDS:
        return FIELDS[path]
    section = path.split(".", 1)[0]
    if section not in SECTIONS:
        raise KeyError(f"no owner for setting {path!r}")
    return SECTIONS[section]


def split(data: dict[str, Any]) -> dict[Owner, dict[str, Any]]:
    """Split a config mapping (as in config.yaml) by owner. Settings with no owner (unknown keys,
    say from a newer app) go with "pc", so they stay on the machine untouched."""
    out: dict[Owner, dict[str, Any]] = {o: {} for o in OWNERS}
    for key, value in data.items():
        if key not in SECTIONS:
            out["pc"][key] = value
            continue
        inner = {f.split(".", 1)[1] for f in FIELDS if f.startswith(key + ".")}
        if inner and isinstance(value, dict):
            for name, v in value.items():
                out[owner(f"{key}.{name}") if name in inner else SECTIONS[key]].setdefault(key, {})[name] = v
        else:
            out[SECTIONS[key]][key] = value
    return out


def merge(*parts: dict[str, Any]) -> dict[str, Any]:
    """Put split parts back together (the inverse of `split`)."""
    out: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    return out


def export() -> dict[str, Any]:
    """The settings contract for a hosted service: the config's JSON Schema and each setting's owner."""
    return {"schema": Config.model_json_schema(), "sections": dict(SECTIONS), "fields": dict(FIELDS)}
