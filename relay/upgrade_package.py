"""Bounded, site-specific signed incremental capsules; never execute package code."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import re
import stat
import time
import zipfile
import zlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = MAX_PACKAGE_BYTES - 32768
MAX_MANIFEST_BYTES = 16384
MAX_AGE_SECONDS = 30 * 86400
_HEX = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'schema', 'site_id', 'target_build', 'base_builds', 'upgrade_sha256', 'created_at', 'expires_at'}


class UpgradePackageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise UpgradePackageError(code)


def _site(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) is not None,
             'UPGRADE_SITE_INVALID')
    return value


def _time(now):
    now = time.time() if now is None else now
    _require(type(now) in (int, float) and math.isfinite(now) and now >= 0, 'UPGRADE_TIME_INVALID')
    return int(now)


def capsule_metadata(source: bytes) -> dict:
    """Read the builder's literal CAPSULE assignment without importing the script."""
    _require(isinstance(source, bytes) and 0 < len(source) <= MAX_SOURCE_BYTES, 'UPGRADE_SOURCE_SIZE_INVALID')
    try:
        tree = ast.parse(source.decode('utf-8-sig'))
        nodes = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'CAPSULE' for target in node.targets)]
        _require(len(nodes) == 1 and len(nodes[0].targets) == 1, 'UPGRADE_CAPSULE_INVALID')
        _require(sum(1 for _ in ast.walk(nodes[0].value)) <= 100000, 'UPGRADE_CAPSULE_INVALID')
        meta = ast.literal_eval(nodes[0].value)
        _require(isinstance(meta, dict) and type(meta.get('schema')) is int and meta['schema'] == 1,
                 'UPGRADE_CAPSULE_INVALID')
        _require(isinstance(meta.get('build_id'), str) and _HEX.fullmatch(meta['build_id']) is not None,
                 'UPGRADE_CAPSULE_INVALID')
        bases = meta.get('baselines')
        _require(isinstance(bases, dict) and 1 <= len(bases) <= 100, 'UPGRADE_CAPSULE_INVALID')
        _require(all(isinstance(k, str) and _HEX.fullmatch(k) and isinstance(v, str) and _HEX.fullmatch(v)
                     for k, v in bases.items()), 'UPGRADE_CAPSULE_INVALID')
        return {'target_build': meta['build_id'], 'base_builds': sorted(set(bases.values()))}
    except UpgradePackageError:
        raise
    except (ValueError, TypeError, SyntaxError, UnicodeError, RecursionError, MemoryError):
        raise UpgradePackageError('UPGRADE_CAPSULE_INVALID') from None


def _private_key(pem):
    _require(isinstance(pem, bytes) and 0 < len(pem) <= 16384, 'UPGRADE_SIGNING_KEY_INVALID')
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise UpgradePackageError('UPGRADE_SIGNING_KEY_INVALID') from None
    _require(isinstance(key, Ed25519PrivateKey), 'UPGRADE_SIGNING_KEY_INVALID')
    return key


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'UPGRADE_MANIFEST_INVALID')
        result[key] = value
    return result


def _read_archive(blob):
    _require(isinstance(blob, bytes) and 0 < len(blob) <= MAX_PACKAGE_BYTES, 'UPGRADE_PACKAGE_SIZE_INVALID')
    _require(blob.startswith(b'PK\x03\x04') and len(blob) >= 22 and blob[-22:-18] == b'PK\x05\x06',
             'UPGRADE_PACKAGE_INVALID')
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            items = archive.infolist()
            _require(len(items) == 3 and {item.filename for item in items} ==
                     {'manifest.json', 'signature.bin', 'upgrade.py'}, 'UPGRADE_PACKAGE_MEMBERS_INVALID')
            _require(not archive.comment, 'UPGRADE_PACKAGE_MEMBERS_INVALID')
            result = {}
            limits = {'manifest.json': MAX_MANIFEST_BYTES, 'signature.bin': 64, 'upgrade.py': MAX_SOURCE_BYTES}
            for item in items:
                mode = item.external_attr >> 16
                _require(not item.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                         and not (item.flag_bits & 1)
                         and item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                         and 0 < item.file_size <= limits[item.filename], 'UPGRADE_PACKAGE_MEMBERS_INVALID')
                with archive.open(item) as stream:
                    raw = stream.read(limits[item.filename] + 1)
                _require(len(raw) == item.file_size, 'UPGRADE_PACKAGE_SIZE_INVALID')
                result[item.filename] = raw
            return result
    except UpgradePackageError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise UpgradePackageError('UPGRADE_PACKAGE_INVALID') from None


