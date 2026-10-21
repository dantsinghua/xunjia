"""MQ template/package additions preserve existing secret and automation gates."""
from pathlib import Path
import re

import pytest
from deploy.build_offline import DeployError, input_files, template_is_clean

ROOT=Path(__file__).resolve().parents[2]


def changed_template(tmp_path,key,value):
    text=(ROOT/'config/xunjia.env.example').read_text()
    text,count=re.subn(r'^'+re.escape(key)+r'=.*$',key+'='+value,text,flags=re.MULTILINE)
    assert count==1
    path=tmp_path/'xunjia.env.example';path.write_text(text)
    return path


def test_mq_quote_and_login_template_defaults_remain_secret_free_and_closed():
    values=template_is_clean(ROOT/'config/xunjia.env.example')
    assert values['XUNJIA_MQ_CONNECTION_CONFIG']==''
    assert values['QB_MYSQL_PASSWORD']==''
    assert values['XUNJIA_RUN_BACKGROUND']=='1'
    assert 'XUNJIA_ENABLE_LIVE_TRANSPORT' not in values
    for key in ('XUNJIA_AUTH_DEV_LOGIN','XUNJIA_QUOTE_AUTOSTART'):
        assert values[key]=='0'


def test_quote_database_password_cannot_enter_shipping_template(tmp_path):
    path=changed_template(tmp_path,'QB_MYSQL_PASSWORD','synthetic-template-secret')
    with pytest.raises(DeployError,match='SECRET') as error:template_is_clean(path)
    assert 'synthetic-template-secret' not in str(error.value)


@pytest.mark.parametrize('key',['XUNJIA_AUTH_DEV_LOGIN','XUNJIA_QUOTE_AUTOSTART'])
def test_template_cannot_enable_dev_login_or_quote_autostart(tmp_path,key):
    with pytest.raises(DeployError,match='AUTOMATION'):
        template_is_clean(changed_template(tmp_path,key,'1'))


def test_unregistered_template_fields_still_fail_closed(tmp_path):
    path=tmp_path/'xunjia.env.example'
    path.write_text((ROOT/'config/xunjia.env.example').read_text()+'\nXUNJIA_UNDECLARED_PREVIEW_FIELD=x\n')
    with pytest.raises(DeployError,match='UNKNOWN_TEMPLATE_FIELD'):template_is_clean(path)


def test_files_mq_configuration_example_is_an_offline_package_input():
    relative='config/mq-connection.example.json'
    files=input_files(ROOT)
    assert files[relative]==ROOT/relative and files[relative].is_file()


def test_template_default_dispatch_cannot_be_silently_disabled(tmp_path):
    with pytest.raises(DeployError,match='TEMPLATE_BACKGROUND_DISABLED'):
        template_is_clean(changed_template(tmp_path,'XUNJIA_RUN_BACKGROUND','0'))


def test_obsolete_extra_dispatch_switch_cannot_enter_shipping_template(tmp_path):
    path=tmp_path/'xunjia.env.example'
    path.write_text((ROOT/'config/xunjia.env.example').read_text()+"\nXUNJIA_ENABLE_LIVE_TRANSPORT='0'\n")
    with pytest.raises(DeployError,match='UNKNOWN_TEMPLATE_FIELD'):
        template_is_clean(path)
