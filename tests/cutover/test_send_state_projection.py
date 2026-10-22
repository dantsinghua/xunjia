"""Submission state agrees across market, RFQ and monitor projections. No live I/O."""
import pytest
from tests.cutover.test_production_flow import desk, draft, confirm, item, OWNER


def all_labels(app, client):
    app.state.market.rows_provider=lambda channel,broker:[
        {'code':code,'name':code,'flowId':i+1,'bondUniCode':str(i+1)}
        for i,code in enumerate(('SYN-A','SYN-B'))]
    market=client.get('/api/bonds').json()['data']
    rfq=client.get('/api/im/rfq/status').json()['data']
    peers=client.get('/api/monitor/counterparties').json()['data']
    return {
        'market':{r['code']:r['status'] for r in market},
        'rfq':{r['code']:r['status'] for r in rfq['items']},
        'monitor':{b['code']:b['stage'] for p in peers for b in p['bonds']},
    }


def submit_two(desk):
    app,client,_,transport,clock=desk
    value=confirm(client,draft(client,[item(code,['peer-a']) for code in ('SYN-A','SYN-B')]))
    assert len(value['commands'])==1
    return value['commands'][0]['command_id']


@pytest.mark.parametrize('phase,expected',[
    ('queued','待回执'),('pending','待回执'),('unknown','待核对'),
    ('sent','询价中'),('failed','发送失败'),('expired','发送失败')])
def test_combined_submission_never_looks_sent_before_native_evidence(desk,phase,expected):
    app,client,_,transport,clock=desk
    command_id=submit_two(desk)
    if phase=='expired':
        clock.value+=121
        app.state.dispatcher.run_once()
        assert not transport.sent
    elif phase!='queued':
        if phase!='sent':
            def send(command):
                transport.sent.append(dict(command))
                return {**{k:command[k] for k in ('command_id','account_uid','peer_id','content_hash')},
                        'status':'accepted' if phase=='pending' else phase,
                        'evidence':'synthetic-native-'+phase}
            transport.send=send
        app.state.dispatcher.run_once()
    assert all_labels(app,client)=={surface:{code:expected for code in ('SYN-A','SYN-B')}
                                    for surface in ('market','rfq','monitor')}
    state=app.state.inquiry.commands_core.get(OWNER,command_id)['state']
    assert state=={'queued':'QUEUED','pending':'PENDING','unknown':'UNKNOWN',
                   'sent':'SENT','failed':'FAILED','expired':'CANCELLED'}[phase]


def test_paused_member_is_not_aggregated_as_inquiring(desk):
    app,client,*_=desk
    submit_two(desk)
    app.state.dispatcher.run_once()
    for group in app.state.repository.group_rows(OWNER):
        for peer in group['peers']:
            app.state.repository.set_sending(OWNER,False,member_id=peer['member_id'])
    assert all_labels(app,client)=={surface:{code:'已暂停' for code in ('SYN-A','SYN-B')}
                                    for surface in ('market','rfq','monitor')}