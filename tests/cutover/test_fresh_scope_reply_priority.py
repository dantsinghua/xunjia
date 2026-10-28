"""Fresh in-pass inbound scopes reorder only the original rotating reply batch."""
from test_reference_window_policy import DAY, OWNER, Desk
from server.negotiation.prices import LimitPolicy


def desk_with_members(tmp_path,count=6):
    case=Desk(tmp_path/'fresh-priority.sqlite')
    codes={case.member:'SYN-A'}
    for index in range(count-1):
        code='SYN-'+chr(66+index) if index<24 else 'SYN-X'+str(index)
        group=case.store.create_group(OWNER,bond_code=code,our_side='BUY',target_volume='10000',
            trade_day=DAY,policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
        mid,thread=case.store.add_member(OWNER,group,account_uid='synthetic-account',peer_uid='synthetic-peer')
        assert thread==case.thread
        codes[mid]=code
    case.store.set_sending(OWNER,True)
    return case,codes


def test_successfully_analyzed_fresh_inbound_scope_precedes_old_due_members_without_losing_them(tmp_path,monkeypatch):
    case,codes=desk_with_members(tmp_path)
    original=sorted(codes);target=original[-1]
    case.inbound(codes[target]+' 稍等，我再看一下')
    case.flows.analyze=lambda context:{'schema_version':'2.2','observations':[],
        'scopes':{row['message_id']:[target] for row in context['messages']}}
    calls=[]
    def decide(owner,member):
        assert owner==OWNER;calls.append(member)
        return {'state':'RETRY'}
    monkeypatch.setattr(case.engine,'decide_member',decide)
    result=case.engine.run_once(force=True)
    assert calls==[target]+original[:-1], 'Only the fresh scoped member should move ahead of the old batch'
    assert result=={'analyzed':1,'decided':len(original)}
    calls.clear()
    again=case.engine.run_once(force=True)
    assert calls==original and again=={'analyzed':0,'decided':len(original)}


def test_unresolved_and_invalid_fresh_members_remain_in_scope_but_outbound_or_history_do_not_gain_priority(tmp_path,monkeypatch):
    for relation in ('UNRESOLVED','INVALID','outbound','historical'):
        directory=tmp_path/relation;directory.mkdir()
        case,codes=desk_with_members(directory)
        original=sorted(codes);target=original[-1]
        changes=({'direction':'out','native_id':'synthetic-old-outbound'} if relation=='outbound'
                 else {'historical':True,'occurred_at':case.now-1000} if relation=='historical' else {})
        incoming=case.inbound('那只券我再看一下' if relation=='UNRESOLVED' else codes[target]+' 稍等',**changes)
        def analyze(context):
            result={'schema_version':'2.2','observations':[],
                    'scopes':{row['message_id']:[target] for row in context['messages']}}
            if relation=='UNRESOLVED':
                result['scopes']={row['message_id']:[] for row in context['messages']}
                result['unresolved']={row['message_id']:[target] for row in context['messages']}
            elif relation=='INVALID':
                result['observations']=[{'member_id':target,'side':'ofr',
                                         'source_ids':[row['message_id'] for row in context['messages']],
                                         'validation_error':'synthetic source-invalid observation'}]
            return result
        case.flows.analyze=analyze
        calls=[]
        monkeypatch.setattr(case.engine,'decide_member',lambda owner,member:calls.append(member) or {'state':'RETRY'})
        result=case.engine.run_once(force=True)
        if relation in ('UNRESOLVED','INVALID'):
            with case.store.db.transaction(write=False) as tx:
                row=tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',(incoming,target)).fetchone()
            assert row and row['relation']==relation, 'The scenario must exercise real committed scope status'
            assert calls==[target]+original[:-1]
        else:
            assert calls==original, 'A thread watermark change alone does not grant inbound scope priority'
        assert result['decided']==len(original)


def test_priority_stays_inside_the_original_hundred_member_page_and_cursor_follows_original_page_tail(tmp_path,monkeypatch):
    case,codes=desk_with_members(tmp_path,105)
    ordered=sorted(codes);case.engine._reply_cursor=ordered[49]
    original_page=ordered[50:]+ordered[:45]
    target=original_page[-1];outside=ordered[48]
    assert outside not in original_page and len(original_page)==100
    case.inbound(codes[target]+' / '+codes[outside]+' 稍等')
    case.flows.analyze=lambda context:{'schema_version':'2.2','observations':[],
        'scopes':{row['message_id']:[target,outside] for row in context['messages']}}
    calls=[]
    def decide(owner,member):
        calls.append(member)
        return None if member==original_page[0] else {'state':'RETRY'}
    monkeypatch.setattr(case.engine,'decide_member',decide)
    first=case.engine.run_once(force=True)
    assert calls==[target]+original_page[:-1] and outside not in calls
    assert case.engine._reply_cursor==original_page[-1]
    assert first=={'analyzed':1,'decided':99}
    calls.clear()
    next_page=ordered[45:]+ordered[:40]
    second=case.engine.run_once(force=True)
    assert calls==next_page and len(calls)==100
    assert case.engine._reply_cursor==next_page[-1]
    assert second=={'analyzed':0,'decided':99}
