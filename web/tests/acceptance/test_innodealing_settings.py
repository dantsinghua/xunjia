"""Browser regression for the restored Innodealing panel; all APIs intercepted."""
import json
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect
@pytest.fixture
def inno(ui):
    page,base,shared,*_=ui
    state={'account':'synthetic-market','sid_set':True,'cookie_set':True,'device_id_set':True,'configured':True,'can_manage':True,'reader_configured':True,'collector_state':'stopped','collector_active':False,'note':'synthetic stopped','stats':{'quotes_received':0,'quotes_written':0},'reader':{'ok':True,'note':'synthetic reader'}}
    calls=[]
    def route(r):
        req=r.request;path=urlsplit(req.url).path;body=req.post_data_json if req.post_data else None
        calls.append((req.method,path,body))
        if path.endswith('/test'):
            data={'ok':True,'wss_open':True,'note':'synthetic WebSocket connected','saved':False,'database':{'ok':True,'note':'synthetic db ready'}}
        else:
            if req.method=='PUT':state['account']=body['account']
            if path.endswith('/start'):state.update(collector_state='streaming',collector_active=True)
            if path.endswith('/stop'):state.update(collector_state='stopped',collector_active=False)
            data=state
        r.fulfill(content_type='application/json',body=json.dumps({'ok':True,'data':data}))
    page.route(base+'/api/im/quote**',route)
    return page,base,state,calls
def open_page(inno):
    page,base,_,_=inno;page.goto(base+'/#/link');expect(page.get_by_test_id('inno-config')).to_be_visible();expect(page.get_by_test_id('inno-save')).to_be_enabled();return page
def test_fields_recovered_no_secret_readback(inno):
    page=open_page(inno)
    expect(page.locator('[name=inno-account]')).to_have_value('synthetic-market')
    for name in ('sid','cookie','device'):
        field=page.locator('[name=inno-'+name+']');expect(field).to_have_value('');expect(field).to_have_attribute('type','password')
        expect(field).to_have_attribute('placeholder','已保存，留空不修改')
def test_save_only_does_not_test_or_start(inno):
    page=open_page(inno);page.locator('[name=inno-cookie]').fill('sid=synthetic-only-cookie')
    page.get_by_test_id('inno-save').click();expect(page.locator('[name=inno-cookie]')).to_have_value('')
    writes=[c for c in inno[3] if c[0]!='GET'];assert len(writes)==1
    assert writes[0][0:2]==('PUT','/api/im/quote')
    assert writes[0][2]['cookie']=='sid=synthetic-only-cookie'
    assert page.evaluate("JSON.stringify(localStorage)").find('synthetic-only-cookie')==-1
def test_test_does_not_save(inno):
    page=open_page(inno);page.get_by_test_id('inno-test').click()
    expect(page.get_by_test_id('inno-test-result')).to_contain_text('连接测试通过')
    writes=[c for c in inno[3] if c[0]!='GET'];assert len(writes)==1 and writes[0][1]=='/api/im/quote/test'
def test_start_stop_controls(inno):
    page=open_page(inno);page.get_by_test_id('inno-start').click()
    expect(page.get_by_test_id('inno-stop')).to_be_enabled();expect(page.get_by_test_id('inno-save')).to_be_disabled()
    page.get_by_test_id('inno-stop').click();expect(page.get_by_test_id('inno-start')).to_be_enabled()
    assert [c[1] for c in inno[3] if c[0]=='POST']==['/api/im/quote/start','/api/im/quote/stop']
def test_missing_reader_does_not_claim_complete(inno):
    inno[2]['reader_configured']=False;page=open_page(inno)
    expect(page.get_by_test_id('inno-start')).to_be_disabled()
    expect(page.get_by_test_id('inno-config')).to_contain_text('连接测试通过不等于行情已进入工作台')
def test_im_failure_does_not_hide_quote_configuration(inno):
    page,base,_,_=inno
    page.route(base+'/api/im/links',lambda r:r.fulfill(status=503,content_type='application/json',body=json.dumps({'ok':False,'hint':'synthetic IM down'})))
    open_page(inno);expect(page.get_by_test_id('inno-test')).to_be_enabled()

def test_panel_has_no_manager_gate(inno):
    inno[2].pop('can_manage',None)
    page=open_page(inno)
    expect(page.get_by_test_id('inno-test')).to_be_enabled()
    expect(page.get_by_test_id('inno-start')).to_be_enabled()
    expect(page.get_by_test_id('inno-config')).not_to_contain_text('权限')
