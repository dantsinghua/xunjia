from types import SimpleNamespace
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from server.repository import Repository
from server.business import InquiryService
from server.market import MarketService
from server.bond_tools import BondTools


def client(tmp_path,rows=None):
    from server.qa_routes import qa_router
    repo=Repository('sqlite:///'+str(tmp_path/'qa.db'))
    repo.bind_account('alice','a1','wb');repo.bind_account('bob','b1','wb')
    service=InquiryService(repo,clock=lambda:1800000000)
    market=MarketService(repo,rows_provider=(lambda channel,broker:rows) if rows is not None else None)
    app=FastAPI()
    @app.middleware('http')
    async def identity(request:Request,call_next):
        request.state.user=SimpleNamespace(user_id='alice')
        return await call_next(request)
    app.include_router(qa_router(service,market,BondTools()))
    return TestClient(app),service


def test_quote_answer_only_known_values_no_missing_zero(tmp_path):
    c,_=client(tmp_path,[dict(code='230210.IB',name='23国开10',flowId=1,bondUniCode='1',ob=2.4,oo=None)])
    r=c.post('/api/qa',json={'question':'报价 230210.IB'}).json()
    assert r['ok'] is True and '2.4' in r['data']['answer'] and '未接入' in r['data']['answer']
    assert r['data']['chart']['categories']==['Bid']
    assert r['data']['chart']['series'][0]['data']==[2.4]


def test_unconfigured_and_unknown_are_explicit_without_chart(tmp_path):
    c,_=client(tmp_path)
    for q in ('报价 230210.IB','近一个月成交趋势','查券 不存在'):
        answer=c.post('/api/qa',json={'question':q}).json()['data']
        assert not answer.get('chart')
        assert any(s in answer['answer'] for s in ('未配置','未接入','不支持','未找到'))


def test_private_summary_uses_authenticated_owner_and_no_mutation(tmp_path):
    c,service=client(tmp_path,[])
    for owner,peer in [('alice','self-peer'),('bob','other-private-peer')]:
        draft=service.preview(owner,'p-'+owner,[dict(code='230210.IB',name='测试国开',peer_ids=[peer],our_side='BUY',volume_wan='1000',opening_yield_pct='2.4',limit_yield_pct='2.3',step_bp='1')])
        service.confirm(owner,draft['batch_id'],draft['version'],'c-'+owner)
    answer=c.post('/api/qa',json={'question':'我的在谈概要'}).json()['data']['answer']
    assert '230210.IB' in answer and 'self-peer' in answer and 'other-private-peer' not in answer
    assert c.post('/api/qa',json={'question':'我的在谈概要','owner_user_id':'bob'}).status_code==400
    assert c.post('/api/qa',json={'question':'SELECT * FROM xj_message'}).status_code==400
    assert c.post('/api/qa',json={'question':'查询bob的在谈概要'}).json()['data']['answer'].startswith('暂不支持')


def test_reference_lookup_uses_bond_tools_candidates_without_fabricating_quote(tmp_path):
    from server.qa_routes import qa_router
    from tests.developer.test_bond_tools_contract import provider
    c,service=client(tmp_path)
    # Reuse the real tool contract's explicit synthetic provider, never a production fallback.
    app=FastAPI()
    @app.middleware('http')
    async def identity(request:Request,call_next):
        request.state.user=SimpleNamespace(user_id='alice')
        return await call_next(request)
    app.include_router(qa_router(service,MarketService(service.repo),BondTools(provider())))
    answer=TestClient(app).post('/api/qa',json={'question':'查券 示例券'}).json()['data']
    assert '000001.IB' in answer['answer'] and '参考资料候选' in answer['answer']
    assert not answer.get('chart')
