"""Installed runtime checks only; no database, model or IM requests."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sys


MODULES = (
    'ssl', 'sqlite3', 'ctypes', 'bz2', 'lzma', 'zlib', 'venv', 'ensurepip',
    'pymysql', 'fastapi', 'starlette', 'pydantic', 'pydantic_core', 'uvicorn',
    'websockets', 'httpx', 'cryptography', '_cffi_backend', 'openpyxl', 'yaml',
    'server.app', 'relay.file_gateway', 'relay.site_egress', 'collector.managed_session', 'collector.bond_dictionary',
)


def check(bundle: Path, prefix: Path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import safe_prefix, verify_bundle
    prefix = safe_prefix(prefix)
    version = verify_bundle(bundle)
    app = prefix / 'app'
    if not app.is_dir() or not (prefix / 'venv/bin/python').is_file():
        raise ValueError('INSTALLED_RUNTIME_MISSING')
    deployment = json.loads((app / '.deployment.json').read_text())
    if deployment.get('build_id') != version['build_id']:
        raise ValueError('INSTALLED_BUILD_MISMATCH')
    if Path(sys.prefix).resolve() != (prefix / 'venv').resolve():
        raise ValueError('INSTALLED_VENV_REQUIRED')
    if sys.version_info[:2] != (3, 12) or platform.machine() not in version['architectures']:
        raise ValueError('RUNTIME_VERSION_OR_ARCHITECTURE_MISMATCH')
    for name in list(os.environ):
        if name.startswith(('XUNJIA_', 'QB_')):
            os.environ.pop(name)
    sys.path.insert(0, str(app))
    packages = {}
    for line in (bundle / 'requirements.lock').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)', line.strip())
        if not match or importlib.metadata.version(match[1]) != match[2]:
            raise ValueError('INSTALLED_DEPENDENCY_MISMATCH')
        packages[match[1]] = match[2]
    for name in MODULES:
        importlib.import_module(name)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv, plaintext = bytes(range(16)), bytes(16), bytes(range(16))
    encryptor = Cipher(algorithms.SM4(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    decryptor = Cipher(algorithms.SM4(key), modes.CBC(iv)).decryptor()
    if decryptor.update(ciphertext) + decryptor.finalize() != plaintext:
        raise ValueError('SM4_ROUNDTRIP_FAILED')
    import ssl
    return {'passed': True, 'scope': 'offline_installed_runtime_only',
            'build_id': version['build_id'], 'python': platform.python_version(),
            'machine': platform.machine(), 'glibc': os.confstr('CS_GNU_LIBC_VERSION'),
            'openssl': ssl.OPENSSL_VERSION, 'packages': packages,
            'imports': list(MODULES), 'sm4': 'passed',
            'database_requests': 0, 'model_requests': 0, 'im_submissions': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--prefix', type=Path, default=os.getenv('XUNJIA_PREFIX', '/home/xunjia'))
    parser.add_argument('--site-config', action='store_true', help='Validate new site credentials/TLS locally; never issue network requests')
    args = parser.parse_args()
    try:
        result=check(args.bundle.resolve(), args.prefix)
        if args.site_config:
            from common import effective_env, read_env, validate_config
            values=effective_env(read_env(args.prefix/'config/xunjia.env'),args.prefix,args.prefix/'app')
            validate_config(values,ready=True)
            if not values.get('XUNJIA_INSTITUTION_CONFIG'):
                raise ValueError('INSTITUTION_CONFIG_REQUIRED_FOR_FRESH_DEPLOYMENT')
            path=Path(values['XUNJIA_INSTITUTION_CONFIG'])
            if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size>65536:
                raise ValueError('INSTITUTION_CONFIG_PERMISSIONS_INVALID')
            config=json.loads(path.read_text())
            from relay.gateway_paths import CONTRACT
            from relay.file_gateway_connection import TLS_FIELDS, load_connection
            if (config.get('contract')!=CONTRACT or
                    set(config)-{'contract','base_url','auth_file','hostname','addresses','directory_state_dir',*TLS_FIELDS}):
                raise ValueError('INSTITUTION_CONFIG_CONTRACT_INVALID')
            os.environ.update(values)
            client=load_connection(path)
            if 'REPLACE_' in client.auth.key or b'REPLACE_' in client.auth.sm4_key:
                raise ValueError('INSTITUTION_AUTH_TEMPLATE_UNFILLED')
            if values.get('QB_DICTIONARY_SOURCE') not in (None,'','innodealing'):
                raise ValueError('DICTIONARY_SOURCE_INVALID')
            if values.get('QB_DICTIONARY_CONFIG'):
                from collector.bond_dictionary import load_external
                load_external(Path(values['QB_DICTIONARY_CONFIG']))
            result['site_config']='validated_locally_no_network'
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        # Import exceptions may contain site paths or environment values.
        print(json.dumps({'passed': False, 'error_type': type(exc).__name__,
                          'code': str(exc) if re.fullmatch('[A-Z_]+', str(exc)) else 'RUNTIME_CHECK_FAILED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
