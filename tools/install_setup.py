"""The second half of install.bat, run with the new venv's Python.

Creates config.yaml and .env from their templates (never overwriting yours), asks for the booth's
API keys, downloads the OCR models, runs `check`, and offers desktop shortcuts. Safe to re-run.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from commentator.settings_files import env_value, set_commentary, set_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEYS = [("OPENAI_API_KEY", "OpenAI", "https://platform.openai.com/api-keys"),
        ("ANTHROPIC_API_KEY", "Anthropic", "https://console.anthropic.com/settings/keys")]


def ask_yes(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def setup_config(booth: bool) -> None:
    config = ROOT / "config.yaml"
    if config.exists():
        text = config.read_text(encoding="utf-8")
        commentary = (yaml.safe_load(text) or {}).get("commentary") or {}
        if booth and commentary.get("enabled") is False and ask_yes(
                "config.yaml has commentary off (tracking only). Turn the booth on?"):
            config.write_text(set_commentary(text, True), encoding="utf-8")
            print("config.yaml: commentary on.")
        else:
            print("config.yaml: already there, left as is.")
        return
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    if not booth:
        text = set_commentary(text, False)
    config.write_text(text, encoding="utf-8")
    print("config.yaml: created" + ("" if booth else " (commentary off: tracking only)") + ".")


def setup_env(booth: bool) -> bool:
    """Create .env; for the booth, ask for missing keys. True if both booth keys are set."""
    env = ROOT / ".env"
    if not env.exists():
        shutil.copyfile(ROOT / ".env.example", env)
        print(".env: created from .env.example.")
    if not booth:
        return False
    text = env.read_text(encoding="utf-8")
    for key, name, url in KEYS:
        if env_value(text, key):
            print(f"{name} key: already in .env.")
            continue
        print(f"\nPaste your {name} API key ({url}), or press Enter to skip.")
        value = getpass.getpass(f"{name} key (hidden as you paste): ").strip()
        if value:
            text = set_env(text, key, value)
            print(f"{name} key saved (...{value[-4:]}).")
        else:
            print(f"Skipped. Add {key}= to .env later.")
    env.write_text(text, encoding="utf-8")
    if env_value(text, "ANTHROPIC_API_KEY") and not env_value(text, "OPENAI_API_KEY"):
        print("\nNo OpenAI key: the booth can run on Claude alone with voices on this PC. See\n"
              "docs/booth.md, \"Choosing a setup\" (the \"one key\" blocks in config.yaml + Kokoro).")
    return all(env_value(text, key) for key, _, _ in KEYS)


def download_ocr_models() -> None:
    print("\nDownloading the OCR models (about 30 MB, once)...")
    try:
        from rapidocr import RapidOCR

        RapidOCR()
        print("OCR models ready.")
    except Exception as exc:  # the booth retries on first use, so this isn't fatal
        print(f"Couldn't get the OCR models now ({exc}); they'll download on first use.")


def run_check() -> None:
    print("\nChecking every model answers (python -m commentator check)...")
    subprocess.run([sys.executable, "-m", "commentator", "check"], cwd=ROOT)


def make_shortcuts() -> None:
    # The Desktop may be redirected (OneDrive), so ask Windows where it is.
    targets = [("Shotcast", ROOT / "start-booth.bat"), ("Shotcast Stats", ROOT / "stats.bat")]
    lines = ["$d = [Environment]::GetFolderPath('Desktop')", "$s = New-Object -ComObject WScript.Shell"]
    for name, target in targets:
        target, root = str(target).replace("'", "''"), str(ROOT).replace("'", "''")
        lines += [f"$l = $s.CreateShortcut((Join-Path $d '{name}.lnk'))",
                  f"$l.TargetPath = '{target}'", f"$l.WorkingDirectory = '{root}'", "$l.Save()"]
    result = subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(lines)])
    print("Desktop shortcuts added: Shotcast, Shotcast Stats." if result.returncode == 0
          else "Couldn't add the shortcuts; double-click start-booth.bat and stats.bat instead.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tracking", "booth"], required=True)
    args = parser.parse_args()
    booth = args.mode == "booth"
    os.chdir(ROOT)
    setup_config(booth)
    keys_set = setup_env(booth)
    download_ocr_models()
    if booth and keys_set:
        run_check()
    if os.name == "nt" and ask_yes("\nAdd desktop shortcuts for Shotcast and its stats page?"):
        make_shortcuts()
    print("""
Installed. Next:
  1. Start GSPro and go to a hole (any course, any player).
  2. Double-click screen-check.bat: every line should say OK. If some say FIX, run
     screen-check.bat --fix from this folder.
  3. Double-click start-booth.bat (or the Shotcast shortcut) and play.
  4. stats.bat (or Shotcast Stats) opens the stats page after your first round.
Update later with update.bat. Settings live in config.yaml, keys in .env.""")
    if booth and not keys_set:
        print("The booth needs both API keys in .env before start-booth.bat will start. Add them, then\n"
              "run .venv\\Scripts\\python -m commentator check from this folder.")


if __name__ == "__main__":
    main()
