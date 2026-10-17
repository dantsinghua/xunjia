"""User-approved exact MQ HTTP exception; all credentials and IO are synthetic."""
import base64
from datetime import datetime, timezone
import json
import re
import sys

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from imlink.sts_credentials import DEFAULT_STS_URL, MQ_STS_URL, CredentialsError, STSCredentials


NOW = 1_800_000_000
KEY = b"0123456789abcdef"
MQ = "http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts"


def decrypt_token(token):
    raw = base64.b64decode(token, validate=True)
    decryptor = Cipher(algorithms.SM4(KEY), modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads(unpadder.update(padded) + unpadder.finalize())


def test_exact_mq_endpoint_uses_the_documented_header_and_sm4_contract():
    assert MQ_STS_URL == MQ
    calls = []

    def transport(url, headers, timeout):
        calls.append((url, headers))
        return 200, json.dumps({"access_key_id": "synthetic-id", "access_key_secret": "synthetic-secret", "security_token": "synthetic-security-token",
                               "expiration": datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat(), "bucket": "synthetic-bucket"}).encode()

    provider = STSCredentials(auth_key="合成业务口令", sm4_key=KEY, url=MQ, transport=transport, clock=lambda: NOW)
    assert calls == []
    assert provider.get().bucket == ""
    assert len(calls) == 1 and calls[0][0] == MQ
    assert set(calls[0][1]) == {"access_token"}
    assert decrypt_token(calls[0][1]["access_token"]) == {"key": "合成业务口令", "ts": str(NOW)}


@pytest.mark.parametrize("url", [
    "http://nytoken.yingmi.com/api/sts",
    MQ.replace("yingmi-inc.com", "yingmi-inc.com.evil.invalid"),
    MQ.replace("nginx.mq.dq.", "nginx.mq."),
    MQ.replace("/its-accesspoint/v1/api/sts", "/api/sts"),
    MQ + "/", MQ + "?x=1", MQ + "?", MQ + "#fragment", MQ + "#",
    MQ.replace("http://", "http://user@"),
    MQ.replace(".com/", ".com:80/"),
])
def test_http_exception_does_not_expand_to_similar_urls(url):
    with pytest.raises(CredentialsError):
        STSCredentials(auth_key="synthetic-auth", sm4_key=KEY, url=url)


@pytest.mark.parametrize("url", [MQ, DEFAULT_STS_URL])
def test_request_generator_writes_a_private_file_without_requesting_or_printing_credentials(tmp_path, monkeypatch, capsys, url):
    from imlink import sts_request

    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Request generation must not contact STS")

    monkeypatch.setenv("XUNJIA_STS_AUTH_KEY", "synthetic-private-auth")
    monkeypatch.setenv("XUNJIA_STS_SM4_KEY", KEY.decode())
    monkeypatch.setenv("XUNJIA_STS_URL", url)
    monkeypatch.setattr("imlink.sts_credentials.http_get", forbidden)
    monkeypatch.setattr(sts_request.time, "time", lambda: NOW)
    path = tmp_path / "request.curl"
    monkeypatch.setattr(sys, "argv", ["sts_request", "--output", str(path)])
    sts_request.main()
    assert path.stat().st_mode & 0o777 == 0o600
    content = path.read_text()
    assert json.loads(content.splitlines()[0].split("=", 1)[1]) == url
    token = re.search(r'^header = "access_token: ([^"]+)"$', content, re.MULTILINE)[1]
    assert decrypt_token(token) == {"key": "synthetic-private-auth", "ts": str(NOW)}
    output = capsys.readouterr()
    if any(secret in output.out + output.err for secret in (token, "synthetic-private-auth", KEY.decode())):
        pytest.fail("The request generator exposed a credential on stdout/stderr")
    assert calls == []
    assert 'request = "GET"' in content
    assert not any(line.strip().split("=", 1)[0].strip() in ("location", "insecure") for line in content.splitlines())


def test_request_generator_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    from imlink import sts_request

    monkeypatch.setenv("XUNJIA_STS_AUTH_KEY", "synthetic-auth")
    monkeypatch.setenv("XUNJIA_STS_SM4_KEY", KEY.decode())
    monkeypatch.setenv("XUNJIA_STS_URL", MQ)
    path = tmp_path / "existing.curl"
    path.write_text("original-private-file")
    monkeypatch.setattr(sys, "argv", ["sts_request", "--output", str(path)])
    with pytest.raises(FileExistsError):
        sts_request.main()
    assert path.read_text() == "original-private-file"


def test_missing_secret_never_falls_back_to_static_oss_keys_or_creates_a_request(tmp_path, monkeypatch):
    from imlink import sts_request

    monkeypatch.delenv("XUNJIA_STS_AUTH_KEY", raising=False)
    monkeypatch.setenv("XUNJIA_STS_SM4_KEY", KEY.decode())
    monkeypatch.setenv("XUNJIA_STS_URL", MQ)
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "synthetic-long-lived-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "synthetic-long-lived-secret")
    path = tmp_path / "must-not-exist.curl"
    monkeypatch.setattr(sys, "argv", ["sts_request", "--output", str(path)])
    with pytest.raises(CredentialsError):
        sts_request.main()
    assert not path.exists()
