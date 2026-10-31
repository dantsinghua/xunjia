"""QT panel on the real Vue collection page; only HTTP edges are intercepted."""
import json
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect


@pytest.fixture
def qt(ui):
    page,base,*_=ui
    state={'revision':'qt-r1','mode':'fix','shared':True,'enabled':False,'configured':True,'mapped':True,
        'host':'qt.synthetic.invalid','port':8443,'username':'synthetic-fix-account','password_set':True,
        'sender_comp_id':'BANK','target_comp_id':'QTRADE','transport':'tls','plaintext_confirmed':False,
        'server_name':'','ca_file':'','client_cert_file':'','client_key_file':'','heartbeat_seconds':60,
        'connect_timeout_seconds':10,'encoding':'utf-8','sending_timezone':'Asia/Shanghai','timezone':'Asia/Shanghai',
        'qt_account_ids':['qt-self'],'mapped_account_count':2,'collector_active':False,'collector_state':'stopped',
        'code':'QT_STOPPED','note':'synthetic QT stopped','fix_session_connected':False,
        'stats':{'quotes_received':0,'quotes_written':0,'duplicate_quotes':0,'rejected_quotes':0,'reconnects':0}}
    calls=[]
    def route(r):
        req=r.request;path=urlsplit(req.url).path;body=req.post_data_json if req.post_data else None
        calls.append((req.method,path,body))
        if req.method!='GET' and body.get('expected_revision')!=state['revision']:
            r.fulfill(status=400,content_type='application/json',body=json.dumps({'ok':False,'hint':'QT_CONFIG_CHANGED_RELOAD'}));return
        if req.method=='PUT':
            for k,v in body.items():
                if k not in ('password','expected_revision'):state[k]=v
            state['revision']='qt-r2'
        if path.endswith('/start'):state.update(collector_state='streaming',collector_active=True,fix_session_connected=True,revision='qt-started',enabled=True)
        if path.endswith('/stop'):state.update(collector_state='stopped',collector_active=False,fix_session_connected=False,revision='qt-stopped',enabled=False)
        data={'ok':True,'note':'synthetic FIX logon/logout verified','saved':False} if path.endswith('/test') else state
        r.fulfill(content_type='application/json',body=json.dumps({'ok':True,'data':data}))
    page.route(base+'/api/im/qt-quote**',route)
    page.goto(base+'/#/link')
    page.locator('#settings-nav-qt').click()
    expect(page.get_by_test_id('qt-save')).to_be_enabled()
    return page,base,state,calls


def test_account_ids_and_secret_flags_in_same_collection_page(qt):
    page,_,_,_=qt
    expect(page.get_by_test_id('qt-config')).to_be_visible()
    expect(page.get_by_test_id('inno-config')).to_be_hidden()
    page.locator('#settings-nav-innodealing').click()
    expect(page.get_by_test_id('inno-config')).to_be_visible()
    page.locator('#settings-nav-qt').click()
    expect(page.locator('[name=qt-username]')).to_have_value('synthetic-fix-account')
    expect(page.locator('[name=qt-accounts]')).to_have_value('qt-self')
    expect(page.locator('[name=qt-password]')).to_have_value('')
    expect(page.locator('[name=qt-password]')).to_have_attribute('type','password')
    expect(page.get_by_test_id('qt-config')).to_contain_text('连接配置和启停状态共用')


def test_save_cas_secret_clear_no_implicit_start_or_browser_storage(qt):
    page,_,_,calls=qt
    page.locator('[name=qt-password]').fill('synthetic-new-secret')
    page.locator('[name=qt-accounts]').fill('qt-self\nqt-new-self')
    page.get_by_test_id('qt-save').click()
    expect(page.locator('[name=qt-password]')).to_have_value('')
    writes=[x for x in calls if x[0]!='GET']
    assert len(writes)==1 and writes[0][0]=='PUT'
    assert writes[0][2]['expected_revision']=='qt-r1'
    assert writes[0][2]['qt_account_ids']==['qt-self','qt-new-self']
    assert 'account_owners' not in writes[0][2] and 'owner_id' not in writes[0][2]
    assert 'synthetic-new-secret' not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')


def test_start_navigation_and_return_never_stop_background_source(qt):
    page,base,state,calls=qt
    page.get_by_test_id('qt-start').click()
    expect(page.get_by_test_id('qt-stop')).to_be_enabled()
    expect(page.get_by_test_id('qt-save')).to_be_disabled()
    page.goto(base+'/#/quote')
    expect(page.locator('.mkt-row').first).to_be_visible()
    assert state['collector_active'] and not any(x[1].endswith('/stop') for x in calls)
    page.goto(base+'/#/link')
    page.locator('#settings-nav-qt').click()
    expect(page.get_by_test_id('qt-state')).to_contain_text('持续接收中')
    page.get_by_test_id('qt-stop').click()
    expect(page.get_by_test_id('qt-start')).to_be_enabled()
    assert [p for method,p,_ in calls if method=='POST']==['/api/im/qt-quote/start','/api/im/qt-quote/stop']


def test_probe_only_does_not_persist_enable_or_change_form(qt):
    page,_,state,calls=qt
    page.get_by_test_id('qt-test').click()
    expect(page.get_by_test_id('qt-config')).to_contain_text('synthetic FIX logon/logout verified')
    assert not state['collector_active'] and not state['enabled']
    assert [p for method,p,_ in calls if method!='GET']==['/api/im/qt-quote/test']


def test_unsaved_form_never_silently_starts_another_configuration(qt):
    page,_,_,calls=qt
    page.locator('[name=qt-host]').fill('my-draft.invalid')
    page.get_by_test_id('qt-start').click()
    expect(page.get_by_test_id('qt-config').get_by_role('alert')).to_contain_text('请先保存')
    assert not any(x[0]=='POST' for x in calls)


def test_other_user_save_preserves_local_draft_and_stale_save_is_rejected(qt):
    page,_,state,calls=qt
    page.locator('[name=qt-host]').fill('my-draft.invalid')
    state.update(host='shared-new.invalid',revision='another-user-revision')
    expect(page.get_by_test_id('qt-config-conflict')).to_be_visible(timeout=6000)
    expect(page.locator('[name=qt-host]')).to_have_value('my-draft.invalid')
    page.get_by_test_id('qt-save').click()
    expect(page.get_by_test_id('qt-config').get_by_role('alert')).to_contain_text('共享配置已被其他用户修改')
    assert calls[-1][2]['expected_revision']=='qt-r1'
    page.get_by_test_id('qt-reload').click()
    expect(page.locator('[name=qt-host]')).to_have_value('shared-new.invalid')


def test_auth_failure_not_reported_as_connected_and_can_update_credentials(qt):
    page,_,state,_=qt
    state.update(collector_state='auth_failed',fix_session_connected=False,note='synthetic authentication rejected')
    page.get_by_test_id('qt-reload').click()
    expect(page.get_by_test_id('qt-state')).to_contain_text('认证失效')
    expect(page.get_by_test_id('qt-state')).to_contain_text('未连接')
    expect(page.get_by_test_id('qt-save')).to_be_enabled()


def test_tcp_requires_visible_operator_confirmation_and_layout_is_bounded(qt):
    page,_,_,_=qt
    page.set_viewport_size({'width':1366,'height':1000})
    page.locator('[name=qt-transport]').select_option('tcp')
    expect(page.locator('[name=qt-plaintext]')).to_be_visible()
    expect(page.locator('[name=qt-plaintext]')).not_to_be_checked()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
