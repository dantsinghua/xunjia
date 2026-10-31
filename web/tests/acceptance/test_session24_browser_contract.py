"""Independent user logout/transport-failure checks; no real backend traffic."""
import json
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.acceptance.test_session24_isolation_contract import private_desk


@pytest.mark.parametrize('failure',['502','offline','401'])
def test_transient_transport_failure_retains_session_but_real_401_expires(ui,failure):
    page,base,_,requests,_=ui
    page.goto(base+'/#/chat')
    expect(page.locator('.q-input')).to_be_visible()
    original=page.evaluate("localStorage.getItem('xj_auth_token')")
    called=[]
    def fail(route):
        called.append(route.request.url)
        if failure=='offline':route.abort('internetdisconnected')
        else:route.fulfill(status=int(failure),json={'ok':False,'hint':'synthetic transport boundary'})
    page.route(base+'/api/qa',fail)
    page.locator('.q-input').fill('只读合成请求，不调用模型')
    expect(page.locator('button.send')).to_be_enabled()
    page.locator('.q-input').press('Enter')
    if failure=='401':
        expect(page.get_by_test_id('xj-login-gate')).to_be_visible()
        assert page.evaluate("localStorage.getItem('xj_auth_token')") is None
    else:
        expect(page.locator('.input-bar [role=alert]')).to_contain_text('synthetic transport boundary' if failure=='502' else 'Failed to fetch')
        expect(page.locator('.md-body')).to_have_count(0)
        expect(page.locator('.q-input')).to_be_visible()
        expect(page.get_by_role('button',name='退出登录',exact=True)).to_be_visible()
        assert page.evaluate("localStorage.getItem('xj_auth_token')")==original
    assert len(called)==1
    assert not [req for req in requests if '/quote/' in req.url and req.method!='GET']


def test_two_browser_users_logout_does_not_stop_or_clear_shared_market(browser,frontend_url,private_desk,monkeypatch):
    desk=private_desk;contexts=[];pages={};requests=[];stops=[]
    async def prohibited_stop(owner):
        stops.append(owner)
        raise AssertionError('Browser logout must not stop shared quote ingestion')
    original_stop=desk['quote'].stop
    monkeypatch.setattr(desk['quote'],'stop',prohibited_stop)
    def route(request_route):
        request=request_route.request;parsed=urlsplit(request.url)
        if not request.url.startswith(frontend_url+'/'):
            request_route.abort();return
        if not parsed.path.startswith('/api/'):
            request_route.continue_();return
        requests.append((request.method,parsed.path))
        if parsed.path=='/api/negotiation/updates':
            request_route.fulfill(status=200,content_type='text/event-stream',body=': synthetic idle stream\n\n');return
        forwarded={k:v for k,v in request.headers.items() if k in ('authorization','x-auth-token','x-xunjia-context','content-type')}
        response=desk['client'].request(request.method,parsed.path+('?' + parsed.query if parsed.query else ''),
            headers=forwarded,content=request.post_data_buffer)
        request_route.fulfill(status=response.status_code,body=response.content,
                              content_type=response.headers.get('content-type','application/json'))
    try:
        for owner in ('alice','bob'):
            context=browser.new_context(viewport={'width':1440,'height':1000});contexts.append(context)
            context.add_init_script("Object.defineProperty(globalThis,'SharedWorker',{value:undefined,configurable:true});Object.defineProperty(globalThis,'Worker',{value:undefined,configurable:true})")
            token=desk['users'][owner]['token']
            context.add_init_script("localStorage.setItem('xj_auth_token',"+json.dumps(token)+")")
            context.route('**/*',route)
            page=context.new_page();pages[owner]=page;page.set_default_timeout(7000)
            page.route_web_socket('**/*',lambda ws:None)
            page.goto(frontend_url+'/#/monitor')
            expect(page.locator('.online b')).to_have_text('Synthetic '+owner)
            expect(page.locator('.cp-panel')).to_contain_text('peer-'+owner)
            expect(page.locator('.cp-panel')).not_to_contain_text('peer-'+('bob' if owner=='alice' else 'alice'))
        alice,bob=pages['alice'],pages['bob']
        alice.get_by_role('button',name='退出登录',exact=True).click()
        expect(alice.get_by_test_id('xj-login-gate')).to_be_visible()
        assert alice.evaluate("localStorage.getItem('xj_auth_token')") is None
        assert bob.evaluate("localStorage.getItem('xj_auth_token')")==desk['users']['bob']['token']
        assert not stops and desk['public'][0]['name']=='shared-public-bond'
        desk['public'][0]['ob']='2.33'
        bob.goto(frontend_url+'/#/quote')
        expect(bob.locator('.online b')).to_have_text('Synthetic bob')
        expect(bob.get_by_text('shared-public-bond',exact=True)).to_be_visible()
        expect(bob.locator('.mkt-row').filter(has_text='shared-public-bond')).to_contain_text('2.33')
        assert not stops
        assert not [(method,path) for method,path in requests if method!='GET' and path.endswith(('/stop','/start'))]
        assert desk['public'][0]['ob']=='2.33'
        assert desk['app'].state.repository.raw_page('alice',desk['users']['alice']['peer']['thread_id'])['messages'][0]['text']=='private-history-alice'
        assert desk['app'].state.repository.raw_page('bob',desk['users']['bob']['peer']['thread_id'])['messages'][0]['text']=='private-history-bob'
    finally:
        for context in contexts:context.close()
        # Process shutdown may legitimately stop its own background collector;
        # only the browser logout interval is subject to this prohibition.
        monkeypatch.setattr(desk['quote'],'stop',original_stop)
