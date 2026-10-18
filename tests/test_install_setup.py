import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("install_setup", ROOT / "tools" / "install_setup.py")
install_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_setup)


def test_tracking_turns_commentary_off_and_keeps_comments():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    off = install_setup.set_commentary(text, False)
    assert yaml.safe_load(off)["commentary"]["enabled"] is False
    assert off.count("\n") == text.count("\n")
    assert "record shots and rounds only" in off
    assert yaml.safe_load(install_setup.set_commentary(off, True))["commentary"]["enabled"] is True


def test_env_keys_fill_the_template_lines():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert install_setup.env_value(text, "OPENAI_API_KEY") == ""
    text = install_setup.set_env(text, "OPENAI_API_KEY", r"sk-a\1b")
    assert install_setup.env_value(text, "OPENAI_API_KEY") == r"sk-a\1b"
    assert text.count("OPENAI_API_KEY=") == 1
    assert install_setup.env_value(install_setup.set_env("", "NEW", "x"), "NEW") == "x"
