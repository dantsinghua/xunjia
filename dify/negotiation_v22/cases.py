"""Synthetic counterpart messages for real-model semantic/latency measurement.

Expected facts are declared separately from both prompts and model output.
Nothing here creates an IM message, command, account or external trade.
"""
from server.negotiation.facts import Quote
from server.reference_data import ReferenceSnapshot
from datetime import datetime
import time
from zoneinfo import ZoneInfo

DAY = '2026-10-17'


class FixtureClock:
    """Synthetic trading-day origin; elapsed lease/backoff time is monotonic."""
    def __init__(self, *, anchor=None, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.anchor = dict(anchor) if anchor is not None else {
            'epoch': datetime.fromisoformat(DAY + 'T12:00:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp(),
            'monotonic': monotonic(),
        }

    def __call__(self):
        elapsed = self.monotonic() - self.anchor['monotonic']
        if elapsed < 0:
            raise ValueError('The QA clock cannot resume across a reset monotonic origin')
        return self.anchor['epoch'] + elapsed


def cases():
    result = []
    for i in range(18):
        side = 'ofr' if i < 12 else 'bid'
        price = f'3.{10+i%3:02}' if side=='ofr' else f'3.{24+i%3:02}'
        amount = str(500+i*100)
        text = f'SYN-A {price}{"出" if side=="ofr" else "收"}，{amount}万，还可以谈'
        result.append({'id':f'ordinary-{i+1:02}','path':'common','text':text,'our_side':'BUY' if side=='ofr' else 'SELL',
                       'expected':[{'member':'a','side':side,'price':price,'volume':amount,'finality':'NEGOTIABLE'}], 'reply_kind':'REPLY'})
    scenarios = [
        ('decimal-volume','SYN-A 3.10出，0.5亿',[{'member':'a','side':'ofr','price':'3.10','volume':'5000'}]),
        ('bare-volume','SYN-A 10出 5000',[{'member':'a','side':'ofr','price':'3.10','volume':'5000'}]),
        ('two-sided','SYN-A 3.10出500万；3.08收800万',[{'member':'a','side':'ofr','price':'3.10','volume':'500'},{'member':'a','side':'bid','price':'3.08','volume':'800'}]),
        ('multi-bond','SYN-A 3.10出500万；SYN-B 3.05出800万',[{'member':'a','side':'ofr','price':'3.10','volume':'500'},{'member':'b','side':'ofr','price':'3.05','volume':'800'}]),
        ('net-price','SYN-A 净价34.19出500万',[{'member':'a','side':'ofr','price':'34.19','kind':'NET_PRICE','volume':'500'}]),
        ('dirty-price','SYN-A 全价99.32出500万',[{'member':'a','side':'ofr','price':'99.32','kind':'DIRTY_PRICE','volume':'500'}]),
        ('withdraw','SYN-A 撤价，先不出了',[{'member':'a','side':'ofr','validity':'WITHDRAWN'}]),
        ('final-price','SYN-A 3.10出500万，这已经是我能给的最好条件，一点也不可能再让',[{'member':'a','side':'ofr','price':'3.10','volume':'500','finality':'FINAL'}]),
        ('quote-question','SYN-A 能3.10出500万吗？',[{'member':'a','side':'ofr','assertion':'QUESTION'}]),
        ('reported','听说别家有SYN-A 3.10出500万，不是我的报价',[{'member':'a','side':'ofr','assertion':'REPORTED'}]),
        ('quantity-revision','SYN-A 出量改成800万，价格不变',[{'member':'a','side':'ofr','volume':'800'}]),
        ('referenced-confirmation','SYN-A 按你刚才那个3.20收的价，我出500万，不能再改善了',[{'member':'a','side':'ofr','price':'3.20','volume':'500','finality':'FINAL'}]),
        ('bp-minus','SYN-A 中债估值减5BP收1000万',[{'member':'a','side':'bid','price':'2.53','volume':'1000'}]),
        ('bp-plus','SYN-A 中债估值加5BP出1000万',[{'member':'a','side':'ofr','price':'3.04','volume':'1000'}]),
        ('carry-suffix','SYN-A 04出1000万',[{'member':'a','side':'ofr','price':'3.04','volume':'1000'}]),
        ('current-bond-wins','SYN-B 3.05出800万',[{'member':'b','side':'ofr','price':'3.05','volume':'800'}]),
        ('net-settlement','SYN-A 净价34.19出500万，结算日还没定',[{'member':'a','side':'ofr','price':'34.19','kind':'NET_PRICE','volume':'500'}]),
        ('partial-multi','SYN-A 3.10出500万，另一只我再看一下',[{'member':'a','side':'ofr','price':'3.10','volume':'500'}]),
    ]
    for key,text,expected in scenarios:
        entry={'id':key,'path':'contextual','text':text,'our_side':'BUY','expected':expected}
        if key in ('net-price','dirty-price','net-settlement'):entry['reply_kind']='CLARIFY'
        result.append(entry)
    # Fresh conversations legitimately have fewer than the default 20 actual
    # messages; measure that ordinary fastest path as a separate reference.
    for index, ordinary in enumerate(result[:6]):
        result.append({**ordinary,'id':f'fast-initial-{index+1:02}','path':'fast_common','history_count':2})
    result.append({'id':'explicit-history-over-twenty','path':'history','our_side':'BUY','history_count':22,
                   'required_history':True,'text':'SYN-A 按消息 {history_anchor_id} 里你报的那个价，我出500万，不能再改善了。',
                   'expected':[{'member':'a','side':'ofr','price':'3.20','volume':'500','finality':'FINAL'}]})
    for key,text,tool in [
        ('tool-bond-info','SYN-A 3.10出500万。另外请查询公开bond_id=SYN-A的债券资料，确认发行主体后回复。','bond_info'),
        ('tool-similar-bonds','SYN-A 3.10出500万。另外请用相似券工具查询：公开anchor_bond_id=SYN-A，按到期口径剩余0到365天、同发行人，比较剩余天数和估值收益率。','similar_bonds'),
        ('tool-issuer-info','SYN-A 3.10出500万。另外请查询公开issuer_id=SYN-ISSUER的发行主体资料。','issuer_info'),
        ('tool-bond-calc','SYN-A 3.10出500万。另外请用债券计算工具核算：假设同一到期收益率口径，3.10和你之前3.20收的差是多少BP？答复不重复这两个原始收益率，不作成交。','bond_calc'),
    ]:
        result.append({'id':key,'path':'tools','text':text,'our_side':'BUY','required_tool':tool,
                       'expected':[{'member':'a','side':'ofr','price':'3.10','volume':'500'}]})
    return result


def reference_snapshot(case_id):
    """Explicit synthetic public master data, distinct from a live customer feed."""
    source_id='synthetic-measurement'
    def fact(value):
        return {'value':value,'quality':'verified','source_ref':source_id,'as_of':DAY,'missing_reason':None}
    rating={'agency':'Synthetic','scale':'synthetic','value':'AAA','rating_date':DAY}
    industry={'scheme':'synthetic','level':'1','code':'SYN-INDUSTRY'}
    bonds=[]
    for code,issuer,maturity in [('SYN-A','SYN-ISSUER','2027-10-17'),('SYN-B','SYN-ISSUER','2027-09-17'),('SYN-C','SYN-OTHER','2027-08-17')]:
        value='2.5818' if case_id=='bp-minus' and code=='SYN-A' else '2.9872'
        master={'remaining_days':365 if code=='SYN-A' else 335 if code=='SYN-B' else 304,'tenor_basis':'maturity',
                'maturity_date':maturity,'exercise_date':maturity,'balance':'100000','balance_unit':'万','balance_date':DAY,
                'coupon_type':'fixed','coupon_rate_pct':'3.00','issuer_id':issuer,'bond_type':'fixed','industry':industry,
                'bond_rating':rating,'issuer_rating':rating}
        valuation={'provider':'synthetic-valuation','basis':'maturity','yield_pct':value,'clean_price':'100.00',
                   'dirty_price':'100.50','duration':'1.0','valuation_date':DAY,'published_at':DAY+'T09:00:00+08:00'}
        bonds.append({'bond_id':code,'code':code,'name':code,'market':'IB','aliases':[],
                      'valuation_yield_pct':value,'valuation_date':DAY,
                      'master':{key:fact(v) for key,v in master.items()},'valuation':{key:fact(v) for key,v in valuation.items()},'quotes':[]})
    issuer={'issuer_id':'SYN-ISSUER',**{key:fact(value) for key,value in {
        'uscc':'SYNTHETIC-NOT-A-REAL-CREDIT-CODE','full_name':'Synthetic Issuer','short_name':'SYN-ISSUER',
        'aliases':['SYN-ISSUER'],'former_names':[],'registered_region':'Synthetic Region','region_code':'SYN-REGION',
        'ownership':'synthetic','industry':industry,'issuer_rating':rating}.items()}}
    return ReferenceSnapshot('synthetic-measurement-v2',DAY,
        sources=[{'id':source_id,'origin':'explicit synthetic fixture; not a live customer or valuation feed',
                  'transport':'in-memory fixture','dataset_version':'2','as_of':DAY}],bonds=bonds,issuers=[issuer])


def snapshot(case):
    members=[]
    # The ordinary high-frequency path is one active bond / one counterparty,
    # not an artificially inflated multi-bond snapshot used as its reference.
    suffixes=('a','b') if case['id'] in ('multi-bond','current-bond-wins','partial-multi') else ('a',)
    for suffix in suffixes:
        code='SYN-'+suffix.upper()
        history=[]
        for index in range(case.get('history_count',20)):
            inbound=index%2==1
            text=code+' 稍后回复' if inbound else code+' 3.20%收，请报可做价量，数量按万沟通'
            history.append({'message_id':f'{suffix}-h{index}','text':text,'direction':'in' if inbound else 'out',
                            'historical':False,'trade_day':DAY,'kind':'message'})
        old_price='3.04' if case['id']=='carry-suffix' else '3.10'
        quotes={side:Quote(side).dump() for side in ('bid','ofr')}
        quotes['ofr'].update(price={'kind':'YIELD','value':old_price},volume_wan='500',confirmed_day=DAY)
        members.append({'member_id':suffix,'bond_code':code,'trade_day':DAY,'our_side':case['our_side'],'context':history,
                        'quotes':quotes,'plan':{'name':code,'code':code,'our_side':case['our_side'],'trade_day':DAY},
                        'our_quote_json':'{"price":{"kind":"YIELD","value":"3.20"}}'})
    message={'message_id':'current-'+case['id'],'text':case['text'],'direction':'in','historical':False,'kind':'message','trade_day':DAY,
             'source_seq':21,'ingest_revision':21}
    return {'schema_version':'2.2','stage':'analysis','thread_id':'synthetic-'+case['id'],'from_revision':20,'through_revision':21,
            'context_messages':20,'messages':[message],'members':members,'tool_results':[]}


def review_cases():
    """Independent review inputs; the original 47-case corpus stays unchanged."""
    entries = [
        ("review-bp-minus-omitted", "SYN-A 估值-5收5000万", "SELL", [("bid", "2.53", "5000")]),
        ("review-bp-plus-omitted", "SYN-A 估值加5出1000万", "BUY", [("ofr", "2.63", "1000")]),
        ("review-bp-fullwidth", "SYN-A 中债估值－５收５０００万", "SELL", [("bid", "2.53", "5000")]),
        ("review-bp-explicit-control", "SYN-A 估值-5BP收5000万", "SELL", [("bid", "2.53", "5000")]),
        ("review-bp-two-sided", "SYN-A 估值-5收5000万，估值+5出1000万", "BUY", [("bid", "2.53", "5000"), ("ofr", "2.63", "1000")]),
        ("review-negative-yield", "SYN-A 收益率-0.05%出1000万", "BUY", [("ofr", "-0.05", "1000")]),
    ]
    return [{"id": key, "path": "review_regression", "text": text, "our_side": direction,
             "reference_case_id": "bp-minus",
             "expected": [{"member": "a", "side": side, "price": price, "volume": volume}
                          for side, price, volume in expected]}
            for key, text, direction, expected in entries]
