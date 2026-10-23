"""Public connection/TLS fields reuse server-owned auth; no browser credential editor."""
import json
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect


@pytest.fixture
def mq(ui):
    page, base, *_ = ui
    state = {'editable': True, 'configured': True, 'backend': 'file_gateway',
             'base_url': 'https://mq.synthetic.invalid', 'credentials_set': True,
             'account_uid': 'synthetic-account', 'workbench_id': 'synthetic-wb',
             'revision': 'synthetic-revision-one', 'applied': True, 'note': 'synthetic applied'}
    calls = [];behavior = {'passed': True, 'save_error': None, 'applied': True}
    def route(r):
        req=r.request;path=urlsplit(req.url).path;body=req.post_data_json if req.post_data else None
        calls.append((req.method,path,body))
        if req.method=='PUT':
            if behavior['save_error']:
                r.fulfill(status=409,content_type='application/json',body=json.dumps({'ok':False,'hint':behavior['save_error']}));return
            state.update(base_url=body['base_url'],revision='synthetic-revision-two',applied=behavior['applied'],saved=True,
                         note='配置已应用' if behavior['applied'] else '配置已保存，现役网关尚未加载')
            data=state
        elif path.endswith('/test'):
            data={'passed':behavior['passed'],'connected':behavior['passed'],'saved':False,
                  'steps':[{'name':'pending','ok':behavior['passed'],'detail':'待收文件 7 个，未取件' if behavior['passed'] else 'FILE_AUTH_REJECTED'}]}
        else:data=state
        r.fulfill(content_type='application/json',body=json.dumps({'ok':True,'data':data}))
    page.route(base+'/api/im/mq-config**',route)
    return page,base,state,calls,behavior


def open_form(mq):
    page,base,*_=mq;page.goto(base+'/#/link')
    expect(page.get_by_test_id('mq-config')).to_be_visible()
    expect(page.locator('[name=mq-base-url]')).to_have_value(mq[2]['base_url'])
    return page


def writes(mq):return [call for call in mq[3] if call[0]!='GET']


def test_connection_and_tls_fields_no_sts_bucket_or_secret_readback(mq):
    page=open_form(mq);expect(page.get_by_test_id('mq-config').locator('input,textarea,select')).to_have_count(4)
    expect(page.locator('[name=mq-config-yaml]')).to_have_count(0)
    for name in ('oss-sts-url','oss-endpoint','oss-bucket','oss-auth-key','oss-sm4-key'):
        assert page.locator('[name='+name+']').count()==0


def test_save_public_fields_revision_without_credentials_or_other_operations(mq):
    page=open_form(mq)
    page.locator('[name=mq-base-url]').fill('https://new-mq.synthetic.invalid')
    page.get_by_test_id('mq-save').click();expect(page.locator('[name=mq-config-yaml]')).to_have_count(0)
    values=writes(mq);assert len(values)==1 and values[0][:2]==('PUT','/api/im/mq-config')
    assert set(values[0][2])=={'base_url','expected_revision'}
    assert values[0][2]['expected_revision']=='synthetic-revision-one'
    assert 'synthetic-browser-mq-key' not in page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')


def test_server_credentials_retained_and_public_status_survives_refresh(mq):
    page=open_form(mq);page.get_by_test_id('mq-save').click();expect(page.get_by_test_id('mq-save')).to_be_enabled()
    page.reload();expect(page.get_by_test_id('mq-config')).to_be_visible()
    expect(page.locator('[name=mq-base-url]')).to_have_value(mq[2]['base_url'])
    expect(page.locator('[name=mq-config-yaml]')).to_have_count(0);assert 'config_yaml' not in writes(mq)[0][2]


def test_pending_uses_unsaved_form_without_saving_or_claiming_full_send(mq):
    page=open_form(mq)
    page.locator('[name=mq-base-url]').fill('https://unsaved-mq.synthetic.invalid')
    page.get_by_test_id('mq-test').click();result=page.get_by_test_id('mq-test-result')
    expect(result).to_contain_text('待收文件 7 个');expect(result).to_contain_text('不代表')
    values=writes(mq);assert len(values)==1 and values[0][:2]==('POST','/api/im/mq-config/test')
    assert values[0][2]['base_url'].startswith('https://unsaved-mq.') and mq[2]['base_url']=='https://mq.synthetic.invalid'


