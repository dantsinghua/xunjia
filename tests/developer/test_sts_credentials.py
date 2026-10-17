"""STS 传输全部替身；断言协议及过期行为，不访问任何服务。"""
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b'0123456789abcdef'
NOW = 1791849600


def response(expiration=NOW + 3600, **changes):
    data = dict(access_key_id='temporary-id', access_key_secret='temporary-secret',
                security_token='session-token', bucket='temporary-bucket',
                expiration=datetime.fromtimestamp(expiration, timezone.utc).isoformat())
    data.update(changes)
    return 200, json.dumps(data).encode()


def provider(transport, clock=lambda: NOW, **kw):
    from imlink.sts_credentials import STSCredentials
    return STSCredentials(auth_key='业务口令', sm4_key=KEY, transport=transport, clock=clock, **kw)


def test_access_token_is_sm4_cbc_utf8_string_seconds_and_standard_base64():
    from imlink.sts_credentials import build_access_token
    iv = bytes(range(16))
    token = build_access_token('业务口令', KEY, NOW, iv)
    raw = base64.b64decode(token, validate=True)
    assert raw[:16] == iv
    decryptor = Cipher(algorithms.SM4(KEY), modes.CBC(iv)).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    assert json.loads(unpadder.update(padded) + unpadder.finalize()) == {'key': '业务口令', 'ts': str(NOW)}
    assert token == base64.b64encode(raw).decode()
    assert build_access_token('业务口令', KEY, NOW) != build_access_token('业务口令', KEY, NOW)


@pytest.mark.parametrize('key,iv', [(b'short', b'a'*16), (KEY, b'short')])
def test_rejects_invalid_sm4_key_or_iv_without_echo(key, iv):
    from imlink.sts_credentials import build_access_token
    with pytest.raises(ValueError) as err:
        build_access_token('private-password', key, NOW, iv)
    assert 'private-password' not in str(err.value)
    assert 'short' not in str(err.value)


def test_refresh_window_old_snapshot_and_backoff_are_atomic():
    clock = [NOW]
    calls = []
    def transport(url, headers, timeout):
        calls.append(headers)
        if len(calls) > 1:
            return 502, b'private-password'
        return response(NOW + 600)
    client = provider(transport, clock=lambda: clock[0])
    old = client.get()
    clock[0] += 301
    assert client.get() is old
    assert client.get() is old
    assert len(calls) == 2
    assert set(calls[0]) == {'access_token'}
    clock[0] = NOW + 590
    from imlink.sts_credentials import CredentialsError
    with pytest.raises(CredentialsError) as err:
        client.get()
    assert 'private-password' not in str(err.value)
    assert old.access_id == 'temporary-id'


def test_five_concurrent_readers_share_one_sts_request():
    barrier = threading.Barrier(5)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def transport(*args):
        calls.append(args)
        entered.set()
        assert release.wait(3)
        return response()
    client = provider(transport)
    def get():
        barrier.wait(timeout=3)
        return client.get()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(get) for _ in range(5)]
        assert entered.wait(3)
        release.set()
        snapshots = [future.result(timeout=3) for future in futures]
    assert len(calls) == 1
    assert all(snapshot is snapshots[0] for snapshot in snapshots)


@pytest.mark.parametrize('changes', [dict(security_token=''),
                                     dict(expiration='2026-10-13T00:00:00'),
                                     dict(expiration='invalid'), dict(access_key_secret=None)])
def test_incomplete_or_wrong_bucket_response_is_rejected(changes):
    from imlink.sts_credentials import CredentialsError
    client = provider(lambda *args: response(**changes), expected_bucket='temporary-bucket')
    with pytest.raises(CredentialsError):
        client.get()


def test_unchanged_expiration_does_not_create_refresh_storm():
    calls = []
    def transport(*args):
        calls.append(args)
        return response(NOW + 200)
    client = provider(transport)
    for _ in range(5):
        client.get()
    assert len(calls) == 1


def test_production_config_rejects_legacy_keys(monkeypatch):
    from imlink.sts_credentials import CredentialsError, credentials_from_env
    monkeypatch.delenv('XUNJIA_STS_AUTH_KEY', raising=False)
    monkeypatch.delenv('XUNJIA_STS_SM4_KEY', raising=False)
    monkeypatch.setenv('OSS_ACCESS_KEY_ID', 'legacy-id')
    monkeypatch.setenv('OSS_ACCESS_KEY_SECRET', 'legacy-secret')
    with pytest.raises(CredentialsError):
        credentials_from_env()


def test_oss_get_put_sign_token_and_refresh_only_expired_token(monkeypatch):
    from imlink.oss_http import OSSHTTP
    calls = []
    sts_calls = []
    def sts(*args):
        sts_calls.append(args)
        return response(security_token=f'token-{len(sts_calls)}')
    client = provider(sts)
    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return 403, b'<Error><Code>SecurityTokenExpired</Code></Error>'
        return 200, b'object'
    oss = OSSHTTP(endpoint='https://oss.invalid', bucket='configured-bucket', credentials=client, transport=transport)
    assert oss.request('PUT', 'wb/test/in/xunjia/cmd.bin', b'fixed-body')[0] == 200
    assert len(sts_calls) == 2
    assert [call[2]['x-oss-security-token'] for call in calls] == ['token-1', 'token-2']
    assert calls[0][2]['Authorization'] != calls[1][2]['Authorization']
    assert calls[0][1] == calls[1][1] == 'https://configured-bucket.oss.invalid/wb/test/in/xunjia/cmd.bin'
    assert calls[0][3] == calls[1][3] == b'fixed-body'
    oss.request('GET', 'wb/test/index.json')
    assert calls[-1][2]['x-oss-security-token'] == 'token-2'
    denied = OSSHTTP(endpoint='https://oss.invalid', bucket='configured-bucket', credentials=client,
                     transport=lambda *args: (403, b'<Error><Code>AccessDenied</Code></Error>'))
    assert denied.request('GET', 'index.json')[0] == 403
    assert len(sts_calls) == 2


