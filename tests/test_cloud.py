import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path

import httpx
import pytest

from commentator import cloud

API = "https://cloud.test"
_CLIENT = httpx.Client
_PAIR = cloud.pair


class FakeAPI:
    """Shotcast Cloud's device-facing API as the PC sees it (shotcast-cloud's devices.py and rounds.py):
    pairing, refresh-token rotation and round uploads."""

    def __init__(self, host="cloud.test"):
        self.host = host
        self.now = 1000.0
        self.calls: list[tuple[str, str, str]] = []
        self.polls = ["authorization_pending", "slow_down"]   # then approved
        self.refresh = {"r0": None}                            # refresh token -> its successor once used
        self.access = {"a0"}
        self.n = 0
        self.revoked = False
        self.signed_out = False
        self.tenant, self.sim, self.device = "t1", "Bay 1", "d1"
        self.default: str | None = "lg1"
        self.rounds: dict[str, bytes] = {}                     # round -> the JSON it was sent as
        self.puts: list[tuple[str, int, dict]] = []
        self.pictures: list[dict] = []
        # The cloud's hole pictures as holepictures.py keeps them: (course, hole) -> (sha256, area); and
        # the (course, hole, sha256) an admin removed.
        self.kept: dict[tuple[str, int], tuple[str, int]] = {}
        self.removed: set[tuple[str, int, str]] = set()
        self.hook = None                                       # a test's own answer to a request, before these
        self.answers: list = []                                # scripted answers for the next PUTs
        self.minimum: str | None = None                        # clientversion's: below it, every call is 426
        self.claim_cards = False                               # whether the tenant is a venue (its bays show cards)
        self.claimed: dict[tuple[str, str], str] = {}          # (round, slot) -> the claimer's short name

    def remove_picture(self, course: str, hole: int) -> None:
        """An admin removes a hole's picture on the site."""
        digest, _ = self.kept.pop((course.casefold(), hole))
        self.removed.add((course.casefold(), hole, digest))

    def tokens(self, refresh):
        self.n += 1
        access = f"a{self.n}"
        self.access.add(access)
        return {"device_id": self.device, "league_id": self.default, "tenant_id": self.tenant,
                "sim_name": self.sim, "refresh_token": refresh, "access_token": access, "access_expires_in": 600}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.url.host, req.method, path))
        assert req.headers["user-agent"].startswith("Shotcast/")
        if req.method != "GET":   # CloudFront's signed hop needs every body's hash
            assert req.headers["x-amz-content-sha256"] == hashlib.sha256(req.content).hexdigest()
        if self.hook is not None and (hooked := self.hook(req)) is not None:
            return hooked
        if self.minimum:
            return httpx.Response(426, headers={"X-Shotcast-Min-Version": self.minimum},
                                  json={"error": "client_too_old", "action": "update",
                                        "message": f"update Shotcast to {self.minimum}"})
        if path.startswith("/api/rounds/") and req.method == "PUT":
            return self.put(req)
        if path == "/api/devices/hole-pictures" and req.method == "PUT":
            pic = json.loads(req.content)
            self.pictures.append(pic)
            data = base64.b64decode(pic["jpeg"])
            key, digest = (pic["course"].casefold(), pic["hole"]), hashlib.sha256(data).hexdigest()
            if (*key, digest) in self.removed:
                return httpx.Response(200, json={"kept": False, "removed": True})
            from PIL import Image

            w, h = Image.open(io.BytesIO(data)).size
            if key in self.kept and self.kept[key][1] >= w * h:
                return httpx.Response(200, json={"kept": False, "removed": False})
            self.kept[key] = (digest, w * h)
            return httpx.Response(200, json={"kept": True, "removed": False})
        if path == "/api/devices/hole-pictures/check":
            asked = json.loads(req.content)
            send, drop = [], []
            for p in asked["holes"]:
                key = (asked["course"].casefold(), p["hole"])
                if (*key, p["sha256"]) in self.removed:
                    drop.append(p["hole"])
                elif key not in self.kept or (self.kept[key][0] != p["sha256"]
                                              and self.kept[key][1] < p["width"] * p["height"]):
                    send.append(p["hole"])
            return httpx.Response(200, json={"send": send, "drop": drop})
        body = json.loads(req.content) if req.content else None
        if path == "/api/devices/pair":
            assert body["name"] and len(body["fingerprint"]) == 64
            return httpx.Response(201, json={"device_code": "dc", "user_code": "ABCD-EFGH",
                                             "verification_uri": f"{API}/pair",
                                             "verification_uri_complete": f"{API}/pair?code=ABCD-EFGH",
                                             "expires_in": 600, "interval": 5})
        if path == "/api/devices/token":
            if self.polls:
                return httpx.Response(400, json={"error": self.polls.pop(0)})
            return httpx.Response(200, json=self.tokens("r0"))
        if path == "/api/devices/refresh":
            old = body["refresh_token"]
            if self.signed_out:
                return httpx.Response(401, json={"error": "device_signed_out", "action": "repair",
                                                 "message": "this PC signed out"})
            if self.revoked or old not in self.refresh:
                return httpx.Response(401, json={"error": "refresh_token_invalid", "action": "repair",
                                                 "message": "refresh token not valid; pair this PC again"})
            # as koi's refresh: a spent token gets its successor again until the successor is used
            # itself; after that it's refused (and nothing is revoked)
            if self.refresh[old] is None:
                new = f"r{len(self.refresh)}"
                self.refresh[old], self.refresh[new] = new, None
            elif self.refresh[self.refresh[old]] is not None:
                return httpx.Response(401, json={"error": "refresh_token_invalid", "action": "repair",
                                                 "message": "refresh token not valid; pair this PC again"})
            return httpx.Response(200, json=self.tokens(self.refresh[old]))
        if req.headers.get("X-Shotcast-Device") not in self.access or self.revoked:
            return httpx.Response(401, json={"error": "access_token_invalid", "action": "retry",
                                             "message": "device access token not valid"})
        if path == "/api/devices/me":
            if req.method == "DELETE":
                self.revoked = self.signed_out = True
                return httpx.Response(204)
            return httpx.Response(200, json={"device_id": self.device, "league_id": self.default,
                                             "tenant_id": self.tenant, "sim_name": self.sim, "name": "PC",
                                             "league_active": True, "claim_cards": self.claim_cards})
        if path.startswith("/api/devices/rounds/") and path.endswith("/claims") and req.method == "GET":
            rid = path.split("/")[4]
            if rid not in self.rounds:   # (the bay's own rounds only, once they're here)
                return httpx.Response(404, json={"error": "not_found", "action": "fix", "message": "no such round"})
            return httpx.Response(200, json=[
                {"slot_id": sl["slot_id"], "claimed": (rid, sl["slot_id"]) in self.claimed,
                 "name": self.claimed.get((rid, sl["slot_id"]))} for sl in json.loads(self.rounds[rid])["slots"]])
        return httpx.Response(404, json={"detail": "Not Found"})

    def put(self, req):
        assert req.headers["content-encoding"] == "gzip"
        if req.headers.get("X-Shotcast-Device") not in self.access or self.revoked:
            return httpx.Response(401)
        rid = req.url.path.split("/")[3]
        assert req.url.path == f"/api/rounds/{rid}"   # (a round is sent once: no revision in its address)
        self.puts.append((rid, 1, json.loads(gzip.decompress(req.content))))
        if self.answers:
            return self.answers.pop(0)
        # as koi's put_round: by the sha256 of the JSON sent (not of its gzip bytes)
        sent = gzip.decompress(req.content)
        if rid not in self.rounds:
            self.rounds[rid] = sent
            return httpx.Response(201, json={"status": "stored"})
        if self.rounds[rid] == sent:
            return httpx.Response(200, json={"status": "duplicate"})
        return httpx.Response(409, json={"error": "round_final", "action": "fix", "message": "already here",
                                         "sha256": hashlib.sha256(self.rounds[rid]).hexdigest()})


def mock_http(monkeypatch, *fakes):
    """httpx.Client, answered by the fake API for each fake's host."""
    by_host = {f.host: f for f in fakes}
    transport = httpx.MockTransport(lambda req: by_host[req.url.host](req))
    def client(*a, **kw):
        kw.pop("transport", None)
        return _CLIENT(*a, transport=transport, **kw)

    monkeypatch.setattr(httpx, "Client", client)


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    mock_http(monkeypatch, fake)
    return fake


def pair(store, api, url=API, prepare=None):
    return cloud.pair(store, url, "Sim PC", clock=lambda: api.now, sleep=lambda s: None, say=lambda s: None,
                      prepare=prepare)


def paired(api, url=API, seed=None, env=None):
    """Pair this PC as the command line does (the profile made before the marker): the profile
    Shotcast then starts in."""
    pair(cloud.store(), api, url, prepare=lambda rec: cloud.make_profile(cloud.folder_key(rec), rec, seed, env))
    prof = cloud.locate()
    assert prof is not None and prof.connected, prof and prof.problem
    return prof


