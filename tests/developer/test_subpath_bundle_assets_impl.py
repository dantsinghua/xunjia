"""Developer regressions for exact subpath assets in the actual offline archive."""
import hashlib
import json
from pathlib import Path

import pytest

from deploy import build_offline as builder
from tests.release.test_minimal_offline_bundle import bundle_inputs, contents

ASSETS = ("nginx-xunjia-map.conf", "nginx-xunjia-locations.conf")
SOURCE = Path(__file__).resolve().parents[2]


def stage(root):
    deploy = root / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "mount_xunjia.py").write_text("# synthetic deployment entrypoint\n")
    for name in ASSETS:
        (deploy / name).write_bytes((SOURCE / "deploy" / name).read_bytes())
    (deploy / "njcb.conf").write_text("PRIVATE_SITE_CANARY_NEVER_PACKAGE\n")
    (deploy / "operator-private.conf").write_text("PRIVATE_SITE_CANARY_NEVER_PACKAGE\n")
    return deploy


def test_exact_mount_templates_are_selected_without_arbitrary_site_configs(tmp_path):
    deploy = stage(tmp_path)
    files = builder.input_files(tmp_path)
    for name in ASSETS:
        key = "deploy/" + name
        assert key in files
        assert files[key].read_bytes() == (deploy / name).read_bytes()
    assert "deploy/njcb.conf" not in files
    assert "deploy/operator-private.conf" not in files


@pytest.mark.parametrize("name", ASSETS)
def test_mount_entrypoint_cannot_ship_with_a_missing_template(tmp_path, name):
    deploy = stage(tmp_path)
    (deploy / name).unlink()
    with pytest.raises(builder.DeployError, match="XUNJIA_MOUNT_CONFIG_MISSING"):
        builder.input_files(tmp_path)


@pytest.mark.parametrize("name", ASSETS)
def test_mount_template_symlink_cannot_import_operator_material(tmp_path, name):
    root = tmp_path / "source"
    deploy = stage(root)
    outside = tmp_path / "operator-secret.conf"
    outside.write_text("SYNTHETIC_ONLY_DO_NOT_INCLUDE\n")
    (deploy / name).unlink()
    (deploy / name).symlink_to(outside)
    with pytest.raises(builder.DeployError, match="XUNJIA_MOUNT_CONFIG_MISSING"):
        builder.input_files(root)


@pytest.mark.parametrize("minimal", [False, True])
def test_real_archive_contains_both_mount_templates_and_integrity_hashes(bundle_inputs, minimal):
    deploy = stage(bundle_inputs.root)
    builder.render_package(bundle_inputs.output, bundle_inputs.wheels,
        arm_runtime=bundle_inputs.runtime, collector_wheel=bundle_inputs.collector,
        minimal_aarch64=minimal)
    payload = contents(bundle_inputs.output)
    manifest = json.loads(payload["SOURCE-HASHES.json"])
    checksums = payload["SHA256SUMS.txt"].decode("utf8")
    for name in ASSETS:
        key = "deploy/" + name
        expected = (deploy / name).read_bytes()
        assert payload[key] == expected
        digest = hashlib.sha256(expected).hexdigest()
        assert manifest[key] == digest
        assert digest + "  " + key + "\n" in checksums
    assert "deploy/njcb.conf" not in payload
    assert "deploy/operator-private.conf" not in payload
    assert b"PRIVATE_SITE_CANARY_NEVER_PACKAGE" not in b"\n".join(payload.values())
