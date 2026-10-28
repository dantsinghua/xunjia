"""Host-side behavior checks; real ARM runtime acceptance is separate."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def sandbox(tmp_path, *, arch="aarch64", libc="glibc 2.28"):
    bundle = tmp_path / "bundle"
    deploy = bundle / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/entry.sh", deploy / "entry.sh")
    fallback = ROOT / "deploy/bootstrap_no_python.sh"
    if fallback.exists():
        shutil.copy2(fallback, deploy / fallback.name)
    bins = tmp_path / "bin"
    bins.mkdir()
    for command in ("bash", "dirname", "sha256sum", "mktemp", "cp", "chmod", "tar", "gzip", "stat", "getconf"):
        (bins / command).symlink_to(shutil.which(command))
    for name, output in (("uname", arch), ("getconf", libc)):
        target = bins / name
        if target.is_symlink():
            target.unlink()
        target.write_text("#!/bin/bash\nprintf '%s\\n' '" + output + "'\n")
        target.chmod(0o755)
    env = {"PATH": str(bins), "XUNJIA_PREFIX": str(tmp_path / "absent"), "TMPDIR": str(tmp_path)}
    return bundle, bins, env


def invoke(bundle, env, *args):
    return subprocess.run(["/bin/bash", str(bundle / "deploy/entry.sh"), *args], env=env,
                          text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize("arch,libc,code", [
    ("x86_64", "glibc 2.28", "BOOTSTRAP_REQUIRES_AARCH64"),
    ("aarch64", "glibc 2.27", "BOOTSTRAP_REQUIRES_GLIBC_228"),
    ("aarch64", "unknown", "BOOTSTRAP_REQUIRES_GLIBC_228"),
])
def test_no_python_rejects_wrong_platform(tmp_path, arch, libc, code):
    bundle, _, env = sandbox(tmp_path, arch=arch, libc=libc)
    result = invoke(bundle, env, "preflight")
    assert result.returncode == 2
    assert code in result.stderr


def test_no_python_requires_runtime(tmp_path):
    bundle, _, env = sandbox(tmp_path)
    result = invoke(bundle, env)
    assert result.returncode == 2
    assert "BOOTSTRAP_RUNTIME_MISSING" in result.stderr


def test_no_python_rejects_changed_runtime_before_extraction(tmp_path):
    bundle, _, env = sandbox(tmp_path)
    (bundle / "runtime").mkdir()
    (bundle / "runtime/python-runtime-aarch64.tar.gz").write_bytes(b"untrusted bytes")
    result = invoke(bundle, env)
    assert result.returncode == 2
    assert "BOOTSTRAP_RUNTIME_HASH_MISMATCH" in result.stderr
    assert not list(tmp_path.glob("xunjia-no-python-*"))


def test_existing_python_entry_retains_arguments_and_status(tmp_path):
    bundle, bins, env = sandbox(tmp_path)
    python = bins / "python3.12"
    python.write_text("#!/bin/bash\nprintf '<%s>\\n' \"$@\"\nexit 23\n")
    python.chmod(0o755)
    result = invoke(bundle, env, "verify", "argument with spaces")
    assert result.returncode == 23
    assert result.stdout.splitlines() == [f"<{bundle}/deploy/manager.py>", "<verify>", "<argument with spaces>"]


def host_runtime_fixture(bundle, bins):
    """Model only the audited ARM asset on x86; execute the real shell and cleanup.

    The hash utility maps exactly this fixture's hash to the pinned ARM digest.
    Untrusted-byte rejection above uses the real hash utility without this adapter.
    """
    archive = bundle / "runtime/python-runtime-aarch64.tar.gz"
    archive.parent.mkdir()
    shim = f'#!/bin/bash\nexec "{sys.executable}" "$@"\n'.encode()
    with tarfile.open(archive, "w:gz") as package:
        item = tarfile.TarInfo("python/bin/python3.12")
        item.mode = 0o700
        item.size = len(shim)
        package.addfile(item, io.BytesIO(shim))
    actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    hasher = bins / "sha256sum"
    hasher.unlink()
    hasher.write_text(
        '#!/bin/bash\n'
        f'actual="$("{shutil.which("sha256sum")}" "$@")"\n'
        f'[[ "${{actual%% *}}" = "{actual_hash}" ]] || exit 2\n'
        'printf "%s  fixture\\n" c2083943c86dbb21ca0211238362fd922de7b0475688f26c135cf5d20a1c2f48\n'
    )
    hasher.chmod(0o755)


@pytest.mark.parametrize("ending,status", [
    ("raise SystemExit(0)", 0),
    ("raise SystemExit(19)", 19),
    ("signal.raise_signal(signal.SIGINT)", 130),
    ("signal.raise_signal(signal.SIGTERM)", 143),
])
def test_bootstrap_preserves_status_arguments_and_cleans_owned_root(tmp_path, ending, status):
    bundle, bins, env = sandbox(tmp_path)
    host_runtime_fixture(bundle, bins)
    sentinel = tmp_path / "unrelated"
    sentinel.mkdir()
    (sentinel / "keep").write_text("untouched")
    result_file = tmp_path / "observed.json"
    env["TEST_RESULT"] = str(result_file)
    (bundle / "deploy/bootstrap_runtime.py").write_text(
        "import json, os, pathlib, signal, stat, sys\n"
        "roots=list(pathlib.Path(os.environ['TMPDIR']).glob('xunjia-no-python-*'))\n"
        "pathlib.Path(os.environ['TEST_RESULT']).write_text(json.dumps({"
        "'args': sys.argv[1:], 'roots':len(roots), 'mode':stat.S_IMODE(roots[0].stat().st_mode)}))\n"
        + ending + "\n"
    )
    result = invoke(bundle, env, "verify", "value with spaces", "")
    assert result.returncode == status, result.stderr
    assert json.loads(result_file.read_text()) == {
        "args": ["verify", "value with spaces", ""], "roots": 1, "mode": 0o700,
    }
    assert not list(tmp_path.glob("xunjia-no-python-*"))
    assert (sentinel / "keep").read_text() == "untouched"
