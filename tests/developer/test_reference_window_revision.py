"""Policy migration: retain prices, ask confirmation, never fetch older rows."""
from copy import deepcopy

from server.negotiation.reply_render import render_reply_plan
from test_v22_analysis_coverage import desk, OWNER


def prepared(tmp_path):
    app,mid,ingest,analyze,quote=desk(tmp_path)
    anchor=ingest(1,'SYN-A 3.10出500万')
    assert analyze([quote(anchor,price={'kind':'YIELD','value':'3.10'},volume_wan='500',
                          assertion='QUOTE',validity='ACTIVE',confirmed_day='2026-10-17')])['accepted']
    for seq in range(2,24):ingest(seq,'SYN-A 稍后交流')
    assert analyze([])['facts_committed']
    assert anchor not in {m['message_id'] for m in app.state.repository.context(OWNER,mid)}
    return app,mid,ingest,analyze,quote,anchor


def test_named_reference_outside_window_cannot_commit_another_price_or_finality(tmp_path):
    app,mid,ingest,analyze,quote,anchor=prepared(tmp_path)
    before=app.state.repository.quote(OWNER,mid,'ofr').dump()
    incoming=ingest(24,'SYN-A 按消息 '+anchor+' 里的报价，我出500万，不能再改善了')
    result=analyze([quote(incoming,price={'kind':'YIELD','value':'3.10'},volume_wan='500',
                          assertion='QUOTE',validity='ACTIVE',finality='FINAL',confirmed_day='2026-10-17')])
    assert result['facts_committed'] and not result['accepted']
    assert result['rejected'][0]['reason']=='REFERENCE_CONFIRMATION_REQUIRED'
    assert not result['repair']['attempted']
    assert app.state.repository.quote(OWNER,mid,'ofr').dump()==before
    engine=app.state.negotiation_engine
    with app.state.repository.db.transaction(write=False) as tx:
        ctx=engine._reply_context(tx,OWNER,mid,{})
    assert ctx['reference_confirmation_required'] is True
    rendered=render_reply_plan(ctx,{'kind':'CLARIFY','question':'ASK_CURRENT_QUOTE'})
    assert '上次记录' in rendered['proposal']['text'] and '3.10' in rendered['proposal']['text']
    assert engine._validate_reply(ctx,rendered['proposal'])['kind']=='CLARIFY'
    assert app.state.repository.quote(OWNER,mid,'ofr').dump()==before


def test_legacy_model_history_request_uses_one_call_and_no_upward_query(tmp_path,monkeypatch):
    app,mid,ingest,_analyze,_quote,anchor=prepared(tmp_path)
    ingest(24,'SYN-A 按那条历史价格出500万')
    store=app.state.repository;engine=app.state.negotiation_engine
    assert engine.context_policy_revision=='latest_quote_confirmation_v1'
    calls=[]
    def model(snapshot):
        calls.append(deepcopy(snapshot))
        return {'schema_version':'2.2','history_requests':[{'member_id':mid,'message_ids':[anchor]}]}
    engine.client.analyze=model
    monkeypatch.setattr(store,'context',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('No upward history query')))
    with store.db.transaction(write=False) as tx:
        thread=store._member(tx,OWNER,mid)['thread_id']
    result=engine.analyze_thread(OWNER,thread,force=True)
    assert len(calls)==1 and result['facts_committed']
    assert result['rejected'][0]['reason']=='REFERENCE_CONFIRMATION_REQUIRED'


def test_explicit_window_gap_takes_priority_but_keeps_the_model_error_in_audit(tmp_path):
    app,mid,ingest,analyze,_quote,anchor=prepared(tmp_path)
    ingest(24,'SYN-A 按消息 '+anchor+' 的报价出500万')
    original={'member_id':mid,'side':'ofr','validation_error':'Unloaded model evidence','validation_path':'observations[0].slots'}
    result=analyze([original])
    assert result['facts_committed'] and not result['accepted']
    assert result['rejected'][0]['reason']=='REFERENCE_CONFIRMATION_REQUIRED'
    assert result['repair']['initial_response']['observations'][0]==original
