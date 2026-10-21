"""2026-10-14 remediation regressions: synthetic data, no production integrations."""
import json
import re

import pytest

from server.business import InquiryService
from server.chat_runtime import ChatRunner, GuardError
from server.repository import Repository


@pytest.fixture
def service(tmp_path):
    repo=Repository('sqlite:///' + str(tmp_path / 'remediation.db'))
    repo.bind_account('owner-a','synthetic-001','synthetic-workbench')
    return InquiryService(repo,clock=lambda:1800000000.0)


def item(code='000100.IB'):
    return {'code':code,'name':'合成债券','peer_ids':['peer-A'],
            'our_side':'BUY','volume_wan':'1000','opening_yield_pct':'2.50',
            'limit_yield_pct':'2.40','step_bp':'2'}


def authorize(service,code='000100.IB',suffix='first'):
    preview=service.preview('owner-a','preview-'+suffix,[item(code)])
    result=service.confirm('owner-a',preview['batch_id'],preview['version'],'confirm-'+suffix)
    return result['commands'][0]['command_id']


def conversation(service):
    return service.conversations('owner-a')[0]


def command(service,command_id):
    with service.repo.db.transaction(write=False) as tx:
        return dict(tx.execute('SELECT * FROM xj_command WHERE command_id=?',(command_id,)).fetchone())


def test_explicit_human_lock_not_cleared_by_stop_then_confirm(service):
    authorize(service)
    cid=conversation(service)['conversation_id']
    service.takeover('owner-a',cid)
    assert conversation(service)['human_lock']==1
    service.set_bot('owner-a',False)
    assert conversation(service)['mode']=='STOPPED'
    draft=service.preview('owner-a','new-preview',[item(code='000200.IB')])
    with pytest.raises(ValueError,match='human|接管|恢复'):
        service.confirm('owner-a',draft['batch_id'],draft['version'],'new-confirm')
    assert conversation(service)['mode']=='STOPPED'
    assert conversation(service)['human_lock']==1
    assert len(service.commands('owner-a'))==1


def test_late_success_stores_sent_price_without_resuming_auto(service):
    cid=authorize(service)
    thread=conversation(service)['conversation_id']
    with service.repo.db.transaction() as tx:
        tx.execute("UPDATE xj_command SET status='pending' WHERE command_id=?",(cid,))
    service.takeover('owner-a',thread)
    service.receipt(cid,account_uid='synthetic-001',peer_id='peer-A',
                    status='sent',evidence='native-success-evidence')
    session=service.sessions('owner-a')[0]
    assert session['state']=='HANDOFF'
    assert conversation(service)['mode']=='HUMAN'
    assert session['our_quote']=={}, 'Late receipt must not mutate a stopped/human session'
    assert command(service,cid)['status']=='sent'
    with service.repo.db.transaction(write=False) as tx:
        assert tx.execute('SELECT 1 FROM xj_message WHERE message_id=?',('out_'+cid,)).fetchone()
    resumed=service.resume('owner-a',thread)
    assert resumed['mode']=='AUTO'
    session=service.sessions('owner-a')[0]
    assert session['our_quote']['yield_pct']=='2.50'
    assert session['our_quote']['source_command_id']==cid
    assert session['pending_command_id'] is None


def test_negated_deal_does_not_trigger_explicit_stop(service):
    authorize(service)
    event={'event_id':'event-negated','account_uid':'synthetic-001',
           'peer_id':'peer-A','source_seq':1,'occurred_at':1800000000.0,
           'direction':'in','kind':'message','text':'这笔还没成交，可以继续谈'}
    result=service.ingest('owner-a',event)
    assert result['processing']=='queued'
    assert conversation(service)['mode']=='AUTO'


def test_direct_stop_still_overrides_negated_deal(service):
    authorize(service)
    event={'event_id':'event-direct-stop','account_uid':'synthetic-001',
           'peer_id':'peer-A','source_seq':1,'occurred_at':1800000000.0,
           'direction':'in','kind':'message','text':'还没有成交，先别发了'}
    result=service.ingest('owner-a',event)
    assert result['processing']=='ignored'
    assert conversation(service)['mode']=='HUMAN'


