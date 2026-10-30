import json
import sqlite3
import zipfile

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
