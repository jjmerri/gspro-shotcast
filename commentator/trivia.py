"""The real course behind the one in GSPro, from Wikipedia, for the booth's course-trivia asides.

GSPro's courses are mostly replicas of real ones (sometimes under another name: "The Ancient Course"
is the Old Course at St Andrews). The article is found once per course, then handed to the model as
fact, so the history it tells is real rather than half-remembered. A course with no confident match
(a fictional design, a name the search can't place) gets no trivia, never a wrong article.
"""

import logging
import re

import httpx

log = logging.getLogger(__name__)

_API = "https://en.wikipedia.org/w/api.php"
_UA = "gspro-shotcast/1.0 (booth course trivia; https://github.com/jjmerri/gspro-shotcast)"
# Words that don't identify a course: a search hit has to share some other word with the name.
_GENERIC = {"the", "golf", "course", "courses", "club", "links", "country", "resort", "national", "and", "at",
            "of", "by", "gc", "cc", "old", "new", "north", "south", "east", "west", "black", "blue", "red"}
_SKIP_SECTIONS = re.compile(r"^(see also|references|external links|further reading|notes|gallery|sources)$", re.I)
MAX_CHARS = 9000


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4 and w not in _GENERIC}


def clean(text: str, limit: int = MAX_CHARS) -> str:
    """The article's prose, without its reference sections, cut at a paragraph near `limit`."""
    out, skipping = [], False
    for block in re.split(r"\n(?==+ )", text):
        head = re.match(r"=+ (.*?) =+", block)
        if head:
            skipping = bool(_SKIP_SECTIONS.match(head.group(1).strip()))
        if not skipping:
            out.append(block.strip())
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(b for b in out if b))
    text = re.sub(r"[ \t]+", " ", text)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0]
    return text


class CourseTrivia:
    """Finds and caches each course's article. Never raises."""

    def __init__(self, pages: dict[str, str] | None = None, timeout: float = 10.0) -> None:
        self.pages = {k.strip().lower(): v for k, v in (pages or {}).items()}
        self.timeout = timeout
        self._cache: dict[str, tuple[str, str] | None] = {}

    async def article(self, course: str | None) -> tuple[str, str] | None:
        """(title, text) of the course's article, or None."""
        key = (course or "").strip().lower()
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        found = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": _UA}) as client:
                if key in self.pages:
                    found = await self._extract(client, self.pages[key])
                else:
                    for title in await self._search(client, course):
                        if found := await self._extract(client, title):
                            break
        except Exception as e:
            log.warning("Course trivia lookup for %r failed: %s", course, e)
            return None   # not cached: a network blip shouldn't cost the course its trivia for good
        self._cache[key] = found
        log.info("Course trivia for %r: %s", course, f"{found[0]!r}, {len(found[1])} chars" if found else "no article")
        return found

    async def _search(self, client: httpx.AsyncClient, course: str) -> list[str]:
        """Search hits that share a distinctive word with the course's name, best first."""
        resp = await client.get(_API, params={"action": "query", "list": "search", "srsearch": f"{course} golf course",
                                              "srlimit": 5, "format": "json", "formatversion": 2})
        resp.raise_for_status()
        want = _words(course)
        hits = [h.get("title", "") for h in resp.json().get("query", {}).get("search", [])]
        return [t for t in hits if not t.lower().startswith("list of") and want & _words(t)][:3]

    async def _extract(self, client: httpx.AsyncClient, title: str) -> tuple[str, str] | None:
        resp = await client.get(_API, params={"action": "query", "prop": "extracts", "explaintext": 1,
                                              "titles": title, "redirects": 1, "format": "json",
                                              "formatversion": 2})
        resp.raise_for_status()
        page = (resp.json().get("query", {}).get("pages") or [{}])[0]
        raw = page.get("extract") or ""
        lead = raw.split("\n==", 1)[0]
        text = clean(raw)
        # The article has to be about golf from its first paragraph: "Riviera" alone finds the coast.
        if "golf" not in lead.lower() or len(text.split()) < 80:
            return None
        return page.get("title") or title, text
