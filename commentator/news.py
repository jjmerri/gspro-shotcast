"""Recent golf headlines for the booth's between-shots banter, pulled live from public RSS/Atom feeds.

The commentary model's sense of current events is frozen at its training cutoff, so asked to sound
current it invents stale or wrong "news". Instead real headlines are fetched here and handed to it
verbatim as facts; it riffs on them but is told not to invent any detail beyond the text. Read-only,
a handful of requests at startup and on a slow refresh. A feed that fails is skipped, never fatal.
"""

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_UA = "gspro-shotcast/1.0 (booth news reader)"
_ATOM = "{http://www.w3.org/2005/Atom}"
_LISTICLE = re.compile(r"(here are|here's|here is|takeaways)\b", re.IGNORECASE)


def _clean(text: str, limit: int) -> str:
    text = _WS.sub(" ", _TAGS.sub(" ", html.unescape(text or ""))).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _source(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


def _published(text: str | None) -> datetime | None:
    """An RSS pubDate ("Sun, 18 Oct 2026 18:54:19 EST") or an Atom ISO date, timezone-aware, or None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return when if when.tzinfo else when.astimezone()


def age_text(published: datetime, now: datetime) -> str:
    """When a story came out, the way people say it: "today", "yesterday", "on Sunday, 2 days ago"."""
    days = (now.date() - published.astimezone(now.tzinfo).date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"on {published.astimezone(now.tzinfo):%A}, {days} days ago"


@dataclass
class Headline:
    title: str
    summary: str
    source: str
    published: datetime | None = None

    def substantive(self) -> bool:
        """Whether the summary gives the booth something to talk about. A teaser ("What were the keys
        to the comeback? Who stepped up?") or a listicle intro ("Here are the main takeaways...") has
        no facts in it, and the booth can only be vague about it."""
        text = self.summary.strip()
        return len(text.split()) >= 8 and not _LISTICLE.match(text) and not text.rstrip(" \"'”").endswith("?")

    def as_fact(self, now: datetime | None = None) -> str:
        when = f", published {age_text(self.published, now or datetime.now().astimezone())}" if self.published else ""
        return f'"{self.title}"' + (f" — {self.summary}" if self.summary else "") + f" (via {self.source}{when})"

    def age_days(self, now: datetime) -> float | None:
        return (now - self.published).total_seconds() / 86400 if self.published else None


class GolfNews:
    """Live golf headlines, refreshed on demand. Safe to use before the first fetch (empty)."""

    def __init__(self, feeds: list[str], limit: int = 12, timeout: float = 10.0, max_age_days: float = 3.0) -> None:
        self.feeds = list(feeds)
        self.limit = limit
        self.max_age_days = max_age_days
        self.timeout = timeout
        self.headlines: list[Headline] = []

    async def refresh(self) -> int:
        items: list[Headline] = []
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": _UA},
                                     follow_redirects=True) as client:
            for url in self.feeds:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    items += self._parse(resp.text, _source(url))
                except Exception as e:  # a down or malformed feed must never take the booth off air
                    log.warning("Golf news feed %s failed: %s", url, e)
        seen: set[str] = set()
        deduped: list[Headline] = []
        for h in items:
            key = h.title.lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(h)
        if deduped:  # keep the last good set if a refresh comes back empty
            self.headlines = deduped[: self.limit]
            log.info("Golf news: %d headlines from %d feed(s)", len(self.headlines), len(self.feeds))
        else:
            log.info("Golf news: no headlines fetched; keeping %d existing", len(self.headlines))
        return len(self.headlines)

    def _parse(self, text: str, source: str) -> list[Headline]:
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as e:
            log.warning("Golf news feed %s wasn't valid XML: %s", source, e)
            return []
        out: list[Headline] = []
        for item in root.iter("item"):  # RSS 2.0
            title = _clean(item.findtext("title") or "", 200)
            if title:
                out.append(Headline(title, _clean(item.findtext("description") or "", 300), source,
                                    _published(item.findtext("pubDate"))))
        if out:
            return out
        for entry in root.iter(f"{_ATOM}entry"):  # Atom fallback
            title = _clean(entry.findtext(f"{_ATOM}title") or "", 200)
            summary = entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content") or ""
            if title:
                when = entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")
                out.append(Headline(title, _clean(summary, 300), source, _published(when)))
        return out

    def pick(self, rng) -> Headline | None:
        return rng.choice(self.headlines) if self.headlines else None

    def pick_many(self, rng, n: int, avoid: set[str] = frozenset(), now: datetime | None = None) -> list[Headline]:
        """Up to n different stories worth a real exchange, none of them in `avoid` and none older than
        max_age_days (a story with no date is taken as current)."""
        now = now or datetime.now().astimezone()
        pool = [h for h in self.headlines if h.substantive() and h.title not in avoid
                and ((age := h.age_days(now)) is None or age <= self.max_age_days)]
        rng.shuffle(pool)
        return pool[:n]
