"""Where GSPro is installed (C:\\GSProV1 unless it was put elsewhere), and the config.yaml paths that
follow from it: GSPconnect's log and the two course libraries."""
from __future__ import annotations

import os
import subprocess
from pathlib import PureWindowsPath

from .settings_files import set_yaml

DEFAULT = r"C:\GSProV1"
CONNECTOR_LOG = r"Core\GSPC\ConnectDebug.txt"
COURSES = (r"Courses", r"Core\GSP\Courses")
EXE = r"Core\GSP\GSPro.exe"


def looks_like_gspro(folder: str) -> bool:
    return bool(folder) and os.path.isfile(os.path.join(folder, EXE))


def find() -> str | None:
    """GSPro's folder on this PC (see find_gspro.ps1), or None."""
    if os.name != "nt":
        return None
    import tempfile

    script = os.path.join(os.path.dirname(__file__), "find_gspro.ps1")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "gspro.txt")
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", script, "-Out", out], capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with open(out, encoding="mbcs", errors="replace") as f:   # the script writes ANSI
                found = f.read().strip()
        except (OSError, subprocess.SubprocessError):
            return None
    return found or None


def paths(folder: str) -> tuple[str, list[str]]:
    """(source.connector_log, source.gspro_courses_dirs) for GSPro in `folder`."""
    root = PureWindowsPath(folder)
    return str(root / CONNECTOR_LOG), [str(root / c) for c in COURSES]


def folder_of(connector_log: str) -> str | None:
    """The GSPro folder a config's connector_log is in, if it's in the usual place."""
    p = PureWindowsPath(connector_log)
    if len(p.parts) > 3 and [x.lower() for x in p.parts[-3:]] == ["core", "gspc", "connectdebug.txt"]:
        return str(p.parents[2])
    return None


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and PureWindowsPath(a).as_posix().rstrip("/").lower() == \
        PureWindowsPath(b).as_posix().rstrip("/").lower()


def apply(config_text: str, folder: str) -> str:
    """config.yaml's text pointed at GSPro in `folder`."""
    log, courses = paths(folder)
    text = set_yaml(config_text, ("source", "connector_log"), log)
    return set_yaml(text, ("source", "gspro_courses_dirs"), courses)
