"""Build a self-contained small incremental capsule from verified offline bundles."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incremental as runtime

SCHEMA_FILES = ('app/server/schema.py', 'sql/oceanbase/001_xunjia_schema_mysql.sql')

def inspect(root):
    root = Path(root).absolute()
    version = json.loads((root / 'VERSION.json').read_text())
    sha = runtime.digest(root / 'SHA256SUMS.txt')
    names = runtime.verify_tree(root, version['build_id'], sha)
    names['SHA256SUMS.txt'] = sha
    return version, names

def build(baselines, target, output):
    target, output = Path(target), Path(output)
    if output.exists() or output.with_suffix(output.suffix + '.sha256').exists():
        raise runtime.IncrementalError('OUTPUT_ALREADY_EXISTS')
    version, files = inspect(target)
    if not baselines:
        raise runtime.IncrementalError('BASELINE_REQUIRED')
    sources = []
    for source in baselines:
        info, hashes = inspect(source)
        for name in SCHEMA_FILES:
            if name not in files or hashes.get(name) != files[name]:
                raise runtime.IncrementalError('SCHEMA_CHANGE_REQUIRES_EXPLICIT_MIGRATION')
        sources.append((Path(source), info, hashes))
    payload = {}
    for name, sha in files.items():
        if any(hashes.get(name) != sha for _, _, hashes in sources):
            payload[name] = base64.b64encode(zlib.compress((target / name).read_bytes(), 9)).decode('ascii')
    capsule = {'schema': 1, 'build_id': version['build_id'], 'manifest_sha256': files['SHA256SUMS.txt'],
               'baselines': {hashes['SHA256SUMS.txt']: info['build_id'] for _, info, hashes in sources},
               'files': files, 'sizes': {name: (target / name).stat().st_size for name in files}}
    template = Path(runtime.__file__).read_text()
    assert template.count('CAPSULE = None') == template.count('PAYLOAD = {}') == 1
    rendered = template.replace('CAPSULE = None', 'CAPSULE = ' + repr(capsule)).replace('PAYLOAD = {}', 'PAYLOAD = ' + repr(payload))
    compile(rendered, str(output), 'exec')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8') as stream:
        stream.write(rendered)
    output.chmod(0o700)
    sha = runtime.digest(output)
    output.with_suffix(output.suffix + '.sha256').write_text(sha + '  ' + output.name + '\n')
    return {'script': str(output), 'sha256': sha, 'bytes': output.stat().st_size,
            'build_id': version['build_id'], 'changed_files': len(payload), 'reused_files': len(files) - len(payload),
            'supported_baselines': len(capsule['baselines']), 'database_ddl_executed': False}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, action='append', required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.target, args.output)))
