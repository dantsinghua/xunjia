"""Safe, read-only STS probe. No keys or returned AK/SK/token are logged or stored.

Run directly in a trusted local interactive terminal. Never use --allow-insecure-dev-http
in production: an HTTP STS response exposes temporary OSS credentials on the wire.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from imlink.sts_credentials import (CredentialsError, DEFAULT_STS_URL,
                                    MQ_STS_URL, STSCredentials, build_access_token)


def self_test() -> None:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    auth, sm4 = 'synthetic-auth-for-local-testing', b'0123456789abcdef'
    ts = int(time.time())
    encoded = build_access_token(auth, sm4, ts)
    payload = base64.b64decode(encoded, validate=True)
    decipher = Cipher(algorithms.SM4(sm4), modes.CBC(payload[:16])).decryptor()
    padded = decipher.update(payload[16:]) + decipher.finalize()
    pad = padding.PKCS7(128).unpadder()
    assert json.loads(pad.update(padded) + pad.finalize()) == {'key':auth, 'ts':str(ts)}

    def fake(_url, _headers, _timeout):
        return 200, json.dumps({
            'access_key_id':'synthetic', 'access_key_secret':'synthetic',
            'security_token':'synthetic', 'bucket':'synthetic-bucket',
            'expiration':datetime.fromtimestamp(ts+3600,timezone.utc).isoformat()
        }).encode()
    creds=STSCredentials(auth_key=auth,sm4_key=sm4,transport=fake,
                         expected_bucket='synthetic-bucket').get()
    assert creds.bucket=='synthetic-bucket'
    print('Local SM4 / STS synthetic contract: PASS; no network request')


def main() -> int:
    p=argparse.ArgumentParser(description='Read-only STS credential contract test, no OSS access')
    p.add_argument('--url',default=DEFAULT_STS_URL)
    p.add_argument('--expected-bucket',default=None)
    p.add_argument('--allow-insecure-dev-http',action='store_true',
                   help='EXPLICIT RISK ACCEPTANCE: development MQ HTTP may expose returned temporary credentials')
    p.add_argument('--self-test',action='store_true',help='Synthetic credentials only, never calls a service')
    args=p.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.url == MQ_STS_URL:
        if not args.allow_insecure_dev_http:
            print('BLOCKED: MQ HTTP has no TLS; pass --allow-insecure-dev-http for a controlled development-only test.',file=sys.stderr)
            return 2
        print('WARNING: MQ HTTP has no server authentication or response confidentiality. Development test ONLY.')
    elif not args.url.startswith('https://'):
        print('BLOCKED: only verified HTTPS or the exact MQ development endpoint is allowed.',file=sys.stderr)
        return 2

    # Both prompts use getpass; do not include values in command arguments,
    # shell history, process environment, URLs, tracebacks or saved artifacts.
    auth=getpass.getpass('STS auth key (hidden): ')
    sm4=getpass.getpass('SM4 original 16-byte key (hidden): ').encode('utf-8')
    if len(sm4)!=16:
        print('FAIL: SM4 key must be exactly 16 UTF-8 bytes.',file=sys.stderr)
        return 2
    try:
        provider=STSCredentials(auth_key=auth,sm4_key=sm4,url=args.url,
                                expected_bucket=args.expected_bucket,timeout=12)
        result=provider.get(force=True)
        print('STS authentication: PASS')
        print('Complete short-lived credential set: '+str(bool(
            result.access_id and result.access_secret and result.security_token and result.bucket)))
        print('Expires in seconds: '+str(int(result.expiration.timestamp()-time.time())))
        print('Expected bucket verified: '+str(bool(args.expected_bucket)))
        print('No OSS GET/PUT/LIST was performed.')
        return 0
    except CredentialsError as e:
        print('STS authentication failed: '+str(e),file=sys.stderr)
        return 1
    except Exception as e:
        print('STS authentication failed: '+type(e).__name__,file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
