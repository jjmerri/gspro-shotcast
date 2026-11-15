import json

import pytest

from commentator import fixtures
from commentator.config import ScreenConfig, Region
from commentator.health import (
    Health, RoundCounters, check_changed, check_files, check_screen, evaluate_round, report_line,
)
from commentator.sources.gspro_log import GsproState, parse_events

REQUIRED = ["carry (game)", "carry (raw)", "total length"]


def test_issue_raised_shown_dismissed_and_cleared(tmp_path):
    h = Health(tmp_path / "health.json")
    seen = []
    h.add_listener(lambda key, issue: seen.append((key, issue.level if issue else None)))
    assert h.status() == (None, [])
    h.raise_issue("a", "warn", "something's off")
    h.raise_issue("a", "warn", "something's off")      # unchanged: no second notice
    assert h.status() == ("warn", ["WARN: something's off"])
    h.raise_issue("b", "fail", "broken")
    level, lines = h.status()
    assert level == "fail" and lines[0] == "FAIL: broken"
    h.dismiss()
    assert h.status() == (None, [])
    h.raise_issue("c", "warn", "new")                  # a new issue brings the chip back
    assert h.status()[0] == "fail"
    h.clear("a")
    h.clear("a")
    assert seen == [("a", "warn"), ("b", "fail"), ("c", "warn"), ("a", None)]
    again = Health(tmp_path / "health.json")           # persisted across a restart
    assert {i.key for i in again.issues()} == {"b", "c"}


def test_a_changed_fact_waits_for_a_clean_round(tmp_path):
    h = Health(tmp_path / "health.json")
    check_changed(h, "gspro_version", "3.1.6.20", "GSPro's version")
    assert not h.issues()                              # first sighting: nothing to compare against
    check_changed(h, "gspro_version", "3.1.6.20", "GSPro's version")
    assert not h.issues()
    check_changed(h, "gspro_version", "3.2.0", "GSPro's version")
    (issue,) = h.issues()
    assert issue.verify and "3.1.6.20 to 3.2.0" in issue.message
    h.clear_verified()
    assert not h.issues() and Health(tmp_path / "health.json").fact("gspro_version") == "3.2.0"


def test_a_screen_size_is_flagged_only_until_a_clean_round_reads_it(tmp_path):
    h = Health(tmp_path / "health.json")
    check_screen(h, None)                                  # GSPro's view isn't up: nothing to check
    check_screen(h, "3840x2160")
    assert not h.issues()
    h.clear_verified(screen="3840x2160")
    check_screen(h, "3450x2160")                           # the projector came on
    assert "3450x2160" in h.get("changed.screen").message
    h.clear_verified(screen="3840x2126")                   # changed mid-round: proves no size
    check_screen(h, "3450x2160")
    assert h.get("changed.screen")
    h.clear_verified(screen="3450x2160")
    for size in ("3840x2160", "3450x2160", "3840x2160"):   # projector off, on, off: both are proven
        check_screen(h, size)
        assert not h.issues()
    assert Health(tmp_path / "health.json").fact("screens_proven") == ["3840x2160", "3450x2160"]


def test_the_last_screen_size_from_before_sizes_were_remembered_counts_as_read(tmp_path):
    (tmp_path / "health.json").write_text('{"facts": {"screen": "1920x1080"}, "issues": []}')
    h = Health(tmp_path / "health.json")
    check_screen(h, "2560x1440")
    assert "rounds at 1920x1080" in h.get("changed.screen").message
    check_screen(h, "1920x1080")
    assert not h.issues()


def test_an_unproven_screen_size_is_never_taken_as_read(tmp_path):
    # From before sizes were remembered, with the last size's change still waiting: not proven.
    (tmp_path / "health.json").write_text(
        '{"facts": {"screen": "3450x2160"}, "issues": [{"key": "changed.screen", "level": "warn", '
        '"message": "the screen size changed", "since": "", "verify": true}]}')
    h = Health(tmp_path / "health.json")
    check_screen(h, "3450x2160")
    h.clear_verified(screen="3840x2160")               # the round ended at another size
    check_screen(h, "3450x2160")
    assert h.get("changed.screen") and h.fact("screens_proven") == []
    legacy = tmp_path / "legacy.json"                  # OCR turned off, then on again
    legacy.write_text((tmp_path / "health.json").read_text().replace('"screens_proven": []', '"x": 0'))
    off = Health(legacy)
    off.note_screen(None)
    off.clear("changed.screen")
    check_screen(off, "3450x2160")
    assert off.fact("screens_proven") == [] and off.get("changed.screen")
    fresh = Health()
    check_screen(fresh, "1920x1080")
    check_screen(fresh, "1920x1080")                   # a first size isn't flagged, nor proven by startups
    assert not fresh.issues() and fresh.fact("screens_proven") == []
    check_screen(fresh, "2560x1440")
    assert "no size is proven" in fresh.get("changed.screen").message


def test_missing_files_fail_and_clear(tmp_path):
    h = Health()
    log = tmp_path / "output_log.txt"
    check_files(h, {"GSPro log": str(log)})
    assert h.get("file.GSPro log").level == "fail"
    log.write_text("x")
    check_files(h, {"GSPro log": str(log)})
    assert not h.issues()


def _kinds(n):
    return {"state": n, "tdist": n, "select": n, "shot_fired": n, "launch_mode": n}


