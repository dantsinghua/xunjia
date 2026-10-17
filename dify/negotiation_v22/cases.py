"""Synthetic counterpart messages for real-model semantic/latency measurement.

Expected facts are declared separately from both prompts and model output.
Nothing here creates an IM message, command, account or external trade.
"""
from server.negotiation.facts import Quote

DAY = '2026-10-17'


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
    return result


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