def base_cfg(tmp_path):
    """A self-hosted booth's config, with its own files and outbound jobs."""
    from commentator.config import Config

    cfg = Config(log_dir=str(tmp_path / "logs"), db_path=str(tmp_path / "logs" / "season.db"),
                 league_file=str(tmp_path / "league.json"), history_file=str(tmp_path / "history.json"))
    cfg.publish.enabled, cfg.publish.bucket, cfg.publish.recaps = True, "stats-bucket", True
    cfg.backup.s3, cfg.squabbit.enabled = True, True
    return cfg


def paired_cfg(tmp_path, api, url=API):
    """The booth's config on a PC paired with `api` (as __main__ makes it at startup)."""
    from commentator.config import load_config

    prof = paired(api, url)
    return cloud.booth_config(load_config(prof.config), prof)


# ---- where the pairing and the profile are ---------------------------------------------------

def test_the_pairing_is_the_machines_at_a_fixed_place_no_config_moves(tmp_path, monkeypatch):
    assert cloud.root() == cloud.TEST_ROOT   # (tests.cloud_isolation: each test its own)
    monkeypatch.setattr(cloud, "TEST_ROOT", None)
    monkeypatch.setenv("SHOTCAST_CLOUD_DIR", str(tmp_path / "elsewhere"))   # (nothing in the environment moves it)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))      # (a roaming profile: never there)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    for platform, want in (("win32", tmp_path / "Local" / "Shotcast" / "cloud"),
                           ("darwin", Path.home() / "Library" / "Application Support" / "Shotcast" / "cloud")):
        monkeypatch.setattr(cloud.sys, "platform", platform)
        assert cloud.root() == want and cloud.marker() == want / "device.json"


def test_unpaired_is_only_a_missing_marker_and_changes_nothing(tmp_path):
    cfg = base_cfg(tmp_path)
    assert cloud.locate() is None
    assert cloud.booth_config(cfg, None) is cfg and cloud.startup is None
    assert not cloud.root().exists() or not any(cloud.root().iterdir())


def test_pairing_keeps_who_it_is_in_the_clear_and_the_tokens_protected_with_it(api):
    store = cloud.store()
    d = pair(store, api)
    assert (d["api"], d["tenant_id"], d["device_id"], d["sim_name"]) == (API, "t1", "d1", "Bay 1")
    assert "tokens" not in d
    rec = json.loads(store.path.read_text())
    secret = json.loads(cloud.unprotect(rec["secret"]))
    assert secret["bind"] == [API, "t1", "d1"] and secret["tokens"]["refresh_token"] == "r0"
    assert secret["machine"] == cloud.machine_id()
    assert '"refresh_token"' not in store.path.read_text()
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600
    assert cloud.identity(cloud.pairing()) == (API, "t1", "d1", "name:Bay 1")


def test_the_api_origin_is_kept_and_compared_in_one_form():
    assert cloud.canon("HTTPS://Cloud.Test:443/") == "https://cloud.test"
    assert cloud.canon("http://LOCALHOST:80") == "http://localhost"
    assert cloud.canon("http://[::1]:8000/") == "http://[::1]:8000"
    assert cloud.canon("https://cloud.test:8443") == "https://cloud.test:8443"
    assert cloud.check_api(" HTTPS://Cloud.TEST/ ") == "https://cloud.test"
    for bad in ("http://cloud.test", "https://cloud.test/api", "https://u:p@cloud.test", "https://cloud.test:x"):
        with pytest.raises(cloud.CloudError):
            cloud.check_api(bad)
    assert cloud.identity({"api": "HTTPS://CLOUD.TEST:443/", "tenant_id": "t1", "device_id": "d1"})[0] == API


def test_a_pairing_made_with_another_spelling_of_the_address_is_the_same_one(api):
    pair(cloud.store(), api, "HTTPS://Cloud.Test:443/")
    rec = cloud.store().read()
    assert rec["api"] == API and cloud.Cloud(cloud.store(), clock=lambda: api.now).access_token() == "a1"


@pytest.mark.parametrize("edit", [{"api": "https://evil.test"}, {"tenant_id": "t2"}, {"device_id": "d2"}])
def test_an_edit_to_the_clear_pairing_never_sends_its_tokens_anywhere(api, monkeypatch, edit):
    evil = FakeAPI("evil.test")
    mock_http(monkeypatch, api, evil)
    prof = paired(api)
    store = cloud.store()
    rec = json.loads(store.path.read_text())
    store.path.write_text(json.dumps(rec | edit))
    before = len(api.calls)
    with pytest.raises(cloud.PairingChanged, match="changed outside Shotcast"):
        cloud.Cloud(store)
    # Damaged: nothing starts, and never as the self-hosted Shotcast.
    with pytest.raises(cloud.Damaged, match="changed outside Shotcast"):
        cloud.locate()
    assert "changed outside Shotcast" in cloud.inspect().damage
    assert len(api.calls) == before and evil.calls == [] and prof.home.is_dir()


def test_a_request_only_goes_to_the_address_the_tokens_were_issued_for(api, monkeypatch):
    evil = FakeAPI("evil.test")
    mock_http(monkeypatch, api, evil)
    store = cloud.store()
    pair(store, api)
    before = len(api.calls)
    c = cloud.Cloud(store, http=httpx.Client(base_url="https://evil.test"))
    with pytest.raises(cloud.PairingChanged, match="nothing sent"):
        c.request("GET", "/api/devices/me")
    api.now += 600   # (a refresh too)
    with pytest.raises(cloud.PairingChanged):
        c.access_token(stale="a0")
    assert evil.calls == [] and len(api.calls) == before


def _forge(rec, bind, machine=None, tokens=None):
    """A marker with `rec` in the clear and `bind` protected (as TokenStore.write, by hand)."""
    secret = {"tokens": {"refresh_token": "r0", "access_token": "a0", "access_exp": 0} if tokens is None else tokens,
              "bind": bind, "machine": machine or cloud.machine_id()}
    cloud.marker().write_text(json.dumps({k: v for k, v in rec.items() if k != "secret"}
                                         | {"secret": cloud.protect(json.dumps(secret).encode())}))


@pytest.mark.parametrize("damage", ["garbage", "version", "secret", "not-a-dict", "bad-api", "bad-bound-api",
                                    "no-tenant", "number-device"])
def test_a_damaged_marker_starts_nothing_and_is_never_self_hosted(api, damage):
    paired(api)
    path = cloud.marker()
    rec = json.loads(path.read_text())
    before = sorted(p.name for p in cloud.root().iterdir())
    if damage == "bad-api":   # (an address that doesn't even parse: damage, not a crash)
        _forge(rec | {"api": "https://cloud.test:x"}, ["https://cloud.test:x", "t1", "d1"])
    elif damage == "bad-bound-api":
        _forge(rec, ["https://[cloud.test", "t1", "d1"])
    elif damage == "no-tenant":
        _forge({k: v for k, v in rec.items() if k != "tenant_id"}, [API, "t1", "d1"])
    elif damage == "number-device":
        _forge(rec | {"device_id": 7}, [API, "t1", 7])
    else:
        path.write_text({"garbage": "{not json", "version": json.dumps(rec | {"v": 2}),
                         "secret": json.dumps(rec | {"secret": "plain:bm90IGpzb24="}),
                         "not-a-dict": "[1, 2]"}[damage])
    with pytest.raises(cloud.Damaged) as e:
        cloud.locate()
    assert "unpair --force" in str(e.value)
    got = cloud.inspect()
    assert got.damage and not got.connected and got.origin is None
    assert sorted(p.name for p in cloud.root().iterdir()) == before   # (nothing made: no recovery profile)
    with pytest.raises(cloud.CloudError):
        cloud.Cloud(cloud.store())


def test_a_pairing_is_bound_to_the_machine_it_was_made_on(api, monkeypatch):
    paired(api)
    monkeypatch.setattr(cloud, "machine_id", lambda: "another-pc")
    with pytest.raises(cloud.Damaged, match="another computer"):
        cloud.locate()
    with pytest.raises(cloud.PairingChanged, match="another computer"):
        cloud.Cloud(cloud.store())


def test_a_marker_with_no_profile_starts_nothing_and_makes_none(api):
    import shutil

    prof = paired(api)
    shutil.rmtree(prof.home)
    with pytest.raises(cloud.Damaged, match="missing"):
        cloud.locate()
    assert not prof.home.exists() and not (cloud.root() / "recovery").exists()


