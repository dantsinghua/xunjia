import json
import pytest
from server.negotiation.application_store import ApplicationStore


def test_market_filter_before_pagination_and_private_overlay(tmp_path):
    from server.market import MarketService
    repo = ApplicationStore('sqlite:///' + str(tmp_path/'business.db'), initialize=True)
    repo.bind_account('a','a1','wb')
    repo.bind_account('b','b1','wb')
    rows = [dict(code=f'SYN{i:03}.IB',name='甲' if i%2 else '乙',bondUniCode=str(i),flowId=i,
                 ob=2.4,oo=2.2,obv=1000,oov=2000,val_yield=2.3,status='某人的私有状态',ib=99,
                 bondType='中票',term='2Y',tags=[],clearing='+1') for i in range(1,121)]
    svc = MarketService(repo, rows_provider=lambda channel,broker: rows)
    result=svc.page('a',filter_spec={'keyword':'甲'},limit=50)
    assert result['total']==60 and len(result['data'])==50 and result['hasMore']
    assert all(r['status']=='待询价' and r['ib'] is None for r in result['data'])
    selected=svc.resolve('a',{'selectAll':True,'filter':{'keyword':'甲'},'except':['SYN119.IB']})
    assert len(selected)==59 and all(r['name']=='甲' and r['code']!='SYN119.IB' for r in selected)
    assert svc.page('b',filter_spec={'prices':{'Bid':{'lo':'2.5','hi':''}}})['total']==0


def test_market_unknown_filter_does_not_expand_selection(tmp_path):
    from server.market import MarketService
    repo=ApplicationStore('sqlite:///' + str(tmp_path/'x.db'), initialize=True)
    svc=MarketService(repo,rows_provider=lambda c,b:[])
    with pytest.raises(ValueError):svc.page('a',filter_spec={'channel':'other'})
    with pytest.raises(ValueError):svc.page('a',filter_spec={'chips':{'类型':100}})
    with pytest.raises(ValueError):svc.page('a',filter_spec={'prices':{'Bid':{'lo':'nan'}}})
