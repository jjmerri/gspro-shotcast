"""The static scorecard (par, stroke index, tee yardages, altitude) for a GSPro course.

GSPro ships this per course in two files, between them covering every course: a labelled, tab
separated `<course>.unity3d.csv` (also has yardages), and a pipe-delimited `coursedetails.txt`.
Reading either is exact and needs no OCR, so handicap stroke allocation and par-based scoring
don't depend on catching the on-screen scorecard.

Course folder names are munged ("sandbox_gsp" is "The Sandbox", "giantoaks_gsp" is "Giant Oak
Golf club"), so a course is found by the real name inside these files, not the folder name.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .league import course_match_score

log = logging.getLogger(__name__)

# base dir -> (folder count when built, {canonical course name: folder}). Rebuilt if the count changes.
_index_cache: dict[str, tuple[int, dict[str, Path]]] = {}


@dataclass
class CourseCard:
    name: str
    par: dict[int, int]
    index: dict[int, int]
    location: str | None = None
    altitude_ft: float | None = None
    yards: dict[str, dict[int, int]] = field(default_factory=dict)  # tee name -> hole -> yards


def load(courses_dirs: str | Path | list, course_name: str | None, folder: str | None = None) -> CourseCard | None:
    """The card for the GSPro course whose name best matches `course_name`, across one or more
    course libraries, or None. `folder` is the course folder GSPro's log says was selected: two
    courses can match a name equally ("Alpine Valley" is in "Swiss Alpine Valley" and in "Alpine
    Valley - Meadows & Sugar Creek"), and that folder breaks the tie. It never beats a better match:
    it may still be the last course's, logged before this one was picked."""
    if not course_name:
        return None
    dirs = [courses_dirs] if isinstance(courses_dirs, (str, Path)) else courses_dirs
    best, score = None, 0.0
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for name, path in _index(base).items():
            s = course_match_score(name, course_name)
            if s > score or (s == score and folder and path.name.lower() == folder.lower()):
                best, score = path, s
    if best is None or score < 0.6:
        log.info("No GSPro course matched %r (best score %.2f)", course_name, score)
        return None
    return _load_folder(best)


def _index(base: Path) -> dict[str, Path]:
    folders = [d for d in base.iterdir() if d.is_dir()]
    cached = _index_cache.get(str(base))
    if cached and cached[0] == len(folders):
        return cached[1]
    index: dict[str, Path] = {}
    for folder in folders:
        name = _canonical_name(folder)
        if name:
            index[name] = folder
    _index_cache[str(base)] = (len(folders), index)
    return index


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
    for csv in folder.glob("*.unity3d.csv"):
        try:
            cells = _first_line(csv).split("\t")
            if len(cells) > 1 and cells[0].strip() == "CourseName" and cells[1].strip():
                return cells[1].strip()
        except OSError:
            pass
    return None


def _load_folder(folder: Path) -> CourseCard | None:
    for csv in folder.glob("*.unity3d.csv"):  # preferred: also carries yardages
        card = _parse_csv(csv)
        if card:
            return card
    details = folder / "coursedetails.txt"
    if details.is_file():
        return _parse_details(details)
    return None


# ---- the labelled CSV -------------------------------------------------------------------------

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
                yards[label] = row
    if not par:
        return None
    return CourseCard(name=name or path.stem, par=par, index=index, location=location,
                      altitude_ft=altitude, yards=yards)


# ---- the pipe-delimited coursedetails.txt ----------------------------------------------------
# Layout: name|author|altitude(ft)|description|location|1|1|total par|<N pars>|<N stroke indexes>|
# <per-tee rating|slope pairs>|... The description can hold anything, so par and index are found by
# their shape: N pars (3..6) followed by N indexes that are exactly 1..N.


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
                    name=tokens[0] or path.parent.name,
                    par={h + 1: pars[h] for h in range(n)},
                    index={h + 1: index[h] for h in range(n)},
                    location=tokens[4] if len(tokens) > 4 and not tokens[4].isdigit() else None,
                    altitude_ft=altitude,
                )
    return None
