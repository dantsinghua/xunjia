"""Kylin Python 3.7 bootstrap contracts; no ARM binary or real service is run.

The helper's manifest/archive/ELF validation and cleanup execute unchanged.
Only the target CPU/libc probes and subprocess execution are synthetic.
"""
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = 'runtime/python-runtime-aarch64.tar.gz'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def elf(*, machine=183, elf_class=2, endian=1):
    header = bytearray(64)
    header[:7] = b'\x7fELF' + bytes([elf_class, endian, 1])
    header[16:18] = (2).to_bytes(2, 'little')
    header[18:20] = machine.to_bytes(2, 'little')
    header[20:24] = (1).to_bytes(4, 'little')
    return bytes(header)


def make_bundle(root, *, payload=None, mode=0o755, extra=()):
    bundle = root/'bundle'
    (bundle/'deploy').mkdir(parents=True)
    (bundle/'runtime').mkdir()
    (bundle/'deploy/manager.py').write_text('# synthetic manager, not executed\n')
    with tarfile.open(bundle/RUNTIME, 'w:gz') as archive:
        for name in ('python', 'python/bin', 'python/lib'):
            entry = tarfile.TarInfo(name); entry.type = tarfile.DIRTYPE; entry.mode = 0o755
            archive.addfile(entry)
        data = elf() if payload is None else payload
        entry = tarfile.TarInfo('python/bin/python3.12'); entry.mode = mode; entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
        for entry, data in extra:
            archive.addfile(entry, io.BytesIO(data) if data is not None else None)
    seal(bundle)
    return bundle


def seal(bundle, *, source_digest=None, checksum_digest=None, duplicate=False, missing_checksum=False):
    actual = digest(bundle/RUNTIME)
    source = {RUNTIME: source_digest or actual, 'deploy/manager.py': digest(bundle/'deploy/manager.py')}
    if (bundle/'deploy/package_tests.py').is_file():
        source['deploy/package_tests.py'] = digest(bundle/'deploy/package_tests.py')
    (bundle/'SOURCE-HASHES.json').write_text(json.dumps(source))
    lines = [digest(bundle/'SOURCE-HASHES.json')+'  SOURCE-HASHES.json',
             digest(bundle/'deploy/manager.py')+'  deploy/manager.py']
    if 'deploy/package_tests.py' in source:
        lines.append(source['deploy/package_tests.py']+'  deploy/package_tests.py')
    if not missing_checksum:
        lines.append((checksum_digest or actual)+'  '+RUNTIME)
    if duplicate:
        lines.append(actual+'  '+RUNTIME)
    (bundle/'SHA256SUMS.txt').write_text('\n'.join(lines)+'\n')


def helper():
    path = ROOT/'deploy/bootstrap_runtime.py'
    assert path.is_file(), 'Python 3.7 needs a stdlib-only bundled-runtime bootstrap before manager.py'
    spec = importlib.util.spec_from_file_location('independent_arm_bootstrap', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def harness(module, bundle, tmp_path, monkeypatch, *, manager_code=0, version_fails=False,
            machine='aarch64', libc='glibc 2.28', target_name='manager.py'):
    monkeypatch.setattr(module, '__file__', str(bundle/'deploy/bootstrap_runtime.py'))
    monkeypatch.setattr(module.platform, 'machine', lambda: machine)
    monkeypatch.setattr(module.os, 'confstr', lambda name: libc)
    roots, calls = [], []
    original_temporary = module.tempfile.TemporaryDirectory
    scratch = tmp_path/'owned-work'
    scratch.mkdir()
    def owned_temporary(*args, **kwargs):
        kwargs['dir'] = str(scratch)
        temporary = original_temporary(*args, **kwargs)
        roots.append(Path(temporary.name))
        assert stat.S_IMODE(Path(temporary.name).stat().st_mode) == 0o700
        return temporary
    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', owned_temporary)
    def run(command, **kwargs):
        calls.append((list(command), kwargs))
        executable = Path(command[0])
        assert executable.is_file() and executable.read_bytes()[:4] == b'\x7fELF'
        assert any(executable.is_relative_to(root) for root in roots)
        assert list(command[1:3]) == ['-I', '-B']
        if '-c' in command:
            if version_fails:
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)
        assert Path(command[3]) == bundle/'deploy'/target_name
        return SimpleNamespace(returncode=manager_code)
    monkeypatch.setattr(module.subprocess, 'run', run)
    return roots, calls


