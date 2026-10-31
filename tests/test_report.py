import json
import sqlite3
import struct
import zipfile

import pytest
import yaml

from PIL import Image

from commentator import report
from commentator.config import Config
from commentator.store import Store


def test_redact_config_blanks_secret_values_and_keeps_the_rest():
    text = ('"obs_password": hunter22  # OBS\nmonitor: 2\nntfy: {topic: my-topic}\n'
            "live:\n  token: |\n    multi-line-secret\nmax_tokens: 900\n")
    out = report.redact_config(text)
    assert "hunter22" not in out and "my-topic" not in out and "multi-line-secret" not in out
    assert yaml.safe_load(out) == {"obs_password": "<redacted>", "monitor": 2, "ntfy": {"topic": "<redacted>"},
                                   "live": {"token": "<redacted>"}, "max_tokens": 900}
    broken = report.redact_config("obs_password: [private-password\n")
    assert "left out" in broken and "private-password" not in broken


def test_scrubber_blanks_short_secrets_but_not_ordinary_short_values():
    scrub = report.Scrubber([("NTFY_TOPIC", "xy"), ("LOG_LEVEL", "info")])
    assert scrub("topic xy at info, xylophone") == "topic <redacted> at info, xylophone"


def test_report_zips_logs_screens_and_maps_without_the_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret-value-123\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("screen:\n  monitor: 1\n", encoding="utf-8")
    (logs / "booth-2099-01-01.log").write_text("started with sk-secret-value-123\n", encoding="utf-8")
    (logs / "health.json").write_text(json.dumps(
        {"facts": {"gspro_version": "1.2"}, "issues": [{"key": "a", "level": "warn", "message": "Log silent",
                                                         "since": "now"}]}), encoding="utf-8")
    Image.new("RGB", (8, 8)).save(logs / "fixture-frame.png")
    gspro = tmp_path / "GSPro"
    gspro.mkdir()
    (gspro / "Player.log").write_text("beta log\n", encoding="utf-8")
    cfg = Config(log_dir="logs", db_path="logs/season.db")
    cfg.source.gspro_log = str(gspro / "output_log.txt")
    cfg.source.connector_log = str(tmp_path / "missing.txt")
    store = Store(cfg.db_path)
    store.record_hole_image("Pebble", 3, b"jpegbytes", (0.1, 0.2), None)
    store.db.commit()
    report.keep_frame(logs, "no-player", Image.new("RGB", (8, 8)), {"hole": 3})

    path = report.build(cfg, tmp_path / "config.yaml")

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        summary = z.read("summary.txt").decode()
        booth = z.read("logs/booth-2099-01-01.log").decode()
    assert ".env" not in names and not any(n.endswith(".env") for n in names)
    assert "sk-secret-value-123" not in booth and "<redacted>" in booth
    assert {"config.yaml", "gspro/Player.log", "screens/fixture-frame.jpg", "minimaps/Pebble-03-no-pin.jpg",
            "logs/health.json"} <= names
    assert any(n.startswith("screens/report-frames/no-player-") and n.endswith(".jpg") for n in names)
    assert "OPENAI_API_KEY" in summary and "sk-secret" not in summary
    assert "Log silent" in summary and "output_log.txt: missing" in summary


def test_keep_frame_keeps_only_the_newest_few(tmp_path, monkeypatch):
    stamps = iter(f"20990101-0000{i:02d}" for i in range(10))
    monkeypatch.setattr(report.time, "strftime", lambda fmt, *a: next(stamps) if fmt == "%Y%m%d-%H%M%S" else "x")
    for _ in range(6):
        report.keep_frame(tmp_path, "minimap-miss", Image.new("RGB", (4, 4)), {}, keep=3)
    kept = sorted(p.name for p in (tmp_path / report.FRAMES_DIR).glob("*.jpg"))
    assert kept == [f"minimap-miss-20990101-0000{i:02d}.jpg" for i in (3, 4, 5)]
    assert len(list((tmp_path / report.FRAMES_DIR).glob("*.json"))) == 3


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_upload_asks_the_drop_for_a_link_then_posts_the_zip_to_it(tmp_path, monkeypatch):
    import httpx
    import pytest

    zp = tmp_path / "r.zip"
    zp.write_bytes(b"PK" + b"x" * 100)
    calls = []
    drop = {"code": "ABCD2345", "url": "https://bucket.s3/", "fields": {"key": "reports/ABCD2345.zip"},
            "max_bytes": 1000}

    def post(url, **kw):
        calls.append((url, kw))
        if url == "https://drop/":
            return _Resp(200, drop)
        assert kw["data"] == drop["fields"] and kw["files"]["file"][1].read() == zp.read_bytes()
        return _Resp(204)

    monkeypatch.setattr(httpx, "post", post)
    assert report.show_code(report.upload(zp, "https://drop/")) == "ABCD-2345"
    assert [u for u, _ in calls] == ["https://drop/", "https://bucket.s3/"]

    drop["max_bytes"] = 50                      # over the drop's cap: never uploaded
    calls.clear()
    with pytest.raises(RuntimeError, match="over the drop"):
        report.upload(zp, "https://drop/")
    assert len(calls) == 1
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(429, {"error": "full for today"}))
    with pytest.raises(RuntimeError, match="full for today"):
        report.upload(zp, "https://drop/")


