"""Packaging regression inputs remain current and preserve BOM source bytes."""
import ast
import importlib.util
import json
from pathlib import Path
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_current_offline_inputs_have_no_missing_retired_test():
    build = module('retention_offline_build', ROOT / 'deploy/build_offline.py')
    files = build.input_files(ROOT)
    assert 'tests/acceptance/test_sts_bucket_decoupling_20261015.py' not in files
    assert all(source.is_file() for source in files.values())
    assert 'tests/institution_test_support.py' in files
    assert 'tests/tls_material.py' in files


def test_all_current_python_inputs_accept_their_source_encoding():
    build = module('retention_offline_parse', ROOT / 'deploy/build_offline.py')
    files = build.input_files(ROOT)
    for name, path in files.items():
        if name.endswith('.py'):
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=name)


def test_package_builder_parses_bom_without_rewriting_source(tmp_path, monkeypatch):
    from deploy.frontend_build import source_hash

    build = module('retention_offline_bom', ROOT / 'deploy/build_offline.py')
    source = tmp_path / 'bom.py'
    original = b'\xef\xbb\xbf"""Preserve the original source bytes."""\nVALUE = 23\n'
    source.write_bytes(original)
    template = tmp_path / 'config/xunjia.env.example'
    template.parent.mkdir()
    template.write_bytes((ROOT / 'config/xunjia.env.example').read_bytes())
    files = {'app/relay/bom.py': source, 'config/xunjia.env.example': template}
    # Include the real required modules; the runtime completeness gate stays enabled.
    for name in ('__init__.py', 'managed_session.py', 'qb_gateway.py', 'qb_sign.py',
                 'qb_ws.py', 'qb_apollo.py', 'pb_raw.py', 'store.py'):
        target = tmp_path / 'collector' / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((ROOT / 'collector' / name).read_bytes())
        files['app/collector/' + name] = target
    web = tmp_path / 'web'
    (web / 'src').mkdir(parents=True)
    (web / 'src/main.ts').write_text('export const synthetic = true;\n')
    (web / 'index.html').write_text('<div id="app"></div>')
    (web / 'package.json').write_text('{}')
    dist = web / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<script src="/assets/app.js"></script>')
    (dist / 'assets/app.js').write_text('const synthetic = true;\n')
    assets = {path.relative_to(dist).as_posix(): build.sha256(path)
              for path in dist.rglob('*') if path.is_file()}
    (dist / 'assets/build-version.json').write_text(json.dumps({
        'version': 'synthetic-bom-regression', 'source_hash': source_hash(web), 'assets': assets}))
    files.update({'frontend/dist/' + path.relative_to(dist).as_posix(): path
                  for path in dist.rglob('*') if path.is_file()})
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    monkeypatch.setattr(build, 'input_files', lambda _: dict(files))

    def synthetic_revision(command, *, cwd, text):
        assert command == ['git', 'rev-parse', 'HEAD'] and cwd == tmp_path and text is True
        return '1' * 40 + '\n'

    monkeypatch.setattr(build.subprocess, 'check_output', synthetic_revision)
    collector = tmp_path / 'collector.whl'
    with zipfile.ZipFile(collector, 'w'):
        pass
    (tmp_path / 'collector-runtime-manifest.json').write_text(json.dumps({
        'sha256': build.sha256(collector), 'source_hashes': {}}))
    for arch in ('x86_64', 'aarch64', 'common'):
        path = tmp_path / 'wheels' / arch
        path.mkdir(parents=True)
        (path / 'synthetic.whl').write_bytes(b'offline synthetic wheel')
    output = tmp_path / 'package.tar.gz'
    build.render_package(output, tmp_path / 'wheels', collector_wheel=collector)
    with tarfile.open(output) as package:
        member = next(value for value in package.getmembers() if value.name.endswith('/app/relay/bom.py'))
        assert package.extractfile(member).read() == original
