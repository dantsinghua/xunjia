"""Offline QT delivery closure: new runtime modules and clean configuration template."""
from pathlib import Path
import json
import pytest
from deploy import build_offline as build
ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('field',['password','username','host','sender_comp_id','target_comp_id','account_owners'])
def test_qt_template_cannot_ship_filled_site_configuration(tmp_path,field):
    data=json.loads((ROOT/'config/qt-connection.example.json').read_text())
    data[field]={'qt-private':'private-owner'} if field=='account_owners' else 'PRIVATE_CANARY'
    path=tmp_path/'qt.json';path.write_text(json.dumps(data))
    with pytest.raises(build.DeployError,match='QT_TEMPLATE_CONTAINS_SITE_CONFIG'):build.validate_qt_template(path)

@pytest.mark.parametrize('name',['app/collector/qtrade_fix.py','app/server/qt_credentials.py','app/server/qt_quotes.py','config/qt-connection.example.json'])
def test_qt_control_cannot_ship_without_its_required_runtime(name):
    files=build.input_files(ROOT);assert name in files
    build.validate_quote_runtime(files)
    del files[name]
    with pytest.raises(build.DeployError,match='QT_RUNTIME_MISSING'):build.validate_quote_runtime(files)