def verify_package(blob: bytes, public_key_pem: bytes, site_id: str, current_build: str, now=None):
    """Return (signed manifest, upgrade.py bytes) after all trust and base checks."""
    _site(site_id)
    _require(isinstance(current_build, str) and _HEX.fullmatch(current_build) is not None,
             'UPGRADE_CURRENT_BUILD_INVALID')
    files = _read_archive(blob)
    _require(isinstance(public_key_pem, bytes) and 0 < len(public_key_pem) <= 16384,
             'UPGRADE_PUBLIC_KEY_INVALID')
    try:
        key = serialization.load_pem_public_key(public_key_pem)
        _require(isinstance(key, Ed25519PublicKey), 'UPGRADE_PUBLIC_KEY_INVALID')
        key.verify(files['signature.bin'], files['manifest.json'])
    except InvalidSignature:
        raise UpgradePackageError('UPGRADE_SIGNATURE_INVALID') from None
    except (ValueError, TypeError):
        raise UpgradePackageError('UPGRADE_PUBLIC_KEY_INVALID') from None
    try:
        manifest = json.loads(files['manifest.json'], object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise UpgradePackageError('UPGRADE_MANIFEST_INVALID') from None
    _require(isinstance(manifest, dict) and set(manifest) == _FIELDS
             and type(manifest['schema']) is int and manifest['schema'] == 1, 'UPGRADE_MANIFEST_INVALID')
    _require(manifest['site_id'] == site_id, 'UPGRADE_SITE_MISMATCH')
    _require(isinstance(manifest['target_build'], str) and _HEX.fullmatch(manifest['target_build']) is not None
             and isinstance(manifest['upgrade_sha256'], str) and _HEX.fullmatch(manifest['upgrade_sha256']) is not None,
             'UPGRADE_MANIFEST_INVALID')
    bases = manifest['base_builds']
    _require(isinstance(bases, list) and 1 <= len(bases) <= 100
             and all(isinstance(value, str) and _HEX.fullmatch(value) for value in bases)
             and bases == sorted(set(bases)), 'UPGRADE_MANIFEST_INVALID')
    created, expires, clock = manifest['created_at'], manifest['expires_at'], _time(now)
    _require(type(created) is int and type(expires) is int and 0 <= created < expires
             and expires - created <= MAX_AGE_SECONDS, 'UPGRADE_MANIFEST_INVALID')
    _require(created <= clock, 'UPGRADE_PACKAGE_FROM_FUTURE')
    _require(clock < expires, 'UPGRADE_PACKAGE_EXPIRED')
    source = files['upgrade.py']
    _require(hashlib.sha256(source).hexdigest() == manifest['upgrade_sha256'], 'UPGRADE_SOURCE_HASH_MISMATCH')
    meta = capsule_metadata(source)
    _require(meta['target_build'] == manifest['target_build'] and meta['base_builds'] == bases,
             'UPGRADE_CAPSULE_MANIFEST_MISMATCH')
    _require(current_build in bases or current_build == manifest['target_build'], 'UPGRADE_BASE_NOT_SUPPORTED')
    return manifest, source


def build_package(source: bytes, private_key_pem: bytes, site_id: str, now=None, ttl_days=30) -> bytes:
    """Sign a literal incremental capsule into the fixed three-member ZIP format."""
    _site(site_id)
    _require(type(ttl_days) is int and 1 <= ttl_days <= 30, 'UPGRADE_TTL_INVALID')
    meta = capsule_metadata(source)
    created = _time(now)
    manifest = {'schema': 1, 'site_id': site_id, **meta, 'created_at': created,
                'expires_at': created + ttl_days * 86400, 'upgrade_sha256': hashlib.sha256(source).hexdigest()}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('ascii')
    _require(len(raw) <= MAX_MANIFEST_BYTES, 'UPGRADE_MANIFEST_INVALID')
    signature = _private_key(private_key_pem).sign(raw)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in (('manifest.json', raw), ('signature.bin', signature), ('upgrade.py', source)):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, value)
    result = output.getvalue()
    _require(len(result) <= MAX_PACKAGE_BYTES, 'UPGRADE_PACKAGE_SIZE_INVALID')
    return result
