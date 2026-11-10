"""The seams a hosted service would plug into: where AWS clients come from and who owns each
setting. Self-hosted, each must behave exactly as before."""
import sys
import types

from commentator import backend, settings_owner
from commentator.config import Config


def test_aws_clients_come_from_boto3_unless_another_source_is_plugged_in(monkeypatch):
    made = []
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: made.append((a, k))))
    backend.s3()
    backend.s3("eu-west-1")
    backend.dynamodb("us-east-1")
    assert made == [(("s3",), {}), (("s3",), {"region_name": "eu-west-1"}),
                    (("dynamodb",), {"region_name": "us-east-1"})]
    try:
        backend.use(lambda service, region: f"{service}@{region}")
        assert backend.s3("us-east-2") == "s3@us-east-2"
    finally:
        backend.use(None)
    backend.s3()
    assert len(made) == 4


def test_every_setting_has_an_owner():
    assert set(settings_owner.SECTIONS) == set(Config.model_fields)
    for path in settings_owner.FIELDS:
        section, name = path.split(".", 1)
        assert name in Config.model_fields[section].annotation.model_fields, path


def test_splitting_the_config_by_owner_loses_nothing():
    data = Config().model_dump()
    data["from_a_newer_app"] = {"x": 1}
    parts = settings_owner.split(data)
    assert parts["service"]["tts"] == {k: data["tts"][k] for k in
                                       ("provider", "openai_model")}
    assert parts["web"]["tts"]["openai_voices"] == data["tts"]["openai_voices"]
    assert parts["pc"]["from_a_newer_app"] == {"x": 1}
    assert "screen" in parts["pc"] and "commentary" in parts["web"] and "llm" in parts["service"]
    assert settings_owner.merge(*parts.values()) == data
    assert Config.model_validate(settings_owner.merge(*parts.values())) == Config()