def test_a_profile_made_for_another_pairing_or_a_season_file_bound_to_another_starts_nothing(api):
    from commentator import cloud_rounds

    prof = paired(api)
    db = cloud_rounds.connect(str(prof.db))
    db.execute("UPDATE cloud_pairing SET device_id = 'd9'")
    db.close()
    with pytest.raises(cloud.Damaged, match="isn't this Shotcast Cloud pairing's"):
        cloud.locate()
    prof.db.unlink()
    from commentator.store import Store

    Store(str(prof.db), new_session=False).db.close()   # a season file put there, bound to nothing
    with pytest.raises(cloud.Damaged, match="season file"):
        cloud.locate()
    (prof.home / cloud.FOLDER_FILE).write_text(json.dumps({"key": [API, "t1", "d9"]}))
    with pytest.raises(cloud.Damaged, match="another Shotcast Cloud pairing"):
        cloud.locate()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "not-a-season"])
def test_an_established_profile_without_its_season_file_starts_nothing_and_never_makes_one(api, damage):
    import sqlite3

    prof = paired(api)
    prof.db.unlink()
    if damage == "corrupt":
        prof.db.write_bytes(b"this is not a database at all" * 100)
    elif damage == "not-a-season":
        sqlite3.connect(prof.db).close()
    with pytest.raises(cloud.Damaged, match="season file"):
        cloud.locate()
    assert prof.db.exists() == (damage != "missing")
    if damage == "corrupt":
        assert prof.db.read_bytes().startswith(b"this is not")


@pytest.mark.parametrize("text", ["log_dir: [unclosed\n", "source: 5\n", None])
def test_a_profile_whose_config_is_invalid_starts_nothing(api, text):
    prof = paired(api)
    if text is None:
        prof.config.unlink()
    else:
        prof.config.write_text(text, encoding="utf-8")
    with pytest.raises(cloud.Damaged, match="config.yaml"):
        cloud.locate()


# ---- the profile ------------------------------------------------------------------------------

def _self_hosted(tmp_path):
    """A self-hosted Shotcast home: config.yaml with private bits and outbound jobs, and a .env."""
    import yaml

    home = tmp_path / "self-hosted"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "llm": {"live": {"model": "my-model"}}, "source": {"port": 1234},
        "news": {"enabled": True, "feeds": ["https://private.example/feed?token=abc"]},
        "weather": {"enabled": True, "location": "Nottingham, NH", "latitude": 43.1, "longitude": -71.1,
                    "place": "Nottingham"},
        "publish": {"enabled": True, "bucket": "stats-bucket"}, "squabbit": {"enabled": True},
        "chat": {"enabled": True, "table": "site-table"}}), encoding="utf-8")
    (home / ".env").write_text("OPENAI_API_KEY=sk-open\nAZURE_SPEECH_KEY=az\nAWS_ACCESS_KEY_ID=AKIA\n"
                               "AWS_SECRET_ACCESS_KEY=shh\nNTFY_TOPIC=my-topic\nSQUABBIT_PASSWORD=pw\n"
                               "TYPESAFE_API_KEY=ts\n", encoding="utf-8")
    return home


def _pair_cli(api, monkeypatch, *argv):
    real = cloud.pair
    monkeypatch.setattr(cloud, "pair", lambda store, url, name, prepare=None: real(
        store, url, name, sleep=lambda s: None, say=lambda s: None, prepare=prepare))
    cloud.main(["pair", "--api", API, *argv])
    return cloud.locate()


def test_pairing_seeds_the_profile_from_the_allow_list_without_private_bits(tmp_path, api, monkeypatch, capsys):
    import yaml

    from commentator.config import load_config

    home = _self_hosted(tmp_path)
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    prof = _pair_cli(api, monkeypatch, "--config", str(home / "config.yaml"))
    assert prof.home.parent == cloud.root() and prof.connected
    assert "Restart Shotcast to apply" in capsys.readouterr().out
    data = yaml.safe_load(prof.config.read_text(encoding="utf-8"))
    assert set(data) <= set(cloud.CARRIED)
    text = prof.config.read_text(encoding="utf-8")
    for private in ("private.example", "Nottingham", "43.1", "stats-bucket", "site-table"):
        assert private not in text
    cfg = load_config(prof.config)
    assert cfg.llm.live.model == "my-model" and cfg.source.port == 1234 and cfg.news.enabled
    assert not cfg.weather.enabled and cfg.weather.latitude is None
    assert prof.env.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-open\nAZURE_SPEECH_KEY=az\n"
    assert {p.name: p.read_bytes() for p in home.iterdir()} == before   # the self-hosted files untouched


def test_the_paired_config_is_built_from_the_allow_list_and_works_in_the_profile(tmp_path, api):
    from commentator.config import Config

    prof = paired(api)
    cfg = base_cfg(tmp_path)   # (a profile config.yaml someone filled in by hand)
    cfg.live.enabled, cfg.live.local_keep_days, cfg.live.local_dir = False, 9, "my-recordings"
    cfg.chat.enabled, cfg.chat.table, cfg.chat.alerts = True, "site-table", True
    cfg.booking.enabled, cfg.notify.provider = True, "ntfy"
    cfg.report.upload_url = "https://drop.self-hosted.test"
    cfg.llm.live.model, cfg.tts.provider, cfg.source.port = "my-model", "console", 1234
    booth = cloud.booth_config(cfg, prof)
    assert (booth.log_dir, booth.db_path, booth.history_file, booth.league_file) == (
        str(prof.logs), str(prof.db), str(prof.home / "data" / "history.json"), None)
    assert (booth.llm.live.model, booth.tts.provider, booth.source.port) == ("my-model", "console", 1234)
    assert not booth.publish.enabled and booth.publish.bucket is None and not booth.publish.recaps
    assert not booth.backup.s3 and booth.backup.dir is None
    assert not booth.live.enabled and not booth.live.local_keep_days and not booth.live.with_rounds
    assert not booth.chat.enabled and booth.chat.table is None and not booth.chat.alerts
    assert not booth.squabbit.enabled and not booth.booking.enabled and booth.notify.provider == "none"
    fresh = Config()
    forced = {"log_dir", "db_path", "history_file", "league_file", "publish", "backup", "live", "chat",
              "squabbit", "booking", "notify"}
    for name in type(cfg).model_fields:
        if name not in cloud.CARRIED and name not in forced:
            assert getattr(booth, name) == getattr(fresh, name), name


def test_a_setting_added_later_is_off_when_paired_unless_it_is_carried_on_purpose(tmp_path, api):
    from commentator.config import Config

    class Later(Config):
        uploads_somewhere: bool = False

    cfg = Later(**base_cfg(tmp_path).model_dump(), uploads_somewhere=True)
    assert cloud.booth_config(cfg, paired(api)).uploads_somewhere is False


def test_the_profiles_season_file_is_bound_to_the_pairing_as_it_is_made(api):
    from commentator import cloud_rounds

    prof = paired(api)
    db = cloud_rounds.connect(str(prof.db))
    assert cloud_rounds.bound(db) == (API, "t1", "d1")
    db.close()
    assert not list(prof.logs.glob("season.db.new*"))


def test_making_a_profile_again_never_replaces_what_is_there(api):
    prof = paired(api)
    prof.config.write_text("commentary:\n  enabled: false\n", encoding="utf-8")
    con = __import__("sqlite3").connect(prof.db)
    con.execute("CREATE TABLE kept (x)")
    con.commit()
    con.close()
    cloud.make_profile(cloud.folder_key(prof.rec), prof.rec, {"commentary": {"enabled": True}}, {"X": "y"})
    assert prof.config.read_text(encoding="utf-8") == "commentary:\n  enabled: false\n"
    con = __import__("sqlite3").connect(prof.db)
    assert con.execute("SELECT name FROM sqlite_master WHERE name = 'kept'").fetchone()
    con.close()


def test_profiles_are_made_one_process_at_a_time(monkeypatch):
    """make_profile works under the profile's own lock, checking again inside it."""
    import contextlib

    held = []

    @contextlib.contextmanager
    def lock(path, timeout, what):
        held.append(path)
        yield
        held.append(None)

    monkeypatch.setattr("commentator.locks.file_lock", lock)
    key = (API, "t1", "d1")
    home = cloud.make_profile(key, {"sim_name": "Bay 1"})
    assert held == [cloud.root() / f"{home.name}.lock", None] and (home / "logs" / "season.db").is_file()


def test_another_pairing_is_another_profile_and_a_renamed_bay_the_same():
    rec = {"api": API, "tenant_id": "t1", "device_id": "d1", "sim_name": "Bay 1"}

    def home(r):
        return cloud.home_for(cloud.folder_key(r))
    a = home(rec)
    assert home(rec | {"sim_name": "Bay One"}) == a and home(rec | {"api": "HTTPS://CLOUD.TEST/"}) == a
    assert len({a, home(rec | {"device_id": "d2"}), home(rec | {"tenant_id": "t2"}),
                home(rec | {"api": "https://other.test"})}) == 4


