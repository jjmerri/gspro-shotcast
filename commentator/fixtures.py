"""Regression fixtures: real GSPro screens and log lines the booth is known to read right, kept in the
repo (logs redacted) so a GSPro update, a new screen or a re-picked data tile can be checked against them.

- A screen fixture is the HUD regions the booth OCRs, cropped from one full-resolution frame, plus
  what the booth read from them. The test re-reads the crops and
  expects the same; `fixture check` reads a new frame and reports any field or tile the fixtures read
  but the new frame doesn't (the HUD moved, a tile was swapped).
- A log fixture is one round's game-state lines from GSPro's output_log.txt (only the lines the parser
  uses, with Windows user names and player names replaced), plus the events they parse to. The test
  re-parses them; `fixture check` parses the live log's last round and reports any line kind the
  fixtures have that it doesn't, and any line that changed wording.

Screen fixtures come in kinds, each its own set of regions and reads: `shot` (the HUD on a full shot),
`scorecard` (GSPro's between-holes card), `setup_players` (the setup screen's PLAYERS tab: the Play
button's course and the slot names), `setup_holes` (its HOLES tab: which nines are picked) and
`setup_match` (its MATCH SETTINGS tab: whether the game mode is a scramble).

The booth saves `log_dir/fixture-frame.png` on each round's first full shot (the tiles holding real
values), and `fixture-frame-<kind>.png` when it reads the scorecard or either setup tab, so
`fixture save NAME` works over ssh, with no one at the screen.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from .config import DEFAULT_REGIONS, Region
from .hud import Hud
from .sources.gspro_log import parse_events

ROOT = Path("tests/fixtures")
# The HUD regions read for a shot. Not `others` (other players' names) or the minimap (course art).
SCREEN_REGIONS = ("player_box", "hole_box", "tiles", "tiles_left", "lie", "club")
SETUP_SLOTS = tuple(f"setup_slot_{i}" for i in range(1, 9))
KIND_REGIONS = {
    "shot": SCREEN_REGIONS,
    "scorecard": ("scorecard_title", "scorecard"),
    "setup_players": ("setup_course", "setup_players_tab", *SETUP_SLOTS),
    "setup_holes": ("setup_course", "setup_holes_tab", "setup_front9_box", "setup_back9_box"),
    "setup_match": ("setup_course", "setup_match_tab", "setup_game_mode"),
}
FIELDS = ("player_text", "shot_number", "to_pin", "score_to_par", "course", "par", "hole_length", "hole_number", "lie",
          "club", "handed")


def _regions(regions: dict[str, Region] | None) -> dict[str, Region]:
    return {**DEFAULT_REGIONS, **(regions or {})}


# ---- screens ----------------------------------------------------------------------------------

def summary(hud: Hud) -> dict:
    """What the booth read, as compared across fixtures."""
    out = {k: getattr(hud, k) for k in FIELDS}
    out["tiles"] = dict(sorted(hud.tiles.items()))
    return out


def coverage(s: dict) -> tuple[set[str], set[str]]:
    """(fields read, tile labels read) of a summary."""
    return {k for k in FIELDS if s.get(k) not in (None, "")}, set(s.get("tiles") or {})


def read_kind(kind: str, screen, img: Image.Image) -> dict:
    """What the booth reads from a frame of this kind, as compared across fixtures. The setup reads use
    the setup watcher's own tests (tab lit, pill filled, box checked), so a fixture fails where it would."""
    if kind == "shot":
        return summary(screen.read_hud(list(SCREEN_REGIONS), img))
    if kind == "scorecard":
        hud = screen.read_hud(list(KIND_REGIONS["scorecard"]), img)
        card = hud.scorecard
        # JSON round-trip so int hole keys compare equal to a loaded fixture's string keys.
        return json.loads(json.dumps({"open": hud.scorecard_open, "holes": card.holes if card else [],
                                      "par": card.par if card else {}, "scores": card.scores if card else {}}))
    from . import engine as e
    tones = screen.region_tones(list(KIND_REGIONS[kind]), img)
    course = e._play_button_course(" ".join(screen.read_region_lines(["setup_course"], img).get("setup_course", [])))
    if kind == "setup_players":
        filled = [s for s in SETUP_SLOTS if s in tones and e._slot_state(tones[s]) == "filled"]
        lines = screen.read_region_lines(filled, img) if filled else {}
        return {"course": course, "players_tab": tones["setup_players_tab"].mean >= e._PLAYERS_TAB_LIT,
                "slots": [e._setup_slot_name(lines.get(s, [])) if s in filled else None for s in SETUP_SLOTS]}
    if kind == "setup_holes":
        f, b = (tones[n].light >= e._NINE_CHECKED for n in ("setup_front9_box", "setup_back9_box"))
        return {"course": course, "holes_tab": tones["setup_holes_tab"].mean >= e._HOLES_TAB_LIT,
                "picked": [1, 18] if f and b else [1, 9] if f else [10, 9] if b else None}
    if kind == "setup_match":
        mode = " ".join(screen.read_region_lines(["setup_game_mode"], img).get("setup_game_mode", []))
        return {"course": course, "match_tab": tones["setup_match_tab"].mean >= e._MATCH_TAB_LIT,
                "mode_read": bool(mode.strip()), "scramble": "SCRAMBLE" in mode.upper()}
    raise ValueError(f"unknown screen fixture kind {kind!r}")


