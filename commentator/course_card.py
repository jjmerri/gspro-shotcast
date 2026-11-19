"""The static scorecard (par, stroke index, tee yardages and ratings, altitude) for a GSPro course,
and the GSPro listing's course notes the booth's commentary draws on.

GSPro ships this per course in two files, between them covering every course: a labelled, tab
separated CSV (also has yardages, in metres like every course file), and a pipe-delimited
`coursedetails.txt`. The CSV is `<course>.gspcrse.csv` for a course in GSPro's current format, and
`<course>.unity3d.csv` for one in the old: a folder with both keeps the old beside the current, maybe
from an earlier version of the course (another par, other tees), so the current one is read first.
Reading either is exact and needs no OCR, so handicap stroke allocation and par-based scoring
don't depend on catching the on-screen scorecard.

The course file (`<course>.GKD`, JSON) gives each tee box's whole length, and the listing's metadata:
the course description (`CourseInfo`, the same text as `coursedetails.txt`'s, which is read first)
and its style tags (the `Keyword*` flags and `KeywordsString`). These are only
commentary background: a field that's missing or malformed is left unset and never costs the scorecard.

Course folder names are munged ("sandbox_gsp" is "The Sandbox", "giantoaks_gsp" is "Giant Oak
Golf club"), so a course is found by the real name inside these files, not the folder name.
"""

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .league import course_match_score
from .sources.gspro_log import M_TO_YD

log = logging.getLogger(__name__)

# base dir -> (folder count when built, {canonical course name: folder}, names more than one folder has).
# Rebuilt if the count changes.
_index_cache: dict[str, tuple[int, dict[str, Path], set[str]]] = {}


@dataclass
class CourseCard:
    name: str
    par: dict[int, int]
    index: dict[int, int]
    location: str | None = None
    altitude_ft: float | None = None
    yards: dict[str, dict[int, int]] = field(default_factory=dict)  # tee name -> hole -> yards
    folder: str | None = None   # the GSPro course folder it was read from
    ratings: dict[str, tuple[float, float]] = field(default_factory=dict)  # tee name -> (course rating, slope)
    lengths: dict[str, tuple[int, int]] = field(default_factory=dict)  # GKD tee box -> (yards, holes): see tee_lengths
    description: str | None = None   # the GSPro listing's description of the course, whitespace collapsed
    styles: list[str] = field(default_factory=list)   # plain words: "links", "major championship venue", ...
    notes_read: bool = False         # the two above were looked for (load's `notes`, or add_notes)
    path: Path | None = field(default=None, repr=False, compare=False)   # the folder, in its course library
    name_unsure: bool = False   # the name asked for was a near miss, or matched another course as well


# The tees coursedetails.txt rates, in its order: the same as the CSV's tee rows.
TEE_ORDER = ("Black", "White", "Green", "Blue", "Yellow", "Red", "Junior", "Par 3")


def load(courses_dirs: str | Path | list, course_name: str | None, folder: str | None = None,
         notes: bool = True) -> CourseCard | None:
    """The card for the GSPro course whose name best matches `course_name`, across one or more
    course libraries, or None. See find() for `folder`. `notes` also reads the listing's course notes
    (description and styles), which only commentary uses."""
    path, unsure = _find(courses_dirs, course_name, folder)
    card = _load_folder(path) if path else None
    if card:
        card.folder, card.path, card.name_unsure = path.name, path, unsure
        gkd = _read_gkd(path)
        card.lengths = tee_lengths(gkd) if gkd else {}
        if notes:
            _add_listing(card, path, gkd)
    return card


# The course file's (GKD) tee boxes, as hole_layouts.tee_box names them.
GKD_TEES = ("Black", "White", "Green", "Blue", "Yellow", "Red", "Junior", "Par3")


