"""The user retired NYOSS/nytoken; only the current institutional files path remains configurable."""
from pathlib import Path
import json

from server import transports

ROOT=Path(__file__).resolve().parents[2]


def test_retired_direct_oss_sts_modules_and_probe_are_absent():
    removed=('imlink/oss_http.py','imlink/oss_catalog.py','imlink/oss_send.py',
             'imlink/sts_credentials.py','imlink/sts_request.py','imlink/store.py',
             'imlink/routes.py','imlink/catalog.py','imlink/commands.py','imlink/uplink.py',
             'imlink/client.py','imlink/dify_inquiry.py','scripts/sts_probe.py','server/oss_settings.py')
    assert not any((ROOT/name).exists() for name in removed)
    assert not hasattr(transports,'OSSTransport')


def test_runtime_sources_and_live_examples_have_no_old_service_defaults():
    paths=[p for directory in ('server','relay','imlink') for p in (ROOT/directory).rglob('*.py')]
    paths.extend([ROOT/'config/xunjia.env.example',ROOT/'config/institution-connection.example.json',ROOT/'config/README.md'])
    old=('nytoken.yingmi.com','qtrade-nyoss','oss-cn-hangzhou.aliyuncs.com','its-accesspoint/v1/api/sts')
    for path in paths:
        text=path.read_text()
        assert not any(value in text for value in old),str(path.relative_to(ROOT))


def test_only_current_institution_template_and_file_transport_are_shipped():
    value=json.loads((ROOT/'config/institution-connection.example.json').read_text())
    assert value['contract']=='its-relay-path-v1'
    assert {'base_url','auth_file'} <= value.keys()
    assert not (ROOT/'config/transport.example.json').exists()
    assert not (ROOT/'config/mq-connection.example.json').exists()
    assert not hasattr(transports,'MailTransport')
    assert not hasattr(transports,'MailConfig')
