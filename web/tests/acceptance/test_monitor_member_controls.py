"""Requested monitor layout and peer/bond pause/resume, with real API/core and synthetic inputs."""
from copy import deepcopy
from datetime import datetime, timezone
import os
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect
from web.tests.acceptance.test_v22_projection import v22_api, connect, open_monitor, refresh, OWNER


def test_red_panel_is_removed_without_losing_original_three_columns(ui):
    page,url,_,requests,_=ui
    page.clock.install(time=datetime.fromisoformat('2026-10-17T12:00:00+08:00'))
    page.goto(url+'/#/monitor')
    expect(page.locator('.v22-groups')).to_have_count(0)
    expect(page.get_by_text('券组目标与对手独立价量',exact=False)).to_have_count(0)
    for selector in ('.hero','.cp-panel','.chat-panel','.bond-panel'):
        expect(page.locator(selector)).to_be_visible()
    assert not any(urlsplit(r.url).path=='/api/negotiation/state' for r in requests)
    expect(page.get_by_text('市场 Bid（%）',exact=True)).to_have_count(0)
    expect(page.get_by_text('市场 Ofr（%）',exact=True)).to_have_count(0)


def quote(side,kind,price,volume):
    return {'side':side,'price':None if price is None else {'kind':kind,'value':price},
        'volume_wan':volume,'assertion':'QUOTE','validity':'ACTIVE','finality':'NEGOTIABLE',
        'confirmed_day':'2026-10-17','revision':4,'evidence':{}}


@pytest.mark.parametrize('own_side,kind,price,volume,display',[
    ('BUY','YIELD','3.10','1250.5','3.10%'),
    ('SELL','YIELD','-0.05','0','-0.05%'),
    ('BUY','NET_PRICE','99.875','500','99.875 净价'),
    ('SELL','DIRTY_PRICE','101.2','600','101.2 全价'),
    ('BUY','YIELD','0','0','0%'),
    ('BUY','YIELD',None,None,'—'),
])
def test_primary_fields_use_current_counterparty_side_never_market(ui,own_side,kind,price,volume,display):
    page,url,state,_,_=ui
    relevant='ofr' if own_side=='BUY' else 'bid';other='bid' if relevant=='ofr' else 'ofr'
    state['counterparties_data']=[{'id':'qa-peer','name':'当前对手','bonds':[{
        'session_id':'member-current','code':'SYN-A','name':'当前债券','own_side':own_side,
        'member_send_allowed':True,'send_allowed':True,'group_send_allowed':True,
        'state':'OPEN','stage':'询价中','trade_day':'2026-10-17','market_bid':'999.991','market_ofr':'999.992',
        'peer_quote':'999.993','peer_volume_wan':'999.994',
        'quotes':{relevant:quote(relevant,kind,price,volume),other:quote(other,'YIELD','999.995','999.996')}}]}]
    page.clock.install(time=datetime.fromisoformat('2026-10-17T12:00:00+08:00'))
    page.goto(url+'/#/monitor')
    board=page.get_by_test_id('trade-board')
    expect(board.locator('[data-field="peer_quote"]')).to_have_text(display)
    expect(board.locator('[data-field="peer_volume_wan"]')).to_have_text(volume if volume is not None else '—')
    expect(board).not_to_contain_text('999.')
    expect(board).to_contain_text('对手报价');expect(board).to_contain_text('对手报量（万）')


def test_give_up_resume_retains_same_round_quotes_and_other_members(ui,v22_api):
    app,client,groups,_=v22_api;repo=app.state.repository
    group_id=groups['SYN-A']['group_id'];member=groups['SYN-A']['peers'][0]['member_id']
    other,_=repo.add_member(OWNER,group_id,account_uid='synthetic-account',peer_uid='synthetic-other')
    app.state.negotiation_engine.run_once()
    before=repo.snapshot(OWNER)
    original=repo.quote(OWNER,member,'ofr').dump();context=repo.context(OWNER,member)
    _,requests=connect(ui,client);page,card=open_monitor(ui)
    toggle=card.get_by_test_id('bond-negotiation-toggle')
    # Global pause is independent of a member pause; the label must not confuse them.
    expect(toggle).to_have_text('放弃本轮');expect(toggle).to_be_enabled()
    toggle.click();expect(card).to_have_count(0)
    recovery=page.get_by_test_id('paused-bond-actions')
    recovery.locator('summary').click()
    expect(recovery.get_by_role('button',name='独立验收A券 · 恢复本券',exact=True)).to_be_visible()
    current=repo.snapshot(OWNER)
    a=next(g for g in current['groups'] if g['group_id']==group_id)
    assert a['send_allowed'] and next(p for p in a['peers'] if p['member_id']==other)['send_allowed']
    assert not next(p for p in a['peers'] if p['member_id']==member)['send_allowed']
    assert next(g for g in current['groups'] if g['bond_code']=='SYN-B')['peers'][0]['send_allowed']
    recovery.get_by_role('button',name='独立验收A券 · 恢复本券',exact=True).click()
    card.locator('.rb-hd').click()
    expect(toggle).to_have_text('放弃本轮')
    after=repo.snapshot(OWNER)
    assert {(g['group_id'],p['member_id'],p['thread_id']) for g in before['groups'] for p in g['peers']} == {(g['group_id'],p['member_id'],p['thread_id']) for g in after['groups'] for p in g['peers']}
    assert repo.quote(OWNER,member,'ofr').dump()==original and repo.context(OWNER,member)==context
    assert after['send_allowed']==before['send_allowed'] is False
    posts=[r for r in requests if r[0]=='POST']
    assert posts==[('POST','/api/negotiation/members/'+member+'/sending',{'allowed':False}),('POST','/api/negotiation/members/'+member+'/sending',{'allowed':True})]
    toggle.click();expect(card).to_have_count(0)
    page.reload();expect(page.get_by_test_id('paused-bond-actions')).to_be_visible()
    expect(card).to_have_count(0)
    page.get_by_test_id('paused-bond-actions').locator('summary').click()
    expect(page.get_by_role('button',name='独立验收A券 · 恢复本券',exact=True)).to_be_visible()


