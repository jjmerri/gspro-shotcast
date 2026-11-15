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


def test_other_programs_files_lose_sign_ins_but_keep_the_shot_data():
    text = ('Signed in as jo.smith+golf@mail.example.com\n'
            '{"password": "hunter22", "access_token":"abc.def", "session_id": "821", "ClubSpeed": 40.5}\n'
            'Authorization: Bearer abcDEF123456789xyz\n'
            'apiKey=QWERTY12345; refresh_token=zzzz9999zzzz&next=1\n'
            'jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig\n'
            'key 9f8e7d6c5b4a39281706f5e4d3c2b1a0Zz998877 then C:\\Users\\jo\\AppData\\LocalLow\\UNEEKOR\\VIEW\n'
            'serialized statistic info, shot_guid 5333396602524808431-30870-54801, serial GC3-12345\n'
            '[65.4664459,29.970499,-3.88416982,-507.0,7274.689,7257.0,-3.99639463,0.0]\n'
            'abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz\n')
    out = report.scrub_private(text)
    for secret in ("jo.smith", "hunter22", "abc.def", "abcDEF123456789xyz", "QWERTY12345", "zzzz9999zzzz",
                   "eyJhbGci", "9f8e7d6c5b4a"):
        assert secret not in out
    for kept in ('"session_id": "821"', '"ClubSpeed": 40.5', "&next=1", "C:\\Users\\jo\\AppData\\LocalLow\\UNEEKOR\\VIEW",
                 "serialized statistic info", "5333396602524808431-30870-54801", "serial GC3-12345",
                 "[65.4664459,29.970499,-3.88416982,-507.0,7274.689,7257.0,-3.99639463,0.0]",
                 "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz"):
        assert kept in out
    assert '"password": "<redacted>"' in out and "Signed in as <email>" in out
    gspro = report.scrub_private("Game Object 'Bunker_Leftside_Group_-_Hole_12_Mesh_v2_Collider_9' a@b.co", blobs=False)
    assert gspro == "Game Object 'Bunker_Leftside_Group_-_Hole_12_Mesh_v2_Collider_9' <email>"


def test_sign_ins_in_xml_nested_json_and_cookies_go_and_json_stays_json():
    out = report.scrub_private('<AccessToken>abcd1234-secret</AccessToken> <ns:Password a="1">pw 9</ns:Password>\n'
                               'Cookie: theme=dark; session=2c95f03ea6ac47018ad7cb89171f93fe\n'
                               'GET /api?token=abc123&club=7i\n', blobs=False)
    assert "abcd1234" not in out and "pw 9" not in out and "2c95f03e" not in out and "abc123" not in out
    assert "<AccessToken><redacted></AccessToken>" in out and "&club=7i" in out
    nested = json.dumps({"message": json.dumps({"access_token": "abcd1234-secret", "n": 1})})
    assert json.loads(json.loads(report.scrub_private(nested))["message"])["access_token"] == "<redacted>"
    nested = json.dumps({"message": json.dumps({"password": 'before"after123', "ClubSpeed": 40.5})})
    inner = json.loads(json.loads(report.scrub_private(nested))["message"])
    assert inner == {"password": "<redacted>", "ClubSpeed": 40.5}
    out = report.scrub_private("12:01 INFO Cookie: theme=dark; session=short-secret\n"
                               "<Password><![CDATA[hunter22]]></Password>", blobs=False)
    assert "short-secret" not in out and "hunter22" not in out and out.startswith("12:01 INFO Cookie: ")
    for doc in ({"message": "Cookie: session=abc123", "ClubSpeed": 40.5},
                {"password": ["liveCredential123"], "ClubSpeed": 40.5},
                {"token": {"a": {"b": "liveCredential123"}, "c": "y"}, "ClubSpeed": 40.5}):
        out = report.scrub_private(json.dumps(doc))
        assert "liveCredential123" not in out and "abc123" not in out and json.loads(out)["ClubSpeed"] == 40.5
    deep = json.dumps({"password": "liveCredential123", "n": 1})
    for _ in range(2):
        deep = json.dumps({"message": deep})
    deep = json.loads(json.loads(json.loads(report.scrub_private(deep))["message"])["message"])
    assert deep == {"password": "<redacted>", "n": 1}
    deep = json.dumps({"payload": json.dumps({"message": "password=liveCredential123\nClubSpeed=40.5"})})
    deep = json.loads(json.loads(report.scrub_private(deep))["payload"])
    assert deep == {"message": "password=<redacted>\nClubSpeed=40.5"}
    for raw in ('{"password": [\n"liveCredential123"\n], "ClubSpeed": 40.5}',
                '{"message": "password=liveCredential123", "ClubSpeed": 40.5}',
                '{"message": "Login failed: password=", "ClubSpeed": 40.5}', '{"m": "Cookie: ", "ClubSpeed": 40.5}',
                json.dumps({"m": "GET /\r\nCookie: s=liveCredential123\r\nHost: x", "ClubSpeed": 40.5}),
                json.dumps({"m": "Account:\tjo@example.com\nToken=liveCredential123", "ClubSpeed": 40.5}),
                json.dumps({"m": "password=O'liveCredential123!", "ClubSpeed": 40.5}),
                json.dumps({"m": "password=\\liveCredential123\nnext", "ClubSpeed": 40.5}),
                json.dumps({"m": "Authorization: Token liveCredential123\r\nHost: x", "ClubSpeed": 40.5}),
                json.dumps({"m": 'Authorization: Digest username="u", response="liveCredential123"', "ClubSpeed": 40.5})):
        out = report.scrub_private(raw)
        assert "liveCredential123" not in out and "jo@" not in out and json.loads(out)["ClubSpeed"] == 40.5
    out = report.scrub_private('Cookie: session="liveCredential123"; other=private456\nClubSpeed=40.5\n'
                               '<Password>\n  <![CDATA[liveCredential123]]>\n</Password>', blobs=False)
    assert "liveCredential123" not in out and "private456" not in out and "ClubSpeed=40.5" in out
    assert report.scrub_private('password=\n{"ClubSpeed":40.5}') == 'password=\n{"ClubSpeed":40.5}'
    for doc in ({"password": None, "ClubSpeed": 40.5}, {"password": 'a"b c', "token": 12345, "Speed": 1}):
        back = json.loads(report.scrub_private(json.dumps(doc)))
        assert back["ClubSpeed" if "ClubSpeed" in doc else "Speed"] == doc.get("ClubSpeed", doc.get("Speed"))
        assert back["password"] in (None, "<redacted>") and back.get("token", "<redacted>") == "<redacted>"


