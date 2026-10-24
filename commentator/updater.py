"""Updates for the Windows app (the tray, commentator/tray.py): the newest GitHub release's installer.

A release carries `Shotcast-Setup-<version>.exe` and `<that name>.sha256`. The installer is
downloaded in the background and checked against the hash; the tray offers it, and running it
(silently) replaces the app and starts the tray again. Settings and the season file live outside
the app's folder, so an update never touches them. Releases aren't code-signed yet; when they are,
nothing here needs to change.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

log = logging.getLogger(__name__)

REPO = "jjmerri/gspro-shotcast"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALLER = re.compile(r"^Shotcast-Setup-.+\.exe$")


@dataclass
class Release:
    version: str
    url: str          # the installer
    sha256_url: str
    name: str
    page: str         # the release's page on GitHub, for its notes


def current_version() -> str:
    try:
        return metadata.version("gspro-shotcast")
    except metadata.PackageNotFoundError:
        return "0"


def parse(version: str) -> tuple[int, ...]:
    """'v1.2.10' -> (1, 2, 10). Anything after the numbers (a dev build's suffix) is ignored."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else (0,)


def newer(latest: str, current: str) -> bool:
    a, b = parse(latest), parse(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def pick(data: dict) -> Release | None:
    """The installer in a GitHub release (the API's JSON), or None if it hasn't one."""
    if data.get("draft") or data.get("prerelease"):
        return None
    assets = {a.get("name", ""): a.get("browser_download_url", "") for a in data.get("assets") or []}
    for name, url in assets.items():
        if INSTALLER.match(name) and assets.get(name + ".sha256"):
            return Release(version=data.get("tag_name", "").lstrip("v"), url=url, sha256_url=assets[name + ".sha256"],
                           name=name, page=data.get("html_url", ""))
    return None


def check(current: str | None = None, client=None) -> Release | None:
    """The latest release if it's newer than this one, else None. Raises on a network error."""
    import httpx

    get = (client or httpx).get
    r = get(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "shotcast-updater"},
            timeout=20, follow_redirects=True)
    r.raise_for_status()
    release = pick(r.json())
    if release and newer(release.version, current or current_version()):
        return release
    return None


def download(release: Release, dest: str | Path, client=None) -> Path:
    """Download the installer into `dest` and check its hash. Returns the file; reuses a good copy."""
    import httpx

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / release.name
    http = client or httpx
    expected = http.get(release.sha256_url, timeout=20, follow_redirects=True)
    expected.raise_for_status()
    want = expected.text.split()[0].strip().lower()
    if out.exists() and _sha256(out) == want:
        return out
    part = out.with_name(out.name + ".part")
    with http.stream("GET", release.url, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        with open(part, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    got = _sha256(part)
    if got != want:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{release.name} doesn't match its published hash")
    part.replace(out)
    for old in dest.glob("Shotcast-Setup-*.exe"):
        if old != out:
            old.unlink(missing_ok=True)
    return out


def install(installer: Path, home: Path | None = None) -> None:
    """Run the installer silently, on its own: it closes this app, replaces it and starts it again
    (with `home` as --home, when the tray wasn't using the default folder)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    args = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    if home is not None:
        args.append(f"/home={home}")
    subprocess.Popen(args,
                     creationflags=flags, close_fds=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
