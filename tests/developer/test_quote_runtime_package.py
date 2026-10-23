"""Validate the actual deployment payload, independent of the checkout imports."""
from pathlib import Path

import pytest

from deploy import build_offline


ROOT = Path(__file__).resolve().parents[2]
REQUIRED = (
    '__init__.py', 'managed_session.py', 'qb_gateway.py', 'qb_sign.py',
    'qb_ws.py', 'qb_apollo.py', 'pb_raw.py', 'store.py',
)


def test_complete_payload_passes_quote_runtime_validation():
    build_offline.validate_quote_runtime(build_offline.input_files(ROOT))


@pytest.mark.parametrize('missing', REQUIRED)
def test_each_missing_transitive_quote_module_is_rejected(missing):
    files = build_offline.input_files(ROOT)
    del files['app/collector/' + missing]
    with pytest.raises(RuntimeError, match='QUOTE_RUNTIME_MISSING'):
        build_offline.validate_quote_runtime(files)


def test_package_builder_rejects_incomplete_payload_before_wheel_install(monkeypatch, tmp_path):
    from scripts import export_oceanbase_schema

    root = tmp_path / 'source'
    dist = root / 'web/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('synthetic build')
    schema = root / 'schema.sql'
    schema.write_text('synthetic schema')
    monkeypatch.setattr(export_oceanbase_schema, 'SQL_FILE', schema)
    monkeypatch.setattr(export_oceanbase_schema, 'render', lambda: 'synthetic schema')
    monkeypatch.setattr(build_offline, 'ROOT', root)
    monkeypatch.setattr(build_offline, 'template_is_clean', lambda path: {})
    monkeypatch.setattr(build_offline, 'input_files', lambda path: {})
    output = tmp_path / 'package.tar.gz'
    with pytest.raises(RuntimeError, match='QUOTE_RUNTIME_MISSING'):
        build_offline.render_package(output, tmp_path / 'wheels')
    assert not output.exists()
