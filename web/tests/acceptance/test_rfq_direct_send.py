"""Real Vue + production direct-submit HTTP; temporary DB and synthetic transport."""
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect
from test_rfq_multibond_regression import multibond,open_two,fill_active
from tests.cutover.test_direct_rfq_send import desk,counts,OWNER

def ready(multibond):
    page,modal=open_two(multibond)
    fill_active(modal)
    modal.get_by_test_id('rfq-opening').fill('1.10')
    modal.locator('.bond').nth(1).locator('.bond-hd').click()
    fill_active(modal)
    modal.get_by_test_id('rfq-side').select_option('SELL')
    modal.get_by_test_id('rfq-opening').fill('2.90')
    return page,modal

def bridge(page,client,*,lose_first=False):
    seen=[]
    def submit(route):
        body=route.request.post_data_json;seen.append(body)
        response=client.post('/api/im/rfq/send-direct',json=body)
        if lose_first and len(seen)==1:
            assert response.status_code==200,response.text
            route.abort('failed');return
        route.fulfill(status=response.status_code,json=response.json())
    def receipt(route):
        response=client.get(urlsplit(route.request.url).path)
        route.fulfill(status=response.status_code,json=response.json())
    page.route('**/api/im/rfq/send-direct',submit)
    page.route('**/api/im/receipts/*',receipt)
    return seen

def test_direct_click_skips_preview_and_queues_combined_text_once(multibond,tmp_path):
    with desk(tmp_path) as (app,client,transport):
        page,modal=ready(multibond);seen=bridge(page,client)
        button=modal.get_by_test_id('rfq-direct-send');expect(button).to_be_enabled()
        button.click()
        expect(modal.get_by_test_id('rfq-submission')).to_contain_text('已入队，尚未发送')
        assert len(seen)==1 and multibond[0][4]['/api/im/rfq/preview']==0
        assert counts(app)['xj2_command']==1 and transport.sent==[]
        assert len(seen[0]['items'])==2
        assert seen[0]['items'][0]['opening_yield_pct']=='1.10'
        assert seen[0]['items'][1]['opening_yield_pct']=='2.90'
        expect(modal.get_by_test_id('rfq-opening')).to_be_disabled()
        button.click()
        assert len(seen)==1 and counts(app)['xj2_command']==1

def test_lost_direct_response_retries_same_request_and_receipt_becomes_sent(multibond,tmp_path):
    with desk(tmp_path) as (app,client,transport):
        page,modal=ready(multibond);seen=bridge(page,client,lose_first=True)
        modal.get_by_test_id('rfq-direct-send').click()
        expect(modal.get_by_role('button',name='重试核对本次发送',exact=True)).to_be_enabled()
        expect(modal).to_contain_text('提交结果未确认')
        modal.get_by_test_id('rfq-direct-send').click()
        expect(modal.get_by_test_id('rfq-submission')).to_contain_text('已入队')
        assert len(seen)==2 and seen[0]==seen[1] and counts(app)['xj2_command']==1
        app.state.dispatcher.run_once()
        modal.get_by_test_id('rfq-direct-send').click()
        # Quote page closes its modal only after the real receipt becomes SENT.
        expect(modal).to_have_count(0)
        assert len(transport.sent)==1
        cid=transport.sent[0]['command_id']
        assert app.state.inquiry.commands_core.get(OWNER,cid)['state']=='SENT'

def test_preview_flow_still_available_without_initial_price_comparison(multibond):
    page,modal=ready(multibond)
    expect(modal.get_by_test_id('rfq-preview')).to_be_enabled()
    modal.get_by_test_id('rfq-preview').click()
    expect(modal.get_by_test_id('rfq-server-preview')).to_be_visible()
    expect(modal.get_by_test_id('rfq-confirm')).to_be_visible()
    expect(modal.get_by_test_id('rfq-direct-send')).to_have_count(0)
    assert multibond[0][4]['/api/im/rfq/preview']==1

def test_direct_is_disabled_until_all_bond_fields_and_recipients_are_complete(multibond):
    page,modal=open_two(multibond)
    expect(modal.get_by_test_id('rfq-direct-send')).to_be_disabled()
    fill_active(modal)
    expect(modal.get_by_test_id('rfq-direct-send')).to_be_disabled()
    modal.locator('.bond').nth(1).locator('.bond-hd').click();fill_active(modal)
    expect(modal.get_by_test_id('rfq-direct-send')).to_be_enabled()
