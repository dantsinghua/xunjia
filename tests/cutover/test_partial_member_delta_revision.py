"""Keep valid member facts while rejecting copied candidates for another bond."""
from copy import deepcopy
from functools import lru_cache
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.prices import LimitPolicy
from server.negotiation.slots import collect_candidates
from tests.cutover.test_reference_window_policy import Desk, OWNER, DAY


@lru_cache(maxsize=2)
def contract(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(node['data']['code'] for node in graph['nodes'] if node['id'] == 'contract'), namespace)
    return namespace['main']


def observation(snapshot, member, *, price=None, volume=None, intent='QUOTE', validity='ACTIVE', space='UNKNOWN'):
    current = snapshot['messages'][-1]
    candidates = [row for row in collect_candidates(snapshot) if row['message_id'] == current['message_id']]
    selections = {}
    for field, token, role in [('price', price, 'YIELD'), ('volume_wan', volume, 'QUANTITY')]:
        if token is not None:
            selections[field] = next(row['id'] for row in candidates if row['token'] == token and role in row['roles'])
    return {'member_id': member, 'source_ids': [current['message_id']], 'side': 'ofr', 'slots': selections,
        'meaning': {'intent': intent, 'validity': validity, 'negotiation_space': space,
                    'reason': '仅按该券当前原文记录变化'}}


def analyze(case, version, make_observations):
    calls = []
    def response(snapshot):
        before = deepcopy(snapshot)
        value = {**deepcopy(snapshot), 'schema_version': '2.2', 'stage': 'analysis'}
        raw = {'schema_version': '2.2', 'scopes': {message['message_id']: [case.member, case.other]
               for message in snapshot['messages']}, 'observations': make_observations(value)}
        decoded = json.loads(contract(version)(json.dumps(raw), json.dumps(value))['result'])
        assert snapshot == before
        calls.append({'raw': raw, 'decoded': deepcopy(decoded)})
        return decoded
    case.flows.analyze = response
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(calls) == 1 and result['facts_committed'] and not result['repair']['attempted']
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 0
    return result, calls[0]


def prepared(tmp_path, version):
    case = Desk(tmp_path/'partial-member.sqlite')
    group = case.store.create_group(OWNER, bond_code='SYN-B', our_side='BUY', target_volume='10000',
        trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
    case.other, thread = case.store.add_member(OWNER, group,
        account_uid='synthetic-account', peer_uid='synthetic-peer')
    assert thread == case.thread
    case.inbound('SYN-A 3.10出500万；SYN-B 3.05出700万')
    result, _ = analyze(case, version, lambda snapshot: [
        observation(snapshot, case.member, price='3.10', volume='500万'),
        observation(snapshot, case.other, price='3.05', volume='700万')])
    assert len(result['accepted']) == 2 and not result['rejected']
    case.before_a = case.quote()
    case.before_b = case.store.quote(OWNER, case.other, 'ofr').dump()
    return case


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_native_style_cross_member_copy_is_rejected_without_rolling_back_the_valid_sibling(tmp_path, version):
    case = prepared(tmp_path, version)
    current = case.inbound('SYN-A 3.10出500万，另一只我再看一下')
    def copied(snapshot):
        first = observation(snapshot, case.member, price='3.10', volume='500万')
        first['meaning']['reason'] = 'SYN-A 3.10出500万'
        return [first, {**deepcopy(first), 'member_id': case.other}]
    result, call = analyze(case, version, copied)
    assert len(call['raw']['observations']) == len(call['decoded']['observations']) == 2
    assert call['decoded']['observations'][1]['validation_error'] == 'Selected source explicitly names another member'
    assert [row['member_id'] for row in result['accepted']] == [case.member]
    assert result['rejected'][0]['index'] == 1 and result['rejected'][0]['member_id'] == case.other
    assert result['rejected'][0]['reason'] == 'Selected source explicitly names another member'
    saved = case.quote()
    assert saved['price']['value'] == '3.10' and saved['volume_wan'] == '500'
    assert saved['revision'] == case.before_a['revision']+1 and saved['evidence']['price'] == [current]
    assert case.store.quote(OWNER, case.other, 'ofr').dump() == case.before_b


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_only_changed_a_observation_is_valid_even_if_scopes_mentions_both_members(tmp_path, version):
    case = prepared(tmp_path, version)
    case.inbound('SYN-A 3.10出500万，另一只我再看一下')
    result, call = analyze(case, version, lambda snapshot: [
        observation(snapshot, case.member, price='3.10', volume='500万')])
    assert all(len(members) == 2 for members in call['raw']['scopes'].values())
    assert not result['rejected'] and len(result['accepted']) == 1
    assert case.quote()['revision'] == case.before_a['revision']+1
    assert case.store.quote(OWNER, case.other, 'ofr').dump() == case.before_b


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('change', ['withdrawal', 'quantity', 'finality'])
def test_explicit_b_delta_without_new_price_is_not_silently_dropped(tmp_path, version, change):
    case = prepared(tmp_path, version)
    text = {'withdrawal': 'SYN-B 不出了', 'quantity': 'SYN-B 量改800万',
            'finality': 'SYN-B 这个价不能再改善了'}[change]
    current = case.inbound('SYN-A 3.10出500万；'+text)
    def observations(snapshot):
        second = observation(snapshot, case.other, volume='800万' if change == 'quantity' else None,
            intent='WITHDRAW' if change == 'withdrawal' else 'QUOTE',
            validity='WITHDRAWN' if change == 'withdrawal' else 'ACTIVE',
            space='FINAL' if change == 'finality' else 'UNKNOWN')
        return [observation(snapshot, case.member, price='3.10', volume='500万'), second]
    result, call = analyze(case, version, observations)
    assert 'price' not in call['raw']['observations'][1]['slots']
    assert not result['rejected'] and {row['member_id'] for row in result['accepted']} == {case.member, case.other}
    saved = case.store.quote(OWNER, case.other, 'ofr').dump()
    assert saved['revision'] == case.before_b['revision']+1 and saved['price'] == case.before_b['price']
    assert saved['evidence']['price'] == case.before_b['evidence']['price']
    if change == 'withdrawal':
        assert saved['validity'] == 'WITHDRAWN' and saved['evidence']['validity'] == [current]
    elif change == 'quantity':
        assert saved['volume_wan'] == '800' and saved['evidence']['volume_wan'] == [current]
    else:
        assert saved['finality'] == 'FINAL' and saved['evidence']['finality'] == [current]
    assert case.quote()['price']['value'] == '3.10' and case.quote()['volume_wan'] == '500'
