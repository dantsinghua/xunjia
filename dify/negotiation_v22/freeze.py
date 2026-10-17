"""Freeze only source artifacts, never local credentials or business data."""
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_sources(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Measurement source snapshot already exists; choose a new output directory')
    allowed = {'.py', '.yml', '.yaml', '.json', '.md'}
    files = sorted(p for name in ('server', 'dify', 'scripts') for p in (root/name).rglob('*')
                   if p.is_file() and not p.is_symlink() and p.suffix in allowed and '__pycache__' not in p.parts)
    hashes = {}
    destination.mkdir(parents=True)
    for source in files:
        relative = str(source.relative_to(root))
        data = source.read_bytes()
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    changed = [relative for relative, expected in hashes.items() if digest(root/relative) != expected]
    if changed:
        raise ValueError('Source changed during freeze: '+','.join(changed))
    manifest = {'original_root':str(root), 'frozen_root':str(destination), 'files':hashes,
                'manifest_sha256':hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    (destination.parent/'source-manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def drift(manifest, *, frozen=True):
    root = Path(manifest['frozen_root' if frozen else 'original_root'])
    return [relative for relative, expected in manifest['files'].items()
            if not (root/relative).is_file() or digest(root/relative) != expected]
