"""Sign and upload one incremental release to a configured exclusive upgrade queue.

keygen --private-key PATH --public-key PATH
pack --config PATH --source upgrade.py --output release.zip
upload --config PATH --package release.zip

Configuration (0600) fixes site_id, queue, private_key, and either connection_file
or private_capsule. No URL, queue or key can be supplied by a downloaded package.
Uploads have durable markers: a completed or uncertain attempt is never retried.
"""
from __future__ import annotations

import argparse
import ast
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app' if (ROOT / 'app/relay').is_dir() else ROOT))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth
from relay.file_gateway_connection import connection_client, protected_auth_path, resolve_tls_files
from relay.upgrade_package import (MAX_PACKAGE_BYTES, MAX_SOURCE_BYTES,
                                   build_package, capsule_metadata, verify_package, _read_archive)


def private_read(path, limit):
    """Open a protected regular file without following a final-component symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 0 < info.st_size <= limit:
            raise ValueError('UPLOAD_PRIVATE_FILE_INVALID')
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('UPLOAD_PRIVATE_FILE_INVALID')
    return value


def exclusive_write(path, data, mode=0o600):
    path = Path(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_config(path):
    path = Path(path).absolute()
    config = json.loads(private_read(path, 65536))
    allowed = {'site_id', 'queue', 'private_key', 'connection_file', 'private_capsule', 'base_url'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError('UPLOAD_CONFIG_INVALID')
    if not isinstance(config.get('site_id'), str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', config['site_id']) is None:
        raise ValueError('UPLOAD_SITE_INVALID')
    if not isinstance(config.get('queue'), str) or re.fullmatch(r'[A-Za-z0-9_-]{1,20}', config['queue']) is None:
        raise ValueError('UPLOAD_QUEUE_INVALID')
    # Reserved business queues are not a software distribution channel.
    if config['queue'].startswith(('xj_', 'xunjia')):
        raise ValueError('UPLOAD_BUSINESS_QUEUE_FORBIDDEN')
    if bool(config.get('connection_file')) == bool(config.get('private_capsule')):
        raise ValueError('UPLOAD_CONNECTION_REQUIRED')
    for name in ('private_key', 'connection_file', 'private_capsule'):
        value = config.get(name)
        if value is None and name != 'private_key':
            continue
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise ValueError('UPLOAD_PATH_INVALID')
        selected = Path(value).expanduser()
        config[name] = selected if selected.is_absolute() else path.parent / selected
    config['_directory'] = path.parent
    return config


@contextmanager
def gateway(config):
    """Read only local credentials; site.py is parsed as data and never imported."""
    if config.get('connection_file'):
        path = config['connection_file']
        connection = json.loads(private_read(path, 65536))
        auth = GatewayAuth.from_file(protected_auth_path(path, connection['auth_file']))
        client = connection_client(resolve_tls_files(connection, path.parent), auth, path=config['queue'])
        client.timeout = 30
        yield client
        return
    tree = ast.parse(private_read(config['private_capsule'], 2 * 1024 * 1024).decode('utf-8-sig'))
    nodes = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == 'MATERIALS' for target in node.targets)]
    if len(nodes) != 1:
        raise ValueError('UPLOAD_PRIVATE_CAPSULE_INVALID')
    materials = ast.literal_eval(nodes[0].value)
    if not isinstance(materials, dict):
        raise ValueError('UPLOAD_PRIVATE_CAPSULE_INVALID')
    with tempfile.TemporaryDirectory(prefix='xj-upgrade-credentials-') as temporary:
        os.chmod(temporary, 0o700)
        cert, key = Path(temporary) / 'client.crt', Path(temporary) / 'client.key'
        for path, name in ((cert, 'tls/client.crt'), (key, 'tls/client.key')):
            exclusive_write(path, base64.b64decode(materials[name], validate=True))
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key, password=lambda: '')
        auth = GatewayAuth.from_yaml(base64.b64decode(materials['institution-auth.yaml'], validate=True).decode('utf-8'))
        yield FileGatewayClient(config.get('base_url', 'https://its-relay.yingmi.cn'), auth,
                                path=config['queue'], ssl_context=context, timeout=30)


def save_marker(path, record):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    exclusive_write(temporary, (json.dumps(record, sort_keys=True) + '\n').encode())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def upload(config, package):
    with Path(package).open('rb') as stream:
        blob = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(blob) > MAX_PACKAGE_BYTES:
        raise ValueError('UPGRADE_PACKAGE_SIZE_INVALID')
    key = serialization.load_pem_private_key(private_read(config['private_key'], 16384), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('UPGRADE_SIGNING_KEY_INVALID')
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    # This uploader has no live site version; the receiver verifies its real base.
    claimed = json.loads(_read_archive(blob)['manifest.json'])
    manifest, _ = verify_package(blob, public, config['site_id'], claimed.get('target_build'))
    sha = hashlib.sha256(blob).hexdigest()
    directory = config['_directory'] / 'upgrade-upload-records'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('UPLOAD_RECORD_DIRECTORY_INVALID')
    marker = directory / (sha + '.json')
    if marker.exists():
        raise ValueError('UPLOAD_ALREADY_ATTEMPTED_NO_RETRY')
    # A queue-scoped flock also excludes concurrent sends of different packages.
    import fcntl
    lock = directory / (hashlib.sha256(config['queue'].encode()).hexdigest() + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('UPLOAD_ALREADY_RUNNING') from None
        if marker.exists():
            raise ValueError('UPLOAD_ALREADY_ATTEMPTED_NO_RETRY')
        with gateway(config) as client:
            if client.pending() != 0:
                raise ValueError('UPLOAD_QUEUE_NOT_EMPTY')
            name = 'xj-upgrade-' + manifest['target_build'][:16] + '.zip'
            record = {'state': 'uploading', 'sha256': sha, 'queue': config['queue'],
                      'target_build': manifest['target_build'], 'started_at': int(time.time())}
            exclusive_write(marker, (json.dumps(record) + '\n').encode())
            try:
                result = client.upload(name, blob)
                if result.get('file_name') != name:
                    raise FileGatewayError('UPLOAD_NAME_MISMATCH', ambiguous=True)
                record.update(state='uploaded', finished_at=int(time.time()))
                save_marker(marker, record)
            except BaseException:
                # A restart or failure after sending must not silently send again.
                record.update(state='unknown', finished_at=int(time.time()))
                save_marker(marker, record)
                raise
    finally:
        os.close(fd)
    return {**record, 'bytes': len(blob), 'record': str(marker)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    keygen = commands.add_parser('keygen')
    keygen.add_argument('--private-key', type=Path, required=True)
    keygen.add_argument('--public-key', type=Path, required=True)
    pack = commands.add_parser('pack')
    pack.add_argument('--config', type=Path, required=True)
    pack.add_argument('--source', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    pack.add_argument('--ttl-days', type=int, default=30)
    send = commands.add_parser('upload')
    send.add_argument('--config', type=Path, required=True)
    send.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'keygen':
        if args.private_key.exists() or args.public_key.exists() or args.private_key.absolute() == args.public_key.absolute():
            raise ValueError('UPGRADE_KEY_OUTPUT_EXISTS')
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        exclusive_write(args.private_key, private)
        exclusive_write(args.public_key, public)
        result = {'state': 'key_created', 'public_key_sha256': hashlib.sha256(public).hexdigest()}
    elif args.command == 'pack':
        config = load_config(args.config)
        if args.source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('UPGRADE_SOURCE_SIZE_INVALID')
        with args.source.open('rb') as stream:
            source = stream.read(MAX_SOURCE_BYTES + 1)
        blob = build_package(source, private_read(config['private_key'], 16384), config['site_id'], ttl_days=args.ttl_days)
        exclusive_write(args.output, blob)
        result = {'state': 'package_created', 'sha256': hashlib.sha256(blob).hexdigest(),
                  'bytes': len(blob), **capsule_metadata(source)}
    else:
        result = upload(load_config(args.config), args.package)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        code = getattr(exc, 'code', str(exc))
        if not isinstance(code, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', code) is None:
            code = 'UPGRADE_UPLOAD_FAILED_' + type(exc).__name__.upper()
        print(json.dumps({'ok': False, 'code': code}), file=sys.stderr)
        raise SystemExit(2)