def test_failed_pending_displays_error_without_success_claim(mq):
    mq[4]['passed']=False;page=open_form(mq);page.get_by_test_id('mq-test').click()
    expect(page.get_by_test_id('mq-test-result')).to_contain_text('FILE_AUTH_REJECTED')
    expect(page.get_by_test_id('mq-test-result')).not_to_contain_text('检查通过');assert len(writes(mq))==1


def test_saved_not_applied_never_claims_active_connection(mq):
    mq[4]['applied']=False;page=open_form(mq);page.get_by_test_id('mq-save').click();panel=page.get_by_test_id('mq-config')
    expect(panel).to_contain_text('已保存');expect(panel).to_contain_text('尚未加载');expect(panel).not_to_contain_text('已保存并应用')


def test_conflict_visible_without_auto_resubmit(mq):
    mq[4]['save_error']='MQ配置已变化，请重新读取后再保存';page=open_form(mq);page.get_by_test_id('mq-save').click()
    expect(page.get_by_text(mq[4]['save_error'],exact=False)).to_be_visible();assert len(writes(mq))==1


def test_unwired_profile_is_not_a_user_permission_denial(mq):
    mq[2].update(editable=False,applied=False,note='现役网关尚未确认此配置');page=open_form(mq)
    expect(page.get_by_test_id('mq-save')).to_be_enabled();expect(page.get_by_test_id('mq-test')).to_be_enabled()
    expect(page.get_by_test_id('mq-config')).not_to_contain_text('无权')
    assert writes(mq)==[]


def test_im_failure_does_not_hide_mq_configuration(mq):
    page,base,*_=mq
    page.route(base+'/api/im/links',lambda r:r.fulfill(status=503,content_type='application/json',body=json.dumps({'ok':False,'hint':'synthetic IM down'})))
    open_form(mq);expect(page.get_by_test_id('mq-save')).to_be_enabled()


@pytest.mark.parametrize("action", ["save", "test"])
def test_tls_paths_are_submitted_with_current_connection_only(mq, action):
    page = open_form(mq)
    page.locator('[name=mq-client-cert]').fill('/synthetic/tls/client.crt')
    page.locator('[name=mq-client-key]').fill('/synthetic/tls/client.key')
    page.locator('[name=mq-ca-file]').fill('/synthetic/tls/ca.crt')
    page.get_by_test_id('mq-' + action).click()
    expect(page.get_by_test_id('mq-' + action)).to_be_enabled()
    calls = writes(mq)
    assert len(calls) == 1
    payload = calls[0][2]
    assert payload['client_cert_file'] == '/synthetic/tls/client.crt'
    assert payload['client_key_file'] == '/synthetic/tls/client.key'
    assert payload['ca_file'] == '/synthetic/tls/ca.crt'
    assert 'path' not in payload and 'bucket' not in payload and 'config_yaml' not in payload


def test_saved_certificate_paths_are_displayed_on_reload(mq):
    mq[2].update(client_cert_file='/saved/client.crt', client_key_file='/saved/client.key')
    page = open_form(mq)
    expect(page.locator('[name=mq-client-cert]')).to_have_value('/saved/client.crt')
    expect(page.locator('[name=mq-client-key]')).to_have_value('/saved/client.key')
    assert writes(mq) == []


def test_missing_server_credentials_explains_operator_action_without_browser_secret_input_or_writes(mq):
    mq[2].update(credentials_set=False, configured=False, applied=False)
    page = open_form(mq)
    expect(page.get_by_test_id('mq-config')).to_contain_text('服务端尚未配置认证信息，请联系运维完成配置')
    expect(page.get_by_test_id('mq-save')).to_be_disabled()
    expect(page.get_by_test_id('mq-test')).to_be_disabled()
    expect(page.locator('[name=mq-config-yaml]')).to_have_count(0)
    expect(page.get_by_role('button', name='重新读取配置', exact=True)).to_be_enabled()
    page.get_by_role('button', name='重新读取配置', exact=True).click()
    expect(page.get_by_test_id('mq-save')).to_be_disabled()
    assert writes(mq) == []
