"""Real Vue plus real current HTTP handlers: reset, new identity and offline cache."""
from urllib.parse import urlsplit
from playwright.sync_api import expect
from tests.institution_test_support import selection, OWNER
from tests.cutover.test_fresh_workspace import seed_command, counts


def connect(page,api):
    runtime=api.app.state.institution
    runtime.background_enabled=True;runtime.start()
    seen=[]
    def forward(route):
        request=route.request;parsed=urlsplit(request.url)
        url=parsed.path+('?' + parsed.query if parsed.query else '')
        body=request.post_data_json if request.method in ('POST','PUT') else None
        seen.append((request.method,url))
        response=api.request(request.method,url,json=body)
        route.fulfill(status=response.status_code,json=response.json())
    page.route('**/api/im/**',forward)
    return seen


def test_ui_account_switch_clears_unknown_and_old_conversation_without_manual_pause(ui,selection):
    page,base,*_=ui;s=selection;seed_command(s,'UNKNOWN')
    with s['repository'].write(OWNER) as tx:tx.execute('UPDATE xj2_owner SET send_allowed=1 WHERE owner_id=?',(OWNER,))
    connect(page,s['api']);page.goto(base+'/#/link')
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-a')
    page.get_by_test_id('collector-workbench').select_option('wb-b')
    page.get_by_test_id('collector-account').select_option('account-c')
    page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-c')
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('peer-wb-b-account-c')
    expect(page.get_by_test_id('collector-account-settings').get_by_role('alert')).to_have_count(0)
    assert all(n==0 for n in counts(s).values())
    assert not s['repository'].binding_for(OWNER)['bot_enabled']
    page.reload()
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-c')


def test_ui_replacing_connection_clears_old_selection_and_requests_new_selection(ui,selection):
    page,base,*_=ui;s=selection;seed_command(s)
    connect(page,s['api']);page.goto(base+'/#/link')
    expect(page.get_by_test_id('collector-current')).to_contain_text('account-a')
    page.locator('[name=mq-base-url]').fill('https://new-configuration.invalid')
    page.get_by_test_id('mq-save').click()
    expect(page.get_by_test_id('collector-current')).to_have_count(0)
    expect(page.get_by_test_id('mq-config')).to_contain_text('已更新配置并清空旧业务')
    assert s['repository'].binding_for(OWNER) is None and all(n==0 for n in counts(s).values())
    assert s['app'].state.transport.adapters=={}


def test_ui_offline_read_is_available_and_keeps_same_binding_and_unknown(ui,selection):
    page,base,*_=ui;s=selection;seed_command(s);binding=s['repository'].binding_for(OWNER)
    s['advance'](61);connect(page,s['api']);page.goto(base+'/#/link')
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    read=page.get_by_test_id('collector-read-saved');expect(read).to_be_enabled();read.click()
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('过期缓存')
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('peer-wb-a-account-a')
    assert counts(s)['xj2_command']==1 and s['repository'].binding_for(OWNER)==binding
