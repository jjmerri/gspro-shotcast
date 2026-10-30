import webbrowser

import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """No test opens a browser tab on the machine running them."""
    monkeypatch.setattr(webbrowser, "open", lambda *a, **kw: True)
