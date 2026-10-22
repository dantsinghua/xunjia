"""Production entrypoint acceptance for R01/R03/R06 and design D03/D09."""
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
import pytest


NOW = 1_800_000_000
SSO_SECRET = "acceptance-sso-secret"
SESSION_SECRET = "acceptance-session-secret-at-least-32-bytes"


def sso_body(user="trader-a", *, timestamp=NOW):
    name = "合成交易员-" + user
    ts = str(timestamp)
    signature = hashlib.md5(f"{user}|{name}|{ts}|{SSO_SECRET}".encode()).hexdigest()
    return {"userName": user, "nickName": name, "ts": ts, "sign": signature}


@pytest.fixture
def application(tmp_path):
    from server.app import create_app
    from server.auth import AuthConfig

    return create_app(
        database_url="sqlite:///" + str(tmp_path / "business.db"),
        auth_config=AuthConfig(sso_secret=SSO_SECRET, session_secret=SESSION_SECRET, clock=lambda: NOW),
        enable_background=False,
    )


def login(client, user="trader-a"):
    response = client.post("/api/auth/sso", json=sso_body(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["user_id"] == user
    assert body["display_name"] == "合成交易员-" + user
    return body["token"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/auth/me"),
    ("GET", "/api/im/links"),
    ("GET", "/api/im/rfq/status"),
    ("POST", "/api/im/send"),
    ("POST", "/api/im/rfq/send"),
    ("POST", "/api/bot"),
])
def test_anonymous_business_access_is_rejected(application, method, path):
    with TestClient(application) as client:
        response = client.request(method, path)
    assert response.status_code == 401


@pytest.mark.parametrize("header", ["Authorization", "X-Auth-Token"])
def test_verified_identity_reads_only_its_fixed_account(application, header):
    for index in range(5):
        application.state.repository.bind_account(
            owner_user_id=f"trader-{index}", account_uid=f"91000{index}",
            workbench_id="synthetic-workbench", transport="oss",
        )
    with TestClient(application) as client:
        for index in range(5):
            token = login(client, f"trader-{index}")
            value = "Bearer " + token if header == "Authorization" else token
            response = client.get("/api/auth/me", headers={header: value})
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["user_id"] == f"trader-{index}"
            assert data["account_uid"] == f"91000{index}"
            assert data["binding"]


@pytest.mark.parametrize("alteration", ["wrong-signature", "expired", "future", "forged-name"])
def test_sso_rejects_invalid_proofs(application, alteration):
    body = sso_body(timestamp=NOW - 61 if alteration == "expired" else NOW + 61 if alteration == "future" else NOW)
    if alteration == "wrong-signature":
        body["sign"] = "0" * 32
    if alteration == "forged-name":
        body["userName"] = "other-trader"
    with TestClient(application) as client:
        response = client.post("/api/auth/sso", json=body)
    assert response.status_code == 401
    assert "token" not in response.json()


def test_token_tampering_cannot_impersonate_another_trader(application):
    with TestClient(application) as client:
        token = login(client)
        offset = len(token) // 2
        tampered = token[:offset] + ("A" if token[offset] != "A" else "B") + token[offset + 1:]
        response = client.get("/api/auth/me", headers={"Authorization": "Bearer " + tampered})
    assert response.status_code == 401


def test_unconfigured_auth_has_no_default_login_secret(tmp_path):
    from server.app import create_app

    app = create_app(database_url="sqlite:///" + str(tmp_path / "unconfigured.db"))
    with TestClient(app) as client:
        response = client.post("/api/auth/sso", json=sso_body())
        me = client.get("/api/auth/me", headers={"X-Auth-Token": "dev"})
    assert response.status_code in (401, 503)
    assert "token" not in response.json()
    assert me.status_code == 401


def test_anonymous_health_does_not_expose_credentials(application):
    with TestClient(application) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert SSO_SECRET not in response.text
    assert SESSION_SECRET not in response.text


def test_session_expires_at_the_exact_deadline(tmp_path):
    from server.app import create_app
    from server.auth import AuthConfig

    clock = [NOW]
    app = create_app(database_url="sqlite:///" + str(tmp_path / "expiring.db"),
                     auth_config=AuthConfig(sso_secret=SSO_SECRET, session_secret=SESSION_SECRET,
                                            token_ttl_seconds=30, clock=lambda: clock[0]))
    with TestClient(app) as client:
        token = login(client)
        headers = {"Authorization": "Bearer " + token}
        clock[0] = NOW + 29
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        clock[0] = NOW + 30
        assert client.get("/api/auth/me", headers=headers).status_code == 401


@pytest.mark.parametrize("timestamp", [NOW - 60, NOW + 60])
def test_valid_sso_timestamp_at_skew_boundary_is_accepted(application, timestamp):
    with TestClient(application) as client:
        response = client.post("/api/auth/sso", json=sso_body(timestamp=timestamp))
    assert response.status_code == 200
    assert response.json()["token"]


@pytest.mark.parametrize("payload", [
    {"aud": "ibquote", "v": 1, "sub": "other-user", "name": "Other", "exp": NOW + 100},
    {"aud": "xunjia", "v": 999, "sub": "other-user", "name": "Other", "exp": NOW + 100},
    {"aud": "xunjia", "v": 1, "sub": "other-user", "name": "Other", "exp": str(NOW + 100)},
    [], None,
])
def test_foreign_or_malformed_signed_session_is_a_clean_401(application, payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/api/auth/me", headers={"X-Auth-Token": encoded + "." + signature})
    assert response.status_code == 401


def test_query_user_id_cannot_override_verified_identity(application):
    application.state.repository.bind_account(owner_user_id="trader-a", account_uid="910001", workbench_id="test-wb")
    application.state.repository.bind_account(owner_user_id="trader-b", account_uid="910002", workbench_id="test-wb")
    with TestClient(application) as client:
        token = login(client)
        response = client.get("/api/auth/me?user_id=trader-b&account_uid=910002",
                              headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json()["data"]["account_uid"] == "910001"
    assert "910002" not in response.text
