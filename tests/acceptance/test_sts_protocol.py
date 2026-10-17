"""Independent STS/OSS protocol acceptance; synthetic credentials only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import threading

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
import pytest

from imlink.oss_http import OSSHTTP
from imlink.sts_credentials import CredentialSet, CredentialsError, STSCredentials, build_access_token, credentials_from_env


NOW = 1_800_000_000
KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
IV = bytes(range(16))


def response(*, expires=NOW + 3600, generation="one", **overrides):
    data = {
        "access_key_id": "synthetic-id-" + generation,
        "access_key_secret": "synthetic-secret-" + generation,
        "security_token": "synthetic-token-" + generation,
        "expiration": datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z"),
        "bucket": "synthetic-bucket",
    }
    data.update(overrides)
    return 200, json.dumps(data).encode()


def provider(transport, **kwargs):
    return STSCredentials(auth_key="业务口令", sm4_key=KEY, transport=transport,
                          clock=kwargs.pop("clock", lambda: NOW), **kwargs)


def decrypted(token):
    data = base64.b64decode(token, validate=True)
    decryptor = Cipher(algorithms.SM4(KEY), modes.CBC(data[:16])).decryptor()
    padded = decryptor.update(data[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize(), padded


def test_chinese_password_matches_independent_openssl_vector():
    # Generated independently using openssl enc -sm4-cbc, with fixed key/IV.
    expected = "AAECAwQFBgcICQoLDA0OD+DNqCZs7qPfrCuj/7IPuJdCXNNXp/CuGN0InKmbWhbGFVj74AP1MPV5LZ2P1wY+oQ=="
    assert build_access_token("业务口令", KEY, NOW, IV) == expected


def test_full_block_plaintext_gets_full_pkcs7_padding():
    token = build_access_token("abcd", KEY, NOW, IV)
    plain, padded = decrypted(token)
    assert len(plain) == 32
    assert padded[-16:] == b"\x10" * 16
    assert json.loads(plain) == {"key": "abcd", "ts": str(NOW)}
    assert token == base64.b64encode(base64.b64decode(token)).decode()


def test_each_auth_request_has_a_new_iv_and_string_seconds():
    tokens = [build_access_token("业务口令", KEY, NOW) for _ in range(20)]
    assert len({base64.b64decode(token)[:16] for token in tokens}) == 20
    for token in tokens:
        assert json.loads(decrypted(token)[0]) == {"key": "业务口令", "ts": "1800000000"}


@pytest.mark.parametrize("key", [b"", b"x" * 15, b"x" * 17, KEY.hex().encode()])
def test_invalid_key_sizes_are_rejected_without_echo(key):
    with pytest.raises(ValueError) as error:
        build_access_token("private-auth-password", key, NOW)
    assert "private-auth-password" not in str(error.value)


def test_utc_offset_expiration_and_request_budget_are_enforced():
    clock = [NOW]
    expiry = datetime.fromtimestamp(NOW + 100, timezone(timedelta(hours=8))).isoformat()
    client = provider(lambda *_: response(expiration=expiry), clock=lambda: clock[0])
    old = client.get()
    assert old.expiration.timestamp() == NOW + 100
    clock[0] = NOW + 99
    with pytest.raises(CredentialsError):
        client.get()


def test_five_accounts_share_a_single_inflight_refresh():
    ready = threading.Barrier(5)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def transport(url, headers, timeout):
        calls.append((url, headers, timeout))
        entered.set()
        assert release.wait(5)
        return response()

    client = provider(transport)

    def read():
        ready.wait(timeout=5)
        return client.get()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(read) for _ in range(5)]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(calls) == 1
    assert set(calls[0][1]) == {"access_token"}
    assert all(result == results[0] for result in results)


def test_invalid_refresh_never_partially_replaces_cached_credentials():
    clock = [NOW]
    replies = iter([response(expires=NOW + 600), response(generation="two", security_token="")])
    client = provider(lambda *_: next(replies), clock=lambda: clock[0])
    old = client.get()
    clock[0] += 301
    current = client.get()
    assert (current.access_id, current.access_secret, current.security_token, current.bucket) == (
        old.access_id, old.access_secret, old.security_token, old.bucket)


def test_failed_refresh_uses_valid_old_snapshot_but_never_expired_one():
    clock = [NOW]
    calls = []

    def transport(*_):
        calls.append(1)
        return response(expires=NOW + 600) if len(calls) == 1 else (502, b"private-auth-password")

    client = provider(transport, clock=lambda: clock[0])
    old = client.get()
    clock[0] += 301
    for _ in range(5):
        assert client.get() == old
    assert len(calls) == 2
    clock[0] = NOW + 601
    with pytest.raises(CredentialsError) as error:
        client.get()
    assert "private-auth-password" not in str(error.value)


@pytest.mark.parametrize("overrides", [
    {"access_key_id": ""}, {"access_key_secret": ""}, {"security_token": ""},
    {"expiration": "2027-01-15T08:00:00"},
    {"expiration": "not-a-time"}, {"security_token": "token\r\nInjected: header"},
])
def test_invalid_response_cannot_become_a_usable_snapshot(overrides):
    client = provider(lambda *_: response(**overrides), expected_bucket="synthetic-bucket")
    with pytest.raises(CredentialsError):
        client.get()


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_security_token_is_in_request_and_signed_canonical_headers(method):
    client = provider(lambda *_: response())
    captured = []
    oss = OSSHTTP(endpoint="https://oss.example.invalid", bucket="synthetic-bucket", credentials=client,
                  transport=lambda *args: captured.append(args) or (200, b"ok"))
    body = b"immutable-sealed-command" if method == "PUT" else None
    assert oss.request(method, "wb/synthetic/in/xunjia/cmd-fixed.bin", body) == (200, b"ok")
    request_method, url, headers, sent, _ = captured[0]
    assert request_method == method
    assert "synthetic-bucket" in url
    assert headers["x-oss-security-token"] == "synthetic-token-one"
    content_type = "application/octet-stream" if method == "PUT" else ""
    canonical = f"{method}\n\n{content_type}\n{headers['Date']}\nx-oss-security-token:synthetic-token-one\n/synthetic-bucket/wb/synthetic/in/xunjia/cmd-fixed.bin"
    signature = base64.b64encode(hmac.new(b"synthetic-secret-one", canonical.encode(), hashlib.sha1).digest()).decode()
    assert headers["Authorization"] == "OSS synthetic-id-one:" + signature
    assert sent == body


@pytest.mark.parametrize("code,expected_calls", [("SecurityTokenExpired", 2), ("AccessDenied", 1), ("SignatureDoesNotMatch", 1)])
def test_only_expired_token_retries_and_preserves_command_identity(code, expected_calls):
    sts_calls, requests = [], []

    def fetch(*_):
        sts_calls.append(1)
        return response(generation=str(len(sts_calls)))

    def transport(*args):
        requests.append(args)
        return 403, f"<Error><Code>{code}</Code></Error>".encode()

    oss = OSSHTTP(endpoint="https://oss.example.invalid", bucket="synthetic-bucket", credentials=provider(fetch), transport=transport)
    status, _ = oss.request("PUT", "wb/synthetic/in/xunjia/cmd-fixed.bin", b"same-sealed-body")
    assert status == 403
    assert len(requests) == len(sts_calls) == expected_calls
    assert all(request[1] == requests[0][1] and request[3] == b"same-sealed-body" for request in requests)


def test_long_lived_keys_cannot_substitute_for_missing_sts(monkeypatch):
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "long-lived-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "long-lived-secret")
    monkeypatch.delenv("XUNJIA_STS_AUTH_KEY", raising=False)
    monkeypatch.delenv("XUNJIA_STS_SM4_KEY", raising=False)
    sent = []
    with pytest.raises(CredentialsError):
        OSSHTTP(endpoint="https://oss.example.invalid", bucket="synthetic-bucket", transport=lambda *args: sent.append(args)).request("GET", "catalog.json")
    assert sent == []


def test_expired_explicit_snapshot_cannot_send_an_oss_request():
    snapshot = CredentialSet("expired-id", "expired-secret", "expired-token",
                             datetime(2000, 1, 1, tzinfo=timezone.utc), "synthetic-bucket")
    sent = []
    with pytest.raises(CredentialsError):
        OSSHTTP(endpoint="https://oss.example.invalid", bucket="synthetic-bucket", credentials=snapshot,
                transport=lambda *args: sent.append(args) or (200, b"ok")).request("PUT", "command.bin", b"sealed")
    assert sent == []


def test_sm4_environment_does_not_guess_hex_encoding(monkeypatch):
    monkeypatch.setenv("XUNJIA_STS_AUTH_KEY", "synthetic-password")
    monkeypatch.setenv("XUNJIA_STS_SM4_KEY", KEY.hex())
    with pytest.raises(CredentialsError):
        credentials_from_env()


def test_sts_errors_and_snapshot_repr_do_not_expose_secrets(caplog):
    current = provider(lambda *_: response()).get()
    rendered = repr(current)
    for secret in (current.access_id, current.access_secret, current.security_token):
        assert secret not in rendered
    client = provider(lambda *_: (401, b"synthetic-private-server-diagnostic"))
    with pytest.raises(CredentialsError) as error:
        client.get()
    assert "synthetic-private-server-diagnostic" not in str(error.value) + caplog.text
