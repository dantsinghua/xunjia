"""Independent minimal ARM bundle gates; synthetic fixtures, no external services."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from deploy import build_offline as builder
from deploy.frontend_build import source_hash


ROOT = Path(__file__).resolve().parents[2]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf8")
    return path


def wheel(directory, name, version, *, tag="py3-none-any", build=""):
    directory.mkdir(parents=True, exist_ok=True)
    stem = name.replace("-", "_")
    filename = f"{stem}-{version}{('-' + build) if build else ''}-{tag}.whl"
    path = directory / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{stem}-{version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        archive.writestr(f"{stem}-{version}.dist-info/WHEEL",
                         f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n")
    return path


@pytest.fixture
def bundle_inputs(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in ("server/__init__.py", "relay/__init__.py", "collector/__init__.py",
                 "collector/managed_session.py", "collector/qb_gateway.py",
                 "collector/qb_sign.py", "collector/qb_ws.py", "collector/qb_apollo.py",
                 "collector/pb_raw.py", "collector/store.py", "deploy/manager.py",
                 "deploy/runtime_check.py", "deploy/package_tests.py",
                 "deploy/collector-qtrade-v22/source/tests/acceptance/test_example.py",
                 "tests/conftest.py", "tests/institution_test_support.py", "tests/tls_material.py",
                 "tests/release/test_example.py", "tests/acceptance/test_oceanbase_schema_sql_20261015.py",
                 "tests/acceptance/test_public_market_tools_20261016.py"):
        write(root / name, "# synthetic package input; never executed\n")
    for name in ("deploy/06-check-runtime.sh", "deploy/05-test-package.sh"):
        write(root / name, "#!/bin/sh\nexit 0\n")
    write(root / "config/xunjia.env.example", (ROOT / "config/xunjia.env.example").read_text())
    write(root / "config/xunjia.env", "PRIVATE_SITE_CANARY_MUST_NOT_SHIP=synthetic-only\n")
    write(root / "config/README.md", "Synthetic configuration instructions\n")
    write(root / "config/institution-connection.example.json", "{}\n")
    write(root / "config/institution-auth.example.yaml",
          "key: REPLACE_INSTITUTION_KEY\nsm4_key: REPLACE_16_BYTES\n")
    write(root / "requirements.lock", "pymysql==1.2.3\ncffi==2.1.1\n")
    write(root / "requirements-dev.lock", "pytest==9.1.1\n")
    write(root / "sql/oceanbase/001_xunjia_schema_mysql.sql", "-- synthetic SQL\n")
    write(root / "dify/negotiation_v22/analysis.yml", "app: synthetic\n")
    write(root / "web/index.html", "<html>synthetic source</html>\n")
    html = write(root / "web/dist/index.html", "<html>synthetic output</html>\n")
    write(root / "web/dist/assets/build-version.json", json.dumps({
        "version": "independent-synthetic", "source_hash": source_hash(root / "web"),
        "assets": {"index.html": hashlib.sha256(html.read_bytes()).hexdigest()},
    }))
    wheels = tmp_path / "wheels"
    wheel(wheels / "aarch64", "pymysql", "1.2.3")
    wheel(wheels / "aarch64", "cffi", "2.1.1", tag="cp312-cp312-manylinux_2_17_aarch64")
    wheel(wheels / "aarch64", "pytest", "9.1.1")
    wheel(wheels / "x86_64", "cffi", "2.1.1", tag="cp312-cp312-manylinux_2_17_x86_64")
    for name, version in (("pip", "25.2"), ("setuptools", "80.9.0"), ("wheel", "0.45.1")):
        wheel(wheels / "common", name, version)
    runtime = write(tmp_path / "python-runtime-aarch64.tar.gz", "synthetic runtime, never executed\n")
    collector = wheel(tmp_path / "collector", "xunjia_collector_runtime", "2.2.11")
    with zipfile.ZipFile(collector, "a") as archive:
        archive.writestr("server/__init__.py", (root / "server/__init__.py").read_bytes())
    write(collector.parent / "collector-runtime-manifest.json", json.dumps({
        "sha256": hashlib.sha256(collector.read_bytes()).hexdigest(),
        "source_hashes": {"server/__init__.py": hashlib.sha256((root / "server/__init__.py").read_bytes()).hexdigest()},
    }))
    monkeypatch.setattr(builder, "ROOT", root)
    # Asset authenticity has a separate real negative gate below. Synthetic
    # package-selection cases do not repeatedly hash an 85 MB ARM runtime.
    real_runtime_validator = builder.validate_arm_runtime
    monkeypatch.setattr(builder, "validate_arm_runtime",
                        lambda path: None if path == runtime else real_runtime_validator(path))
    # Git identity is an external provenance input; all payload selection,
    # template checks, frontend checks, hashing and tar generation remain real.
    original = builder.subprocess.check_output
    def git_identity(command, **kwargs):
        if command == ["git", "rev-parse", "HEAD"]:
            return "0" * 40 + "\n"
        return original(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "check_output", git_identity)
    return SimpleNamespace(root=root, wheels=wheels, runtime=runtime, collector=collector,
                           output=tmp_path / "minimal.tar.gz")


def build(inputs, *, runtime=True):
    builder.render_package(inputs.output, inputs.wheels,
                           arm_runtime=inputs.runtime if runtime else None,
                           collector_wheel=inputs.collector, minimal_aarch64=True)


def contents(path):
    with tarfile.open(path) as archive:
        return {"/".join(Path(member.name).parts[1:]): archive.extractfile(member).read()
                for member in archive.getmembers() if member.isfile()}


def test_minimal_bundle_contains_only_exact_arm_production_dependencies(bundle_inputs):
    build(bundle_inputs)
    payload = contents(bundle_inputs.output)
    packaged = {name for name in payload if name.startswith("wheels/")}
    assert packaged == {
        "wheels/aarch64/pymysql-1.2.3-py3-none-any.whl",
        "wheels/aarch64/cffi-2.1.1-cp312-cp312-manylinux_2_17_aarch64.whl",
        "wheels/common/pip-25.2-py3-none-any.whl",
    }
    assert not any("tests" in Path(name).parts for name in payload)
    assert not {"requirements-dev.lock", "deploy/05-test-package.sh", "deploy/package_tests.py"} & payload.keys()
    assert {"frontend/dist/index.html", "runtime/python-runtime-aarch64.tar.gz",
            "config/xunjia.env", "config/xunjia.env.example", "requirements.lock",
            "sql/oceanbase/001_xunjia_schema_mysql.sql", "deploy/06-check-runtime.sh",
            "deploy/runtime_check.py", "collector-runtime/xunjia_collector_runtime-2.2.11-py3-none-any.whl"} <= payload.keys()
    version = json.loads(payload["VERSION.json"])
    assert version["package_profile"] == "minimal-aarch64"
    assert version["architectures"] == ["aarch64"]
    assert "no" in version["archive_tests"].lower() or "not" in version["archive_tests"].lower()
    assert payload["config/xunjia.env"] == payload["config/xunjia.env.example"]
    assert b"PRIVATE_SITE_CANARY_MUST_NOT_SHIP" not in b"\n".join(payload.values())
    manifest = json.loads(payload["SOURCE-HASHES.json"])
    assert all(hashlib.sha256(payload[name]).hexdigest() == digest for name, digest in manifest.items())


def test_minimal_bundle_requires_bundled_arm_runtime(bundle_inputs):
    with pytest.raises(builder.DeployError):
        build(bundle_inputs, runtime=False)
    assert not bundle_inputs.output.exists()


@pytest.mark.parametrize("kind", ["missing", "invalid", "symlink"])
def test_real_arm_runtime_validator_rejects_missing_unpinned_or_linked_asset(tmp_path, kind):
    runtime = tmp_path / 'python-runtime-aarch64.tar.gz'
    if kind == 'invalid':
        runtime.write_bytes(b'not the audited ARM runtime')
    elif kind == 'symlink':
        target = tmp_path / 'runtime-target'
        target.write_bytes(b'not the audited ARM runtime')
        runtime.symlink_to(target)
    with pytest.raises(builder.DeployError):
        builder.validate_arm_runtime(runtime)


@pytest.mark.parametrize("damage", ["missing", "wrong-version", "duplicate", "wrong-architecture", "wrong-python"])
def test_minimal_bundle_rejects_missing_or_conflicting_production_wheel(bundle_inputs, damage):
    directory = bundle_inputs.wheels / "aarch64"
    target = next(directory.glob("cffi-*.whl"))
    if damage in ("missing", "wrong-version", "wrong-architecture", "wrong-python"):
        target.unlink()
    if damage == "wrong-version":
        wheel(directory, "cffi", "2.1.0", tag="cp312-cp312-manylinux_2_17_aarch64")
    elif damage == "duplicate":
        wheel(directory, "cffi", "2.1.1", tag="cp312-cp312-manylinux_2_28_aarch64")
    elif damage == "wrong-architecture":
        wheel(directory, "cffi", "2.1.1", tag="cp312-cp312-manylinux_2_17_x86_64")
    elif damage == "wrong-python":
        wheel(directory, "cffi", "2.1.1", tag="cp311-cp311-manylinux_2_17_aarch64")
    with pytest.raises(builder.DeployError):
        build(bundle_inputs)
    assert not bundle_inputs.output.exists()


def test_minimal_bundle_rejects_missing_offline_pip(bundle_inputs):
    next((bundle_inputs.wheels / "common").glob("pip-*.whl")).unlink()
    with pytest.raises(builder.DeployError):
        build(bundle_inputs)
    assert not bundle_inputs.output.exists()


@pytest.mark.parametrize("line", ["pymysql>=1.2.3", "pymysql==1.2.3\npymysql==1.2.3",
                                  "pymysql==1.2.3\npymysql==1.2.4"])
def test_minimal_bundle_rejects_ambiguous_or_unpinned_lock(bundle_inputs, line):
    (bundle_inputs.root / "requirements.lock").write_text(line + "\n")
    with pytest.raises(builder.DeployError):
        build(bundle_inputs)
    assert not bundle_inputs.output.exists()


@pytest.mark.parametrize("field", ["XUNJIA_DIFY_ANALYSIS_APP_KEY", "XUNJIA_DIFY_REPLY_APP_KEY",
                                    "XUNJIA_AUTH_SECRET", "XUNJIA_SSO_SECRET", "QB_MYSQL_PASSWORD"])
def test_minimal_bundle_rejects_populated_template_secrets(bundle_inputs, field):
    path = bundle_inputs.root / "config/xunjia.env.example"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(f"{field}='synthetic-not-a-real-secret'" if line.startswith(field + "=") else line
                              for line in lines) + "\n")
    with pytest.raises(builder.DeployError):
        build(bundle_inputs)
    assert not bundle_inputs.output.exists()


@pytest.mark.parametrize("field", ["key", "sm4_key"])
def test_minimal_bundle_rejects_populated_institution_auth_template(bundle_inputs, field):
    values = {"key": "REPLACE_INSTITUTION_KEY", "sm4_key": "REPLACE_16_BYTES"}
    values[field] = "synthetic-sensitive-value"
    write(bundle_inputs.root / "config/institution-auth.example.yaml",
          "\n".join(f"{key}: {value}" for key, value in values.items()) + "\n")
    with pytest.raises(builder.DeployError):
        build(bundle_inputs)
    assert not bundle_inputs.output.exists()


def test_auth_template_rejects_shadowed_secret_duplicate_key(tmp_path):
    template = write(tmp_path / 'institution-auth.example.yaml',
                     'key: synthetic-sensitive-value\nkey: REPLACE_INSTITUTION_KEY\nsm4_key: REPLACE_16_BYTES\n')
    with pytest.raises(builder.DeployError):
        builder.validate_auth_template(template)


@pytest.mark.parametrize("field,value", [("Name", "other-distribution"), ("Version", "2.1.0")])
def test_production_wheel_metadata_must_match_selected_identity(tmp_path, field, value):
    lock = write(tmp_path / 'requirements.lock', 'cffi==2.1.1\n')
    directory = tmp_path / 'wheels'
    path = wheel(directory, 'cffi', '2.1.1', tag='cp312-cp312-manylinux_2_17_aarch64')
    metadata = {'Name': 'cffi', 'Version': '2.1.1'}
    metadata[field] = value
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('cffi-2.1.1.dist-info/METADATA',
                         '\n'.join(f'{key}: {val}' for key, val in metadata.items()) + '\n')
    with pytest.raises(builder.DeployError):
        builder.production_wheels(directory, lock)


def test_minimal_bundle_has_no_need_for_x86_or_development_wheelhouse(bundle_inputs):
    # Remove only individually identified synthetic fixture files.
    for path in (bundle_inputs.wheels / "x86_64").glob("*.whl"):
        path.unlink()
    for pattern in ("setuptools-*.whl", "wheel-*.whl"):
        for path in (bundle_inputs.wheels / "common").glob(pattern):
            path.unlink()
    for path in (bundle_inputs.wheels / "aarch64").glob("pytest-*.whl"):
        path.unlink()
    build(bundle_inputs)
    assert bundle_inputs.output.is_file()


@pytest.mark.parametrize("case,expected", [
    ("wrong-architecture", "BOOTSTRAP_REQUIRES_AARCH64"),
    ("old-glibc", "BOOTSTRAP_REQUIRES_GLIBC_228"),
    ("missing-runtime", "BOOTSTRAP_RUNTIME_MISSING"),
    ("symlink-runtime", "BOOTSTRAP_RUNTIME_MISSING"),
    ("corrupted-runtime", "BOOTSTRAP_RUNTIME_HASH_MISMATCH"),
])
def test_zero_python_entry_rejects_untrusted_runtime_before_extraction(tmp_path, case, expected):
    deploy = tmp_path / "bundle/deploy"
    deploy.mkdir(parents=True)
    for name in ("entry.sh", "bootstrap_no_python.sh"):
        shutil.copyfile(ROOT / "deploy" / name, deploy / name)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "dirname", "sha256sum"):
        (tools / name).symlink_to(shutil.which(name))
    for name, output in (("uname", "x86_64" if case == "wrong-architecture" else "aarch64"),
                         ("getconf", "glibc 2.27" if case == "old-glibc" else "glibc 2.28")):
        write(tools / name, "#!/bin/bash\nprintf '%s\\n' '" + output + "'\n").chmod(0o755)
    marker = tmp_path / "MUST_NOT_EXTRACT_OR_DOWNLOAD"
    for name in ("tar", "curl", "wget", "python", "pip"):
        write(tools / name, "#!/bin/bash\nprintf forbidden > '" + str(marker) + "'\nexit 99\n").chmod(0o755)
    runtime = tmp_path / "bundle/runtime/python-runtime-aarch64.tar.gz"
    if case != "missing-runtime":
        runtime.parent.mkdir()
        if case == "symlink-runtime":
            target = write(tmp_path / "external-runtime", "not trusted\n")
            runtime.symlink_to(target)
        else:
            runtime.write_bytes(b"untrusted runtime bytes\n")
    prefix = tmp_path / "untouched-site"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    completed = subprocess.run(["/bin/bash", str(deploy / "entry.sh"), "verify", "--package-only"],
                               env={"PATH": str(tools), "XUNJIA_PREFIX": str(prefix),
                                    "TMPDIR": str(scratch), "HOME": str(tmp_path)},
                               text=True, capture_output=True, timeout=15)
    assert completed.returncode == 2, completed.stderr
    assert expected in completed.stderr
    assert not prefix.exists()
    assert not marker.exists()
    assert list(scratch.iterdir()) == []
