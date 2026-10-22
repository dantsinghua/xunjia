"""Real Vue reset UI contract; synthetic responses, signed/backend gates separate."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect
from test_collector_account_selection import selector, open_selector, pick_b


def dto(pair,phase='requested'):
    value={'reset_id':'synthetic-ui-reset','binding_version':pair['version'],'account_epoch':1,
        'state':phase,'code':'','snapshot_hash':'a'*64 if phase!='requested' else '',
        'scope':{'workbench_id':pair['workbench_id'],'account_uid':pair['account_uid']},
        'created_at':1800000000,'updated_at':1800000001,'snapshot':None}
    if phase!='requested':
        value['snapshot']={'account_epoch':1,'snapshot_at':1800000001,'published_source_highwater':7,
            'peers':{'synthetic-peer':{'source_watermark':7,'receiver_fence_id':'synthetic-fence','receiver_fence_version':4,'min_control_version':2}},
            'unresolved_history':[{'command_id':'old-unknown','peer_id':'synthetic-peer','account_epoch':0,'status':'unknown'}],
            'old_native_tasks_running':1 if phase=='waiting_native' else 0,'pending_inbound_count':0,'pending_echo_count':0}
    return value


@pytest.fixture
def reset_ui(selector):
    page,base,selection,_,_=selector
    s={'status':None,'calls':[],'get_error':False,'post_error':False,'wrong_scope':False,
       'hold_get':False,'hold_post':False,'held':[]}
    def fulfill(route,data,status=200):route.fulfill(status=status,content_type='application/json',body=json.dumps(data))
    def handler(route):
        req=route.request;s['calls'].append((req.method,req.post_data_json if req.post_data else None))
        if (s['hold_get'] and req.method=='GET') or (s['hold_post'] and req.method=='POST'):
            s['held'].append(route);return
        if req.method=='GET':
            if s['get_error']:fulfill(route,{'ok':False,'hint':'synthetic reset status unavailable'},503);return
            value=deepcopy(s['status'])
            if value and s['wrong_scope']:value['scope']['workbench_id']='foreign-workbench'
            fulfill(route,{'ok':True,'data':value});return
        assert req.method=='POST'
        if s['post_error']:fulfill(route,{'ok':False,'hint':'synthetic reset response lost'},503);return
        s['status']=dto(selection['selected'])
        fulfill(route,{'ok':True,'data':s['status']})
    page.route(base+'/api/im/session-reset**',handler)
    def open_page():
        open_selector(selector)
        expect(page.get_by_test_id('session-reset-settings')).to_be_visible()
        return page
    return SimpleNamespace(page=page,base=base,selection=selection,state=s,open=open_page,fulfill=fulfill)


def posts(c):return [body for method,body in c.state['calls'] if method=='POST']


def test_scope_and_confirmation_are_explicit_and_read_refresh_never_posts(reset_ui):
    c=reset_ui;p=c.open();panel=p.get_by_test_id('session-reset-settings')
    expect(panel.get_by_test_id('session-reset-scope')).to_contain_text('wb-a')
    expect(panel.get_by_test_id('session-reset-scope')).to_contain_text('same-account')
    expect(panel).to_contain_text('全部询价会话')
    expect(panel).to_contain_text('历史聊天、发送回执和去重记录保留')
    panel.get_by_test_id('session-reset-refresh').click();assert posts(c)==[]
    panel.get_by_test_id('session-reset-start').click()
    expect(panel.get_by_test_id('session-reset-confirmation')).to_be_visible();assert posts(c)==[]
    panel.get_by_role('button',name='取消',exact=True).click()
    expect(panel.get_by_test_id('session-reset-confirmation')).not_to_be_visible();assert posts(c)==[]


def test_confirm_has_exact_binding_cas_and_only_complete_claims_dual_completion(reset_ui):
    c=reset_ui;p=c.open();panel=p.get_by_test_id('session-reset-settings')
    panel.get_by_test_id('session-reset-start').click();panel.get_by_test_id('session-reset-confirm').click()
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('等待采集端建立')
    assert len(posts(c))==1
    body=posts(c)[0];assert set(body)=={'request_id','expected_binding_version','confirmed'}
    assert body['confirmed'] is True and body['expected_binding_version']==1 and body['request_id']
    c.state['status']=dto(c.selection['selected'],'waiting_native')
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('等待采集端结束',timeout=5000)
    expect(panel.get_by_test_id('session-reset-start')).to_be_disabled()
    expect(panel.get_by_test_id('session-reset-status')).not_to_contain_text('双端会话已清空并完成同步')
    c.state['status']=dto(c.selection['selected'],'local_applied')
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('等待采集端最终确认',timeout=5000)
    c.state['status']=dto(c.selection['selected'],'complete')
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('双端会话已清空并完成同步',timeout=5000)
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('历史未决执行记录')
    assert len(posts(c))==1
    panel.get_by_test_id('session-reset-progress').locator('summary').click()
    expect(panel.get_by_test_id('session-reset-progress')).to_contain_text('synthetic-peer')


def test_wrong_account_scope_status_never_appears_as_current_success(reset_ui):
    c=reset_ui;c.state['status']=dto(c.selection['selected'],'complete');c.state['wrong_scope']=True
    p=c.open();panel=p.get_by_test_id('session-reset-settings')
    expect(panel.get_by_role('alert')).to_contain_text('不属于当前采集端和账号')
    expect(panel.get_by_test_id('session-reset-status')).not_to_be_visible()
    assert posts(c)==[]


def test_complete_status_get_failure_is_not_current_success_and_recovers_without_post(reset_ui):
    c=reset_ui;c.state['status']=dto(c.selection['selected'],'complete')
    p=c.open();panel=p.get_by_test_id('session-reset-settings')
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('双端会话已清空并完成同步')
    c.state['get_error']=True;panel.get_by_test_id('session-reset-refresh').click()
    expect(panel.get_by_role('alert')).to_contain_text('synthetic reset status unavailable')
    expect(panel.get_by_test_id('session-reset-status')).not_to_contain_text('双端会话已清空并完成同步')
    expect(panel).not_to_contain_text('工作台和采集端已确认同一会话版本')
    c.state['get_error']=False
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('双端会话已清空并完成同步',timeout=5000)
    assert posts(c)==[]


def test_post_network_failure_retries_same_id_without_automatic_post(reset_ui):
    c=reset_ui;c.state['post_error']=True;p=c.open();panel=p.get_by_test_id('session-reset-settings')
    panel.get_by_test_id('session-reset-start').click();panel.get_by_test_id('session-reset-confirm').click()
    expect(panel.get_by_role('alert')).to_contain_text('synthetic reset response lost')
    assert len(posts(c))==1;first=posts(c)[0]
    # GET recovery may run; it must never retry a destructive POST on its own.
    p.wait_for_timeout(1700);assert posts(c)==[first]
    c.state['post_error']=False;panel.get_by_test_id('session-reset-confirm').click()
    expect(panel.get_by_test_id('session-reset-status')).to_contain_text('等待采集端建立')
    assert len(posts(c))==2 and posts(c)[1]==first


@pytest.mark.parametrize('late_method',['GET','POST'])
def test_binding_switch_discards_old_response_and_invalidates_confirmation(reset_ui,late_method):
    c=reset_ui;c.state['hold_get']=late_method=='GET';p=c.open();panel=p.get_by_test_id('session-reset-settings')
    if late_method=='POST':
        c.state['hold_post']=True
        panel.get_by_test_id('session-reset-start').click();panel.get_by_test_id('session-reset-confirm').click()
    else:panel.get_by_test_id('session-reset-start').click()
    expect(panel.get_by_test_id('session-reset-confirmation')).to_be_visible()
    assert c.state['held']
    held=c.state['held'].pop(0);old=deepcopy(c.selection['selected'])
    c.state['hold_get']=False;c.state['hold_post']=False
    pick_b(p);p.get_by_test_id('collector-connect').click()
    expect(p.get_by_test_id('collector-current')).to_contain_text('wb-b')
    expect(panel.get_by_test_id('session-reset-confirmation')).not_to_be_visible()
    c.fulfill(held,{'ok':True,'data':dto(old,'complete')})
    expect(panel.get_by_test_id('session-reset-scope')).to_contain_text('wb-b')
    expect(panel.get_by_test_id('session-reset-status')).not_to_be_visible()
    assert len(posts(c))==(1 if late_method=='POST' else 0)


from tests.cutover.test_im_catalog_selection import selection, OWNER


def test_real_browser_api_and_signed_local_gateway_handshake_reach_same_complete_epoch(selection,browser,frontend_url):
    import socket
    import threading
    import time
    import uvicorn
    from urllib.parse import urlsplit
    from relay.collector_bridge import CollectorBridge
    from server.receiver_ledger import ReceiverLedger
    from tests.institution_test_support import collector as make_collector
    s=selection;app=s['app'];binding=app.state.transport.adapters[(OWNER,'account-a','oss')].binding
    s['runtime'].background_enabled=True
    s['runtime'].start()
    collector=make_collector(s,filename='ui-native-transport.sqlite3')
    receiver=ReceiverLedger(s['tmp']/'ui-native-receiver.sqlite3',collector.binding,clock=lambda:s['clock'][0])
    bridge=CollectorBridge(ledger=receiver,adapters={'oss':collector},
        native_send=lambda _:pytest.fail('No native text may be sent by a session reset'),
        native_reconcile=lambda _:None,native_quiescent=lambda:True)
    bridge.publish_capabilities()
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
    backend='http://127.0.0.1:'+str(listener.getsockname()[1])
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    assert server.started
    context=browser.new_context(viewport={'width':1920,'height':1080})
    context.add_init_script("localStorage.setItem('xj_auth_token',"+json.dumps(s['bearer'])+")")
    page=context.new_page();calls=[];errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def forward(route):
        req=route.request;parsed=urlsplit(req.url)
        if not req.url.startswith(frontend_url+'/'):route.abort();return
        if not parsed.path.startswith('/api/'):route.continue_();return
        if parsed.path=='/api/negotiation/updates':route.abort();return
        calls.append((req.method,parsed.path))
        assert req.method in ('GET','HEAD','OPTIONS') or (req.method=='POST' and parsed.path=='/api/im/session-reset'), 'No financial confirm/send or other mutation allowed'
        response=route.fetch(url=backend+parsed.path+('?' + parsed.query if parsed.query else ''))
        route.fulfill(response=response)
    page.route('**/*',forward);page.route_web_socket('**/*',lambda ws:None)
    try:
        page.goto(frontend_url+'/#/link');panel=page.get_by_test_id('session-reset-settings')
        expect(panel).to_be_visible(timeout=20000)
        expect(panel.get_by_test_id('session-reset-scope')).to_contain_text('wb-a')
        expect(panel.get_by_test_id('session-reset-scope')).to_contain_text('account-a')
        panel.get_by_test_id('session-reset-start').click()
        assert not [x for x in calls if x[0]=='POST']
        panel.get_by_test_id('session-reset-confirm').click()
        expect(panel.get_by_test_id('session-reset-status')).to_contain_text('等待采集端建立')
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            app.state.session_reset.run_once();bridge.run_once();app.state.inbox.poll_once()
            current=s['api'].get('/api/im/session-reset').json()['data']
            if current['state']=='local_applied':break
            time.sleep(.2)
        assert current['state']=='local_applied'
        expect(panel.get_by_test_id('session-reset-status')).to_contain_text('等待采集端最终确认',timeout=5000)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            app.state.session_reset.run_once();bridge.run_once();app.state.inbox.poll_once()
            current=s['api'].get('/api/im/session-reset').json()['data']
            if current['state']=='complete':break
            s['advance'](6)
            time.sleep(.2)
        assert current['state']=='complete' and receiver.account_epoch()==1
        assert receiver.reset_status()['state']=='complete'
        from relay.protocol import digest
        assert current['snapshot_hash']==digest(receiver.reset_status()['snapshot'])
        expect(panel.get_by_test_id('session-reset-status')).to_contain_text('双端会话已清空并完成同步',timeout=5000)
        expect(panel.get_by_test_id('session-reset-status')).to_contain_text('会话版本：1')
        assert [x for x in calls if x[0]=='POST']==[('POST','/api/im/session-reset')]
        with app.state.repository.db.transaction(write=False) as tx:
            assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0
        assert errors==[] and page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    finally:
        context.close();server.should_exit=True;thread.join(timeout=10);assert not thread.is_alive()
