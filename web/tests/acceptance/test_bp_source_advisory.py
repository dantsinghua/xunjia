"""Recorded real three-bond source through actual factory/QB/REST/Vue, no calendar.

Only MySQL and readonly address-book I/O are fixtures. Browser APIs forward to
actual isolated ASGI; confirmation/send are prohibited. Not live market proof.
"""
import json
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.cutover.test_bp_advisory_dates import boot,policies,OWNER


@pytest.mark.parametrize('boot',['recorded-ui'],indirect=True)
def test_recorded_real_null_date_bp_three_bonds_reach_actual_server_preview_and_enabled_confirmation(boot,browser,frontend_url):
    s=boot;app=s['app'];assert app.state.inquiry.calendar is None and app.state.inquiry.reference_provider is None
    direct=s['client'].get('/api/bonds')
    assert direct.status_code==200,direct.text
    assert len(direct.json()['data'])==3,{'direct':direct.json(),'book_codes':list(s['reader'].source.code2uni),'reader_stats':s['reader'].source.stats}
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
    backend='http://127.0.0.1:'+str(listener.getsockname()[1]);calls=[]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    assert server.started
    context=browser.new_context(viewport={'width':1920,'height':1080})
    token=app.state.auth.issue_token(type('User',(),{'user_id':OWNER,'display_name':'隔离三券BP验收'})())
    context.add_init_script("localStorage.setItem('xj_auth_token',"+json.dumps(token)+")")
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    def forward(route):
        req=route.request;p=urlsplit(req.url)
        if not req.url.startswith(frontend_url+'/'):route.abort();return
        if not p.path.startswith('/api/'):route.continue_();return
        if req.method not in ('GET','HEAD','OPTIONS'):
            assert p.path in ['/api/bonds/resolve','/api/im/rfq/preview'],'No financial confirmation/send allowed'
        assert p.path!='/api/im/rfq/send'
        calls.append((req.method,p.path))
        response=route.fetch(url=backend+p.path+('?' + p.query if p.query else ''))
        route.fulfill(response=response)
    page.route('**/*',forward);page.route_web_socket('**/*',lambda ws:None)
    try:
        page.goto(frontend_url+'/#/quote')
        expect(page.locator('.mkt-row')).to_have_count(3,timeout=20000)
        for cb in page.locator('.mkt-row input[type=checkbox]').all():cb.check()
        page.get_by_role('button',name='开始询价',exact=True).click()
        modal=page.get_by_test_id('rfq-modal');expect(modal).to_be_visible()
        for policy in policies(s):
            card=modal.locator('.bond').filter(has=page.locator('.bond-hd b',has_text=policy['name']))
            card.locator('.bond-hd').click()
            expect(modal.get_by_test_id('rfq-pricing-basis')).to_have_value('bp')
            modal.get_by_test_id('rfq-side').select_option('BUY')
            expect(modal.get_by_test_id('rfq-limit')).to_have_value(policy['limit_yield_pct'])
            for field,value in [('volume','1000'),('opening',policy['opening_yield_pct']),('step','2')]:
                modal.get_by_test_id('rfq-'+field).fill(value)
            modal.locator('.person[data-uin="synthetic-peer"]').click()
            expect(modal.get_by_test_id('rfq-valuation')).to_contain_text('日期仅作提示')
        expect(modal.get_by_test_id('rfq-preview')).to_be_enabled()
        modal.get_by_test_id('rfq-preview').click()
        preview=modal.get_by_test_id('rfq-server-preview');expect(preview).to_be_visible()
        expect(preview.get_by_test_id('rfq-valuation-snapshot')).to_have_count(3)
        for item in preview.get_by_test_id('rfq-valuation-snapshot').all():
            expect(item).to_contain_text('Innodealing 中债估值')
            expect(item).to_contain_text('估算参考')
            expect(item).to_contain_text('未提供')
        expect(preview.get_by_test_id('rfq-valuation-advisory')).to_have_count(3)
        expect(modal.get_by_test_id('rfq-confirm')).to_be_enabled()
        assert [v for v in calls if v[0]=='POST']==[('POST','/api/bonds/resolve'),('POST','/api/im/rfq/preview')]
        with app.state.repository.db.transaction(write=False) as tx:
            assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0
            assert tx.execute('SELECT COUNT(*) AS n FROM xj2_group').fetchone()['n']==0
        for policy in policies(s):
            assert app.state.inquiry.valuation(policy['code'])[0].trading_date is None
        assert errors==[]
    finally:
        page.unroute_all(behavior='wait');context.close();server.should_exit=True;thread.join(10);listener.close()
        assert not thread.is_alive()
