"""A pytest plugin (pyproject's addopts: -p tests.cloud_isolation): every test sees its own empty
Shotcast Cloud folder, so a paired development machine's pairing never turns a test into a paired
run (commentator.cloud.root), and starts unpaired (commentator.cloud.startup)."""
import pytest


@pytest.fixture(autouse=True)
def _own_cloud_root(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("SHOTCAST_CLOUD_DIR", str(tmp_path_factory.mktemp("shotcast-cloud")))
    monkeypatch.setattr("commentator.cloud.startup", None)
