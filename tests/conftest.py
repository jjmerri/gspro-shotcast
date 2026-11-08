import webbrowser

import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """No test opens a browser tab on the machine running them."""
    monkeypatch.setattr(webbrowser, "open", lambda *a, **kw: True)


@pytest.fixture(autouse=True)
def _no_desktop_windows(monkeypatch):
    """Screen grabs in tests are fakes: the desktop's real windows (on a Windows runner, with no GSPro
    up) mustn't black them out. A test of the masking sets its own."""
    from commentator import winshot

    monkeypatch.setattr(winshot, "coverage", lambda _title: None)
