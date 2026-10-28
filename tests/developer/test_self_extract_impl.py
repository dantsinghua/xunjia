"""Host-side contract checks for the offline self-extracting delivery format."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from deploy import build_self_extract as package


def archive(path, *, root="xunjia fixture", unsafe=None):
    with tarfile.open(path, "w:gz") as tar:
        member = tarfile.TarInfo(root)
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
        tar.addfile(member)
        data = b"synthetic deployment payload\n"
        member = tarfile.TarInfo(unsafe or root + "/config/xunjia.env")
        member.size = len(data)
        member.mode = 0o600
        tar.addfile(member, io.BytesIO(data))
    return path


def fake_tool():
    return ("#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"$TOOL_LOG\"\n"
            "[[ \"$1\" == tar ]] || exit 71\n"
            "shift\nexec /bin/tar \"$@\"\n").encode()


def shell_fixture(tmp_path, monkeypatch, *, tool=None):
    payload = archive(tmp_path / "payload.tar.gz", root="xunjia-fixture")
    tool = fake_tool() if tool is None else tool
    monkeypatch.setattr(package, "BUSYBOX_SHA256", hashlib.sha256(tool).hexdigest())
    header, offset = package.stable_header(tool, package.digest(payload), "xunjia-fixture")
    script = tmp_path / "delivery with spaces.run"
    script.write_bytes(header + payload.read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uname = bin_dir / "uname"
    uname.write_text("#!/bin/bash\nprintf 'aarch64\\n'\n")
    uname.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = {**os.environ, "PATH": str(bin_dir) + ":/usr/bin:/bin", "TMPDIR": str(scratch), "TOOL_LOG": str(tmp_path / "tool.log")}
    return script, payload, header, offset, env


def run(script, args, env):
    return subprocess.run(["/bin/bash", str(script), *args], env=env, capture_output=True, text=True, timeout=15)


def test_stable_header_is_ascii_and_exact_byte_offset(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    assert header.isascii() and header.endswith(b"\n")
    assert offset == len(header) + 1
    assert script.read_bytes()[offset - 1:] == payload.read_bytes()
    assert str(offset).encode() in header


def test_extract_to_new_directory_with_spaces_and_fixed_safe_tar_flags(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    target = tmp_path / "new directory with spaces"
    result = run(script, ["--extract", str(target)], env)
    assert result.returncode == 0, result.stderr
    assert (target / "xunjia-fixture/config/xunjia.env").read_bytes() == b"synthetic deployment payload\n"
    assert target.stat().st_mode & 0o777 == 0o700
    log = (tmp_path / "tool.log").read_text()
    assert "--no-same-owner" in log and "--no-same-permissions" in log
    assert not list((tmp_path / "scratch").iterdir())
    assert "EXTRACTED_ROOT=" in result.stdout


def test_existing_directory_and_own_script_are_not_overwritten(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("untouched")
    before = script.read_bytes()
    for destination in (target, script):
        result = run(script, ["--extract", str(destination)], env)
        assert result.returncode != 0 and "TARGET_ALREADY_EXISTS" in result.stderr
    assert marker.read_text() == "untouched" and script.read_bytes() == before
    assert not (tmp_path / "tool.log").exists()


def test_corrupted_payload_fails_before_target_creation(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    data = bytearray(script.read_bytes()); data[-8] ^= 1; script.write_bytes(data)
    target = tmp_path / "must-not-exist"
    result = run(script, ["--extract", str(target)], env)
    assert result.returncode != 0 and "PAYLOAD_HASH_MISMATCH" in result.stderr
    assert not target.exists() and not (tmp_path / "tool.log").exists()
    assert not list((tmp_path / "scratch").iterdir())


def test_corrupted_embedded_tool_is_not_executed(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    encoded = base64.b64encode(fake_tool())
    first = encoded[:76]
    altered = bytes([ord("B") if first[0] != ord("B") else ord("C")]) + first[1:]
    assert first in script.read_bytes()
    script.write_bytes(script.read_bytes().replace(first, altered, 1))
    target = tmp_path / "must-not-exist"
    result = run(script, ["--extract", str(target)], env)
    assert result.returncode != 0 and "BOOTSTRAP_HASH_MISMATCH" in result.stderr
    assert not target.exists() and not (tmp_path / "tool.log").exists()


def test_wrong_runtime_architecture_stops_before_tool_execution(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    (tmp_path / "bin/uname").write_text("#!/bin/bash\nprintf 'x86_64\\n'\n")
    result = run(script, ["--extract", str(tmp_path / "new")], env)
    assert result.returncode != 0 and "AARCH64_REQUIRED" in result.stderr
    assert not (tmp_path / "tool.log").exists()


def test_failed_extraction_keeps_partial_destination_without_recursive_cleanup(tmp_path, monkeypatch):
    tool = ("#!/bin/bash\nif [[ \"$2\" == -tzf ]]; then printf 'xunjia-fixture/\\n'; exit 0; fi\n"
            "last=\"${@: -1}\"; mkdir \"$last/partial\"; printf keep > \"$last/partial/keep\"; exit 73\n").encode()
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch, tool=tool)
    target = tmp_path / "partial-result"
    result = run(script, ["--extract", str(target)], env)
    assert result.returncode != 0 and "EXTRACTION_FAILED_PARTIAL_DIRECTORY" in result.stderr
    assert (target / "partial/keep").read_text() == "keep"
    assert not list((tmp_path / "scratch").iterdir())


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute/file", "xunjia-fixture/../outside"])
def test_builder_rejects_unsafe_payload_members(tmp_path, unsafe):
    path = archive(tmp_path / "bad.tar.gz", root="xunjia-fixture", unsafe=unsafe)
    with pytest.raises(package.BuildError):
        package.inspect_archive(path)


def test_builder_rejects_symlinks_and_multiple_roots(tmp_path):
    for multi in (False, True):
        path = tmp_path / ("multi.tar.gz" if multi else "link.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            for name in ("xunjia-fixture", "other" if multi else "xunjia-fixture/link"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE if name == "xunjia-fixture" or multi else tarfile.SYMTYPE
                info.linkname = "/outside"
                tar.addfile(info)
        with pytest.raises(package.BuildError):
            package.inspect_archive(path)


def test_unverified_or_wrong_architecture_busybox_is_rejected(tmp_path):
    binary = tmp_path / "busybox"
    raw = bytearray(64)
    raw[:7] = b"\x7fELF\x02\x01\x01"
    raw[16:18] = (2).to_bytes(2, "little")
    raw[18:20] = (62).to_bytes(2, "little")
    binary.write_bytes(raw)
    with pytest.raises(package.BuildError, match="AARCH64"):
        package.validate_busybox(binary)
    raw[18:20] = (183).to_bytes(2, "little")
    binary.write_bytes(raw)
    with pytest.raises(package.BuildError, match="HASH"):
        package.validate_busybox(binary)


def test_symlink_parent_and_traversal_destination_rejected_before_execution(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    for target in (str(link / "new"), str(real) + "/../new"):
        result = run(script, ["--extract", target], env)
        assert result.returncode != 0
    assert not (real / "new").exists() and not (tmp_path / "tool.log").exists()


def test_existing_output_is_not_replaced_even_with_invalid_inputs(tmp_path):
    output = tmp_path / "existing.run"
    output.write_text("keep original")
    with pytest.raises(package.BuildError, match="OUTPUT_ALREADY_EXISTS"):
        package.build(tmp_path / "missing.tar.gz", tmp_path / "missing-busybox", output)
    assert output.read_text() == "keep original"


def test_missing_extract_argument_never_executes_tool(tmp_path, monkeypatch):
    script, payload, header, offset, env = shell_fixture(tmp_path, monkeypatch)
    result = run(script, [], env)
    assert result.returncode != 0 and "USAGE" in result.stderr
    assert not (tmp_path / "tool.log").exists()


def test_setid_payload_file_is_rejected(tmp_path):
    path = tmp_path / "setid.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        directory = tarfile.TarInfo("xunjia-fixture")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        member = tarfile.TarInfo("xunjia-fixture/tool")
        member.mode = 0o4755
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(package.BuildError, match="ARCHIVE_MEMBER_UNSAFE"):
        package.inspect_archive(path)