def test_the_bay_is_its_server_id_when_given_else_its_exact_name():
    assert cloud.bay_of({"sim_name": "Bay 1", "bay_id": "b7"}) == "id:b7"
    assert cloud.bay_of({"sim_name": "Bay 1"}) == "name:Bay 1"
    assert cloud.bay_of({"sim_name": "Bay 1"}) != cloud.bay_of({"sim_name": "bay 1"})   # never folded


# ---- tokens -----------------------------------------------------------------------------------

def test_access_tokens_refresh_and_rotate_under_the_same_pairing(api):
    store = cloud.store()
    pair(store, api)
    c = cloud.Cloud(store, clock=lambda: api.now, sleep=lambda s: None)
    assert c.access_token() == "a1"
    api.now += 600
    assert c.access_token() == "a2" and store.read()["tokens"]["refresh_token"] == "r1"


def test_a_token_is_never_used_for_another_pairing_than_the_operation_started_with(api, monkeypatch):
    other = FakeAPI("other.test")
    other.tenant, other.device = "t2", "d9"
    mock_http(monkeypatch, api, other)
    store = cloud.store()
    pair(store, api)
    c = cloud.Cloud(store, clock=lambda: api.now, sleep=lambda s: None)
    store.delete()
    pair(store, other, "https://other.test")   # unpaired and paired elsewhere meanwhile
    before = len(api.calls) + len(other.calls)
    with pytest.raises(cloud.PairingChanged):
        c.access_token()
    with pytest.raises(cloud.PairingChanged):
        c.request("GET", "/api/devices/me")
    assert len(api.calls) + len(other.calls) == before   # nothing sent, with either's token, to either
    store.delete()
    with pytest.raises(cloud.NotPaired):
        c.access_token()


def test_a_refresh_that_moves_the_pc_to_another_bay_is_kept_but_aborts_the_operation(api):
    store = cloud.store()
    pair(store, api)
    c = cloud.Cloud(store, clock=lambda: api.now, sleep=lambda s: None)
    api.now += 600
    api.sim = "Bay 2"   # renamed, or the device moved on the site
    with pytest.raises(cloud.PairingChanged):
        c.access_token()
    rec = store.read()
    assert rec["sim_name"] == "Bay 2" and rec["tokens"]["refresh_token"] == "r1"   # the spent token isn't lost
    assert cloud.Cloud(store, clock=lambda: api.now).access_token() == "a2"


def test_a_refused_refresh_keeps_the_pairing_and_says_it_needs_pairing_again(api):
    prof = paired(api)
    c = cloud.Cloud(cloud.store(), clock=lambda: api.now, sleep=lambda s: None)
    api.now += 600
    api.revoked = True
    with pytest.raises(cloud.Refused):
        c.access_token()
    assert cloud.marker().exists() and cloud.store().read()["refused"]
    got = cloud.locate()
    assert got.home == prof.home and got.problem == cloud.REPAIR
    with pytest.raises(cloud.Refused):
        cloud.Cloud(cloud.store())


def test_an_api_that_needs_a_newer_shotcast_says_which(api):
    store = cloud.store()
    pair(store, api)
    c = cloud.Cloud(store, clock=lambda: api.now, sleep=lambda s: None)
    api.minimum = "2.5.0"
    with pytest.raises(cloud.TooOld, match="2.5.0") as e:
        c.request("GET", "/api/devices/me")   # (the refresh first)
    assert e.value.minimum == "2.5.0"
    store.delete()
    with pytest.raises(cloud.TooOld, match="update Shotcast"):
        pair(store, api)


# ---- the command line -------------------------------------------------------------------------

def test_the_command_line_pairs_reports_and_unpairs_each_taking_effect_at_restart(api, monkeypatch, capsys):
    prof = _pair_cli(api, monkeypatch)
    salt = (cloud.root() / cloud.FINGERPRINT_SALT).read_text()
    assert "(sim 'Bay 1')" in capsys.readouterr().out
    assert (prof.home / cloud.FOLDER_FILE).is_file() and prof.db.is_file()   # made at pair time
    cloud.main(["status"])
    out = capsys.readouterr().out
    assert "sim 'Bay 1'" in out and "0 not sent yet" in out and str(prof.home) in out
    cloud.main(["unpair"])
    assert "Restart Shotcast to apply" in capsys.readouterr().out
    assert cloud.locate() is None and api.revoked and prof.home.is_dir()   # (its data kept)
    assert (cloud.root() / cloud.FINGERPRINT_SALT).read_text() == salt   # (and its fingerprint's salt)
    with pytest.raises(SystemExit, match="isn't paired"):
        cloud.main(["status"])


def _unsent_round(prof):
    from commentator.store import Store
    from tests.test_round_record import _hit

    s = Store(str(prof.db))
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    _hit(s, "Ann", 1)
    s.end_round(u, complete=True)
    s.db.commit()
    s.db.close()
    return u


def test_unpair_refuses_with_rounds_not_on_the_cloud_and_force_says_what_is_stranded(api, monkeypatch, capsys):
    prof = _pair_cli(api, monkeypatch)
    u = _unsent_round(prof)
    with pytest.raises(SystemExit, match="1 round") as e:
        cloud.main(["unpair"])
    assert u in str(e.value)
    assert cloud.marker().exists() and not api.revoked
    cloud.main(["unpair", "--force"])
    out = capsys.readouterr().out
    assert "never sent" in out and u in out and cloud.locate() is None and api.revoked


def test_unpair_keeps_the_pairing_when_the_cloud_cant_remove_the_pc_unless_forced(api, monkeypatch):
    _pair_cli(api, monkeypatch)
    real = api.__call__

    def down(req):
        if req.url.path == "/api/devices/me" and req.method == "DELETE":
            return httpx.Response(503, json={"detail": "down"})
        return real(req)
    mock_http(monkeypatch, type("Down", (), {"host": api.host, "__call__": staticmethod(down)})())
    with pytest.raises(SystemExit, match="Not unpaired"):
        cloud.main(["unpair"])
    assert cloud.marker().exists()
    cloud.main(["unpair", "--force"])
    assert cloud.locate() is None


def _host(monkeypatch, handler):
    """The API's host answered by `handler` from now on."""
    mock_http(monkeypatch, type("Host", (), {"host": "cloud.test", "__call__": staticmethod(handler)})())


def test_unpair_refuses_while_shotcast_runs_with_the_pairing(api, monkeypatch):
    prof = _pair_cli(api, monkeypatch)
    for who in ("run", "tray"):
        with cloud.running(prof, who):
            with pytest.raises(SystemExit, match="stop Shotcast first"):
                cloud.main(["unpair"])
    assert cloud.marker().exists() and not api.revoked
    cloud.main(["unpair"])
    assert not cloud.marker().exists() and api.revoked


def test_unpair_checks_under_the_lock_that_it_is_still_the_pairing_it_looked_at(api, monkeypatch):
    _pair_cli(api, monkeypatch)
    real, seen = cloud.inspect, []

    def inspect():
        got = real()
        seen.append(got)
        if len(seen) == 2:   # (re-paired meanwhile, as unpair took the lock)
            got.bound = ("https://other.test", "t2", "d9")
        return got
    monkeypatch.setattr(cloud, "inspect", inspect)
    with pytest.raises(SystemExit, match="changed meanwhile"):
        cloud.main(["unpair"])
    assert len(seen) == 2 and cloud.marker().exists() and not api.revoked


def test_unpair_sends_the_revoke_only_for_the_pairing_it_checked(api, monkeypatch):
    prof = _pair_cli(api, monkeypatch)
    rec = cloud.store().read()
    cloud.store().write(rec | {"device_id": "d9"})   # (another pairing's pass, put there meanwhile)
    with pytest.raises(cloud.PairingChanged):
        cloud.Cloud(cloud.store(), expect=prof.bound)
    before = len(api.calls)
    with pytest.raises(SystemExit):
        cloud._unpair(prof, cloud.store(), force=False)
    assert len(api.calls) == before and cloud.marker().exists()


def test_only_the_apis_204_confirms_the_pc_was_removed(api, monkeypatch, capsys):
    _pair_cli(api, monkeypatch)
    real = api.__call__

    def ok(req):   # (a 200 from something that isn't the API's delete: a proxy's page, say)
        if req.url.path == "/api/devices/me" and req.method == "DELETE":
            return httpx.Response(200, text="<html>ok</html>")
        return real(req)
    _host(monkeypatch, ok)
    with pytest.raises(SystemExit, match="Not unpaired"):
        cloud.main(["unpair"])
    assert cloud.marker().exists()
    mock_http(monkeypatch, api)
    api.revoked = True   # (removed on the site, or its pass refused: either way not confirmed here)
    monkeypatch.setattr(cloud, "REFRESH_WINDOW_S", 0)
    with pytest.raises(SystemExit, match="Remove this PC from the sims on the site"):
        cloud.main(["unpair"])
    assert cloud.marker().exists()
    cloud.main(["unpair", "--force"])
    out = capsys.readouterr().out
    assert "wasn't confirmed" in out and "remove it from the sims on the site" in out
    assert not cloud.marker().exists()