def test_actual_entry_can_route_python_379_to_bootstrap_and_preserve_original_arguments(tmp_path):
    deploy = tmp_path/'bundle/deploy'; deploy.mkdir(parents=True)
    shutil.copyfile(ROOT/'deploy/entry.sh', deploy/'entry.sh')
    (deploy/'bootstrap_runtime.py').write_text('import json,sys\nprint("BOOTSTRAP_ARGS="+json.dumps(sys.argv[1:]))\n')
    fakebin = tmp_path/'bin'; fakebin.mkdir()
    (fakebin/'dirname').symlink_to(shutil.which('dirname'))
    # Emulate only interpreter selection/version probes; execute the tiny
    # routing target with this test interpreter, not a customer Python.
    python = fakebin/'python3'
    python.write_text('#!/bin/bash\nif [[ "$1" == "-c" ]]; then\n'
        '  if [[ "$2" == *"3,9"* || "$2" == *"3, 9"* || "$2" == *"3,12"* || "$2" == *"3, 12"* ]]; then exit 1; fi\n'
        '  if [[ "$2" == *"version_info"* ]]; then exit 0; fi\nfi\n'
        'exec '+shlex_quote(sys.executable)+' "$@"\n')
    python.chmod(0o755)
    prefix = tmp_path/'untouched-prefix'
    args = ['verify', '--package-only', '--bundle', 'bundle path with spaces']
    completed = subprocess.run(['/bin/bash', str(deploy/'entry.sh'), *args],
        env={**os.environ, 'PATH': str(fakebin), 'XUNJIA_PREFIX': str(prefix)},
        capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert 'BOOTSTRAP_ARGS='+json.dumps(args) in completed.stdout
    assert not prefix.exists()


def shlex_quote(value):
    import shlex
    return shlex.quote(value)


def test_helper_is_python37_syntax_and_stdlib_only():
    path = ROOT/'deploy/bootstrap_runtime.py'
    assert path.is_file(), 'Missing Python 3.7 bootstrap'
    tree = ast.parse(path.read_text(), feature_version=(3, 7))
    imported = {name.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names}
    imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= sys.stdlib_module_names | {'__future__'}


def test_verified_arm_runtime_runs_in_owned_temporary_root_and_preserves_site(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path)
    module = helper()
    prefix = tmp_path/'site'
    prefix.mkdir(); (prefix/'keep.txt').write_text('site state must remain unchanged')
    monkeypatch.setenv('XUNJIA_PREFIX', str(prefix))
    original_path = os.environ['PATH']
    roots, calls = harness(module, bundle, tmp_path, monkeypatch, manager_code=37)
    args = ['verify', '--package-only', '--bundle', str(bundle)]
    assert module.main(args) == 37
    assert len(calls) == 2 and calls[-1][0][4:] == args
    assert roots and all(not root.exists() for root in roots)
    assert sorted(path.name for path in prefix.iterdir()) == ['keep.txt']
    assert os.environ['PATH'] == original_path
    assert all(call[1].get('env', os.environ).get('PATH') == original_path for call in calls)


def test_confined_runtime_symlinks_and_hardlinks_remain_usable(tmp_path, monkeypatch):
    symbolic = tarfile.TarInfo('python/bin/python3')
    symbolic.type = tarfile.SYMTYPE; symbolic.linkname = 'python3.12'
    hard = tarfile.TarInfo('python/bin/python-copy')
    hard.type = tarfile.LNKTYPE; hard.linkname = 'python/bin/python3.12'
    bundle = make_bundle(tmp_path, extra=[(symbolic, None), (hard, None)])
    module = helper(); roots, calls = harness(module, bundle, tmp_path, monkeypatch)
    base_run = module.subprocess.run
    def inspect_links(command, **kwargs):
        executable = Path(command[0])
        assert executable.with_name('python3').resolve() == executable
        assert executable.with_name('python-copy').read_bytes() == executable.read_bytes()
        return base_run(command, **kwargs)
    monkeypatch.setattr(module.subprocess, 'run', inspect_links)
    assert module.main(['verify', '--package-only']) == 0
    assert len(calls) == 2 and all(not root.exists() for root in roots)


@pytest.mark.parametrize('damage', ['archive_bytes', 'source_digest', 'checksum_digest', 'duplicate_checksum', 'missing_checksum'])
def test_runtime_requires_identical_actual_source_and_checksum_digest_before_execution(tmp_path, monkeypatch, damage):
    bundle = make_bundle(tmp_path)
    if damage == 'archive_bytes':
        with (bundle/RUNTIME).open('ab') as stream: stream.write(b'changed')
    else:
        seal(bundle, source_digest='0'*64 if damage=='source_digest' else None,
             checksum_digest='1'*64 if damage=='checksum_digest' else None,
             duplicate=damage=='duplicate_checksum', missing_checksum=damage=='missing_checksum')
    module = helper(); roots, calls = harness(module, bundle, tmp_path, monkeypatch)
    assert module.main(['verify', '--package-only']) != 0
    assert not calls and all(not root.exists() for root in roots)


def unsafe_entry(kind):
    name = {'absolute': '/outside', 'traversal': 'python/../outside', 'wrong_root': 'other/file',
            'duplicate': 'python/bin/python3.12'}.get(kind, 'python/lib/unsafe')
    entry = tarfile.TarInfo(name); entry.mode = 0o644
    if kind == 'symlink': entry.type = tarfile.SYMTYPE; entry.linkname = '../../../outside'
    elif kind == 'hardlink': entry.type = tarfile.LNKTYPE; entry.linkname = '/outside'
    elif kind == 'special': entry.type = tarfile.FIFOTYPE
    else: entry.size = 4; return entry, b'bad!'
    return entry, None


@pytest.mark.parametrize('kind', ['absolute', 'traversal', 'wrong_root', 'duplicate', 'symlink', 'hardlink', 'special'])
def test_archive_rejects_unconfined_duplicate_or_special_members(tmp_path, monkeypatch, kind):
    bundle = make_bundle(tmp_path, extra=[unsafe_entry(kind)])
    module = helper(); roots, calls = harness(module, bundle, tmp_path, monkeypatch)
    assert module.main(['verify', '--package-only']) != 0
    assert not calls and all(not root.exists() for root in roots)
    assert not (tmp_path/'outside').exists()


@pytest.mark.parametrize('payload,mode', [(elf(machine=62), 0o755), (elf(elf_class=1), 0o755),
    (elf(endian=2), 0o755), (b'\x7fELF', 0o755), (elf(), 0o644)],
    ids=['x86-machine', 'elf32', 'big-endian', 'truncated-header', 'not-executable'])
def test_runtime_interpreter_must_be_executable_complete_elf64_little_endian_aarch64(tmp_path, monkeypatch, payload, mode):
    bundle = make_bundle(tmp_path, payload=payload, mode=mode)
    module = helper(); roots, calls = harness(module, bundle, tmp_path, monkeypatch)
    assert module.main(['verify', '--package-only']) != 0
    assert not calls and all(not root.exists() for root in roots)


@pytest.mark.parametrize('machine,libc', [('x86_64', 'glibc 2.28'), ('aarch64', 'glibc 2.27'), ('aarch64', None)])
def test_wrong_architecture_old_or_unverified_libc_never_executes_bundled_runtime(tmp_path, monkeypatch, machine, libc):
    bundle = make_bundle(tmp_path)
    module = helper(); roots, calls = harness(module, bundle, tmp_path, monkeypatch, machine=machine, libc=libc)
    assert module.main(['verify', '--package-only']) != 0
    assert not calls and all(not root.exists() for root in roots)


def test_runtime_version_failure_cleans_own_directory_and_never_calls_manager(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path)
    module = helper(); roots, calls = harness(module, bundle, tmp_path, monkeypatch, version_fails=True)
    assert module.main(['verify', '--package-only']) != 0
    assert len(calls) == 1 and roots and all(not root.exists() for root in roots)


def test_package_qa_mode_runs_only_fixed_package_tests_with_same_validation_and_cleanup(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path)
    (bundle/'deploy/package_tests.py').write_text('# fixed synthetic QA runner, not executed\n')
    seal(bundle)
    module = helper()
    roots, calls = harness(module, bundle, tmp_path, monkeypatch,
                           manager_code=23, target_name='package_tests.py')
    args = ['--bundle', str(bundle), '--evidence-parent', str(tmp_path/'evidence with spaces')]
    assert module.main(['--package-tests', *args]) == 23
    assert len(calls) == 2 and calls[-1][0][4:] == args
    assert all(not root.exists() for root in roots)
