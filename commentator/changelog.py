"""What's new (commentator/changelog.md): each release's changes, for the app's What's new window
after an update and the stats page's card."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .updater import newer

FILE = Path(__file__).with_name("changelog.md")
SITE, BOOTH = "Stats page", "Booth"


@dataclass
class Release:
    version: str
    items: dict[str, list[str]] = field(default_factory=dict)   # section -> its items (markdown)


def parse(text: str) -> list[Release]:
    """changelog.md's releases, as written (newest first)."""
    out: list[Release] = []
    section = None
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for line in text.splitlines():
        if m := re.match(r"##\s+v?(\d[\w.]*)\s*$", line):
            out.append(Release(m.group(1)))
            section = None
        elif m := re.match(r"###\s+(.+?)\s*$", line):
            section = m.group(1)
        elif out and section and (m := re.match(r"-\s+(.*)", line)):
            out[-1].items.setdefault(section, []).append(m.group(1).strip())
        elif out and section and line.startswith("  ") and line.strip() and out[-1].items.get(section):
            out[-1].items[section][-1] += " " + line.strip()   # an item's next line
    return out


def releases(path: Path = FILE) -> list[Release]:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def between(old: str, new: str, path: Path = FILE) -> list[Release]:
    """The releases after `old`, up to and including `new` (an update from old to new)."""
    return [r for r in releases(path) if newer(r.version, old) and not newer(r.version, new)]


def plain(item: str) -> str:
    """An item without its markdown, for a window that shows plain text."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", item)


def text(rels: list[Release]) -> str:
    """Releases as plain text, newest first."""
    parts = []
    for r in rels:
        lines = [f"Shotcast {r.version}"]
        for section, items in r.items.items():
            lines += [f"  {section}"] + [f"    • {plain(i)}" for i in items]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def site(limit: int = 8) -> list[dict]:
    """The stats page's part: each release's "Stats page" items, newest first."""
    return [{"version": r.version, "items": r.items[SITE]} for r in releases() if r.items.get(SITE)][:limit]
