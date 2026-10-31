"""HTTPS for urllib that doesn't depend on what Windows happens to have cached.

Python's default context trusts only the roots already in the Windows store, and Windows fetches
most roots on demand (when a browser first needs one), so a fresh PC can fail with "unable to get
local issuer certificate" on a site it's never visited. certifi's bundle (shipped with httpx) is
added on top, keeping the Windows store for anything a company proxy or antivirus adds there."""
from __future__ import annotations

import ssl
import urllib.request


def context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass  # the system store alone, as before
    return ctx


def urlopen(url, timeout: float):
    return urllib.request.urlopen(url, timeout=timeout, context=context())
