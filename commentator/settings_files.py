"""Small edits to config.yaml and .env that keep their comments: shared by install.bat
(tools/install_setup.py) and the Windows app (commentator/tray.py)."""
from __future__ import annotations

import re


def set_commentary(config_text: str, enabled: bool) -> str:
    """Flip `commentary: enabled:` in config.yaml's text, keeping its comments."""
    pattern = re.compile(r"(^commentary:\s*\n(?:[ \t]*#.*\n)*[ \t]+enabled:[ \t]*)(true|false)", re.M)
    if not pattern.search(config_text):
        raise ValueError("no `commentary: enabled:` line")
    return pattern.sub(lambda m: m.group(1) + ("true" if enabled else "false"), config_text, count=1)


def env_value(env_text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", env_text, re.M)
    return m.group(1).strip() if m else ""


def set_env(env_text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=.*$", env_text, re.M):
        return re.sub(rf"^{key}=.*$", lambda _: f"{key}={value}", env_text, count=1, flags=re.M)
    return env_text.rstrip("\n") + f"\n{key}={value}\n"
