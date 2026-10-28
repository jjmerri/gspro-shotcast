"""The real course behind the one in GSPro, from Wikipedia, for the booth's course-trivia asides.

GSPro's courses are mostly replicas of real ones (sometimes under another name: "The Ancient Course"
is the Old Course at St Andrews, "DPC Sodgrass" is TPC Sawgrass). Known replicas map straight to
their article by GSPro's course id or name; anything else is searched for. The article is found once
per course, then handed to the model as fact, so the history it tells is real rather than
half-remembered. A course with no confident match (a fictional design, a name the search can't place,
a menu the OCR misread as a course name) gets no trivia, never a wrong article.
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
# A searched article has to say in its lead that it's a golf course, not just mention golf: "Golf"
# (the 1984 video game) and a casino resort with a course out back both mention it.
_ABOUT_A_COURSE = re.compile(r"golf (course|club|links|resort|facility|complex)|links course|country club", re.I)
# GSPro screen text the OCR can hand over as a course name ("GAME MENU"): never a course.
_UI_TEXT = re.compile(r"\b(menu|settings|options|loading|practice|scorecard|leaderboard|paused?|quit|exit)\b", re.I)

# GSPro's replicas -> the real course's article, keyed by course_key() of GSPro's course id (the log's
# "Selected Course") or its name. Checked before any search: a replica under another name can't be
# searched for, and a club's own name can land on the wrong page. "Title#Section" takes just that
# section; None means the real course has no article of its own (Cabot Links' page isn't Cliffs').
KNOWN_PAGES: dict[str, str | None] = {
    "dpc sodgrass": "TPC at Sawgrass",
    "sodgrass": "TPC at Sawgrass",
    "dpc pebble": "Pebble Beach Golf Links",
    "pebblebeach": "Pebble Beach Golf Links",
    "pebble beach": "Pebble Beach Golf Links",
    "the ancient course": "Old Course at St Andrews",
    "bethpage black": "Bethpage Black Course",
    "pinevalley": "Pine Valley Golf Club",
    "pine valley": "Pine Valley Golf Club",
    "torrey pines south": "Torrey Pines Golf Course",
    "kiawah b 01": "Kiawah Island Golf Resort",
    "wynn": "Wynn Las Vegas#Golf course",
    "wynn golf club": "Wynn Las Vegas#Golf course",
    "cabotcliffs": None,
    "cabot cliffs": None,
}


def course_key(name: str | None) -> str:
    """A course id or name, folded so GSPro's id and its display name meet: "dpc_sodgrass_gsp" and
    "DPC Sodgrass" are both "dpc sodgrass"; version and scan suffixes drop."""
    s = re.sub(r"[_\s]+", " ", (name or "").lower())
    s = re.sub(r"\b(gsp|v\d+|lidar|scene)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def looks_like_a_course(name: str | None) -> bool:
    """False for blank names and GSPro screen text the OCR misread as one."""
    return bool(name and name.strip() and not _UI_TEXT.search(name))


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
        self.pages = {**KNOWN_PAGES, **{course_key(k): v for k, v in (pages or {}).items()}}
        self.timeout = timeout
        self._cache: dict[tuple[str, ...], tuple[str, str] | None] = {}

    async def article(self, *names: str | None) -> tuple[str, str] | None:
        """(title, text) of the course's article, or None. `names` are what the course goes by, best
        first (GSPro's display name, the scorecard's, GSPro's course id): any one of them in the known
        pages settles it, otherwise the first that looks like a course name is searched for."""
        names = tuple(n.strip() for n in names if looks_like_a_course(n))
        key = tuple(dict.fromkeys(course_key(n) for n in names))
        if not names:
            return None
        if key in self._cache:
            return self._cache[key]
        known = next((k for k in key if k in self.pages), None)
        found = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": _UA}) as client:
                if known is not None:
                    if self.pages[known]:
                        found = await self._extract(client, self.pages[known], known=True)
                else:
                    for title in await self._search(client, names[0]):
                        if found := await self._extract(client, title):
                            break
        except Exception as e:
            log.warning("Course trivia lookup for %r failed: %s", names[0], e)
            return None   # not cached: a network blip shouldn't cost the course its trivia for good
        self._cache[key] = found
        log.info("Course trivia for %r: %s", names[0], f"{found[0]!r}, {len(found[1])} chars" if found else "no article")
        return found

    async def _search(self, client: httpx.AsyncClient, course: str) -> list[str]:
        """Search hits that share a distinctive word with the course's name, best first."""
        resp = await client.get(_API, params={"action": "query", "list": "search", "srsearch": f"{course} golf course",
                                              "srlimit": 5, "format": "json", "formatversion": 2})
        resp.raise_for_status()
        want = _words(course)
        hits = [h.get("title", "") for h in resp.json().get("query", {}).get("search", [])]
        return [t for t in hits if not t.lower().startswith("list of") and want & _words(t)][:3]

    async def _extract(self, client: httpx.AsyncClient, title: str, known: bool = False) -> tuple[str, str] | None:
        title, _, section = title.partition("#")
        resp = await client.get(_API, params={"action": "query", "prop": "extracts", "explaintext": 1,
                                              "titles": title, "redirects": 1, "format": "json",
                                              "formatversion": 2})
        resp.raise_for_status()
        page = (resp.json().get("query", {}).get("pages") or [{}])[0]
        raw = page.get("extract") or ""
        if section:
            raw = section_of(raw, section)
        lead = raw.split("\n==", 1)[0]
        text = clean(raw)
        # A searched article has to be about a golf course from its first paragraph: "Riviera" alone
        # finds the coast. A known page is trusted, but still has to say something.
        if len(text.split()) < 80 or not (known or _ABOUT_A_COURSE.search(lead)):
            return None
        return page.get("title") or title, text


def section_of(text: str, name: str) -> str:
    """One section of an article's plain text (with its subsections), or "" if it has none by that name."""
    m = re.search(rf"^(=+) {re.escape(name)} =+$", text, re.M | re.I)
    if not m:
        return ""
    rest = text[m.end():]
    end = re.search(rf"^={{1,{len(m.group(1))}}} [^=].*?=+$", rest, re.M)
    return rest[:end.start() if end else None].strip()
