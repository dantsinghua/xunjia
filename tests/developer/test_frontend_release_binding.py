"""Source and output provenance must both match before release packaging."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from deploy import build_offline


def test_package_builder_rejects_frontend_without_source_provenance(monkeypatch, tmp_path):
    from scripts import export_oceanbase_schema

    root = tmp_path / 'source'
    dist = root / 'web/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('<script src="/assets/old.js"></script>')
    schema = root / 'schema.sql'
    schema.write_text('synthetic schema')
    monkeypatch.setattr(export_oceanbase_schema, 'SQL_FILE', schema)
    monkeypatch.setattr(export_oceanbase_schema, 'render', lambda: 'synthetic schema')
    monkeypatch.setattr(build_offline, 'ROOT', root)
    monkeypatch.setattr(build_offline, 'template_is_clean', lambda path: {})
    monkeypatch.setattr(build_offline, 'validate_quote_runtime', lambda files: None)
    monkeypatch.setattr(build_offline, 'input_files', lambda path: {})
    with pytest.raises(RuntimeError, match='FRONTEND_'):
        build_offline.render_package(tmp_path / 'package.tgz', tmp_path / 'wheels')


def test_old_dist_and_replaced_version_file_cannot_pass_current_source_check(tmp_path):
    from deploy.frontend_build import source_hash, verify_frontend_build

    web = tmp_path / 'web'
    (web / 'src').mkdir(parents=True)
    (web / 'src/main.ts').write_text('old frontend without account context')
    (web / 'index.html').write_text('<div id="app"></div>')
    (web / 'package.json').write_text('{}')
    dist = web / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<script src="/assets/old.js"></script>')
    (dist / 'assets/old.js').write_text('synthetic old frontend')
    manifest = {'version': 'synthetic-v1', 'source_hash': source_hash(web), 'assets': {
        'index.html': hashlib.sha256((dist / 'index.html').read_bytes()).hexdigest(),
        'assets/old.js': hashlib.sha256((dist / 'assets/old.js').read_bytes()).hexdigest(),
    }}
    path = dist / 'assets/build-version.json'
    path.write_text(json.dumps(manifest))
    assert verify_frontend_build(web)['version'] == 'synthetic-v1'
    (web / 'src/main.ts').write_text('new frontend sends X-Xunjia-Context')
    with pytest.raises(RuntimeError, match='FRONTEND_SOURCE_MISMATCH'):
        verify_frontend_build(web)
    # Copying only a current manifest onto old output still cannot bless it.
    manifest['source_hash'] = source_hash(web)
    manifest['assets']['assets/old.js'] = hashlib.sha256(b'new compiled frontend').hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match='FRONTEND_ASSET_MISMATCH'):
        verify_frontend_build(web)


def test_package_checks_staged_outputs_after_concurrent_frontend_completion(monkeypatch, tmp_path):
    from deploy.frontend_build import source_hash
    from scripts import export_oceanbase_schema

    root = tmp_path
    web = root / 'web'
    dist = web / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (web / 'src').mkdir()
    (web / 'src/main.ts').write_text('synthetic source')
    (dist / 'index.html').write_text('<script src="/assets/app.js"></script>')
    (dist / 'assets/app.js').write_text('synthetic output')
    (root / 'config').mkdir()
    (root / 'config/xunjia.env.example').write_text('SYNTHETIC=1\n')
    schema = root / 'schema.sql'
    schema.write_text('synthetic schema')
    wheel = root / 'synthetic.whl'
    with zipfile.ZipFile(wheel, 'w'):
        pass
    (root / 'collector-runtime-manifest.json').write_text(json.dumps({
        'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'source_hashes': {},
    }))
    for arch in ('x86_64', 'aarch64', 'common'):
        folder = root / 'wheels' / arch
        folder.mkdir(parents=True)
        (folder / 'synthetic.whl').write_bytes(b'synthetic')

    def publish_manifest():
        assets = {p.relative_to(dist).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in dist.rglob('*') if p.is_file() and p.name != 'build-version.json'}
        (dist / 'assets/build-version.json').write_text(json.dumps({
            'version': 'synthetic', 'source_hash': source_hash(web), 'assets': assets,
        }))

    publish_manifest()

    def collect_then_publish(_root):
        files = {'config/xunjia.env.example': root / 'config/xunjia.env.example',
                 **{'frontend/dist/' + p.relative_to(dist).as_posix(): p for p in dist.rglob('*') if p.is_file()}}
        (dist / 'late-logo.png').write_bytes(b'synthetic-public')
        publish_manifest()
        return files

    monkeypatch.setattr(build_offline, 'ROOT', root)
    monkeypatch.setattr(build_offline, 'template_is_clean', lambda path: {})
    monkeypatch.setattr(build_offline, 'input_files', collect_then_publish)
    monkeypatch.setattr(build_offline, 'validate_quote_runtime', lambda files: None)
    monkeypatch.setattr(build_offline.subprocess, 'check_output', lambda *a, **kw: 'synthetic\n')
    monkeypatch.setattr(export_oceanbase_schema, 'SQL_FILE', schema)
    monkeypatch.setattr(export_oceanbase_schema, 'render', lambda: 'synthetic schema')
    output = root / 'candidate.tar.gz'
    with pytest.raises(RuntimeError, match='FRONTEND_ASSET_MISMATCH'):
        build_offline.render_package(output, root / 'wheels', collector_wheel=wheel)
    assert not output.exists()
