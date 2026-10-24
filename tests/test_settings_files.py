from pathlib import Path

import pytest
import yaml

from commentator import gspro_dir
from commentator.settings_files import set_yaml
from commentator.tray_ui import apply_changes

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = (ROOT / "config.example.yaml").read_text(encoding="utf-8")


def changed_lines(a: str, b: str) -> list[tuple[str, str]]:
    a, b = a.splitlines(), b.splitlines()
    assert len(a) == len(b)
    return [(x, y) for x, y in zip(a, b) if x != y]


def test_values_change_in_place_keeping_comments_and_their_column():
    text = set_yaml(EXAMPLE, ("booking", "account"), "Golf Shed")
    text = set_yaml(text, ("booking", "schedule_id"), 792418)
    assert changed_lines(EXAMPLE, text) == [
        ('  account: ""             # SuperSaaS account name, e.g. GolfShed',
         "  account: 'Golf Shed'    # SuperSaaS account name, e.g. GolfShed"),
        ("  schedule_id:            # the schedule's ID (end of its Configure page URL), e.g. 792418",
         "  schedule_id: 792418     # the schedule's ID (end of its Configure page URL), e.g. 792418")]
    assert set_yaml(text, ("booking", "schedule_id"), None).count("schedule_id:  ") == 1


def test_a_commented_out_example_becomes_the_setting():
    text = set_yaml(EXAMPLE, "league_file", "data/my league.json")
    text = set_yaml(text, ("publish", "bucket"), "stats-bucket")
    assert changed_lines(EXAMPLE, text) == [
        ("# league_file: data/league.json", "league_file: 'data/my league.json'"),
        ("  # bucket: shotcast-stats-bucket-xxxx   # the stack's BucketName output",
         "  bucket: stats-bucket                 # the stack's BucketName output")]


def test_missing_keys_are_added_to_their_block():
    text = set_yaml("a:\n  b: 1\n\n# end\nc: 2\n", ("a", "new", "deep"), "it's")
    assert text == "a:\n  b: 1\n  new:\n    deep: 'it''s'\n\n# end\nc: 2\n"


def test_gspro_folder_sets_the_log_and_both_course_libraries():
    text = gspro_dir.apply(EXAMPLE, r"D:\Games\GSProV1")
    data = yaml.safe_load(text)["source"]
    assert data["connector_log"] == r"D:\Games\GSProV1\Core\GSPC\ConnectDebug.txt"
    assert data["gspro_courses_dirs"] == [r"D:\Games\GSProV1\Courses", r"D:\Games\GSProV1\Core\GSP\Courses"]
    assert len(changed_lines(EXAMPLE, text)) == 3
    assert gspro_dir.folder_of(data["connector_log"]) == r"D:\Games\GSProV1"
    assert gspro_dir.same(r"d:\games\gsprov1\\", r"D:\Games\GSProV1")


def test_a_one_line_block_isnt_edited():
    with pytest.raises(ValueError, match="one line"):
        set_yaml(EXAMPLE, ("llm", "live", "model"), "x")


def test_settings_that_wouldnt_load_arent_saved():
    with pytest.raises(ValueError, match="don't fit"):
        apply_changes(EXAMPLE, "", {("tts", "provider"): "nonsense"}, {}, None)
    config, env = apply_changes(EXAMPLE, "OPENAI_API_KEY=\n", {("commentary", "enabled"): False},
                                {"OPENAI_API_KEY": "sk-1", "NTFY_TOPIC": "t"}, None)
    assert yaml.safe_load(config)["commentary"]["enabled"] is False
    assert env == "OPENAI_API_KEY=sk-1\nNTFY_TOPIC=t\n"


def test_an_edit_that_would_touch_anything_else_is_refused():
    text = "'publish':\n  bucket: b\n  region: r\n"
    with pytest.raises(ValueError, match="by hand"):
        set_yaml(text, ("publish", "enabled"), True)
    bom = "\ufeffpublish:\n  bucket: b\n"
    assert set_yaml(bom, ("publish", "bucket"), "c") == "\ufeffpublish:\n  bucket: c\n"


def test_env_values_read_as_the_booth_reads_them():
    from commentator.settings_files import env_value, set_env

    text = '\ufeffOPENAI_API_KEY = "sk-1"\nNTFY_TOPIC=a\nNTFY_TOPIC=b\n'
    assert env_value(text, "OPENAI_API_KEY") == "sk-1"
    assert env_value(text, "NTFY_TOPIC") == "b"
    assert env_value("NTFY_TOPIC=a\nNTFY_TOPIC=\n", "NTFY_TOPIC") == "a"
    assert set_env(text, "OPENAI_API_KEY", "sk-2") == "\ufeffOPENAI_API_KEY=sk-2\nNTFY_TOPIC=a\nNTFY_TOPIC=b\n"
