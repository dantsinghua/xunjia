"""The distributable suite must carry its real offline isolation boundary."""
from pathlib import Path

from deploy.build_offline import input_files


ROOT = Path(__file__).resolve().parents[2]


def test_package_includes_the_unchanged_root_offline_conftest():
    files = input_files(ROOT)
    source = ROOT / 'tests/conftest.py'
    assert files.get('tests/conftest.py') == source, (
        'Package QA must retain the root environment/state/network isolation fixture')
    assert [name for name, path in files.items() if path == source] == ['tests/conftest.py']
    assert source.is_file() and not source.is_symlink()