def check_quote_without_volume(text,volume=None):
    svc=type('SyntheticService',(),{})()
    runner=ChatRunner(svc,None,clock=lambda:1800000000.0)
    policy={'code':'000100.IB','name':'合成债券','our_side':'BUY',
            'opening_yield_pct':'2.50','limit_yield_pct':'2.40',
            'volume_wan':'1000','step_bp':'2','strict_match':True}
    session={'session_id':'session-1','bond_code':'000100.IB','policy':policy,
             'our_quote':{},'peer_quote':{},'authorized_at':1799999999.0}
    row={'turn_id':'turn-1','control_version':1,'message_id':'msg-1','created_at':1800000000.0}
    conv={'memory_json':'{}','ambiguity_json':'{}','social_authorized':1,
          'conversation_id':'conv-1','memory_revision':1}
    msg={'message_id':'msg-1','text':text,'occurred_at':1800000000.0}
    snapshot={'sessions':[session]}
    proposal={'schema_version':'1','turn_id':'turn-1','conversation_epoch':1,
              'source_message_id':'msg-1','observations':[
              {'rfq_session_id':'session-1','bond_id':'000100.IB',
               'attribution':'unique','evidence_refs':['msg-1'],
               'peer_quote':{'source_message_id':'msg-1','side':'ofr','firm':True,
                             'value':'2.20','kind':'yield'}}],
              'proposed_actions':[{'type':'COUNTER','rfq_session_id':'session-1',
                                   'yield_pct':'2.48','volume_wan':volume or '1000'}],
              'public_message':None,'trader_view':{},
              'memory_patch':None,'history_request':None,'validation_ref':None}
    return runner._check(row,conv,msg,snapshot,proposal)


def test_counterparty_quote_without_volume_not_assumed_approved_volume():
    with pytest.raises(GuardError,match='人工|授权|数量|报价'):
        check_quote_without_volume('000100.IB 我卖2.20%，实价')


def test_counterparty_with_explicit_matching_volume_is_not_rejected_for_amount():
    result=check_quote_without_volume('000100.IB 我卖2.20%，1000万，实价')
    assert result['handoff'] is False


def configure_bp_authorization(service, *, snapshot_yield='2.00'):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from server.reference_data import ReferenceSnapshot
    valuation_date=datetime.fromtimestamp(service.clock(),ZoneInfo('Asia/Shanghai')).date().isoformat()
    class Provider:
        def __init__(self):
            self.snapshot_id='snapshot-A'
            self.valuation=snapshot_yield
        def snapshot(self):
            def field(value):
                return {'value':value,'source_ref':'official-feed','quality':'verified'}
            return ReferenceSnapshot(snapshot_id=self.snapshot_id,as_of=valuation_date,
                sources=[{'id':'official-feed'}], bonds=[{
                'code':'000100.IB','valuation':{
                    'provider':field('中债'),'basis':field('maturity'),
                    'valuation_date':field(valuation_date),'yield_pct':field(self.valuation)}}])
    provider=Provider()
    service.reference_provider=provider
    with service.repo.db.transaction() as tx:
        row=tx.execute("SELECT config_json FROM xj_binding WHERE owner_user_id='owner-a'").fetchone()
        cfg=json.loads(row['config_json'])
        cfg['floors']={'000100.IB':{'kind':'bp','value':5,'revision':1}}
        cfg['strategy']={'template_id':'bp-basic','start_offset_bp':10,
                         'step_bp':2,'strict_match':True}
        tx.execute("UPDATE xj_binding SET config_json=? WHERE owner_user_id='owner-a'",
                   (json.dumps(cfg,ensure_ascii=False),))
    return provider


def bp_item():
    return {**item(), 'pricing_basis':'bp','opening_yield_pct':'2.10',
            'limit_yield_pct':'2.05'}


def test_bp_mode_requires_verified_server_snapshot(service):
    configure_bp_authorization(service)
    service.reference_provider=None
    with pytest.raises(ValueError,match='BP|可信'):
        service.preview('owner-a','bp-missing-source',[bp_item()])


