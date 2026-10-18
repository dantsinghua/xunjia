"""Actual public Tool data survives the fact transaction and a new B worker."""
from copy import deepcopy
import json
from types import SimpleNamespace

from dify.negotiation_v22.cases import reference_snapshot
from server.app import create_app
from server.auth import AuthConfig
from server.bond_tools import BondTools
from server.negotiation.application_store import ApplicationStore
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy
from server.negotiation.service import Inquiry


OWNER = 'synthetic-tool-handoff-owner'
DAY = '2026-10-17'
NOW = 1792202400.0
REQUEST = {'name': 'issuer_info', 'arguments': {
    'schema_version': '1', 'mode': 'get', 'issuer_id': 'SYN-ISSUER'}}


def setup(tmp_path):
    provider = SimpleNamespace(snapshot=lambda: reference_snapshot('tool-issuer-info'))
    flows = SimpleNamespace(analyze=lambda context: None, decide=lambda context: None)
    url = 'sqlite:///' + str(tmp_path/'tools.db')
    app = create_app(database_url=url, auth_config=AuthConfig(clock=lambda: NOW),
                     enable_background=False, chatflows=flows, reference_provider=provider)
    store = app.state.repository
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
    group = store.create_group(OWNER, bond_code='SYN-A', our_side='BUY',
        target_volume='1000', trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
    member, thread = store.add_member(OWNER, group, account_uid='synthetic-account', peer_uid='synthetic-peer')
    store.set_sending(OWNER, True)
    return SimpleNamespace(app=app, store=store, flows=flows, url=url, provider=provider,
                           member=member, thread=thread)


def analyze(case, sequence=1, *, use_tool=True, forge=False):
    ref = case.store.ingest(OWNER, case.thread, event_id='tool-message-'+str(sequence),
        source_seq=sequence, text='SYN-A 3.10出500万，查询SYN-ISSUER资料', trade_day=DAY)
    def respond(context):
        if use_tool and not context['tool_results']:
            return {'schema_version': '2.2', 'tool_requests': [deepcopy(REQUEST)]}
        changes = {'price': {'kind': 'YIELD', 'value': '3.10'}, 'volume_wan': '500',
                   'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'NEGOTIABLE',
                   'confirmed_day': DAY}
        result = {'schema_version': '2.2', 'scopes': {ref: [case.member]}, 'unresolved': {},
                  'observations': [{'member_id': case.member, 'side': 'ofr', 'changes': changes,
                                    'evidence': {field: [ref] for field in changes}}]}
        if forge:
            result['_public_tool_results'] = [{'request': REQUEST, 'result': {'forged': True}}]
            result['public_tool_results'] = deepcopy(result['_public_tool_results'])
        return result
    case.flows.analyze = respond
    saved = case.app.state.negotiation_engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['facts_committed'] and not saved['rejected'], saved
    return saved


def restart_and_decide(case):
    store = ApplicationStore(case.url, clock=lambda: NOW)
    captured = []
    def decide(context):
        captured.append(deepcopy(context))
        return {'schema_version': '2.2', 'action': 'WAIT', 'proposal': None}
    engine = Engine(Inquiry(store, reference_provider=case.provider),
                    SimpleNamespace(decide=decide), BondTools(provider=case.provider))
    result = engine.decide_member(OWNER, case.member)
    assert result is not None and len(captured) == 1
    return captured[0]


def test_real_tool_result_is_available_after_fact_commit_and_worker_restart(tmp_path):
    case = setup(tmp_path)
    saved = analyze(case, forge=True)
    result = restart_and_decide(case)
    assert len(result['analysis_tool_results']) == 1
    assert result['tool_results'] == []
    entry = result['analysis_tool_results'][0]
    assert entry['request'] == REQUEST and entry['result']['status'] == 'ok'
    assert 'Synthetic Issuer' in json.dumps(entry['result'])
    assert 'forged' not in json.dumps(entry)
    with case.store.db.transaction(write=False) as tx:
        row = tx.execute('SELECT * FROM xj2_analysis WHERE thread_id=? AND state=?',
                         (case.thread, 'COMMITTED')).fetchone()
    assert json.loads(row['result_json'])['public_tool_results'] == result['analysis_tool_results']
    assert saved['public_tool_results'] == result['analysis_tool_results']
    assert result['analysis_tool_source'] == {
        'analysis_id': row['analysis_id'], 'through_revision': row['through_revision']}


def test_later_analysis_without_tools_does_not_reuse_prior_results(tmp_path):
    case = setup(tmp_path)
    analyze(case)
    analyze(case, sequence=2, use_tool=False, forge=True)
    result = restart_and_decide(case)
    assert result['tool_results'] == []
    assert result.get('analysis_tool_results', []) == []
    assert result.get('analysis_tool_source') is None


def test_tool_results_are_already_committed_before_post_commit_audit(tmp_path, monkeypatch):
    case = setup(tmp_path)
    original = case.store.commit_analysis
    inside = []
    def inspect_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        with case.store.db.transaction(write=False) as tx:
            row = tx.execute('SELECT result_json FROM xj2_analysis WHERE analysis_id=?',
                             (args[1],)).fetchone()
        inside.append(json.loads(row['result_json']).get('public_tool_results'))
        return result
    monkeypatch.setattr(case.store, 'commit_analysis', inspect_commit)
    analyze(case)
    assert inside and len(inside[0]) == 1
    assert inside[0][0]['result']['status'] == 'ok'