def test_failed_pause_never_changes_button_or_member_state(ui,v22_api):
    app,client,groups,_=v22_api
    connect(ui,client);page,card=open_monitor(ui)
    member=groups['SYN-A']['peers'][0]['member_id']
    page.route('**/api/negotiation/members/'+member+'/sending',lambda r:r.fulfill(status=503,json={'ok':False,'hint':'合成暂停失败，状态未改'}))
    card.get_by_test_id('bond-negotiation-toggle').click()
    expect(page.get_by_text('合成暂停失败，状态未改',exact=False)).to_be_visible()
    expect(card.get_by_test_id('bond-negotiation-toggle')).to_have_text('放弃本轮')
    expect(card.get_by_test_id('bond-negotiation-toggle')).to_be_enabled()
    projected=client.get('/api/monitor/counterparties').json()['data']
    bond=next(b for cp in projected for b in cp['bonds'] if b['session_id']==member)
    assert bond['member_send_allowed'] is True


def test_resume_continues_from_same_analyzed_context_without_new_round(v22_api,monkeypatch):
    app,client,groups,message_id=v22_api;repo=app.state.repository
    member=groups['SYN-A']['peers'][0]['member_id'];calls=[]
    app.state.negotiation_engine.run_once()
    def decide(context):
        calls.append(deepcopy(context));return {'schema_version':'2.2','action':'WAIT','proposal':None}
    monkeypatch.setattr(app.state.chat_client,'decide',decide)
    assert client.post('/api/bot/toggle',json={'status':'正常'}).status_code==200
    assert client.post('/api/negotiation/members/'+member+'/sending',json={'allowed':False}).status_code==200
    app.state.negotiation_engine.run_once()
    assert not any(c['member_id']==member for c in calls)
    assert client.post('/api/negotiation/members/'+member+'/sending',json={'allowed':True}).status_code==200
    app.state.negotiation_engine.run_once()
    selected=[c for c in calls if c['member_id']==member]
    assert len(selected)==1
    assert selected[0]['quotes']['ofr']['price']['value']=='3.10'
    assert selected[0]['quotes']['ofr']['volume_wan']=='500'
    assert message_id in [m['message_id'] for m in selected[0]['context']]
    assert {g['group_id'] for g in repo.snapshot(OWNER)['groups']}=={g['group_id'] for g in groups.values()}


def test_built_bundle_restores_original_layout_and_counterparty_fields(ui,tmp_path):
    from pathlib import Path
    page,url,state,_,_=ui
    dist=Path(os.environ.get('XJ_TEST_WEB_DIST',str(Path(__file__).resolve().parents[2]/'dist')))
    assert (dist/'index.html').is_file()
    page.set_viewport_size({'width':1920,'height':1080})
    state['counterparties_data']=[{'id':'qa-peer','name':'合成联调对手','bonds':[{
        'session_id':'member-built','code':'SYN-A','name':'合成债券A','own_side':'BUY',
        'member_send_allowed':True,'group_send_allowed':True,'send_allowed':True,'state':'OPEN','stage':'询价中',
        'trade_day':'2026-10-17','own_limit':'3.12','gap_bp':'2',
        'quotes':{'bid':quote('bid','YIELD',None,None),'ofr':quote('ofr','YIELD','3.10','1250.5')}}]}]
    page.route(url+'/',lambda r:r.fulfill(path=str(dist/'index.html'),content_type='text/html'))
    def asset(route):
        path=(dist/urlsplit(route.request.url).path.lstrip('/')).resolve()
        assert path.is_relative_to(dist.resolve()) and path.is_file()
        route.fulfill(path=str(path))
    page.route(url+'/assets/**',asset)
    page.clock.install(time=datetime.fromisoformat('2026-10-17T12:00:00+08:00'))
    page.goto(url+'/#/monitor')
    expect(page.get_by_test_id('v22-groups')).to_have_count(0)
    expect(page.get_by_test_id('trade-board').locator('[data-field="peer_quote"]')).to_have_text('3.10%')
    expect(page.get_by_test_id('trade-board').locator('[data-field="peer_volume_wan"]')).to_have_text('1250.5')
    expect(page.get_by_test_id('bond-negotiation-toggle')).to_have_text('放弃本轮')
    hero=page.locator('.hero').bounding_box();columns=page.locator('.cols').bounding_box()
    assert hero and columns and 0 <= columns['y']-hero['y']-hero['height'] < 40
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(tmp_path/'built-monitor.png'),full_page=True)