def test_catalog_and_live_uplink_use_same_sts_snapshot_without_legacy_env(monkeypatch):
    from imlink.crypto import seal
    from imlink.oss_catalog import load_live_catalog
    from imlink.oss_send import LiveUplink
    monkeypatch.setenv('OSSBOX_KEY_XUNJIA', KEY.hex())
    monkeypatch.setenv('OSS_WORKBENCH_ID', 'unit-test')
    monkeypatch.setenv('XUNJIA_OSS_ENDPOINT', 'https://objects.invalid')
    monkeypatch.delenv('OSS_ACCESS_KEY_ID', raising=False)
    monkeypatch.delenv('OSS_ACCESS_KEY_SECRET', raising=False)
    credentials = provider(lambda *args: response())
    plain = {'workbench_id': 'unit-test', 'accounts': [{'channel': 'qidian', 'account_uid': '123'}]}
    envelope = json.dumps({'v': 1, 'seals': {'xunjia': base64.b64encode(seal(KEY, json.dumps(plain).encode())).decode()}}).encode()
    seen = []
    def get(url, headers, timeout):
        seen.append((url, headers))
        return 200, envelope
    def put(url, headers, body, timeout):
        seen.append((url, headers))
        assert body == b'old-aes-body'
        return 200, b''
    rows = load_live_catalog(transport=get, credentials=credentials)
    assert rows[0]['accounts'][0]['account_uid'] == '123'
    uplink = LiveUplink(credentials=credentials, get_transport=get, put_transport=put)
    uplink.put('wb/unit-test/in/xunjia/cmd.bin', b'old-aes-body')
    assert uplink.get('wb/unit-test/ack/cmd.bin') == envelope
    assert len(seen) == 3
    assert all(url.startswith('https://qtrade-nyoss-lab.objects.invalid/') for url, _ in seen)
    assert all(headers['x-oss-security-token'] == 'session-token' for _, headers in seen)


def test_production_uplink_fails_closed_before_network_when_only_legacy_config(monkeypatch):
    from imlink.oss_send import LiveUplink, OssSendError
    monkeypatch.delenv('XUNJIA_STS_AUTH_KEY', raising=False)
    monkeypatch.delenv('XUNJIA_STS_SM4_KEY', raising=False)
    monkeypatch.setenv('OSS_ACCESS_KEY_ID', 'old-ak')
    monkeypatch.setenv('OSS_ACCESS_KEY_SECRET', 'old-sk')
    with pytest.raises(OssSendError):
        LiveUplink().put('wb/unit-test/in/xunjia/cmd.bin', b'body')


def test_forced_refresh_failure_obeys_backoff():
    calls = []
    def transport(*args):
        calls.append(args)
        if len(calls) == 1:
            return response()
        return 502, b''
    client = provider(transport)
    snapshot = client.get()
    for _ in range(5):
        assert client.get(force=True) is snapshot
    assert len(calls) == 2


def test_expired_explicit_snapshot_cannot_make_an_oss_request():
    from imlink.oss_http import OSSHTTP
    from imlink.sts_credentials import CredentialSet, CredentialsError
    expired = CredentialSet('id', 'secret', 'token', datetime(2000, 1, 1, tzinfo=timezone.utc), 'unit-bucket')
    calls = []
    client = OSSHTTP(endpoint='https://objects.invalid', bucket='configured-bucket', credentials=expired,
                     transport=lambda *args: calls.append(args) or (200, b''))
    with pytest.raises(CredentialsError):
        client.request('GET', 'object')
    assert calls == []


def test_oss_request_refuses_cleartext_endpoint():
    from imlink.oss_http import OSSHTTP
    from imlink.sts_credentials import CredentialsError
    calls = []
    client = OSSHTTP(endpoint='http://objects.invalid', bucket='configured-bucket', credentials=provider(lambda *args: response()),
                     transport=lambda *args: calls.append(args) or (200, b''))
    with pytest.raises(CredentialsError):
        client.request('GET', 'object')
    assert calls == []


def test_put_error_logs_only_safe_status_and_known_code(caplog):
    from imlink.oss_send import LiveUplink, OssSendError
    client = LiveUplink(credentials=provider(lambda *args: response()), put_transport=lambda *args:
                        (403, b'<Error><Code>AccessDenied</Code><Message>private-key-value</Message></Error>'))
    with pytest.raises(OssSendError):
        client.put('object', b'sealed-body')
    assert 'AccessDenied' in caplog.text
    assert 'private-key-value' not in caplog.text


def test_larger_request_budget_refreshes_before_normal_refresh_window():
    clock = [NOW]
    replies = iter([response(NOW + 600), response(NOW + 3600)])
    client = provider(lambda *args: next(replies), clock=lambda: clock[0])
    old = client.get()
    clock[0] += 10
    current = client.get(min_validity=650)
    assert current.expiration > old.expiration