def test_bp_mode_rejects_browser_forged_conversion(service):
    configure_bp_authorization(service)
    with pytest.raises(ValueError,match='BP|不一致'):
        service.preview('owner-a','bp-forged', [{**bp_item(),'limit_yield_pct':'2.02'}])


def test_bp_preview_pins_authoritative_valuation_and_confirm_rechecks(service):
    source=configure_bp_authorization(service)
    preview=service.preview('owner-a','bp-valid',[bp_item()])
    record=preview['items'][0]
    assert record['valuation_snapshot']['snapshot_id']=='snapshot-A'
    assert record['pricing_basis']=='bp'
    assert record['limit_yield_pct']=='2.05'
    assert record['opening_yield_pct']=='2.10'
    source.snapshot_id='snapshot-B'
    with pytest.raises(ValueError,match='快照|估值'):
        service.confirm('owner-a',preview['batch_id'],preview['version'],'bp-confirm')
    assert service.commands('owner-a')==[]


def test_bp_confirm_rejects_changed_saved_strategy(service):
    configure_bp_authorization(service)
    preview=service.preview('owner-a','bp-strategy',[bp_item()])
    with service.repo.db.transaction() as tx:
        row=tx.execute("SELECT config_json FROM xj_binding WHERE owner_user_id='owner-a'").fetchone()
        cfg=json.loads(row['config_json'])
        cfg['strategy']['start_offset_bp']=12
        tx.execute("UPDATE xj_binding SET config_json=? WHERE owner_user_id='owner-a'",
                   (json.dumps(cfg,ensure_ascii=False),))
    with pytest.raises(ValueError,match='BP|策略'):
        service.confirm('owner-a',preview['batch_id'],preview['version'],'bp-strategy-confirm')
    assert service.commands('owner-a')==[]


def test_bp_authorization_consistent_snapshot_succeeds(service):
    configure_bp_authorization(service)
    preview=service.preview('owner-a','bp-accepted',[bp_item()])
    result=service.confirm('owner-a',preview['batch_id'],preview['version'],'bp-ok')
    assert len(result['commands'])==1
    session=service.sessions('owner-a')[0]
    assert session['policy']['valuation_snapshot']['yield_pct']=='2.00'
    assert session['policy']['bp_terms']['floor_bp']=='5'
    assert session['policy']['pricing_basis']=='bp'


def test_explicit_absolute_yield_does_not_require_reference_provider(service):
    result=service.preview('owner-a','absolute-explicit',[item()])
    assert result['items'][0]['pricing_basis']=='absolute'


def test_negative_bp_floor_is_not_stripped_or_abs_converted(service):
    configure_bp_authorization(service)
    with service.repo.db.transaction() as tx:
        row=tx.execute("SELECT config_json FROM xj_binding WHERE owner_user_id='owner-a'").fetchone()
        cfg=json.loads(row['config_json'])
        cfg['floors']['000100.IB']['value']=-5
        tx.execute("UPDATE xj_binding SET config_json=? WHERE owner_user_id='owner-a'",
                   (json.dumps(cfg,ensure_ascii=False),))
    p=service.preview('owner-a','signed-bp',
                      [{**bp_item(),'limit_yield_pct':'1.95'}])
    assert p['items'][0]['bp_terms']['floor_bp']=='-5'
    assert p['items'][0]['limit_yield_pct']=='1.95'


def test_production_rejects_retired_direct_oss_profile_fields(service,tmp_path):
    from server.transport_config import load_transports
    profile={'owner_user_id':'owner-a','account_uid':'synthetic-001',
             'workbench_id':'synthetic-workbench',
             'seal_key_env':'SYNTHETIC_SEAL','sign_key_env':'SYNTHETIC_SIGN',
             'oss':{'endpoint':'https://legacy.invalid','expected_bucket':'synthetic-bucket'}}
    config=tmp_path/'transport.json'
    config.write_text(json.dumps([profile]));config.chmod(0o600)
    with pytest.raises(ValueError,match='不支持字段'):
        load_transports(service.repo,str(config),str(tmp_path/'state'))
