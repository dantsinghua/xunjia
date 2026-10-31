"""Isolated QA workflow regression; synthetic records, no real model or sending."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from server.market import MarketService
from server.qa_workflow import QAWorkflow, WorkflowError


NOW = datetime(2026, 10, 30, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def workflow(tmp_path, *, quotes=(), rows=None, qt=None):
    public = rows if rows is not None else [dict(code='000001.IB', name='合成债A', bondUniCode='1',
        flowId=1, issueTime=NOW * 1000, ob=9.99, oo=9.98, clearing='+1')]
    market = MarketService(SimpleNamespace(market_overlays=lambda owner: ({}, {'000001.IB': {
        'kind': 'yield', 'value': '123.456', 'private': 'secret-floor'}})),
        rows_provider=lambda channel, broker: deepcopy(public),
        detail_provider=lambda code: {'quotes': deepcopy(quotes)}, qt_provider=qt)
    flow = QAWorkflow(market, config_path=tmp_path/'config/qa-workflow.json', clock=lambda: NOW)
    flow.save(dict(enabled=True, base_url='https://dify.invalid', tool_base_url='https://app.invalid/xunjia',
                   app_key='synthetic-key', expected_revision=''))
    return flow


def read(flow, action='best', owner='alice', queries=None):
    _, token = flow._capability(owner, flow._load())
    return flow.read_market(token, {'action': action, 'queries': ['000001.IB'] if queries is None else queries})


def quote(bid=2.0, ofr=2.2, at=NOW-60, source='synthetic-old', clearing='+1'):
    return dict(best_bid=bid, best_ofr=ofr, best_bid_vol=1000, best_ofr_vol=2000,
                channel=source, updated_at=at, clearing=clearing)


def test_latest_uses_per_side_newest_real_quote_while_best_uses_yield_direction(tmp_path):
    flow = workflow(tmp_path, quotes=[quote(), quote(2.1, 2.1, NOW-1, 'synthetic-new')])
    best = read(flow)['data']['groups'][0]['candidates'][0]
    latest = read(flow, 'latest')['data']['groups'][0]['candidates'][0]
    assert (best['bid'], best['ofr']) == (2.0, 2.2)
    assert (latest['bid'], latest['ofr']) == (2.1, 2.1)
    assert latest['bid_source'] == latest['ofr_source'] == 'synthetic-new'
    assert '123.456' not in json.dumps(latest) and 'secret-floor' not in json.dumps(latest)


def test_today_merged_timestamp_cannot_revive_old_or_future_side_prices(tmp_path):
    flow = workflow(tmp_path, quotes=[quote(0.01, 8.88, NOW-86400),
                                      quote(0.02, 8.89, NOW+1), quote(None, 2.2)])
    row = read(flow)['data']['groups'][0]['candidates'][0]
    assert row['bid'] is None and row['ofr'] == 2.2


def test_snapshot_without_verifiable_quote_details_does_not_invent_prices(tmp_path):
    row = read(workflow(tmp_path))['data']['groups'][0]['candidates'][0]
    assert row['bid'] is row['ofr'] is None


def test_quote_source_failure_is_sanitized_instead_of_exposing_connection_details(tmp_path):
    flow = workflow(tmp_path)
    def unavailable(code):raise OSError('synthetic-private-host-and-password')
    flow.market.detail_provider = unavailable
    with pytest.raises(WorkflowError, match='^QA_MARKET_UNAVAILABLE$'):
        read(flow)


def test_different_settlement_quotes_do_not_compete_with_same_day_basis(tmp_path):
    flow = workflow(tmp_path, quotes=[quote(), quote(0.01, 8.88, clearing='+0')])
    row = read(flow)['data']['groups'][0]['candidates'][0]
    assert (row['bid'], row['ofr'], row['clearing']) == (2.0, 2.2, '+1')


def test_capability_rejects_forgery_expiry_replay_and_send_action(tmp_path):
    flow = workflow(tmp_path, quotes=[quote()])
    nonce, token = flow._capability('alice', flow._load())
    with pytest.raises(WorkflowError): flow.read_market(token+'x', {'action': 'best', 'queries': ['000001.IB']})
    with pytest.raises(ValueError): flow.read_market(token, {'action': 'send', 'queries': ['000001.IB']})
    with pytest.raises(ValueError): flow.read_market(token, {'action': 'best', 'queries': ['000001.IB'], 'owner': 'bob'})
    for _ in range(10): flow.read_market(token, {'action': 'best', 'queries': ['000001.IB']})
    with pytest.raises(WorkflowError): flow.read_market(token, {'action': 'best', 'queries': ['000001.IB']})
    with flow._db() as db: db.execute('UPDATE qa_run SET closed=1 WHERE nonce=?', (nonce,))
    with pytest.raises(WorkflowError): flow.read_market(token, {'action': 'smalltalk', 'queries': []})
    _, token = flow._capability('alice', flow._load())
    flow.clock = lambda: NOW+181
    with pytest.raises(WorkflowError): flow.read_market(token, {'action': 'best', 'queries': ['000001.IB']})


def test_answer_only_uses_verified_receipt_and_closes_callback_token(tmp_path):
    flow = workflow(tmp_path, quotes=[quote()])
    seen = {}
    class Opener:
        def open(self, request, timeout):
            seen['url'] = request.full_url
            body = json.loads(request.data)
            assert body['response_mode'] == 'blocking'
            assert body['inputs']['tool_url'] == 'https://app.invalid/xunjia/api/agent/tools/qa_market/query'
            seen['token'] = body['inputs']['capability']
            receipt = flow.read_market(seen['token'], {'action': 'inquiry', 'queries': ['000001.IB']})
            raw = json.dumps({'data': {'status': 'succeeded', 'outputs': {
                'receipt_id': receipt['receipt_id'], 'answer': '已自动发送；Bid 999%'}}}).encode()
            return Response(raw)
    class Response:
        status = 200
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return self.raw[:limit]
    flow.opener = Opener()
    result = flow.answer('alice', '我想询价合成债A')
    assert seen['url'] == 'https://dify.invalid/v1/workflows/run'
    assert result['requires_selection'] is True and result['candidates'][0]['code'] == '000001.IB'
    assert '999' not in result['answer'] and '已自动发送' not in result['answer']
    assert '2%' in result['answer'] and '尚未发送' in result['answer']
    with pytest.raises(WorkflowError): flow.read_market(seen['token'], {'action': 'best', 'queries': ['000001.IB']})


def test_fabricated_dify_receipt_fails_closed(tmp_path):
    flow = workflow(tmp_path)
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({'data': {'status': 'succeeded', 'outputs': {'receipt_id': 'f'*48}}}).encode()
    flow.opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
    with pytest.raises(WorkflowError, match='QA_UNVERIFIED_FACTS'): flow.answer('alice', '报价')


def test_smalltalk_does_not_read_market_and_config_never_returns_credentials(tmp_path):
    flow = workflow(tmp_path)
    flow.market.page = lambda *args, **kwargs: pytest.fail('smalltalk queried market')
    receipt = read(flow, 'smalltalk', queries=[])
    result = flow.render(receipt['data'])
    assert result['action'] == 'smalltalk' and not result['requires_selection'] and result['candidates'] == []
    state = flow.status()
    assert state['app_key_set'] and 'synthetic-key' not in json.dumps(state)
    assert flow.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='已变化'):
        flow.save(dict(enabled=False, expected_revision='stale'))


def test_real_qt_projection_keeps_owner_and_quote_time_while_ignoring_own_or_ended_quotes(tmp_path):
    from server.qt_quotes import QtQuoteBook
    from tests.developer.test_qt_quote_views_20261028 import record
    book = QtQuoteBook(tmp_path/'qt.sqlite', source_id='synthetic-qa',
                       account_owners={'qt-alice': 'alice', 'qt-bob': 'bob'}, clock=lambda: NOW)
    rows = [record('old', BidPx='1.99', OfferPx='2.21'),
            record('new', BidPx='2.02', OfferPx='2.18', TransactTime='20261030-09:59:59'),
            record('other-owner', OwnQtNO='qt-bob', BidPx='0.01', OfferPx='8.88'),
            record('our-quote', Operator='1', BidPx='0.02', OfferPx='8.89')]
    for number, row in enumerate(rows, 1):
        row.update(BidTradeDate='20261030', OfrTradeDate='20261030')
        if row['NegotiationID'] != 'new': row['TransactTime'] = '20261030-09:59:00'
        book.ingest(row, event_id='qa-'+str(number), sequence=number)
    flow = workflow(tmp_path, quotes=[quote()], qt=book)
    best = read(flow)['data']['groups'][0]['candidates'][0]
    latest = read(flow, 'latest')['data']['groups'][0]['candidates'][0]
    assert (best['bid'], best['ofr'], best['bid_source']) == (1.99, 2.21, 'QT')
    assert (latest['bid'], latest['ofr']) == (2.02, 2.18)
    other = read(flow, owner='bob')['data']['groups'][0]['candidates'][0]
    assert (other['bid'], other['ofr']) == (0.01, 8.88)


def test_full_app_routes_require_login_but_capability_callback_bypasses_ab_catchall(tmp_path, monkeypatch):
    import hashlib
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig
    monkeypatch.setenv('XUNJIA_PREFIX', str(tmp_path))
    monkeypatch.delenv('XUNJIA_STATE_DIR', raising=False)
    flow = workflow(tmp_path, quotes=[quote()])
    app = create_app(database_url='sqlite:///'+str(tmp_path/'app.sqlite'), market=flow.market,
        enable_background=False, agent_service_token='synthetic-ab-service-token',
        auth_config=AuthConfig(sso_secret='synthetic-sso', session_secret='synthetic-qa-session-key-over-32-bytes', clock=lambda: NOW))
    with TestClient(app) as client:
        for method, path in [('GET', '/api/qa/workflow'), ('PUT', '/api/qa/workflow'), ('POST', '/api/qa')]:
            assert client.request(method, path).status_code == 401
        callback = '/api/agent/tools/qa_market/query'
        assert client.post(callback, json={'action': 'smalltalk', 'queries': []}).status_code == 403
        # The real app clock is current wall time; generate its capability accordingly.
        real_flow = QAWorkflow(flow.market, config_path=flow.path)
        _, capability = real_flow._capability('alice', real_flow._load())
        result = client.post(callback, json={'action': 'smalltalk', 'queries': []},
                             headers={'Authorization': 'Bearer '+capability})
        assert result.status_code == 200 and result.json()['data']['action'] == 'smalltalk'
        # The unchanged A/B catch-all rejects this undeclared name as a tool result.
        assert 'receipt_id' not in client.post('/api/agent/tools/qa_market', json={}).json()
        sign = hashlib.md5(f'alice|Alice|{int(NOW)}|synthetic-sso'.encode()).hexdigest()
        token = client.post('/api/auth/sso', json=dict(userName='alice', nickName='Alice', ts=str(int(NOW)), sign=sign)).json()['token']
        headers = {'Authorization': 'Bearer '+token}
        state = client.get('/api/qa/workflow', headers=headers).json()['data']
        assert state['enabled'] and 'synthetic-key' not in json.dumps(state)
        result = client.put('/api/qa/workflow', headers=headers, json=dict(enabled=False, expected_revision=state['revision']))
        assert result.status_code == 200 and not result.json()['data']['enabled']
        result = client.post('/api/qa', headers=headers, json={'question': '报价 000001.IB'})
        assert result.status_code == 200 and result.json()['data']['workflow_enabled'] is False
        assert '未启用' in result.json()['data']['notice']


def test_dsl_code_nodes_execute_strict_contract_and_receipt_validation():
    import yaml
    from pathlib import Path
    from dify.qa_workflow.build import build
    generated = build()
    saved = yaml.safe_load(Path('dify/qa_workflow/workflow.yml').read_text())
    assert generated == saved
    nodes = {node['id']: node['data'] for node in generated['workflow']['graph']['nodes']}
    parse = {}
    exec(nodes['contract']['code'], parse)
    body = json.loads(parse['main']('{"action":"latest","queries":["000001.IB"],"reply":"Bid 999%"}')['body'])
    assert body == {'action': 'latest', 'queries': ['000001.IB']}
    with pytest.raises(ValueError): parse['main']('{"action":"send","queries":["000001.IB"],"reply":""}')
    with pytest.raises(ValueError): parse['main']('{"action":"smalltalk","queries":["000001.IB"],"reply":""}')
    receipt = {}
    exec(nodes['receipt']['code'], receipt)
    assert receipt['main'](json.dumps({'ok': True, 'receipt_id': 'a'*48}), 200) == {'receipt_id': 'a'*48}
    with pytest.raises(ValueError): receipt['main'](json.dumps({'ok': True, 'receipt_id': 'a'*48}), 403)