def test_the_network_failing_is_a_cloud_error_and_status_and_forced_unpair_still_work(api, monkeypatch, capsys):
    prof = _pair_cli(api, monkeypatch)
    u = _unsent_round(prof)

    def down(req):
        raise httpx.ConnectError("no route to host", request=req)
    _host(monkeypatch, down)
    monkeypatch.setattr(cloud, "REFRESH_WINDOW_S", 0)
    capsys.readouterr()
    with pytest.raises(cloud.Unreachable, match="couldn't reach"):
        cloud.Cloud(cloud.store(), sleep=lambda s: None).request("GET", "/api/devices/me")
    with pytest.raises(cloud.Unreachable):
        _PAIR(cloud.TokenStore(cloud.root() / "other.json"), API, "Sim PC", sleep=lambda s: None, say=lambda s: None)
    cloud.main(["status"])
    out = capsys.readouterr().out
    assert "didn't answer" in out and "1 not sent yet" in out
    with pytest.raises(SystemExit, match="Not unpaired"):
        cloud.main(["unpair"])
    cloud.main(["unpair", "--force"])
    out = capsys.readouterr().out
    assert "didn't confirm removing" in out and u in out and not cloud.marker().exists()


def test_a_damaged_pairing_can_still_be_looked_at_and_forcibly_unpaired(api, monkeypatch, capsys):
    prof = _pair_cli(api, monkeypatch)
    u = _unsent_round(prof)
    cloud.marker().write_text("{damaged")
    capsys.readouterr()
    cloud.main(["status"])
    out = capsys.readouterr().out
    assert "can't be used" in out and "unpair --force" in out
    with pytest.raises(SystemExit, match="--force"):
        cloud.main(["unpair"])
    with pytest.raises(SystemExit, match="unpair --force"):
        cloud.main(["upload"])
    cloud.main(["unpair", "--force"])
    out = capsys.readouterr().out
    assert "remove it from the sims on the site" in out and not cloud.marker().exists()
    assert prof.db.is_file() and api.revoked is False
    with pytest.raises(SystemExit, match="isn't paired"):
        cloud.main(["status"])
    del u


def test_the_stats_link_is_built_only_from_the_verified_origin(api):
    prof = paired(api)
    assert cloud.web_stats_url(prof) == f"{API}/leagues/lg1/"
    edited = cloud.Profile(prof.home, prof.rec | {"api": "https://evil.test"}, prof.bound)
    assert cloud.web_stats_url(edited) == f"{API}/leagues/lg1/"
    assert cloud.web_stats_url(cloud.Profile(prof.home, prof.rec, prof.bound, damage="x")) is None
    assert cloud.web_stats_url(cloud.Profile(prof.home, prof.rec | {"league_id": "../x?y"}, prof.bound)) is None
    assert cloud.web_stats_url(None) is None


def test_paired_startup_drops_the_service_keys_it_inherited(monkeypatch):
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "TYPESAFE_API_KEY", "AWS_PROFILE", "NTFY_TOPIC"):
        monkeypatch.setenv(k, "inherited")
    monkeypatch.setenv("SHOTCAST_X", "kept")
    gone = cloud.clean_env()
    assert set(gone) >= {"OPENAI_API_KEY", "OPENAI_BASE_URL", "TYPESAFE_API_KEY", "AWS_PROFILE", "NTFY_TOPIC"}
    assert not any(k in os.environ for k in gone) and os.environ["SHOTCAST_X"] == "kept"


# ---- review round 5 ---------------------------------------------------------------------------

@pytest.mark.parametrize("tokens", [{"refresh_token": "r0", "access_token": "a0"},
                                    {"refresh_token": "r0", "access_token": "a0", "access_exp": "soon"},
                                    {"refresh_token": "", "access_token": "a0", "access_exp": 0},
                                    {"access_token": "a0", "access_exp": 0}, ["r0", "a0", 0]])
def test_a_marker_whose_tokens_arent_whole_is_damage_at_startup_and_can_be_forcibly_unpaired(api, monkeypatch,
                                                                                             capsys, tokens):
    _pair_cli(api, monkeypatch)
    rec = json.loads(cloud.marker().read_text())
    _forge(rec, [API, "t1", "d1"], tokens=tokens)
    with pytest.raises(cloud.Damaged, match="pass can't be read"):
        cloud.locate()
    with pytest.raises(cloud.CloudError, match="pair this PC again"):   # (never a KeyError further on)
        cloud.Cloud(cloud.store())
    cloud.main(["status"])
    cloud.main(["unpair", "--force"])
    assert not cloud.marker().exists()


def test_a_forced_unpair_forgets_the_pairing_whatever_goes_wrong_asking_the_cloud(api, monkeypatch, capsys):
    _pair_cli(api, monkeypatch)

    def broken(*a, **kw):
        raise KeyError("access_exp")
    monkeypatch.setattr(cloud, "Cloud", broken)
    with pytest.raises(KeyError):
        cloud.main(["unpair"])
    assert cloud.marker().exists()
    cloud.main(["unpair", "--force"])
    assert "remove it from the sims on the site" in capsys.readouterr().out and not cloud.marker().exists()


def test_the_booth_checks_its_pairing_again_once_it_holds_the_lifecycle_lock(api, monkeypatch):
    prof = _pair_cli(api, monkeypatch)
    with cloud.running(prof, "run") as now:
        assert now.bound == prof.bound and now.home == prof.home
    cloud.main(["unpair"])   # (finished between the booth's first look and its taking the lock)
    with pytest.raises(cloud.PairingChanged, match="unpaired"):
        with cloud.running(prof, "run"):
            pytest.fail("ran in a profile that was unpaired")
    api2 = FakeAPI()
    api2.device = "d2"
    mock_http(monkeypatch, api2)
    _PAIR(cloud.store(), API, "Sim PC", sleep=lambda s: None, say=lambda s: None,   # (and paired again meanwhile)
          prepare=lambda rec: cloud.make_profile(cloud.folder_key(rec), rec))
    again = cloud.locate()
    assert again.bound != prof.bound
    with pytest.raises(cloud.PairingChanged, match="changed"):
        with cloud.running(prof, "tray"):
            pytest.fail("ran in another pairing's place")
    again.db.unlink()
    with pytest.raises(cloud.Damaged):
        with cloud.running(again, "run"):
            pytest.fail("ran damaged")


@pytest.mark.parametrize("damage", ["no-shots-table", "a-damaged-page"])
def test_startup_checks_the_whole_season_file_not_only_its_binding(api, damage):
    import sqlite3

    prof = paired(api)
    con = sqlite3.connect(prof.db)
    size = con.execute("PRAGMA page_size").fetchone()[0]
    page = con.execute("SELECT rootpage FROM sqlite_master WHERE name = 'shots'").fetchone()[0]
    if damage == "no-shots-table":
        con.execute("DROP TABLE shots")
        con.commit()
    con.close()
    if damage == "a-damaged-page":
        data = bytearray(prof.db.read_bytes())
        data[(page - 1) * size:page * size] = b"\xff" * size
        prof.db.write_bytes(bytes(data))
    assert cloud._db_binding is not None
    with pytest.raises(cloud.Damaged, match="season file"):
        cloud.locate()
    assert cloud.inspect().damage and cloud.locate.__doc__


def test_recovering_a_damaged_pairing_never_deletes_one_made_meanwhile(api, monkeypatch, capsys):
    _pair_cli(api, monkeypatch)
    good = cloud.marker().read_text()
    cloud.marker().write_text("{damaged")
    prof = cloud.inspect()
    assert prof.damage and prof.seen
    cloud.marker().write_text(good)   # (another process repaired it, or paired again, meanwhile)
    with pytest.raises(cloud.PairingChanged, match="kept"):
        cloud._unpair(prof, cloud.store(), force=True)
    assert cloud.marker().read_text() == good and cloud.locate() is not None
    cloud.marker().write_text("{damaged")
    cloud._unpair(cloud.inspect(), cloud.store(), force=True)   # (the one looked at: forgotten)
    assert not cloud.marker().exists()