def test_scrubbing_a_long_unbroken_run_is_quick():
    import time
    for run in ("a" * 200_000, "a-" * 100_000, "ab1" * 70_000, "<Password><![CDATA[" * 8_000, "\\" * 200_000,
                'password="' * 50_000, "Cookie: " + "\\" * 100_000):
        start = time.perf_counter()
        report.scrub_private(run)
        assert time.perf_counter() - start < 3.0   # quadratic would take minutes


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
    # The boxes a read was made from go beside it, and out with it.
    for _ in range(4):
        report.keep_frame(tmp_path, "impact-no-shot", Image.new("RGB", (4, 4)), {}, keep=3,
                          crops={"player_box": Image.new("RGB", (2, 2)), "other_side": {"mirrored": False}})
    boxes = sorted(p.name for p in (tmp_path / report.FRAMES_DIR).glob("impact-no-shot-*.png"))
    assert boxes == [f"impact-no-shot-20990101-0000{i:02d}.player_box.png" for i in (7, 8, 9)]


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


def test_the_sent_box_copies_the_report_code(monkeypatch):
    import pytest

    tk = pytest.importorskip("tkinter")
    from commentator.tray_ui import Ui

    class Hidden(tk.Toplevel):   # never shown on the screen running the tests
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.withdraw()

        def focus_force(self):
            pass

    monkeypatch.setattr(tk, "Toplevel", Hidden)
    ui = Ui.__new__(Ui)                         # Tk on this thread, not Ui's own
    try:
        ui.root, ui.scale = tk.Tk(), 1.0
    except tk.TclError:
        pytest.skip("no display")
    try:
        ui.root.withdraw()
        copied = []   # not the real clipboard: that's the tester's
        monkeypatch.setattr(ui.root, "clipboard_clear", lambda: copied.clear())
        monkeypatch.setattr(ui.root, "clipboard_append", copied.append)
        ui._code_box("Sent. Your report code is:", "ABCD-2345", "Pass it on.", "Shotcast")()
        assert copied == ["ABCD-2345"]
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