def _read_gkd(folder: Path) -> dict | None:
    """The course file (GKD) as JSON, or None: the folder's shortest-named one that isn't its par-3 course."""
    files = [f for f in folder.glob("*") if f.suffix.lower() == ".gkd" and "par3" not in f.stem.lower()]
    if not files:
        return None
    try:
        data = json.loads(min(files, key=lambda f: len(f.name)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError) as e:
        log.info("Couldn't read the course file in %s (%s)", folder, e)
        return None
    return data if isinstance(data, dict) else None


def tee_lengths(data: dict) -> dict[str, tuple[int, int]]:
    """Each tee box's length over the whole course, as the course file (GKD, read by _read_gkd) gives it
    hole by hole, in yards (it stores metres), with its hole count: tee box -> (yards, holes). The CSV's
    tee rows can't say this: their units vary by course, and many courses have none. Boxes missing a hole
    are left out."""
    holes = data.get("Holes")
    holes = [h for h in holes if isinstance(h, dict) and h.get("HoleNumber")
             and h.get("Enabled", True)] if isinstance(holes, list) else []
    per: dict[str, list[float]] = {}
    for h in holes:
        tees = h.get("Tees")
        for t in tees if isinstance(tees, list) else []:
            if not isinstance(t, dict):
                continue
            d = t.get("Distance")
            if t.get("TeeType") in GKD_TEES and t.get("Enabled", True) and isinstance(d, (int, float)) \
                    and not isinstance(d, bool) and 0 < d < 1e6:
                per.setdefault(t["TeeType"], []).append(d)
    return {tee: (round(sum(ds) * M_TO_YD), len(ds)) for tee, ds in per.items() if len(ds) == len(holes)}


# A match score (league.course_match_score) this high is the course itself, not a namesake: every word
# of the shorter name is in the longer, or an OCR slip away.
SURE_MATCH = 0.85


def find(courses_dirs: str | Path | list, course_name: str | None, folder: str | None = None) -> Path | None:
    """The folder of the GSPro course whose name best matches `course_name`, or None. Names can
    match equally ("Alpine Valley" is in "Swiss Alpine Valley" and in "Alpine Valley - Meadows &
    Sugar Creek"): `folder`, the course folder GSPro's log says was selected, breaks that tie, then
    the closer spelling. It never beats a better match: it may still be the last course's, logged
    before this one was picked."""
    return _find(courses_dirs, course_name, folder)[0]


def locate(courses_dirs: str | Path | list, course_name: str | None,
           folder: str | None = None) -> tuple[Path | None, bool]:
    """find()'s folder for `course_name`, and whether it's unsure (see _find)."""
    return _find(courses_dirs, course_name, folder)


def _find(courses_dirs, course_name: str | None, folder: str | None = None) -> tuple[Path | None, bool]:
    """find()'s folder, and whether it's unsure: a near miss (only some of the name's words, or other
    numbers: Pinehurst No. 2 isn't No. 4), or another course's name matched as well (a tie the folder
    or the spelling broke)."""
    if not course_name:
        return None, False
    dirs = [courses_dirs] if isinstance(courses_dirs, (str, Path)) else courses_dirs
    best, best_name, key, scores = None, "", (0.0, False, 0.0), []
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        index, shared = _indexes(base)
        for name, path in index.items():
            if name in shared:
                scores.append((course_match_score(name, course_name), None))   # the folder it hides
            k = (course_match_score(name, course_name), bool(folder) and path.name.lower() == folder.lower(),
                 difflib.SequenceMatcher(None, name.lower(), course_name.lower()).ratio())
            scores.append((k[0], path))
            if k > key:
                best, best_name, key = path, name, k
    if best is None or key[0] < 0.6:
        log.info("No GSPro course matched %r (best score %.2f)", course_name, key[0])
        return None, False
    numbers = [set(re.findall(r"\d+", plain_name(n))) for n in (best_name, course_name)]
    return best, (key[0] < SURE_MATCH or numbers[0] != numbers[1]
                  or any(score == key[0] and path != best for score, path in scores))


def identify(courses_dirs: str | Path | list, course_name: str | None) -> str | None:
    """The folder of the installed GSPro course that is `course_name` itself, give or take case and
    punctuation, or None. Unlike find(), never a near miss: "Swiss Alpine Valley", when it isn't
    installed, isn't "Alpine Valley - Meadows & Sugar Creek"."""
    path = find(courses_dirs, course_name)
    if path is None:
        return None
    name = next((n for d in ([courses_dirs] if isinstance(courses_dirs, (str, Path)) else courses_dirs)
                 if Path(d).is_dir() for n, p in _index(Path(d)).items() if p == path), "")
    same = difflib.SequenceMatcher(None, plain_name(name), plain_name(course_name)).ratio() >= 0.9
    return path.name if same else None


def plain_name(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def _index(base: Path) -> dict[str, Path]:
    return _indexes(base)[0]


def _indexes(base: Path) -> tuple[dict[str, Path], set[str]]:
    """`base`'s course names to folders, and the names more than one folder has (two builds of one
    course; the index keeps one of them)."""
    folders = [d for d in base.iterdir() if d.is_dir()]
    cached = _index_cache.get(str(base))
    if cached and cached[0] == len(folders):
        return cached[1], cached[2]
    index: dict[str, Path] = {}
    shared: set[str] = set()
    for folder in folders:
        name = _canonical_name(folder)
        if name:
            if name in index:
                shared.add(name)
            index[name] = folder
    _index_cache[str(base)] = (len(folders), index, shared)
    return index, shared


def _first_line(path: Path) -> str:
    with path.open(encoding="utf-8-sig", errors="replace") as f:
        return f.readline()


def _canonical_name(folder: Path) -> str | None:
    """The course's display name, read cheaply from whichever file the folder has."""
    details = folder / "coursedetails.txt"
    if details.is_file():
        try:
            name = _first_line(details).split("|", 1)[0].strip()
            if name:
                return name
        except OSError:
            pass
    for csv in _csvs(folder):
        try:
            cells = _first_line(csv).split("\t")
            if len(cells) > 1 and cells[0].strip() == "CourseName" and cells[1].strip():
                return cells[1].strip()
        except OSError:
            pass
    return None


def _load_folder(folder: Path) -> CourseCard | None:
    details_file = folder / "coursedetails.txt"
    details = _parse_details(details_file) if details_file.is_file() else None
    for csv in _csvs(folder):  # preferred: also carries yardages
        card = _parse_csv(csv)
        if card:
            if details and details.ratings:
                # The ratings are in tee order: name them by the CSV's tee rows when it has them all.
                names = list(card.yards) if len(card.yards) == len(TEE_ORDER) else list(TEE_ORDER)
                card.ratings = {names[TEE_ORDER.index(t)]: v for t, v in details.ratings.items()}
            return card
    return details


# ---- the labelled CSV -------------------------------------------------------------------------

def _csvs(folder: Path) -> list[Path]:
    """The folder's scorecard CSVs, the current course format's first (see the module docstring)."""
    return [*folder.glob("*.gspcrse.csv"), *folder.glob("*.unity3d.csv")]


_META = {"coursename", "courselocation", "author", "version", "hole", "par", "index"}


def _holes(cells: list[str]) -> list[int | None]:
    """Per column of the HOLE row, the hole number, or None for OUT/IN/TOTAL and blanks."""
    return [int(c) if c.isdigit() and 1 <= int(c) <= 18 else None for c in cells]


def _by_hole(order: list[int | None], cells: list[str]) -> dict[int, int]:
    out = {}
    for i, hole in enumerate(order):
        if hole is not None and i < len(cells) and re.fullmatch(r"-?\d+", cells[i] or ""):
            out[hole] = int(cells[i])
    return out


def _parse_csv(path: Path) -> CourseCard | None:
    name = location = None
    altitude = None
    order: list[int | None] | None = None
    par: dict[int, int] = {}
    index: dict[int, int] = {}
    yards: dict[str, dict[int, int]] = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        cells = [c.strip() for c in line.split("\t")]
        label, values = cells[0], cells[1:]
        if label == "CourseName":
            name = values[0] if values else None
        elif label == "CourseLocation":
            location = values[0] if values else None
        elif label.startswith("Altitude"):
            # Header reads "(m)(ft)", but GSPro stores feet in the first value (what its UI shows);
            # the second is a bad meters-to-feet conversion of that same number, so ignore it.
            nums = [float(v) for v in values if re.fullmatch(r"-?\d+(\.\d+)?", v or "")]
            altitude = nums[0] if nums else None
        elif label == "HOLE":
            order = _holes(values)
        elif label == "Par" and order:
            par = _by_hole(order, values)
        elif label == "Index" and order:
            index = _by_hole(order, values)
        elif order and label and label.lower() not in _META:
            row = _by_hole(order, values)
            if row:
                yards[label] = {h: round(m * M_TO_YD) for h, m in row.items()}   # it's metres
    if not par:
        return None
    return CourseCard(name=name or path.stem, par=par, index=index, location=location,
                      altitude_ft=altitude, yards=yards)


# ---- the pipe-delimited coursedetails.txt ----------------------------------------------------
# Layout: name|author|altitude(ft)|description|location|1|1|total par|<N pars>|<N stroke indexes>|
# <per-tee rating|slope pairs>|... The description can hold anything, so par and index are found by
# their shape: N pars (3..6) followed by N indexes that are exactly 1..N. The rating and slope pairs
# follow in TEE_ORDER, blank for a tee the course doesn't have.


def _ints(tokens: list[str]) -> list[int] | None:
    out = []
    for t in tokens:
        if not re.fullmatch(r"-?\d+", t):
            return None
        out.append(int(t))
    return out


def _parse_details(path: Path) -> CourseCard | None:
    tokens = [t.strip() for t in path.read_text(encoding="utf-8-sig", errors="replace").split("|")]
    for i in range(len(tokens)):
        for n in (18, 9):
            pars = _ints(tokens[i:i + n])
            index = _ints(tokens[i + n:i + 2 * n])
            if pars and index and all(3 <= p <= 6 for p in pars) and set(index) == set(range(1, n + 1)):
                altitude = float(tokens[2]) if len(tokens) > 2 and re.fullmatch(r"-?\d+(\.\d+)?", tokens[2]) else None
                return CourseCard(
                    ratings=_ratings(tokens[i + 2 * n:i + 2 * n + 2 * len(TEE_ORDER)]),
                    name=tokens[0] or path.parent.name,
                    par={h + 1: pars[h] for h in range(n)},
                    index={h + 1: index[h] for h in range(n)},
                    location=tokens[4] if len(tokens) > 4 and not tokens[4].isdigit() else None,
                    altitude_ft=altitude,
                )
    return None


def _ratings(tokens: list[str]) -> dict[str, tuple[float, float]]:
    """Each rated tee's (course rating, slope), from pairs in TEE_ORDER. A blank or malformed pair (one
    course file has "54.0.120") is a tee without a rating."""
    out = {}
    for tee, k in zip(TEE_ORDER, range(0, len(tokens) - 1, 2)):
        rating, slope = tokens[k], tokens[k + 1]
        if re.fullmatch(r"\d+(\.\d+)?", rating) and re.fullmatch(r"\d+(\.\d+)?", slope):
            r, sl = float(rating), float(slope)
            if 20 <= r <= 90 and 55 <= sl <= 155:
                out[tee] = (r, sl)
    return out


# ---- the listing's course notes (commentary background) ----------------------------------------

# A description shorter than this is a placeholder ("A scenic place.", the course's name), not notes.
MIN_DESCRIPTION = 40
_PLACEHOLDER = re.compile(r"lorem ipsum|\bdescription goes here\b", re.I)

# The course file's style flags, in plain words; None leaves one out (how the course was modelled,
# not what it is).
STYLE_WORDS = {
    "links": "links", "parkland": "parkland", "heathland": "heathland", "coastal": "coastal",
    "desert": "desert", "mountain": "mountain", "tropical": "tropical", "historic": "historic",
    "majorvenue": "major championship venue", "tourstop": "pro tour stop",
    "fantasy": "fictional course", "beginnerfriendly": "beginner-friendly",
    "lowpoly": None, "training": None,
}


def add_notes(card: CourseCard) -> None:
    """Read the listing's course notes into a card load() read without them (the booth went on air)."""
    if not card.notes_read and card.path and card.path.is_dir():
        _add_listing(card, card.path, _read_gkd(card.path))


def _add_listing(card: CourseCard, folder: Path, gkd: dict | None) -> None:
    """Fill in the card's course notes: coursedetails.txt's first, then the course file's."""
    card.notes_read = True
    try:
        tokens = (folder / "coursedetails.txt").read_text(encoding="utf-8-sig", errors="replace").split("|")
    except OSError:
        tokens = []
    gkd = gkd or {}
    card.description = clean_description(tokens[3] if len(tokens) > 3 else None) \
        or clean_description(gkd.get("CourseInfo"))
    card.styles = _styles(gkd)


def clean_description(value) -> str | None:
    """A listing's description with its whitespace (newlines, paragraphs) collapsed, or None for a
    missing one or a placeholder."""
    if not isinstance(value, str):
        return None
    text = " ".join(re.sub(r"<br\s*/?>|\\n", " ", value, flags=re.I).split())
    return text if len(text) >= MIN_DESCRIPTION and not _PLACEHOLDER.search(text) else None


# A period that ends an abbreviation or an initial ("St. Andrews", "the U.S. Open", "A. W. Tillinghast").
_NOT_AN_END = re.compile(r"(?:\b(?:St|Mt|Ft|Mr|Mrs|Ms|Dr|Jr|Sr|No|vs|approx|est)|(?:^|[\s.])[A-Z])\.$")


def brief(text: str, limit: int = 4000) -> str:
    """`text` cut to at most `limit` characters, at the last sentence end that fits (else a word end)."""
    if len(text) <= limit:
        return text
    ends = [m.end() for m in re.finditer(r"[.!?][\"')\]]*(?=\s+[\"'(\[]?[A-Z0-9])", text[:limit + 8])
            if m.end() <= limit and not _NOT_AN_END.search(text[:m.end()])]
    if ends:
        return text[:ends[-1]]
    if limit < 4:
        return text[:max(0, limit)]
    room = limit - 3   # for the "..."
    head = text[:room + 1]
    head = head.rsplit(" ", 1)[0] if " " in head else head[:room]
    return head.rstrip(",;:-") + "..."


def _styles(gkd: dict) -> list[str]:
    """The course's style tags in plain words: its true Keyword* flags, then KeywordsString's own."""
    out: list[str] = []

    def add(word: str | None) -> None:
        if word and word.lower() not in (o.lower() for o in out):
            out.append(word)
    for key, on in gkd.items():
        if isinstance(key, str) and key.startswith("Keyword") and key != "KeywordsString" and on is True:
            add(STYLE_WORDS.get(key[len("Keyword"):].lower()))
    extra = gkd.get("KeywordsString")
    if isinstance(extra, str):
        for tag in re.split(r"[,;|/#\n]+", extra):
            tag = " ".join(tag.split())
            plain = re.sub(r"[^a-z]", "", tag.lower())
            if plain in STYLE_WORDS:
                add(STYLE_WORDS[plain])
            elif 2 < len(tag) <= 30:
                add(tag.lower())
    return out[:10]