def test_a_machine_with_no_id_of_its_own_gets_one_made_once_and_kept(monkeypatch):
    import uuid

    monkeypatch.setattr(cloud, "_platform_id", lambda: "")
    nodes = iter(range(1, 100))
    monkeypatch.setattr(uuid, "getnode", lambda: next(nodes))   # (a network card's address isn't stable)
    first = cloud.machine_id()
    kept = (cloud.root() / cloud.INSTALL_ID).read_text()
    assert len(kept) == 32 and cloud.machine_id() == first and (cloud.root() / cloud.INSTALL_ID).read_text() == kept
    (cloud.root() / cloud.INSTALL_ID).write_text("not an id")
    with pytest.raises(cloud.CloudError, match="can't be told"):
        cloud.machine_id()
    (cloud.root() / cloud.INSTALL_ID).unlink()
    assert cloud.machine_id() != first   # (another install: a pairing copied to it is damage, not adopted)
    monkeypatch.setattr(cloud, "_platform_id", lambda: "the-os-id")
    assert cloud.machine_id() == cloud.machine_id() != first


def test_a_pairing_on_a_machine_with_no_id_of_its_own_still_starts_next_time(api, monkeypatch):
    monkeypatch.setattr(cloud, "_platform_id", lambda: "")
    prof = paired(api)
    assert cloud.locate().home == prof.home


def test_the_api_is_reached_through_the_proxy_the_environment_names(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    made = []

    def client(*a, **kw):
        made.append(kw)
        return _CLIENT(*a, **kw)
    monkeypatch.setattr(httpx, "Client", client)
    http = cloud._http(API)
    assert http.trust_env and made[0].get("trust_env", True) and not made[0]["follow_redirects"]
    assert any(m is not None for m in http._mounts.values())   # (the proxy is in its routes)


def test_a_stale_certificate_file_is_the_network_failing_not_a_crash(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    try:
        cloud._http(API)
    except cloud.Unreachable as e:
        assert "couldn't reach" in str(e)


def test_no_api_client_at_all_is_the_network_failing_and_recovery_still_works(api, monkeypatch, capsys):
    _pair_cli(api, monkeypatch)

    def broken(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory")   # (as a stale SSL_CERT_FILE made it)
    monkeypatch.setattr(httpx, "Client", broken)
    with pytest.raises(cloud.Unreachable, match="couldn't reach"):
        cloud.Cloud(cloud.store())
    capsys.readouterr()
    cloud.main(["status"])
    assert "didn't answer" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="Not unpaired"):
        cloud.main(["unpair"])
    cloud.main(["unpair", "--force"])
    assert not cloud.marker().exists()


def test_on_windows_only_a_dpapi_protected_pass_is_read(monkeypatch):
    monkeypatch.setattr(cloud.sys, "platform", "linux")   # (development's: only encoded)
    plain = cloud.protect(b"{}")
    assert plain.startswith("plain:") and cloud.unprotect(plain) == b"{}"
    monkeypatch.setattr(cloud.sys, "platform", "win32")
    with pytest.raises(cloud.CloudError, match="can't be read here"):
        cloud.unprotect(plain)
    monkeypatch.setattr(cloud, "_dpapi", lambda data, protect: b"opened:" + data)
    assert cloud.unprotect("dpapi:" + "eA==") == b"opened:x"


def test_a_paired_profiles_env_gives_only_the_ai_and_voice_keys(api, monkeypatch):
    prof = paired(api)
    prof.env.write_text("OPENAI_API_KEY=mine\nAWS_ACCESS_KEY_ID=AKIA\nAWS_SECRET_ACCESS_KEY=s\nNTFY_TOPIC=t\n"
                        "OPENAI_BASE_URL=https://elsewhere.test\n", encoding="utf-8")
    for k in ("OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "NTFY_TOPIC", "OPENAI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")   # (so monkeypatch puts it back)
    assert cloud.load_profile_env(prof.env) == ["OPENAI_API_KEY"]
    assert os.environ["OPENAI_API_KEY"] == "mine"
    assert not any(k in os.environ for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "NTFY_TOPIC",
                                             "OPENAI_BASE_URL"))


def test_a_check_of_an_address_that_doesnt_parse_is_a_cloud_error():
    for bad in ("https://[cloud.test", "https://cloud.test:x", 7):
        with pytest.raises(cloud.CloudError):
            cloud.check_api(bad)


# ---- review round 6 ---------------------------------------------------------------------------

def test_one_pairing_of_this_pc_at_a_time(api):
    from commentator.locks import file_lock

    with file_lock(cloud.marker().with_name("pairing.lock"), 0):   # (another window, waiting for approval)
        with pytest.raises(cloud.CloudError, match="being paired already"):
            pair(cloud.store(), api)
    assert api.calls == [] and not cloud.marker().exists()   # (no second code, so no second device)
    pair(cloud.store(), api)
    assert cloud.marker().exists()


def test_a_profile_is_never_seeded_from_another_cloud_profile(tmp_path, api, monkeypatch, capsys):
    from commentator import tray

    home = _self_hosted(tmp_path)
    monkeypatch.setattr(tray, "default_home", lambda: home)
    old = _pair_cli(api, monkeypatch)
    old.env.write_text("OPENAI_API_KEY=the-first-pairings-key\n", encoding="utf-8")
    old.config.write_text(old.config.read_text() + "\nmax_cpu_cores: 3\n", encoding="utf-8")
    cloud.main(["unpair"])
    monkeypatch.chdir(old.home)   # (a command prompt the paired tray opened, left open in the old profile)
    path, envs = cloud._self_hosted_files(None)
    assert path == home / "config.yaml" and old.env.resolve() not in [e.resolve() for e in envs]
    with pytest.raises(cloud.CloudError, match="profile"):
        cloud._self_hosted_files(str(old.config))
    api2 = FakeAPI()
    api2.tenant, api2.device = "t2", "d2"
    mock_http(monkeypatch, api2)
    cloud.main(["pair", "--api", API])
    new = cloud.locate()
    assert new.home != old.home and "the-first-pairings-key" not in new.env.read_text()
    assert "max_cpu_cores: 3" not in new.config.read_text()


# ---- review round 8 ---------------------------------------------------------------------------

def test_a_pc_paired_before_its_first_start_gets_the_installers_gspro_folder(tmp_path, api, monkeypatch, capsys):
    from commentator import tray
    from commentator.config import load_config

    monkeypatch.setattr(tray, "default_home", lambda: tmp_path / "never-started")   # (no self-hosted config yet)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tray, "pending_installer_gspro_dir", lambda: r"D:\Golf\GSPro")
    marked = []
    monkeypatch.setattr(tray, "remember_gspro_dir", marked.append)
    prof = _pair_cli(api, monkeypatch)
    source = load_config(prof.config).source
    assert source.connector_log == r"D:\Golf\GSPro\Core\GSPC\ConnectDebug.txt"
    assert source.gspro_courses_dirs == [r"D:\Golf\GSPro\Courses", r"D:\Golf\GSPro\Core\GSP\Courses"]
    assert marked == []   # (still pending for the self-hosted Shotcast's own config.yaml)


def test_with_no_pick_pending_the_profile_keeps_the_self_hosted_gspro_folder(tmp_path, api, monkeypatch, capsys):
    from commentator import tray
    from commentator.config import Config, load_config

    monkeypatch.setattr(tray, "default_home", lambda: tmp_path / "never-started")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tray, "pending_installer_gspro_dir", lambda: None)
    prof = _pair_cli(api, monkeypatch)
    assert load_config(prof.config).source.connector_log == Config().source.connector_log


@pytest.mark.parametrize("answer", [httpx.Response(401, text="<html>sign in</html>", headers={"content-type": "text/html"}),
                                    httpx.Response(401, json={"error": "something_new"}),
                                    httpx.Response(401, json={"detail": "Unauthorized"})])
def test_only_the_apis_own_word_ends_a_pairing_on_refresh(api, answer):
    """A 401 that isn't the API saying this pass is finished (a proxy's, a code this app doesn't know)
    keeps the pairing: tried again later, never marked as needing pairing again."""
    paired(api)
    c = cloud.Cloud(cloud.store(), clock=lambda: api.now, sleep=lambda s: None)
    api.now += 600
    real = api.__call__
    api.__class__ = type("Refusing", (FakeAPI,), {
        "__call__": lambda self, req: answer if req.url.path == "/api/devices/refresh" else real(req)})
    with pytest.raises(cloud.Unreachable):
        c.access_token()
    assert not cloud.store().read().get("refused") and cloud.locate().problem is None
    api.__class__ = FakeAPI
    assert c.access_token()   # (and works again once the API answers)


def test_a_refresh_whose_new_pass_was_never_saved_is_finished_after_a_restart(api):
    """The API rotated the token and this PC stopped before the new one was on its disk (a crash, a
    power cut, a lost answer). Whenever it next runs, days later, it sends the token it still has
    and gets the same new one: nothing was remembered about the refresh, and nothing is revoked."""
    paired(api)
    st = cloud.store()
    c = cloud.Cloud(st, clock=lambda: api.now, sleep=lambda s: None)
    api.now += 600

    def crash(rec):
        raise KeyboardInterrupt
    c._save = crash
    with pytest.raises(KeyboardInterrupt):
        c.access_token()
    assert st.read()["tokens"]["refresh_token"] == "r0" and api.refresh["r0"] == "r1"
    assert set(st.read()["tokens"]) == {"refresh_token", "access_token", "access_exp"}
    api.now += 5 * 86400
    again = cloud.Cloud(st, clock=lambda: api.now, sleep=lambda s: None)
    assert again.access_token() and not api.revoked
    assert st.read()["tokens"]["refresh_token"] == "r1" and len(api.refresh) == 2   # (the same new one)


def test_an_unpair_whose_answer_was_lost_is_confirmed_the_next_time(api, monkeypatch, capsys):
    _pair_cli(api, monkeypatch)
    api.revoked = api.signed_out = True   # (the first unpair's DELETE got through; its 204 didn't arrive)
    cloud.main(["unpair"])
    assert "already removed" in capsys.readouterr().out and cloud.locate() is None


def test_pairing_keeps_polling_through_an_answer_it_doesnt_know_and_sends_a_name_the_api_takes(api):
    api.polls = ["authorization_pending", "something_new"]   # (a code a later API might send: not a no)
    names = []
    real = api.__call__
    api.__class__ = type("Named", (FakeAPI,), {"__call__": lambda self, req: (
        names.append(json.loads(req.content)["name"]) if req.url.path == "/api/devices/pair" else None, real(req))[1]})
    rec = cloud.pair(cloud.store(), API, name="  Bay\tone  " + "x" * 100, http=httpx.Client(
        base_url=API, transport=httpx.MockTransport(api), headers={"User-Agent": "Shotcast/9.9"}),
        clock=lambda: api.now, sleep=lambda s: None, say=lambda s: None)
    assert rec["device_id"] == "d1" and names == [("Bay one " + "x" * 100)[:64]]


@pytest.mark.parametrize("answer", [
    httpx.Response(200, json={"device_id": "d1", "tenant_id": "t1", "sim_name": "Bay 1", "refresh_token": "",
                              "access_token": "", "access_expires_in": 600}),
    httpx.Response(200, json={"refresh_token": "rX", "access_token": "aX"}),
    httpx.Response(200, json={"device_id": "other", "tenant_id": "t1", "sim_name": "Bay 1", "refresh_token": "rX",
                              "access_token": "aX", "access_expires_in": 600}),
    httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}),
    httpx.Response(200, json={"device_id": "d1", "tenant_id": "t1", "sim_name": "Bay 1", "refresh_token": "rX",
                              "access_token": "aX", "access_expires_in": 600, "league_id": 7}),
    httpx.Response(200, json={"device_id": "d1", "tenant_id": "t1", "sim_name": "Bay 1", "refresh_token": "rX",
                              "access_token": "aX", "access_expires_in": 600, "bay_id": ""}),
    httpx.Response(200, json={"device_id": "d1", "tenant_id": "t1", "sim_name": "Bay 1", "refresh_token": "rX",
                              "access_token": "aX", "access_expires_in": "600"}),
    httpx.Response(401, json={"error": "device_signed_out"}),          # (not the API's whole refusal)
    httpx.Response(401, json={"error": "refresh_token_invalid", "action": "retry", "message": "x"}),
])
def test_an_answer_to_a_refresh_that_isnt_whole_changes_nothing_this_pc_holds(api, answer):
    paired(api)
    st = cloud.store()
    c = cloud.Cloud(st, clock=lambda: api.now, sleep=lambda s: None)
    api.now += 600
    real = api.__call__
    api.__class__ = type("Odd", (FakeAPI,), {
        "__call__": lambda self, req: answer if req.url.path == "/api/devices/refresh" else real(req)})
    with pytest.raises(cloud.Unreachable):
        c.access_token()
    kept = st.read()
    assert kept["tokens"]["refresh_token"] == "r0" and not kept.get("refused")   # (and still readable)
    api.__class__ = FakeAPI
    assert c.access_token() and st.read()["tokens"]["refresh_token"] == "r1"