def test_report_gathers_the_launch_monitor_data_gspro_and_its_software_keep(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gspro = tmp_path / "GSPro"
    gspro.mkdir()
    (gspro / "Player.log").write_text('x\nLM Type {"connectType":"Garmin","LMType":"R10"}\ny\n', encoding="utf-8")
    (gspro / "currentRound.dat").write_text('{"Shots": []}', encoding="utf-8")
    con = sqlite3.connect(gspro / "GSPro.db")
    con.execute("create table LastShot (ID integer, DateCreated text, ShotData text)")
    con.execute("insert into LastShot values (7, '2099-01-01', ?)", (json.dumps({"ClubSpeed": 40.5}),))
    con.commit()
    con.close()
    gspc = tmp_path / "GSPC"
    gspc.mkdir()
    for name in ("ConnectDebug.txt", "ConnectDebug.txt.1", "ConnectDebug.txt.2026-11-14"):
        (gspc / name).write_text(f"{name} shot\n", encoding="utf-8")
    vendor = tmp_path / "Vendor"
    (vendor / "12").mkdir(parents=True)
    (vendor / "12" / "shotinfo.json").write_text('{"clubspeed": "40", "user": "jo@mail.example.com"}',
                                                encoding="utf-8")
    monkeypatch.setattr(report, "LM_FILES", [("Some LM shots", str(vendor), "*/*.json", 5, 1000),
                                             ("Absent LM", str(tmp_path / "nowhere"), "*.txt", 1, 1000)])
    sbk = tmp_path / "MLM2PRO" / "appdata" / "logs"
    sbk.mkdir(parents=True)
    (sbk / "mlm2pro-gspro-connect.log").write_text(
        "2026-11-15:15:20:00,1   DEBUG    [mlm2pro_web_api.py:17] UserId: 42, Secret Key: k3y, Secret Value: v4lue\n"
        "2026-11-15:15:20:01,1   INFO     [gspro_connect.py:40] Sending to GSPro data: b'{\"BallData\": {}}'\n",
        encoding="utf-8")
    from commentator.sources import club_feeds
    monkeypatch.setattr(club_feeds, "springbok_dirs", lambda: [sbk])
    programs = tmp_path / "Programs"
    (programs / "aboutGOLF" / "aG Bridge").mkdir(parents=True)
    (programs / "aboutGOLF" / "aG Bridge" / "bridge.log").write_bytes(b"shot\n")
    (programs / "Microsoft").mkdir()
    monkeypatch.setattr(report, "SOFTWARE_ROOTS", [str(programs), str(tmp_path / "absent")])
    cfg = Config(log_dir="logs", db_path="logs/season.db")
    cfg.source.gspro_log = str(gspro / "Player.log")
    cfg.source.connector_log = str(gspc / "ConnectDebug.txt")

    with zipfile.ZipFile(report.build(cfg, tmp_path / "config.yaml")) as z:
        names = set(z.namelist())
        summary = z.read("summary.txt").decode()
        shots = json.loads(z.read("gspro/GSPro.db-shots.json"))
        software = z.read("launch-monitor/software.txt").decode()
        vendor_shot = z.read("launch-monitor/Some_LM_shots/12/shotinfo.json").decode()
        springbok = z.read("launch-monitor/springbok_connector_log/0/mlm2pro-gspro-connect.log").decode()
    assert "aboutGOLF" in software and "aG Bridge/bridge.log  5" in software and "Microsoft" not in software
    assert {"gspro/ConnectDebug.txt", "gspro/ConnectDebug.txt.1", "gspro/ConnectDebug.txt.2026-11-14",
            "gspro/currentRound.dat", "launch-monitor/Some_LM_shots/12/shotinfo.json"} <= names
    assert shots["LastShot"][0]["ShotData"] == {"ClubSpeed": 40.5}
    assert '"clubspeed": "40"' in vendor_shot and "jo@" not in vendor_shot
    assert "Sending to GSPro data" in springbok and "Secret" not in springbok and "k3y" not in springbok
    assert 'LM Type {"connectType":"Garmin","LMType":"R10"}' in summary
    assert "Some LM shots: 1 file(s)" in summary and "Absent LM: none" in summary


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


def test_the_report_says_what_pc_it_is():
    info = {"cpu": [{"Name": "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz ", "NumberOfCores": 4,
                     "NumberOfLogicalProcessors": 8, "MaxClockSpeed": 1800, "LoadPercentage": 87}],
            "os": {"Caption": "Microsoft Windows 10 Home", "TotalVisibleMemorySize": 8 * 2**20, "FreePhysicalMemory": 2**20},
            "gpu": [{"Name": "Intel(R) UHD Graphics 620", "DriverVersion": "27.20.100.8681", "Memory": None,
                     "Mode": "1920 x 1200 x 4294967296 colors", "RefreshRate": 60},
                    {"Name": "NVIDIA GeForce RTX 3060", "DriverVersion": "32.0.15.6094", "Memory": 12 * 2**30,
                     "Mode": None, "RefreshRate": None}],
            "power": "Power saver\r\n", "battery": [{"BatteryStatus": 1, "EstimatedChargeRemaining": 40}],
            "procs": {"Name": "GSPro", "Id": 4120, "CPU": 812.4, "WorkingSet64": 3 * 2**30}}   # one: no list
    assert report.pc_lines(info) == [
        "CPU: Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz, 4 cores / 8 threads, 1800 MHz, 87% busy now",
        "Memory: 8.0 GB total, 1.0 GB free (Microsoft Windows 10 Home)",
        "Video card: Intel(R) UHD Graphics 620, ?, driver 27.20.100.8681, 1920 x 1200 x 4294967296 colors at 60 Hz",
        "Video card: NVIDIA GeForce RTX 3060, 12.0 GB, driver 32.0.15.6094",
        "Power plan: Power saver",
        "Battery: 40%, ON BATTERY",
        "Process GSPro (4120): 812 s of CPU, 3.0 GB in memory"]
