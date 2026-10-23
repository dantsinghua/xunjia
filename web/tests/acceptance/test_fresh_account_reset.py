"""Real Vue/current HTTP: retained workspace facts, new identity and offline cache."""
from urllib.parse import urlsplit
from playwright.sync_api import expect
from tests.institution_test_support import selection, OWNER
from tests.cutover.test_fresh_workspace import seed_command, counts


def connect(page,api):
    # Fixture identity is synthetic; context must come only from this browser.
    api.headers.pop('X-Xunjia-Context',None)
    runtime=api.app.state.institution
    runtime.background_enabled=True;runtime.start()
    seen=[]
    def forward(route):
        request=route.request;parsed=urlsplit(request.url)
        url=parsed.path+('?' + parsed.query if parsed.query else '')
        body=request.post_data_json if request.method in ('POST','PUT') else None
        seen.append((request.method,url))
        context=request.header_value('X-Xunjia-Context')
        headers={'X-Xunjia-Context':context} if context is not None else {}
        response=api.request(request.method,url,json=body,headers=headers)
        route.fulfill(status=response.status_code,json=response.json())
    page.route('**/api/im/**',forward)
    page.route('**/api/negotiation/state',forward)
    return seen


def facts(s,thread):
    with s['repository'].db.transaction(write=False) as tx:
        command=dict(tx.execute('SELECT * FROM xj2_command WHERE command_id=?',('old-command',)).fetchone())
        messages=[dict(row) for row in tx.execute('SELECT * FROM xj2_message WHERE thread_id=? ORDER BY ingest_revision',(thread,)).fetchall()]
    return {'counts':counts(s),'command':command,'messages':messages}


def assert_retired(page,s,group,member,thread,before):
    store=s['repository']
    with store.db.transaction(write=False) as tx:
        retired=tx.execute('SELECT archived,send_allowed FROM xj2_group WHERE group_id=?',(group,)).fetchone()
        assert retired['archived']==1 and retired['send_allowed']==0
        assert tx.execute('SELECT send_allowed FROM xj2_peer WHERE member_id=?',(member,)).fetchone()['send_allowed']==0
        assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?',(OWNER,)).fetchone()['send_allowed']==0
    assert before['command']['state']=='UNKNOWN' and before['messages']
    expected={**before,'command':{**before['command'],'cancel_requested':1}}
    assert facts(s,thread)==expected
    assert s['app'].state.inquiry.commands_core.claim(OWNER,'old-command') is None
    assert s['app'].state.dispatcher.run_once()==0
    assert facts(s,thread)==expected
    # Read the actual current-context projections through the browser bridge.
    state=page.evaluate("async () => (await (await fetch('/api/negotiation/state')).json())")
    assert state['ok'] and state['data']['groups']==[] and state['data']['threads']==[]
    unknown=page.evaluate("async () => (await (await fetch('/api/im/commands/unknown')).json())")
    assert unknown=={'ok':True,'data':[]}


def test_ui_account_switch_retains_unknown_and_isolates_old_conversation_without_manual_pause(ui,selection):
    page,base,*_=ui;s=selection;group,member,thread=seed_command(s,'UNKNOWN')
    before=facts(s,thread);binding=s['repository'].binding_for(OWNER)
    with s['repository'].write(OWNER) as tx:tx.execute('UPDATE xj2_owner SET send_allowed=1 WHERE owner_id=?',(OWNER,))
    seen=connect(page,s['api']);page.goto(base+'/#/link')
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-a')
    page.get_by_test_id('collector-workbench').select_option('wb-b')
    page.get_by_test_id('collector-account').select_option('account-c')
    page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-c')
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('peer-wb-b-account-c')
    expect(page.get_by_test_id('collector-account-settings').get_by_role('alert')).to_have_count(0)
    assert_retired(page,s,group,member,thread,before)
    selected=s['repository'].binding_for(OWNER)
    assert selected['context_id']!=binding['context_id']
    assert selected['workbench_id']=='wb-b' and selected['account_uid']=='account-c'
    assert not selected['bot_enabled']
    page.reload()
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-c')
    assert [call for call in seen if call[0]!='GET']==[('POST','/api/im/binding/select')]


def test_ui_replacing_connection_clears_old_selection_and_requests_new_selection(ui,selection):
    page,base,*_=ui;s=selection;group,member,thread=seed_command(s)
    before=facts(s,thread)
    with s['repository'].write(OWNER) as tx:tx.execute('UPDATE xj2_owner SET send_allowed=1 WHERE owner_id=?',(OWNER,))
    seen=connect(page,s['api']);page.goto(base+'/#/link')
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-a')
    page.locator('[name=mq-base-url]').fill('https://new-configuration.invalid')
    page.get_by_test_id('mq-save').click()
    expect(page.get_by_test_id('collector-current')).to_have_count(0)
    expect(page.get_by_test_id('mq-config')).to_contain_text('已更新配置并归档旧工作区')
    expect(page.get_by_test_id('mq-config')).to_contain_text('历史事实和待核对记录保留')
    assert s['repository'].binding_for(OWNER) is None
    assert s['app'].state.transport.adapters=={}
    assert_retired(page,s,group,member,thread,before)
    assert [call for call in seen if call[0]!='GET']==[
        ('PUT','/api/im/mq-config'),('POST','/api/im/catalog/refresh')]


def test_ui_offline_read_is_available_and_keeps_same_binding_and_unknown(ui,selection):
    page,base,*_=ui;s=selection;_,_,thread=seed_command(s);binding=s['repository'].binding_for(OWNER)
    before=facts(s,thread)
    s['advance'](61);seen=connect(page,s['api']);page.goto(base+'/#/link')
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    read=page.get_by_test_id('collector-read-saved');expect(read).to_be_enabled();read.click()
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('过期缓存')
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('peer-wb-a-account-a')
    assert counts(s)['xj2_command']==1 and s['repository'].binding_for(OWNER)==binding
    assert facts(s,thread)==before
    assert [call for call in seen if call[0]!='GET']==[]