def save_screen(frame: Image.Image, screen, name: str, meta: dict, root: Path = ROOT, kind: str = "shot") -> Path:
    regions = _regions(screen.cfg.regions)
    img = frame.convert("RGB")
    out = root / "screens" / name
    out.mkdir(parents=True, exist_ok=True)
    boxes = {}
    for r in KIND_REGIONS[kind]:
        left, top, w, h = regions[r].to_pixels(img.width, img.height)
        boxes[r] = [left, top, w, h]
        img.crop((left, top, left + w, top + h)).save(out / f"{r}.png", optimize=True)
    data = {"captured": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **meta, "size": [img.width, img.height],
            "regions": {r: regions[r].model_dump() for r in KIND_REGIONS[kind]}, "boxes": boxes,
            "expected": read_kind(kind, screen, img)}
    (out / "fixture.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return out


def load_screen(path: Path) -> tuple[Image.Image, dict]:
    """The fixture's frame rebuilt (its crops pasted back where they were cut from), and its data."""
    data = json.loads((path / "fixture.json").read_text(encoding="utf-8"))
    frame = Image.new("RGB", tuple(data["size"]))
    for r, (left, top, _w, _h) in data["boxes"].items():
        frame.paste(Image.open(path / f"{r}.png").convert("RGB"), (left, top))
    return frame, data


def fixture_kind(path: Path) -> str:
    return json.loads((path / "fixture.json").read_text(encoding="utf-8")).get("kind", "shot")


def screen_fixtures(root: Path = ROOT, kind: str | None = None) -> list[Path]:
    paths = sorted(p.parent for p in (root / "screens").glob("*/fixture.json"))
    return [p for p in paths if kind is None or fixture_kind(p) == kind]


def check_screen(frame: Image.Image, screen, root: Path = ROOT, kind: str = "shot") -> list[str]:
    """What the fixtures of this kind read that this frame doesn't: one line per missing field or tile
    (for the setup and scorecard kinds, any read that came back empty or false here but not there)."""
    got = read_kind(kind, screen, frame.convert("RGB"))
    problems = []
    if kind != "shot":
        for path in screen_fixtures(root, kind):
            want = json.loads((path / "fixture.json").read_text(encoding="utf-8"))["expected"]
            for k, v in want.items():
                if k == "slots":
                    if any(v) and not any(got.get(k) or []):
                        problems.append(f"{path.name}: setup slot names read there but none now")
                elif k == "scramble":
                    continue   # a setting, not a read: another game mode is still a good read
                elif v and not got.get(k):
                    problems.append(f"{path.name}: the {kind} {k} read there but not now")
        return problems
    fields, tiles = coverage(got)
    for path in screen_fixtures(root, "shot"):
        data = json.loads((path / "fixture.json").read_text(encoding="utf-8"))
        want_f, want_t = coverage(data["expected"])
        for f in sorted(want_f - fields):
            problems.append(f"{path.name}: the HUD's {f} read there but not now")
        for t in sorted(want_t - tiles):
            problems.append(f"{path.name}: the {t!r} tile read there but not now")
    return problems


# ---- logs -------------------------------------------------------------------------------------

CORE_KINDS = {"course", "state", "tdist", "select", "shot_fired", "launch_mode", "material", "penalty_surface"}
_USER_PATH = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"']+", re.I)


def redact_text(line: str, names: list[str] = ()) -> str:
    line = _USER_PATH.sub(r"\1USER", line)
    for n in sorted({w for name in names for w in [name, *name.split()] if len(w) > 2}, key=len, reverse=True):
        line = re.sub(rf"\b{re.escape(n)}\b", "Player", line, flags=re.I)
    return line


def last_round(text: str) -> list[str]:
    """The lines of the log's last round (from its course load to its end), else the log's tail."""
    lines = text.splitlines()
    end = next((i for i in range(len(lines) - 1, -1, -1)
                if lines[i].startswith(("Round Is Complete", "End Round Button Click"))), None)
    if end is not None:
        start = next((i for i in range(end, -1, -1) if lines[i].startswith("Selected Course:")), None)
        if start is not None:
            return lines[start:end + 1]
    return lines[-20000:]


def excerpt(text: str, names: list[str] = ()) -> str:
    """The last round's lines the parser uses (the version line first), redacted."""
    keep = [ln for ln in text.splitlines() if ln.startswith("GSPro Version:")][:1]
    keep += [ln for ln in last_round(text) if parse_events(ln)]
    return "\n".join(redact_text(ln, names) for ln in keep) + "\n"


def events_json(text: str) -> list[dict]:
    # Through JSON and back, so it compares equal to a saved expected.json (tuples become lists).
    return json.loads(json.dumps([{k: v for k, v in asdict(e).items() if v is not None} for e in parse_events(text)]))


def save_log(text: str, name: str, names: list[str] = (), root: Path = ROOT) -> Path:
    out = root / "logs" / name
    out.mkdir(parents=True, exist_ok=True)
    ex = excerpt(text, names)
    (out / "output_log.txt").write_text(ex, encoding="utf-8")
    (out / "expected.json").write_text(json.dumps(events_json(ex), indent=0), encoding="utf-8")
    return out


def log_fixtures(root: Path = ROOT) -> list[Path]:
    return sorted(p.parent for p in (root / "logs").glob("*/expected.json"))


def check_log(text: str, root: Path = ROOT) -> list[str]:
    """Line kinds every log fixture has but the live log's last round doesn't, and changed wording."""
    evs = parse_events("\n".join(last_round(text)))
    kinds = {e.kind for e in evs}
    problems = [f"a GSPro log line changed wording: {e.display!r}" for e in evs if e.kind == "unparsed"][:5]
    fixtures = [{e["kind"] for e in json.loads((p / "expected.json").read_text(encoding="utf-8"))}
                for p in log_fixtures(root)]
    if fixtures:
        # Only kinds every round logs (a quit round has no "Round Is Complete", a dry one no splash).
        always = set.intersection(*fixtures) & CORE_KINDS
        for k in sorted(always - kinds):
            problems.append(f"no {k!r} lines in the last round, though every fixture round has them")
    return problems
