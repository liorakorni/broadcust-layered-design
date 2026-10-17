"""Credential resolution from Secrets Manager, with an environment fallback.

The two paths exist because a deployed function has a role to read a secret with
and a local test run does not. Both are exercised here, because a change that
silently breaks the local path makes every other test unrunnable.
"""

import json

import pytest
from botocore.exceptions import ClientError

import conf
from lib import media, secrets
from lib.errors import ConfigurationInvalid

ARN = "arn:aws:secretsmanager:us-east-1:816069163425:secret:Cloudinery-ysYifO"
PAYLOAD = {"cloud_name": "broadcust", "api_key": "123456789012345", "api_secret": "s" * 27}


class FakeSecretsManager:
    """Only the behaviours the code depends on, modelled faithfully."""

    def __init__(self, payload=None, error=None, binary=False):
        self.payload = payload
        self.error = error
        self.binary = binary
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        if self.error:
            raise ClientError(
                {"Error": {"Code": self.error, "Message": "x"}}, "GetSecretValue"
            )
        if self.binary:
            return {"SecretBinary": b"\x00"}
        body = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload)
        )
        return {"SecretString": body}


@pytest.fixture(autouse=True)
def clean():
    secrets.reset_cache()
    media.reset()
    yield
    secrets.reset_cache()
    media.reset()


def use(monkeypatch, fake):
    monkeypatch.setattr(secrets, "_secrets", lambda: fake)
    return fake


# --- resolution ------------------------------------------------------------


def test_reads_a_json_secret(monkeypatch):
    use(monkeypatch, FakeSecretsManager(PAYLOAD))
    assert secrets.get_json(ARN) == PAYLOAD


def test_result_is_cached_for_the_container_lifetime(monkeypatch):
    """One API call per cold start, not one per upload."""
    fake = use(monkeypatch, FakeSecretsManager(PAYLOAD))
    for _ in range(5):
        secrets.get_json(ARN)
    assert fake.calls == 1


def test_missing_fields_are_named(monkeypatch):
    """Better than surfacing later as a 401 from the provider."""
    use(monkeypatch, FakeSecretsManager({"cloud_name": "broadcust"}))
    with pytest.raises(ConfigurationInvalid, match="api_key, api_secret"):
        secrets.get_json(ARN, required_keys=("cloud_name", "api_key", "api_secret"))


def test_blank_field_counts_as_missing(monkeypatch):
    use(monkeypatch, FakeSecretsManager({**PAYLOAD, "api_secret": "   "}))
    with pytest.raises(ConfigurationInvalid, match="api_secret"):
        secrets.get_json(ARN, required_keys=("api_secret",))


def test_non_json_secret_rejected(monkeypatch):
    use(monkeypatch, FakeSecretsManager("not json at all"))
    with pytest.raises(ConfigurationInvalid, match="not valid JSON"):
        secrets.get_json(ARN)


def test_json_that_is_not_an_object_rejected(monkeypatch):
    use(monkeypatch, FakeSecretsManager("[1, 2, 3]"))
    with pytest.raises(ConfigurationInvalid, match="key/value pairs"):
        secrets.get_json(ARN)


def test_binary_secret_rejected(monkeypatch):
    use(monkeypatch, FakeSecretsManager(binary=True))
    with pytest.raises(ConfigurationInvalid, match="binary"):
        secrets.get_json(ARN)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ResourceNotFoundException", "does not exist"),
        ("AccessDeniedException", "not permitted"),
        ("ThrottlingException", "Could not read"),
    ],
)
def test_aws_errors_become_actionable_messages(monkeypatch, code, expected):
    use(monkeypatch, FakeSecretsManager(error=code))
    with pytest.raises(ConfigurationInvalid, match=expected):
        secrets.get_json(ARN)


def test_the_secret_value_never_reaches_the_log(monkeypatch, caplog):
    use(monkeypatch, FakeSecretsManager(PAYLOAD))
    with caplog.at_level("INFO"):
        secrets.get_json(ARN)
    logged = caplog.text
    assert PAYLOAD["api_secret"] not in logged
    assert PAYLOAD["api_key"] not in logged
    # The account id lives in the ARN, so only the short name is logged.
    assert "816069163425" not in logged
    assert "Cloudinery" in logged


# --- which path media.py takes ---------------------------------------------


def test_secrets_manager_is_used_when_the_arn_is_set(monkeypatch):
    fake = use(monkeypatch, FakeSecretsManager(PAYLOAD))
    monkeypatch.setattr(conf, "CLOUDINARY_SECRET_ARN", ARN)
    assert media._credentials() == PAYLOAD
    assert fake.calls == 1


def test_environment_is_used_when_no_arn_is_set(monkeypatch):
    """The local path. No AWS call is made at all."""
    fake = use(monkeypatch, FakeSecretsManager(PAYLOAD))
    monkeypatch.setattr(conf, "CLOUDINARY_SECRET_ARN", "")
    monkeypatch.setattr(conf, "CLOUDINARY_CLOUD_NAME", "local-cloud")
    monkeypatch.setattr(conf, "CLOUDINARY_API_KEY", "local-key")
    monkeypatch.setattr(conf, "CLOUDINARY_API_SECRET", "local-secret")

    assert media._credentials()["cloud_name"] == "local-cloud"
    assert fake.calls == 0


def test_neither_source_configured_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(conf, "CLOUDINARY_SECRET_ARN", "")
    for name in conf.MEDIA_REQUIRED:
        monkeypatch.setattr(conf, name, "")
    with pytest.raises(conf.ConfigurationError, match="CLOUDINARY_CLOUD_NAME"):
        media._credentials()


def test_preflight_accepts_either_source(monkeypatch):
    from lib.stages import preflight

    monkeypatch.setattr(conf, "CLOUDINARY_SECRET_ARN", ARN)
    for name in conf.MEDIA_REQUIRED:
        monkeypatch.setattr(conf, name, "")
    assert preflight._check_configuration() == []


def test_preflight_rejects_when_neither_is_configured(monkeypatch):
    from lib.stages import preflight

    monkeypatch.setattr(conf, "CLOUDINARY_SECRET_ARN", "")
    for name in conf.MEDIA_REQUIRED:
        monkeypatch.setattr(conf, name, "")
    problems = preflight._check_configuration()
    assert any("stable media publishing" in p for p in problems)
