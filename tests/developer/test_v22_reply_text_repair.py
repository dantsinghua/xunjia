"""B repair caches full validation and cannot queue a mutated proposal."""
from copy import deepcopy
import json
from types import SimpleNamespace

import server.negotiation.engine as engine_module
from server.negotiation.application_store import ApplicationStore
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy
from server.negotiation.service import Inquiry
from server.negotiation.store import encoded


OWNER = 'synthetic-text-repair-owner'


def desk(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'text-repair.db'),
                             initialize=True, clock=lambda: 1792202400.0)
    service = Inquiry(store)
    store.bind_account(OWNER,'synthetic-account','synthetic-text-repair-workbench',transport='oss')
    day = service.trade_day()
    group = store.create_group(OWNER, bond_code='SYN-A', our_side='BUY', target_volume='1000',
        trade_day=day, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
    member, thread = store.add_member(OWNER, group, account_uid='synthetic-account', peer_uid='synthetic-peer',workbench_id='synthetic-text-repair-workbench')
    store.set_sending(OWNER,True)
    with store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
                   (encoded({'opening_yield_pct': '3.20', 'step_bp': '2', 'volume_wan': '1000'}), group))
        tx.execute('UPDATE xj2_peer SET our_quote_json=? WHERE member_id=?',
                   (encoded({'price': {'kind': 'YIELD', 'value': '3.20'}, 'native_id': 'synthetic-native'}), member))
    message = store.ingest(OWNER, thread, event_id='synthetic-inbound', source_seq=1,
                           text='SYN-A 3.10出500万', trade_day=day)
    job = store.claim_analysis(OWNER, thread, force=True)
    changes = {'price': {'kind': 'YIELD', 'value': '3.10'}, 'volume_wan': '500', 'confirmed_day': day}
    store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], scopes={message: [member]},
        observations=[{'member_id': member, 'side': 'ofr', 'changes': changes,
                       'evidence': {key: [message] for key in changes}}])
    flows = SimpleNamespace(decide=None)
    engine = Engine(service, flows, SimpleNamespace(invoke=None))
    return store, engine, flows, member


def response(text='3.19%收500万'):
    return {'schema_version': '2.2', 'action': 'SEND', 'proposal': {
        'kind': 'REPLY', 'text': text, 'our_price': {'kind': 'YIELD', 'value': '3.19'}},
        'trader_view': {'reason': 'synthetic immutable explanation'}}


def test_full_reply_validator_runs_once_per_response_and_audit_is_persistent(tmp_path, monkeypatch):
    store, engine, flows, member = desk(tmp_path)
    before = store.quote(OWNER, member, 'ofr').dump()
    calls, checked = [], []
    def decide(context):
        calls.append(deepcopy(context))
        return response('3.19%收500万' if context.get('validation_feedback') else '3.19收500万')
    validate = engine._validate_reply
    def counted(*args):
        checked.append(deepcopy(args[1]))
        return validate(*args)
    monkeypatch.setattr(engine, '_validate_reply', counted)
    flows.decide = decide
    result = engine.decide_member(OWNER, member)
    assert result['state'] == 'QUEUED' and len(calls) == len(checked) == 2
    assert result['repair']['attempted'] is True and result['repair']['succeeded'] is True
    assert store.quote(OWNER, member, 'ofr').dump() == before
    with store.db.transaction(write=False) as tx:
        job = tx.execute('SELECT * FROM xj2_reply_job WHERE member_id=?', (member,)).fetchone()
        audit = tx.execute("SELECT data_json FROM xj2_audit WHERE entity_id=? AND kind='reply_text_repair'", (job['job_id'],)).fetchone()
        command = tx.execute('SELECT body_json FROM xj2_command WHERE command_id=?', (result['command_id'],)).fetchone()
    assert json.loads(job['result_json'])['repair'] == json.loads(audit['data_json']) == result['repair']
    assert json.loads(command['body_json'])['text'] == '3.19%收500万'


def test_cached_validator_refuses_proposal_mutation_at_command_boundary(tmp_path, monkeypatch):
    store, engine, flows, member = desk(tmp_path)
    flows.decide = lambda context: response()
    propose = engine.commands.propose
    def changed(*args, **kwargs):
        kwargs['proposal']['our_price']['value'] = '3.20'
        kwargs['proposal']['text'] = '3.20%收500万'
        return propose(*args, **kwargs)
    monkeypatch.setattr(engine.commands, 'propose', changed)
    result = engine.decide_member(OWNER, member)
    assert result['state'] == 'REJECTED' and result['command_id'] is None
    assert store.quote(OWNER, member, 'ofr').revision == 1


def test_nonfinite_model_price_remains_rejected_with_readable_repair_audit(tmp_path):
    store, engine, flows, member = desk(tmp_path)
    calls = []
    def malformed(context):
        calls.append(context)
        value = response()
        value['proposal']['our_price']['value'] = float('nan')
        return value
    flows.decide = malformed
    result = engine.decide_member(OWNER, member)
    assert result['state'] == 'REJECTED' and result['command_id'] is None and len(calls) == 1
    assert result['repair']['attempted'] is False and result['repair']['initial_errors']
    json.dumps(result['repair'], allow_nan=False)
    with store.db.transaction(write=False) as tx:
        job = tx.execute('SELECT state,result_json FROM xj2_reply_job WHERE member_id=?', (member,)).fetchone()
    assert job['state'] == 'REJECTED'
    assert json.loads(job['result_json'])['repair'] == result['repair']


def test_initial_reply_timeout_keeps_actual_spent_time_in_persistent_audit(tmp_path, monkeypatch):
    store, engine, flows, member = desk(tmp_path)
    moment = [0.0]
    monkeypatch.setattr(engine_module, 'time', SimpleNamespace(monotonic=lambda: moment[0]))
    def timeout(context):
        moment[0] += 2
        raise TimeoutError('synthetic initial HTTP timeout')
    flows.decide = timeout
    result = engine.decide_member(OWNER, member)
    assert result['state'] == 'RETRY' and result['repair']['attempted'] is False
    assert result['repair']['initial_elapsed_ms'] == 2000
    assert result['repair']['initial_response'] is None
    assert 'TimeoutError' in result['repair']['stage_error']
    with store.db.transaction(write=False) as tx:
        job = tx.execute('SELECT result_json FROM xj2_reply_job WHERE member_id=?', (member,)).fetchone()
    assert json.loads(job['result_json'])['repair'] == result['repair']
