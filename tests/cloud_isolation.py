"""A pytest plugin (pyproject's addopts: -p tests.cloud_isolation): every test sees its own empty
Shotcast Cloud folder, so a paired development machine's pairing never turns a test into a paired
run (commentator.cloud.root), starts unpaired (commentator.cloud.startup), and never reads the keys
in this checkout's own .env (commentator.cloud.CODE_ENV)."""
import pytest


@pytest.fixture(autouse=True)
def _own_cloud_root(tmp_path_factory, monkeypatch):
    monkeypatch.setattr("commentator.cloud.TEST_ROOT", tmp_path_factory.mktemp("shotcast-cloud"))
    monkeypatch.setattr("commentator.cloud.startup", None)
    # (nor does pairing in a test seed a profile from the keys in this checkout's own .env)
    monkeypatch.setattr("commentator.cloud.CODE_ENV", tmp_path_factory.mktemp("shotcast-code") / ".env")
    monkeypatch.setattr("commentator.cloud.ENABLED", True)   # (off in the app for now: the tests are of it on)
    monkeypatch.delenv("SHOTCAST_CLOUD_PREVIEW", raising=False)