def test_the_pass_is_on_the_disk_before_it_replaces_the_old_one(api, monkeypatch):
    paired(api)
    st = cloud.store()
    done = []
    real_sync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (done.append("sync"), real_sync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (done.append("replace"), real_replace(a, b))[1])
    st.write(st.read())
    assert done[:2] == ["sync", "replace"]   # (its contents on the disk before it takes the old file's name)


def test_pairing_asks_again_after_an_answer_that_isnt_the_apis_whole_one(api):
    answers = [httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}),
               httpx.Response(200, json={"refresh_token": "r0"})]
    real = api.__call__
    api.polls = []
    api.__class__ = type("Odd", (FakeAPI,), {"__call__": lambda self, req: (
        answers.pop(0) if req.url.path == "/api/devices/token" and answers else real(req))})
    rec = pair(cloud.store(), api)
    assert rec["device_id"] == "d1" and answers == []


def test_pairing_collects_an_approval_for_as_long_as_the_api_still_hands_it_out(api):
    """Approved in the code's last seconds, with the network away till after the code's own time: the
    API still has the first pass for this PC, so this PC still asks."""
    asked = []
    real = api.__call__

    def late(self, req):
        if req.url.path == "/api/devices/token":
            asked.append(api.now)
            api.now += 60   # (each poll a minute on)
            if api.now < 1000 + 600 + 300:   # nothing gets through till five minutes past the code's time
                raise httpx.ConnectError("down")
        return real(req)
    api.polls = []
    api.__class__ = type("Late", (FakeAPI,), {"__call__": late})
    rec = pair(cloud.store(), api)
    assert rec["device_id"] == "d1" and asked[-1] >= 1000 + 600 + 240


def test_pairing_waits_through_both_of_the_apis_windows_for_a_lost_first_pass(api):
    """Approved late and collected late, its answer lost: the API hands the pass out again for ten
    minutes from when it was first collected, which can be twenty past the code's own time."""
    asked = []
    real = api.__call__

    def late(self, req):
        if req.url.path == "/api/devices/token":
            asked.append(api.now)
            api.now += 60
            if api.now < 1000 + 600 + 1100:   # nothing gets through till eighteen minutes past the code's time
                raise httpx.ConnectError("down")
        return real(req)
    api.polls = []
    api.__class__ = type("Late", (FakeAPI,), {"__call__": late})
    assert pair(cloud.store(), api)["device_id"] == "d1" and asked[-1] >= 1000 + 600 + 1000


# ---- elapsed-time pacing, and the note as a PC is paired again -------------------------------------

def test_pairing_again_says_that_an_earlier_pairings_unsent_rounds_stay_on_this_pc(api, monkeypatch, capsys):
    prof = _pair_cli(api, monkeypatch)
    assert "earlier pairing" not in capsys.readouterr().out   # (nothing left behind the first time)
    _unsent_round(prof)
    cloud.main(["unpair", "--force"])
    capsys.readouterr()
    api.device = "d2"   # (pairing again makes another device on Shotcast Cloud)
    cloud.main(["pair", "--api", API])   # (cloud.pair is still the test's)
    again = cloud.locate()
    out = capsys.readouterr().out
    assert again.home != prof.home
    assert "1 round(s) from an earlier pairing of this PC weren't confirmed as on Shotcast Cloud" in out
    assert "stays on this PC only" in out


def test_the_upload_command_waits_for_a_round_to_settle_then_sends_it(api, monkeypatch, capsys):
    import time
    import types

    from commentator import cloud_rounds

    prof = _pair_cli(api, monkeypatch)
    u = _unsent_round(prof)
    clock, naps = [500.0], []
    monkeypatch.setattr(cloud_rounds, "_monotonic", lambda: clock[0])

    def nap(s):
        naps.append(s)
        clock[0] += s
    monkeypatch.setattr(cloud, "time", types.SimpleNamespace(sleep=nap, time=time.time, monotonic=time.monotonic))
    capsys.readouterr()
    cloud.main(["upload"])
    out = capsys.readouterr().out
    assert "Waiting for 1 round(s) to settle" in out and "Sent 1 round(s)" in out and u in api.rounds
    assert sum(naps) == cloud_rounds.QUIET_S and set(naps) == {cloud.UPLOAD_LOOK_S}


def _sent_fingerprints(api):
    sent = []

    def hook(req):
        if req.url.path == "/api/devices/pair":
            sent.append(json.loads(req.content)["fingerprint"])
        return None
    api.hook = hook
    return sent


def test_this_install_pairs_with_the_same_fingerprint_every_time(api):
    sent = _sent_fingerprints(api)
    store = cloud.store()
    pair(store, api)
    salt = (cloud.root() / cloud.FINGERPRINT_SALT).read_text()
    store.path.unlink()   # (unpaired: the salt stays)
    api.polls = []
    pair(store, api)
    assert len(sent) == 2 and sent[0] == sent[1] == cloud.fingerprint() and len(sent[0]) == 64
    assert (cloud.root() / cloud.FINGERPRINT_SALT).read_text() == salt and len(salt) == 64
    assert salt not in sent[0]
    (cloud.root() / cloud.FINGERPRINT_SALT).unlink()   # (another install: another fingerprint)
    assert cloud.fingerprint() != sent[0]


