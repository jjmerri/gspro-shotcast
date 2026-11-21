import gzip
import hashlib
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
        self.tenant, self.sim, self.device = "t1", "Bay 1", "d1"
        self.default: str | None = "lg1"
        self.rounds: dict[str, dict[int, bytes]] = {}          # round -> revision -> body
        self.puts: list[tuple[str, int, dict]] = []
        self.answers: list = []                                # scripted answers for the next PUTs
        self.minimum: str | None = None                        # clientversion's: below it, every call is 426

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
        if self.minimum:
            return httpx.Response(426, headers={"X-Shotcast-Min-Version": self.minimum},
                                  json={"detail": {"message": f"update Shotcast to {self.minimum}"}})
        if path.startswith("/api/rounds/") and req.method == "PUT":
            return self.put(req)
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
            if self.revoked or old not in self.refresh:
                return httpx.Response(401, json={"detail": "refresh token not valid"})
            if self.refresh[old] is None:
                new = f"r{len(self.refresh)}"
                self.refresh[old], self.refresh[new] = new, None
            return httpx.Response(200, json=self.tokens(self.refresh[old]))
        if req.headers.get("X-Shotcast-Device") not in self.access or self.revoked:
            return httpx.Response(401, json={"detail": "device access token not valid"})
        if path == "/api/devices/me":
            if req.method == "DELETE":
                self.revoked = True
                return httpx.Response(204)
            return httpx.Response(200, json={"device_id": self.device, "league_id": self.default,
                                             "tenant_id": self.tenant, "sim_name": self.sim, "name": "PC",
                                             "league_active": True})
        return httpx.Response(404, json={"detail": "Not Found"})

    def put(self, req):
        assert req.headers["content-encoding"] == "gzip"
        if req.headers.get("X-Shotcast-Device") not in self.access or self.revoked:
            return httpx.Response(401)
        parts = req.url.path.split("/")
        rid, n = parts[3], int(parts[5])
        self.puts.append((rid, n, json.loads(gzip.decompress(req.content))))
        if self.answers:
            return self.answers.pop(0)
        # as koi's put_revision: by the sha256 of the stored bytes
        revs = self.rounds.setdefault(rid, {})
        top = max(revs, default=0)
        if n > top:
            revs[n] = req.content
            return httpx.Response(201, json={"status": "stored", "revision": n, "seq": 1})
        if n not in revs:
            return httpx.Response(200, json={"status": "superseded", "revision": top})
        if revs[n] == req.content:
            return httpx.Response(200, json={"status": "duplicate", "revision": top})
        return httpx.Response(409, json={"error": "revision_conflict", "revision": top,
                                         "sha256": hashlib.sha256(revs[top]).hexdigest()})


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
    cfg.live.enabled, cfg.live.local_keep_days, cfg.live.local_dir = True, 9, "my-recordings"
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
    assert "(sim 'Bay 1')" in capsys.readouterr().out
    assert (prof.home / cloud.FOLDER_FILE).is_file() and prof.db.is_file()   # made at pair time
    cloud.main(["status"])
    out = capsys.readouterr().out
    assert "sim 'Bay 1'" in out and "0 held as conflicts" in out and str(prof.home) in out
    cloud.main(["unpair"])
    assert "Restart Shotcast to apply" in capsys.readouterr().out
    assert cloud.locate() is None and api.revoked and prof.home.is_dir()   # (its data kept)
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
    assert "didn't answer" in out and "1 settling" in out
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


def test_a_stale_certificate_file_or_proxy_in_the_environment_doesnt_reach_the_api_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "gone.pem"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    made = []

    def client(*a, **kw):
        made.append(kw)
        return _CLIENT(*a, **kw)
    monkeypatch.setattr(httpx, "Client", client)
    http = cloud._http(API)
    assert made[0]["trust_env"] is False and http.trust_env is False and not made[0].get("follow_redirects")


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