def test_a_clean_round_passes():
    c = RoundCounters(fires_at_start=10, kinds_at_start=_kinds(10), shots=40, setups=40, full_shots=25,
                      hud_to_pin_missing=2, tiles_missing={"carry (raw)": 1})
    found, report = evaluate_round(c, 50, _kinds(50), [], REQUIRED, True, True)
    assert found == [] and report["checked"]
    assert report_line(report).endswith("all clear")


def test_a_round_with_broken_reads_is_flagged():
    c = RoundCounters(fires_at_start=10, kinds_at_start=_kinds(10), shots=40, setups=40, full_shots=25,
                      hud_lie_missing=30, tiles_missing={"carry (game)": 25})
    kinds = {**_kinds(50), "tdist": 10, "unparsed": 3}
    found, _ = evaluate_round(c, 50, kinds, ["Logging tdistance 1.0"], REQUIRED, True, True)
    keys = {k for k, _, _ in found}
    assert keys == {"round.log_kinds", "round.log_unparsed", "round.hud_lie", "round.tile.carry (game)"}


def test_a_silent_log_and_a_short_round():
    c = RoundCounters(fires_at_start=10, kinds_at_start=_kinds(10), shots=12)
    found, _ = evaluate_round(c, 10, _kinds(10), [], REQUIRED, True, False)
    assert [k for k, _, _ in found] == ["round.log_silent"]
    short = RoundCounters(fires_at_start=10, kinds_at_start=_kinds(10), shots=3, setups=3)
    found, report = evaluate_round(short, 10, _kinds(10), [], REQUIRED, True, True)
    assert found == [] and not report["checked"]


def test_log_version_and_reworded_lines():
    text = ("GSPro Version: 3.1.6.20\n"
            "Logging tdist: 12.5 and ActiveGameGimmieDistance: 1.22\n"
            "Logging tdist: 12.5 and GimmeDistance: 1.22\n")      # the same line, reworded
    evs = parse_events(text)
    assert evs[0].kind == "version" and evs[0].display == "3.1.6.20"
    assert evs[-1].kind == "unparsed"
    s = GsproState()
    for e in evs:
        s.apply(e)
    assert s.version == "3.1.6.20" and s.kinds["unparsed"] == 1 and s.unparsed


def test_gspro_4_leaves_collider_debug_line_is_not_a_reworded_line():
    # GSPro 4.0, a pine-straw shot into a tree: the debug line around the hit isn't a wording change,
    # and the hit itself still parses.
    text = ("LeavesCollider buried IGNORED, leavesHit = True, trunkHit = False\n"
            " ** Leaves hitTime, 0.52 , HitCount 1, Penalty 24.6% , Accumulated penalty 24.6% \n"
            "LeavesCollider buried IGNORED, leavesHit = True, trunkHit = False\n")
    assert [(e.kind, e.leaves) for e in parse_events(text)] == [("leaves", (0.52, 1, 24.6, 24.6))]
    # a line glued onto it still parses
    glued = parse_events("LeavesCollider buried IGNORED, leavesHit = True, trunkHit = FalseRoll dist ft9.689")
    assert [e.kind for e in glued] == ["roll"]


def test_log_redaction():
    line = r"Loading C:\Users\jjmer\AppData\LocalLow\GSPro and Alex Sample hit"
    out = fixtures.redact_text(line, ["Alex Sample"])
    assert "jjmer" not in out and "Alex" not in out and "Sample" not in out and r"C:\Users\USER" in out


# ---- the committed fixtures --------------------------------------------------------------------

@pytest.mark.parametrize("path", fixtures.log_fixtures(), ids=lambda p: p.name)
def test_log_fixture_parses_as_recorded(path):
    text = (path / "output_log.txt").read_text(encoding="utf-8")
    assert fixtures.events_json(text) == json.loads((path / "expected.json").read_text(encoding="utf-8"))
    assert "unparsed" not in {e.kind for e in parse_events(text)}
    assert not fixtures.check_log(text)


@pytest.mark.parametrize("path", fixtures.screen_fixtures(), ids=lambda p: p.name)
def test_screen_fixture_reads_as_recorded(path):
    pytest.importorskip("rapidocr")
    from commentator.screen import Screen

    frame, data = fixtures.load_screen(path)
    screen = Screen(ScreenConfig(regions={k: Region(**v) for k, v in data["regions"].items()}))
    assert fixtures.read_kind(data.get("kind", "shot"), screen, frame) == data["expected"]


def test_setup_fixture_check_flags_a_screen_that_no_longer_reads():
    # A blank frame where the setup screen was: the tab, course and slot reads all go missing.
    pytest.importorskip("rapidocr")
    from PIL import Image
    from commentator.screen import Screen

    if not fixtures.screen_fixtures(kind="setup_players"):
        pytest.skip("no setup_players fixture")
    path = fixtures.screen_fixtures(kind="setup_players")[0]
    frame, data = fixtures.load_screen(path)
    screen = Screen(ScreenConfig(regions={k: Region(**v) for k, v in data["regions"].items()}))
    assert not fixtures.check_screen(frame, screen, kind="setup_players")
    problems = fixtures.check_screen(Image.new("RGB", frame.size), screen, kind="setup_players")
    assert any("slot names" in p for p in problems) and any("players_tab" in p for p in problems)