def test_on_windows_the_machines_ids_go_into_the_fingerprint_and_never_out(api, monkeypatch, caplog):
    import logging

    ids = {"guid": "11111111-2222-3333-4444-555555555555", "hw": "00112233445566778899aabbccddeeff",
           "disk": "0badf00d", "serial": "WD-WX12A3456789"}
    monkeypatch.setattr(cloud, "_ON_WINDOWS", False)
    alone = cloud.fingerprint()
    monkeypatch.setattr(cloud, "_ON_WINDOWS", True)
    readers = (lambda: ids["guid"], lambda: ids["hw"], lambda: ids["disk"], lambda: ids["serial"])
    monkeypatch.setattr(cloud, "_WINDOWS_READERS", readers)
    full = cloud.fingerprint()
    assert full != alone and cloud.fingerprint() == full   # (the hardware counts; still the same each time)
    ids["disk"] = "feedface"   # (another disk: another PC as far as the site can tell)
    assert cloud.fingerprint() != full
    ids["disk"] = "0badf00d"
    ids["serial"] = "WD-OTHER"   # (another physical disk, the same image and board: another PC too)
    assert cloud.fingerprint() != full
    ids["serial"] = "WD-WX12A3456789"

    def broken():
        raise OSError("no such key")
    monkeypatch.setattr(cloud, "_WINDOWS_READERS", (lambda: ids["guid"], broken, lambda: ids["disk"],
                                                    lambda: ids["serial"]))
    partial = cloud.fingerprint()   # (one that can't be read: the rest still make one)
    assert len(partial) == 64 and partial not in (full, alone)

    monkeypatch.setattr(cloud, "_WINDOWS_READERS", readers)
    bodies, said = [], []

    def hook(req):
        if req.url.path == "/api/devices/pair":
            bodies.append(req.content.decode())
        return None
    api.hook = hook
    caplog.set_level(logging.DEBUG)
    cloud.pair(cloud.store(), API, "Sim PC", clock=lambda: api.now, sleep=lambda s: None, say=said.append)
    assert [json.loads(b)["fingerprint"] for b in bodies] == [full]
    shown = "\n".join(said) + caplog.text + cloud.store().path.read_text() + "".join(bodies)
    for raw in [*ids.values(), *(v.replace("-", "") for v in ids.values())]:
        assert raw not in shown
    assert cloud.show_fingerprint(full) in "\n".join(said)


def test_the_salt_is_kept_whatever_the_machine_and_the_machines_ids_tell_clones_apart(monkeypatch):
    ids = {"guid": "guid-1", "uuid": "03000200040005000006000700080009", "disk": "0badf00d", "serial": "S1"}
    monkeypatch.setattr(cloud, "_ON_WINDOWS", True)
    monkeypatch.setattr(cloud, "_WINDOWS_READERS", tuple((lambda k=k: ids[k]) for k in ids))
    first = cloud.fingerprint()
    salt = (cloud.root() / cloud.FINGERPRINT_SALT).read_text()
    # A clone of this disk image on a board with the same placeholder UUID, the same MachineGuid and the
    # same volume serial: its own disk's serial still makes it another PC, with the same salt.
    ids["serial"] = "S2"
    clone = cloud.fingerprint()
    assert clone != first and (cloud.root() / cloud.FINGERPRINT_SALT).read_text() == salt
    ids["serial"] = "S1"
    assert cloud.fingerprint() == first   # (the same PC again: the same fingerprint)
    assert not (cloud.root() / "fingerprint-machine").exists()   # (nothing kept about the machine)


def _smbios(*structures: bytes) -> bytes:
    table = b"".join(structures)
    return bytes([0, 3, 4, 0]) + len(table).to_bytes(4, "little") + table


def _structure(kind: int, formatted: bytes, *strings: bytes) -> bytes:
    body = bytes([kind, 4 + len(formatted)]) + b"\x00\x00" + formatted
    return body + (b"".join(s + b"\0" for s in strings) + b"\0" if strings else b"\0\0")


def test_the_smbios_system_uuid_is_found_in_the_firmware_table():
    uuid = bytes(range(16))
    bios = _structure(0, bytes(14), b"Vendor", b"1.0")
    system = _structure(1, bytes(4) + uuid + bytes(5), b"Maker", b"Model")
    end = _structure(127, b"")
    assert cloud.smbios_uuid(_smbios(bios, system, end)) == uuid.hex()
    assert cloud.smbios_uuid(_smbios(system)) == uuid.hex()
    assert cloud.smbios_uuid(_smbios(bios, end)) == ""                          # (no type 1)
    assert cloud.smbios_uuid(_smbios(bios, end, system)) == ""                  # (after the end)
    for unset in (bytes(16), b"\xff" * 16):
        assert cloud.smbios_uuid(_smbios(_structure(1, bytes(4) + unset + bytes(5)))) == ""
    assert cloud.smbios_uuid(_smbios(_structure(1, bytes(4) + uuid[:4]))) == ""   # (type 1 too short)
    whole = _smbios(bios, system)
    for cut in range(len(whole)):
        assert cloud.smbios_uuid(whole[:cut]) == ""   # (shorter than its header says: never an error, nothing read)
    # A type 1 whose strings aren't ended inside the table its header declares: refused.
    formatted = system[:system.index(b"Maker")]
    assert cloud.smbios_uuid(_smbios(formatted)) == ""
    assert cloud.smbios_uuid(_smbios(formatted + b"Maker\0Model\0")) == ""
    assert cloud.smbios_uuid(whole[:8] + whole[8:8 + len(bios) - 1]) == ""
    assert cloud.smbios_uuid(b"") == "" and cloud.smbios_uuid(_smbios(bytes([1, 2]))) == ""


def _descriptor(serial: bytes | None, *, at: int | None = None, size: int | None = None) -> bytes:
    head = bytearray(40)
    tail = (serial + b"\0") if serial is not None else b""
    offset = at if at is not None else (40 if serial is not None else 0)
    total = 40 + len(tail)
    head[4:8] = (size if size is not None else total).to_bytes(4, "little")
    head[24:28] = offset.to_bytes(4, "little")
    return bytes(head) + tail


def test_the_system_disks_own_serial_is_read_from_its_storage_descriptor():
    assert cloud.storage_serial(_descriptor(b"  WD-WX12A3456789 ")) == "WD-WX12A3456789"
    assert cloud.storage_serial(_descriptor(None)) == ""                            # (offset 0: none)
    assert cloud.storage_serial(_descriptor(b"S1", at=4000)) == ""                  # (outside the descriptor)
    assert cloud.storage_serial(_descriptor(b"S1", at=8)) == ""                     # (inside the header)
    assert cloud.storage_serial(_descriptor(b"    ")) == ""                         # (blanks only)
    assert cloud.storage_serial(_descriptor(b"S1")[:-1]) == ""                      # (not ended: cut short)
    assert cloud.storage_serial(_descriptor(b"S1", size=41)) == ""                  # (past the size it says)
    assert cloud.storage_serial(b"") == "" and cloud.storage_serial(bytes(27)) == ""
    assert cloud.storage_serial(_descriptor(b"S1", at=30)) == ""                    # (BusType and on: not a serial)
    assert cloud.storage_serial(_descriptor(b"S1", size=0)) == ""                   # (a size too small to be one)
    extents = (1).to_bytes(4, "little") + bytes(4) + (3).to_bytes(4, "little") + bytes(20)
    assert cloud.volume_disk_number(extents) == 3
    assert cloud.volume_disk_number(bytes(32)) is None and cloud.volume_disk_number(extents[:31]) is None


def test_a_damaged_salt_is_made_again():
    seen = {cloud.fingerprint()}
    for damage in (b"not a salt", b"", b"\xff\xfe\x00 garbage \x80"):
        (cloud.root() / cloud.FINGERPRINT_SALT).write_bytes(damage)
        again = cloud.fingerprint()   # (pairing still works: the site sees a PC it hasn't seen)
        assert again not in seen and cloud.fingerprint() == again
        assert len((cloud.root() / cloud.FINGERPRINT_SALT).read_text()) == 64
        seen.add(again)


def test_an_install_id_thats_there_but_empty_or_not_text_is_refused(monkeypatch):
    monkeypatch.setattr(cloud, "_platform_id", lambda: "")
    cloud.machine_id()
    for damage in (b"", b"  \n", b"\xff\xfe"):
        (cloud.root() / cloud.INSTALL_ID).write_bytes(damage)
        with pytest.raises(cloud.CloudError, match="can't be told"):
            cloud.machine_id()
