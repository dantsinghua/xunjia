"""Independent dev SSO acceptance; synthetic identities, production V2 entrypoint."""
import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


NOW = 1_800_000_000
SECRET = "synthetic-independent-portal-secret"
SESSION = "synthetic-xunjia-session-key-over-32-bytes"


def proof(timestamp=NOW):
    body = {"userName": "qa-dev-a", "nickName": "独立验收甲", "ts": str(timestamp)}
    body["sign"] = hashlib.md5(f"qa-dev-a|独立验收甲|{timestamp}|{SECRET}".encode()).hexdigest()
    return body


def application(tmp_path, **overrides):
    from server.app import create_app
    from server.auth import AuthConfig

    config = dict(sso_secret=SECRET, session_secret=SESSION, clock=lambda: NOW)
    config.update(overrides)
    return create_app(database_url="sqlite:///" + str(tmp_path / "dev-sso.db"),
                      auth_config=AuthConfig(**config), enable_background=False)


def assert_public_safe(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert SECRET not in response.text
    assert SESSION not in response.text

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "password_login":
                    assert type(child) is bool
                else:
                    assert not any(part in key.lower() for part in ("secret", "token", "password", "signing_key")), key
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)
    return payload


def test_auth_config_defaults_are_closed():
    from server.auth import AuthConfig
    config = AuthConfig()
    assert config.dev_login is False
    assert config.dev_user_name == ""
    assert config.dev_nick_name == ""


@pytest.mark.parametrize("value,enabled", [(None, False), ("0", False), ("false", False), ("1", True), ("true", True)])
def test_dev_login_requires_explicit_environment_flag(monkeypatch, value, enabled):
    from server.auth import AuthConfig
    monkeypatch.delenv("XUNJIA_AUTH_DEV_LOGIN", raising=False)
    if value is not None:
        monkeypatch.setenv("XUNJIA_AUTH_DEV_LOGIN", value)
    assert AuthConfig.from_env().dev_login is enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_public_config_never_discloses_signing_material(tmp_path, enabled):
    app = application(tmp_path, dev_login=enabled, dev_user_name="qa-dev-a", dev_nick_name="独立验收甲")
    with TestClient(app) as client:
        for headers in ({}, {"Authorization": "Bearer synthetic-invalid-session"}):
            data = assert_public_safe(client.get("/api/auth/sso-config", headers=headers))
            assert data["dev_login"] is enabled
        assert client.get("/api/auth/me").status_code == 401


def test_config_without_keys_does_not_enable_a_working_dev_login(tmp_path):
    app = application(tmp_path, dev_login=True, sso_secret="", session_secret="")
    with TestClient(app) as client:
        data = assert_public_safe(client.get("/api/auth/sso-config"))
        assert data.get("sso_configured") is False
        response = client.post("/api/auth/sso", json=proof())
        assert response.status_code in (401, 503)
        assert "token" not in response.json()


@pytest.mark.parametrize("identity", ["canonical", "snake", "legacy", "legacy-snake"])
@pytest.mark.parametrize("time_key", ["ts", "timestamp"])
@pytest.mark.parametrize("signature_key", ["sign", "md5", "sig"])
def test_portal_aliases_sign_in_and_read_only_fixed_binding(tmp_path, identity, time_key, signature_key):
    app = application(tmp_path)  # Portal SSO remains available when dev form is disabled.
    app.state.repository.bind_account("qa-dev-a", "910001", "synthetic-wb")
    app.state.repository.bind_account("qa-dev-b", "910002", "synthetic-wb")
    names = {
        "canonical": {"userName": "qa-dev-a", "nickName": "独立验收甲"},
        "snake": {"user_name": "qa-dev-a", "nick_name": "独立验收甲"},
        "legacy": {"userId": "qa-dev-a", "userName": "独立验收甲"},
        "legacy-snake": {"user_id": "qa-dev-a", "user_name": "独立验收甲"},
    }
    body = {**names[identity], time_key: str(NOW), signature_key: proof()["sign"]}
    with TestClient(app) as client:
        response = client.post("/api/auth/sso", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        token = response.json()["token"]
        assert SECRET not in response.text and SESSION not in response.text
        me = client.get("/api/auth/me?user_id=qa-dev-b&account_uid=910002", headers={"Authorization": "Bearer " + token})
        assert me.status_code == 200, me.text
        data = me.json()["data"]
        assert data["user_id"] == "qa-dev-a"
        assert data["display_name"] == "独立验收甲"
        assert data["account_uid"] == "910001"
        assert data["binding"]["account_uid"] == "910001"
        assert "910002" not in me.text


@pytest.mark.parametrize("change", ["bad-sign", "expired", "future", "forged-user", "forged-name", "missing-sign", "bad-timestamp"])
def test_bad_portal_proof_never_issues_session(tmp_path, change):
    body = proof(NOW - 61 if change == "expired" else NOW + 61 if change == "future" else NOW)
    if change == "bad-sign":
        body["sign"] = "0" * 32
    elif change == "forged-user":
        body["userName"] = "qa-dev-b"
    elif change == "forged-name":
        body["nickName"] = "伪造展示名"
    elif change == "missing-sign":
        body.pop("sign")
    elif change == "bad-timestamp":
        body["ts"] = "NaN"
    with TestClient(application(tmp_path, dev_login=True)) as client:
        response = client.post("/api/auth/sso", json=body)
        assert response.status_code == 401, response.text
        assert "token" not in response.json()
        assert SECRET not in response.text


def test_session_has_xunjia_audience_and_rejects_ibquote_credentials(tmp_path):
    with TestClient(application(tmp_path)) as client:
        token = client.post("/api/auth/sso", json=proof()).json()["token"]
        encoded, signature = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        assert payload["aud"] == "xunjia"
        assert hmac.compare_digest(signature, hmac.new(SESSION.encode(), encoded.encode(), hashlib.sha256).hexdigest())
        # Even a foreign audience signed with the local key must be refused.
        payload["aud"] = "ibquote"
        foreign = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        foreign += "." + hmac.new(SESSION.encode(), foreign.encode(), hashlib.sha256).hexdigest()
        # ibquote's actual v2 token: base64(v2|b64uid|b64name|exp|HMAC).
        uid = base64.urlsafe_b64encode(b"qa-dev-a").decode().rstrip("=")
        name = base64.urlsafe_b64encode("独立验收甲".encode()).decode().rstrip("=")
        ibquote_payload = f"v2|{uid}|{name}|{NOW + 300}"
        ibquote = base64.urlsafe_b64encode((ibquote_payload + "|" + hmac.new(SESSION.encode(), ibquote_payload.encode(), hashlib.sha256).hexdigest()).encode()).decode()
        # Also refuse the old base64(username|expiry|HMAC) representation.
        legacy_body = f"qa-dev-a|{NOW + 300}"
        legacy = base64.b64encode((legacy_body + "|" + hmac.new(SESSION.encode(), legacy_body.encode(), hashlib.sha256).hexdigest()).encode()).decode()
        for rejected in (foreign, ibquote, legacy, token[:-1] + ("0" if token[-1] != "0" else "1")):
            assert client.get("/api/auth/me", headers={"X-Auth-Token": rejected}).status_code == 401