def test_report_codes_read_back_however_they_are_typed():
    import pytest

    assert report.parse_code(" abcd-2345 ") == "ABCD2345"
    for bad in ("ABCD-234", "ABCD-2340", "../../x"):   # 0 isn't in a code
        with pytest.raises(ValueError):
            report.parse_code(bad)


def test_the_sent_box_copies_the_report_code():
    import pytest

    tk = pytest.importorskip("tkinter")
    from commentator.tray_ui import Ui

    ui = Ui.__new__(Ui)                         # Tk on this thread, not Ui's own
    try:
        ui.root, ui.scale = tk.Tk(), 1.0
    except tk.TclError:
        pytest.skip("no display")
    try:
        ui.root.withdraw()
        ui._code_box("Sent. Your report code is:", "ABCD-2345", "Pass it on.", "Shotcast")()
        assert ui.root.clipboard_get() == "ABCD-2345"
    finally:
        ui.root.destroy()


def _nrbf_string(s: str) -> bytes:
    b = s.encode()
    return bytes([len(b)]) + b


def _settings_vgs() -> bytes:
    """A small Settings.vgs: a player's name, then game options, one of them an enum."""
    i32 = lambda n: struct.pack("<i", n)   # noqa: E731
    out = b"\x00" + i32(1) + i32(-1) + i32(1) + i32(0)
    out += b"\x0c" + i32(2) + _nrbf_string("Assembly-CSharp")
    out += (b"\x05" + i32(1) + _nrbf_string("GameSettings") + i32(2) + _nrbf_string("PlayerData_a")
            + _nrbf_string("Game_s") + bytes([1, 4]) + _nrbf_string("GameData") + i32(2) + i32(2))
    out += b"\x06" + i32(3) + _nrbf_string("Jane Golfer") + b"\x09" + i32(4)
    out += (b"\x05" + i32(4) + _nrbf_string("GameData") + i32(3) + _nrbf_string("BallReadyHideInterface")
            + _nrbf_string("FollowCamDelay") + _nrbf_string("ScorecardStyle") + bytes([0, 0, 4]) + bytes([1, 11])
            + _nrbf_string("ScorecardStyle") + i32(2) + i32(2))
    out += b"\x01" + struct.pack("<f", 0.5)
    out += (b"\x05" + i32(5) + _nrbf_string("ScorecardStyle") + i32(1) + _nrbf_string("value__") + bytes([0, 8])
            + i32(2) + i32(3))
    return out + b"\x0b"


def test_report_lists_gspro_settings_but_not_its_players(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gspro = tmp_path / "GSPro"
    gspro.mkdir()
    (gspro / "Settings.vgs").write_bytes(_settings_vgs())
    cfg = Config(log_dir="logs", db_path="logs/season.db")
    cfg.source.gspro_log = str(gspro / "Player.log")
    cfg.source.connector_log = str(tmp_path / "missing.txt")

    with zipfile.ZipFile(report.build(cfg, tmp_path / "config.yaml")) as z:
        summary = z.read("summary.txt").decode()
    assert "GSPro settings: BallReadyHideInterface=True, FollowCamDelay=0.5, ScorecardStyle=3" in summary
    assert "Jane" not in summary


def test_a_corrupt_settings_file_is_refused_rather_than_filling_memory():
    from commentator.gspro_settings import _Reader

    i32 = lambda n: struct.pack("<i", n)   # noqa: E731
    header = b"\x00" + i32(1) + i32(-1) + i32(1) + i32(0)
    for body in (b"\x10" + i32(1) + i32(1) + b"\x0e" + i32(2**31 - 1),   # a run of nulls past the array's end
                 b"\x10" + i32(1) + i32(2**31 - 1) + b"\x0e" + i32(2**31 - 1)):   # an absurd array length
        with pytest.raises(ValueError):
            _Reader(header + body + b"\x0b").read()


def test_a_settings_string_length_is_five_bytes_at_most():
    from commentator.gspro_settings import _Reader

    with pytest.raises(ValueError, match="string length"):
        _Reader(b"\xff" * 100_000).string()
