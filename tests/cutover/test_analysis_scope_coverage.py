"""Explicit uncovered quote fragments cannot silently reuse old executable facts.

Scope means association, not a universal requirement to invent observations.
All state/commands use the real app; only model and native transport are synthetic.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_production_flow import OWNER, desk, item, draft, confirm, raw
from test_acceptance_contract import current, observation, parser, setup_groups
from test_analysis_evidence_repair import repair_case, install, run as run_repair


def prepared(desk, *, old_price='3.10', old_finality='NEGOTIABLE', second_peer=False):
    app, client, flows, native, _ = desk
    rows = [item('SYN-A', ['peer-a']), item('SYN-B', ['peer-a', 'peer-b'] if second_peer else ['peer-a'])]
    confirm(client, draft(client, rows))
    app.state.dispatcher.run_once()
    groups = {group['bond_code']: group for group in app.state.repository.group_rows(OWNER)}
    a = groups['SYN-A']['peers'][0]
    b = next(peer for peer in groups['SYN-B']['peers'] if peer['thread_id'] == a['thread_id'])
    def baseline(_snapshot, message):
        first, second = message['text'].split('；')
        return [observation(a, message, price='3.10', segment=first, assertion='QUOTE'),
                observation(b, message, price=old_price, finality=old_finality, segment=second, assertion='QUOTE')]
    flows.analyze = parser(baseline)
    raw(app, a['thread_id'], 'SYN-A 3.10出500万，还可以谈；SYN-B '+old_price+'出500万，'+(
        '不能再改善了' if old_finality == 'FINAL' else '还可以谈'), seq=1)
    initial = app.state.negotiation_engine.analyze_thread(OWNER, a['thread_id'], force=True)
    assert initial['facts_committed'] and not initial['rejected']
    return SimpleNamespace(app=app, flows=flows, native=native, groups=groups, a=a, b=b,
                           old_b=current(app, b).dump(), initial_sends=len(native.sent))


def scope_relation(case, message_id, member):
    with case.app.state.repository.db.transaction(write=False) as tx:
        row = tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',
                         (message_id, member['member_id'])).fetchone()
        return row['relation'] if row else None


def omit_b(case, *, text='SYN-A 3.10出500万；SYN-B 3.05出800万'):
    case.flows.analyze = parser(lambda _snapshot, message: [observation(case.a, message,
        price='3.10', finality='UNKNOWN', segment=message['text'].split('；')[0], assertion='QUOTE')])
    message_id = raw(case.app, case.a['thread_id'], text, seq=2)
    result = case.app.state.negotiation_engine.analyze_thread(OWNER, case.a['thread_id'], force=True)
    return message_id, result


def quoted_reply(context):
    code = context['bond_code']
    value = '3.20' if context['quotes']['ofr']['finality'] == 'FINAL' else '3.19'
    return {'schema_version': '2.2', 'action': 'SEND', 'proposal': {
        'kind': 'REPLY', 'text': code+' '+value+'%收500万', 'our_price': {'kind': 'YIELD', 'value': value}}}


def assert_uncovered(result, member, side='ofr'):
    assert result['facts_committed'], 'A valid sibling must not be discarded'
    assert any(row.get('member_id') == member['member_id'] and row.get('side') == side
               for row in result['rejected']), 'The explicitly quoted but unprocessed member/side was silently accepted'


def test_omitted_second_explicit_quote_cannot_queue_a_reply_from_its_old_fact(desk):
    case = prepared(desk)
    message_id, result = omit_b(case)
    assert_uncovered(result, case.b)
    assert current(case.app, case.a).revision == 2
    assert current(case.app, case.b).dump() == case.old_b, 'Do not invent the omitted new price or destroy audited old facts'
    assert scope_relation(case, message_id, case.b) == 'INVALID'
    case.flows.decide = quoted_reply
    rejected = case.app.state.negotiation_engine.decide_member(OWNER, case.b['member_id'])
    assert rejected is None or rejected['state'] != 'QUEUED'
    accepted = case.app.state.negotiation_engine.decide_member(OWNER, case.a['member_id'])
    assert accepted['state'] == 'QUEUED', 'The correctly processed A quote must remain usable'
    assert len(case.native.sent) == case.initial_sends


def test_old_final_volume_from_an_omitted_revision_cannot_complete_the_group(desk):
    case = prepared(desk, old_price='3.20', old_finality='FINAL', second_peer=True)
    _, result = omit_b(case)
    # Continue the other counterparty's real transaction before asserting the
    # omission, so this gate directly catches the erroneous group handoff.
    other = next(peer for peer in case.groups['SYN-B']['peers'] if peer['member_id'] != case.b['member_id'])
    case.flows.analyze = parser(lambda _snapshot, message: [observation(other, message,
        price='3.20', volume='500', finality='FINAL', segment=message['text'], assertion='QUOTE')])
    raw(case.app, other['thread_id'], 'SYN-B 3.20出500万，不能再改善了', seq=1)
    second = case.app.state.negotiation_engine.analyze_thread(OWNER, other['thread_id'], force=True)
    assert second['facts_committed'] and not second['rejected']
    group = case.app.state.repository.group(OWNER, case.b['group_id'])
    assert group['state'] == 'OPEN', 'Omitted new B 3.05 must not leave its old FINAL 500 eligible for HANDOFF'
    assert not group['summary']['goal_ready']
    assert group['summary']['final_confirmed_volume_wan'] == '500'
    assert_uncovered(result, case.b)
    assert current(case.app, case.b).dump() == case.old_b
    assert len(case.native.sent) == case.initial_sends


@pytest.mark.parametrize('nonquote', [
    'SYN-B 谢谢',
    'SYN-B 能3.05出800万吗？',
    '听说别家有SYN-B 3.05出800万，不是我的报价',
    '听说别家有SYN-B，3.05出800万，不是我的报价',
    'SYN-B 先保持原来的价格和量，没有变化',
])
def test_scope_without_new_facts_is_not_automatically_an_uncovered_quote(desk, nonquote):
    case = prepared(desk, old_price='3.20', old_finality='FINAL')
    message_id, result = omit_b(case, text='SYN-A 3.10出500万；'+nonquote)
    assert result['facts_committed'] and not result['rejected']
    assert current(case.app, case.b).dump() == case.old_b
    assert scope_relation(case, message_id, case.b) == 'MENTION'
    group = case.app.state.repository.group(OWNER, case.b['group_id'])
    assert group['summary']['final_confirmed_volume_wan'] == '500'
    assert not group['summary']['goal_ready']


def test_valid_question_observation_covers_its_member_without_a_fabricated_quote(desk):
    case = prepared(desk)
    def analysis(_snapshot, message):
        fragment = message['text'].split('；')[1]
        question = {'member_id': case.b['member_id'], 'side': 'ofr',
            'changes': {'assertion': 'QUESTION'}, 'evidence': {'assertion': [message['message_id']]},
            'segments': [{'message_id': message['message_id'], 'text': fragment}],
            'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUESTION',
                              'negotiation_space': 'UNKNOWN', 'reason': 'Current explicit question'}}
        return [observation(case.a, message, segment=message['text'].split('；')[0], assertion='QUOTE'), question]
    case.flows.analyze = parser(analysis)
    message_id = raw(case.app, case.a['thread_id'], 'SYN-A 3.10出500万；SYN-B 能3.05出800万吗？', seq=2)
    result = case.app.state.negotiation_engine.analyze_thread(OWNER, case.a['thread_id'], force=True)
    assert not result['rejected'] and len(result['accepted']) == 2
    quote = current(case.app, case.b)
    assert quote.assertion == 'QUESTION' and quote.price.value == '3.10' and quote.volume_wan == '500'
    assert scope_relation(case, message_id, case.b) == 'MENTION'


def test_quantity_only_update_keeps_unmentioned_price_without_triggering_coverage_failure(desk):
    case = prepared(desk)
    def analysis(_snapshot, message):
        return [{'member_id': case.b['member_id'], 'side': 'ofr', 'changes': {'volume_wan': '800'},
            'evidence': {'volume_wan': [message['message_id']]},
            'segments': [{'message_id': message['message_id'], 'text': message['text']}],
            'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                              'negotiation_space': 'UNKNOWN', 'reason': 'Quantity revised; price explicitly unchanged'}}]
    case.flows.analyze = parser(analysis)
    raw(case.app, case.a['thread_id'], 'SYN-B 出量改成800万，价格不变', seq=2)
    result = case.app.state.negotiation_engine.analyze_thread(OWNER, case.a['thread_id'], force=True)
    assert not result['rejected'] and len(result['accepted']) == 1
    assert current(case.app, case.b).price.value == '3.10'
    assert current(case.app, case.b).volume_wan == '800'


def test_single_bond_explicit_quote_cannot_be_acknowledged_with_empty_observations(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk, stopped=False)['SYN-A']['peers'][0]
    flows.analyze = parser(lambda _snapshot, _message: [])
    message_id = raw(app, peer['thread_id'], 'SYN-A 3.10出500万', seq=1)
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'], force=True)
    assert_uncovered(result, peer)
    assert current(app, peer).price is None
    case = SimpleNamespace(app=app)
    assert scope_relation(case, message_id, peer) == 'INVALID'


def test_two_sides_require_independent_coverage_without_destroying_the_valid_side(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk, stopped=False)['SYN-A']['peers'][0]
    flows.analyze = parser(lambda _snapshot, message: [observation(peer, message,
        price='3.10', side='ofr', segment=message['text'].split('；')[0], assertion='QUOTE')])
    raw(app, peer['thread_id'], 'SYN-A 3.10出500万；SYN-A 3.08收800万', seq=1)
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'], force=True)
    assert_uncovered(result, peer, 'bid')
    assert current(app, peer, 'ofr').price.value == '3.10'
    assert current(app, peer, 'bid').price is None
    assert any(row['member_id'] == peer['member_id'] and row['side'] == 'ofr' for row in result['accepted'])


def test_reference_evidence_repair_keeps_both_covered_members_and_our_side_distinct(desk):
    case = repair_case(desk, siblings=True)
    calls, _ = install(case)
    result = run_repair(case)
    assert len(calls) == 2 and result['repair']['succeeded']
    assert not result['rejected']
    assert current(case.app, case.peer).price.value == '3.20'
    assert current(case.app, case.peer, 'bid').price is None, 'Our referenced 收 is not a new counterpart bid'
    assert current(case.app, case.sibling).price.value == '3.10'


@pytest.mark.parametrize('use_latest', [True, False])
def test_one_current_observation_covers_earlier_batched_quotes_but_not_later_ones(desk, use_latest):
    case = prepared(desk)
    earlier = raw(case.app, case.b['thread_id'], 'SYN-B 3.10出500万', seq=2)
    latest = raw(case.app, case.b['thread_id'], 'SYN-B 3.05出', seq=3)
    chosen = latest if use_latest else earlier
    price = '3.05' if use_latest else '3.10'
    def only_one(_snapshot, message):
        if message['message_id'] != chosen:
            return []
        return [{'member_id': case.b['member_id'], 'side': 'ofr',
            'changes': {'price': {'kind': 'YIELD', 'value': price}},
            'evidence': {'price': [message['message_id']]},
            'segments': [{'message_id': message['message_id'], 'text': message['text']}],
            'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                              'negotiation_space': 'UNKNOWN', 'reason': 'One classified current price update'}}]
    case.flows.analyze = parser(only_one)
    result = case.app.state.negotiation_engine.analyze_thread(OWNER, case.b['thread_id'], force=True)
    assert result['facts_committed']
    assert current(case.app, case.b).price.value == price
    assert current(case.app, case.b).volume_wan == '500', 'Unmentioned volume remains sourced from its prior fact'
    if use_latest:
        assert not result['rejected'], 'A later current observation must cover the earlier batched quote'
        assert scope_relation(case, earlier, case.b) == 'MENTION'
        assert scope_relation(case, latest, case.b) == 'MENTION'
    else:
        assert_uncovered(result, case.b)
        assert scope_relation(case, latest, case.b) == 'INVALID', 'Earlier evidence cannot acknowledge an omitted later price'


def test_direct_structural_store_context_seed_keeps_its_existing_empty_observation_contract(desk):
    app, _, _, _, _ = desk
    peer = setup_groups(desk, stopped=False)['SYN-A']['peers'][0]
    raw(app, peer['thread_id'], 'SYN-A 3.10出500万', seq=1)
    store = app.state.repository
    job = store.claim_analysis(OWNER, peer['thread_id'], force=True)
    scopes = {message['message_id']: [peer['member_id']] for message in job['snapshot']['messages']}
    result = store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                                   scopes=scopes, observations=[], unresolved={})
    assert result['facts_committed'] and not result['rejected']
    assert current(app, peer).price is None
