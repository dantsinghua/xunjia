"""Bind the existing frontend build manifest to its inputs and exact outputs."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from deploy.common import DeployError


def source_hash(web):
    web = Path(web)
    files = []
    for path in web.iterdir():
        if path.name in ('src', 'public'):
            if path.is_symlink():
                raise DeployError('FRONTEND_SOURCE_SYMLINK')
            files.extend(p for p in path.rglob('*') if p.is_file() or p.is_symlink())
        elif path.name in ('index.html', 'package.json', 'package-lock.json', 'vite.config.ts', 'build-source.mjs') or re.fullmatch(r'tsconfig.*\.json', path.name):
            files.append(path)
    if any(p.is_symlink() for p in files):
        raise DeployError('FRONTEND_SOURCE_SYMLINK')
    hashes = {p.relative_to(web).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    return hashlib.sha256(json.dumps(hashes, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_frontend_build(web, dist=None):
    web = Path(web)
    dist = Path(dist) if dist is not None else web / 'dist'
    try:
        manifest = json.loads((dist / 'assets/build-version.json').read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise DeployError('FRONTEND_BUILD_PROVENANCE_MISSING') from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get('version'), str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', manifest['version']):
        raise DeployError('FRONTEND_BUILD_PROVENANCE_INVALID')
    if manifest.get('source_hash') != source_hash(web):
        raise DeployError('FRONTEND_SOURCE_MISMATCH', 'rebuild the frontend from the current source')
    assets = manifest.get('assets')
    if not isinstance(assets, dict) or 'index.html' not in assets:
        raise DeployError('FRONTEND_BUILD_PROVENANCE_INVALID')
    actual = {p.relative_to(dist).as_posix() for p in dist.rglob('*') if p.is_file()}
    if actual != set(assets) | {'assets/build-version.json'}:
        raise DeployError('FRONTEND_ASSET_MISMATCH')
    for name, digest in assets.items():
        relative = PurePosixPath(name)
        path = dist / name
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or not path.resolve().is_relative_to(dist.resolve()) or path.is_symlink() or not path.is_file():
            raise DeployError('FRONTEND_ASSET_MISMATCH')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise DeployError('FRONTEND_ASSET_MISMATCH')
    return manifest
