"""Current explicit counterpart quantity cannot be replaced by an older lot.

Actual generated Code -> Engine -> Store, using synthetic local SQLite only.
Wrong legacy selections are rejected, never silently rewritten to the new lot.
"""
from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.prices import LimitPolicy
from server.negotiation.slots import collect_candidates
from test_reference_window_policy import DAY, OWNER, Desk


def quote(case, member=None, side='ofr'):
    return case.store.quote(OWNER, member or case.member, side).dump()


def envelope(snapshot, observations):
    scopes = {message['message_id']: [member['member_id'] for member in snapshot['members']
              if member['bond_code'] in message['text']] for message in snapshot['messages']}
    return {'schema_version': '2.2', 'scopes': scopes, 'observations': observations}


def seed(case, member, code, side, price, amount):
    direction = '出' if side == 'ofr' else '收'
    case.now += 1
    text = code+' '+price+direction+amount+'万，不能再改善'
    source = case.inbound(text, occurred_at=case.now)
    changes = {'price': {'kind': 'YIELD', 'value': price}, 'volume_wan': amount,
               'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'FINAL', 'confirmed_day': DAY}
    observation = {'member_id': member, 'side': side, 'changes': changes,
        'evidence': {field: [source] for field in changes},
        'segments': [{'message_id': source, 'text': text}],
        'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                          'negotiation_space': 'FINAL', 'reason': '本人明确最终价量'}}
    case.flows.analyze = lambda snapshot: envelope(snapshot, [deepcopy(observation)])
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['accepted'] and not saved['rejected'], saved
    return source


def desk(tmp_path, side='ofr'):
    case = Desk(tmp_path / 'current-quantity.sqlite')
    old = seed(case, case.member, 'SYN-A', side, '3.04', '500')
    return case, old


def second_bond(case):
    group = case.store.create_group(OWNER, bond_code='SYN-B', our_side='BUY',
        target_volume='10000', trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
    member, thread = case.store.add_member(OWNER, group,
        account_uid='synthetic-account', peer_uid='synthetic-peer')
    assert thread == case.thread
    seed(case, member, 'SYN-B', 'ofr', '3.15', '600')
    return group, member


@lru_cache(maxsize=2)
def code(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(node['data']['code'] for node in graph['nodes'] if node['id'] == 'contract'), namespace)
    return namespace['main']


def select(snapshot, source, value, role):
    matches = [row for row in collect_candidates(snapshot)
               if row['message_id'] == source and row['value'] == value and role in row['roles']]
    assert len(matches) == 1, (source, value, role, matches)
    return {'candidate_id': matches[0]['id'], 'role': role}


def observation(snapshot, member, side, incoming, *, price=None, volume=None):
    slots = {}
    if price is not None:
        slots['price'] = select(snapshot, *price)
    if volume is not None:
        slots['volume_wan'] = select(snapshot, *volume)
    return {'member_id': member, 'side': side, 'source_ids': [incoming], 'slots': slots,
            'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE',
                        'negotiation_space': 'UNKNOWN', 'reason': '本轮本人报盘'}}


def analyze(case, text, build_observations, *, version='0.15.3', crop=False):
    case.now += 1
    incoming = case.inbound(text, occurred_at=case.now)
    outputs = []
    def model(snapshot):
        context = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis'}
        raw = envelope(context, build_observations(context, incoming))
        decoded = json.loads(code(version)(json.dumps(raw), json.dumps(context))['result'])
        if crop:
            for row in decoded['observations']:
                for segment in row.get('segments', []):
                    if segment['message_id'] == incoming:
                        segment['text'] = 'SYN-A 04出'
        outputs.append(deepcopy(decoded['observations']))
        return decoded
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(outputs) == 1, 'Quantity contradictions are not evidence-repair or additional model rounds'
    return result, outputs[0]


def no_coverage(case, group=None):
    summary = case.store.group(OWNER, group or case.group)['summary']
    assert summary['reported_volume_wan'] == '0'
    assert summary['price_eligible_volume_wan'] == summary['final_confirmed_volume_wan'] == '0'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('side,direction', [('ofr', '出'), ('bid', '收')])
def test_explicit_new_quantity_rejects_the_model_selected_historical_lot_without_rewriting_it(tmp_path, version, side, direction):
    case, old = desk(tmp_path, side)
    before = quote(case, side=side)
    result, selected = analyze(case, 'SYN-A 04'+direction+'1000万', lambda ctx, current: [
        observation(ctx, case.member, side, current,
            price=(old, '3.04', 'YIELD'), volume=(old, '500万', 'QUANTITY'))], version=version)
    assert selected[0]['changes']['volume_wan'] == '500万', 'Do not silently correct the legacy wire to 1000'
    assert result['facts_committed'] and not result['accepted']
    assert len(result['rejected']) == 1 and result['rejected'][0]['member_id'] == case.member
    assert quote(case, side=side) == before
    assert not result['repair']['attempted']
    no_coverage(case)


@pytest.mark.parametrize('side,direction', [('ofr', '出'), ('bid', '收')])
@pytest.mark.parametrize('amount,expected', [('1000万', '1000'), ('0.5亿', '5000')])
def test_current_explicit_quantity_and_units_are_preserved(tmp_path, side, direction, amount, expected):
    case, _ = desk(tmp_path, side)
    result, _ = analyze(case, 'SYN-A 04'+direction+amount, lambda ctx, current: [
        observation(ctx, case.member, side, current,
            price=(current, '04', 'YIELD_SUFFIX'), volume=(current, amount, 'QUANTITY'))])
    assert result['accepted'] and not result['rejected'], result
    current = quote(case, side=side)
    assert current['price']['value'] == '3.04' and Decimal(current['volume_wan']) == Decimal(expected)


@pytest.mark.parametrize('order', [('bid', 'ofr'), ('ofr', 'bid')])
def test_invalid_old_ofr_quantity_does_not_discard_the_valid_current_bid_sibling(tmp_path, order):
    case, old_ofr = desk(tmp_path)
    seed(case, case.member, 'SYN-A', 'bid', '3.00', '300')
    before = quote(case)
    def observations(ctx, current):
        values = {
            'bid': observation(ctx, case.member, 'bid', current,
                price=(current, '3.03', 'YIELD'), volume=(current, '700万', 'QUANTITY')),
            'ofr': observation(ctx, case.member, 'ofr', current,
                price=(current, '3.04', 'YIELD'), volume=(old_ofr, '500万', 'QUANTITY')),
        }
        return [values[side] for side in order]
    result, _ = analyze(case, 'SYN-A 3.03收700万，3.04出1000万', observations)
    assert [(row['side'], row['index']) for row in result['accepted']] == [('bid', order.index('bid'))]
    assert [(row['side'], row['index']) for row in result['rejected']] == [('ofr', order.index('ofr'))]
    assert quote(case) == before
    assert quote(case, side='bid')['price']['value'] == '3.03'
    assert quote(case, side='bid')['volume_wan'] == '700'
    no_coverage(case)


def test_two_current_sides_keep_their_separate_quantities(tmp_path):
    case, _ = desk(tmp_path)
    seed(case, case.member, 'SYN-A', 'bid', '3.00', '300')
    result, _ = analyze(case, 'SYN-A 3.03收700万，3.04出1000万', lambda ctx, current: [
        observation(ctx, case.member, side, current, price=(current, price, 'YIELD'),
                    volume=(current, amount, 'QUANTITY'))
        for side, price, amount in [('bid', '3.03', '700万'), ('ofr', '3.04', '1000万')]])
    assert len(result['accepted']) == 2 and not result['rejected'], result
    assert quote(case, side='bid')['volume_wan'] == '700'
    assert quote(case)['volume_wan'] == '1000'


@pytest.mark.parametrize('reverse', [False, True])
def test_old_quantity_rejection_is_member_scoped_and_preserves_other_bond_new_facts(tmp_path, reverse):
    case, old_a = desk(tmp_path)
    group_b, member_b = second_bond(case)
    before_a = quote(case)
    def observations(ctx, current):
        rows = [observation(ctx, case.member, 'ofr', current,
                    price=(old_a, '3.04', 'YIELD'), volume=(old_a, '500万', 'QUANTITY')),
                observation(ctx, member_b, 'ofr', current,
                    price=(current, '3.16', 'YIELD'), volume=(current, '800万', 'QUANTITY'))]
        return rows[::-1] if reverse else rows
    result, _ = analyze(case, 'SYN-A 04出1000万；SYN-B 3.16出800万', observations)
    assert [row['member_id'] for row in result['accepted']] == [member_b]
    assert [row['member_id'] for row in result['rejected']] == [case.member]
    assert quote(case) == before_a
    after_b = quote(case, member_b)
    assert after_b['price']['value'] == '3.16' and after_b['volume_wan'] == '800'
    no_coverage(case)
    assert case.store.group(OWNER, group_b)['summary']['reported_volume_wan'] == '800'


def test_multiple_bonds_with_correct_current_quantities_are_not_globally_rejected(tmp_path):
    case, _ = desk(tmp_path)
    _, member_b = second_bond(case)
    result, _ = analyze(case, 'SYN-A 04出1000万；SYN-B 3.16出800万', lambda ctx, current: [
        observation(ctx, case.member, 'ofr', current,
            price=(current, '04', 'YIELD_SUFFIX'), volume=(current, '1000万', 'QUANTITY')),
        observation(ctx, member_b, 'ofr', current,
            price=(current, '3.16', 'YIELD'), volume=(current, '800万', 'QUANTITY'))])
    assert len(result['accepted']) == 2 and not result['rejected'], result
    assert quote(case)['volume_wan'] == '1000' and quote(case, member_b)['volume_wan'] == '800'


@pytest.mark.parametrize('side,direction', [('ofr', '出'), ('bid', '收')])
def test_unmentioned_quantity_keeps_previous_value_and_source(tmp_path, side, direction):
    case, _ = desk(tmp_path, side)
    before = quote(case, side=side)
    result, _ = analyze(case, 'SYN-A 3.05'+direction, lambda ctx, current: [
        observation(ctx, case.member, side, current, price=(current, '3.05', 'YIELD'))])
    assert result['accepted'] and not result['rejected'], result
    after = quote(case, side=side)
    assert after['price']['value'] == '3.05' and after['volume_wan'] == '500'
    assert after['evidence']['volume_wan'] == before['evidence']['volume_wan']


@pytest.mark.parametrize('side,direction', [('ofr', '出'), ('bid', '收')])
def test_actual_window_reference_to_old_quantity_is_valid_without_a_current_contradiction(tmp_path, side, direction):
    case, old = desk(tmp_path, side)
    result, _ = analyze(case, 'SYN-A 3.04'+direction+'，量按刚才的', lambda ctx, current: [
        observation(ctx, case.member, side, current,
            price=(current, '3.04', 'YIELD'), volume=(old, '500万', 'QUANTITY'))])
    assert result['accepted'] and not result['rejected'], result
    after = quote(case, side=side)
    assert after['volume_wan'] == '500' and old in after['evidence']['volume_wan']


def test_current_equal_quantity_does_not_make_matching_historical_evidence_illegal(tmp_path):
    case, old = desk(tmp_path)
    result, _ = analyze(case, 'SYN-A 04出500万', lambda ctx, current: [
        observation(ctx, case.member, 'ofr', current,
            price=(old, '3.04', 'YIELD'), volume=(old, '500万', 'QUANTITY'))])
    assert result['accepted'] and not result['rejected'], result
    assert quote(case)['volume_wan'] == '500'


def test_cropped_segment_cannot_hide_the_current_explicit_quantity(tmp_path):
    case, old = desk(tmp_path)
    before = quote(case)
    result, selected = analyze(case, 'SYN-A 04出1000万', lambda ctx, current: [
        observation(ctx, case.member, 'ofr', current,
            price=(old, '3.04', 'YIELD'), volume=(old, '500万', 'QUANTITY'))], crop=True)
    assert selected[0]['changes']['volume_wan'] == '500万'
    assert result['facts_committed'] and not result['accepted'] and result['rejected']
    assert quote(case) == before
    no_coverage(case)


@pytest.mark.parametrize('text', [
    'SYN-A 别家报3.04出1000万，我这边仍按刚才的价量。',
    'SYN-A 昨天的3.04出1000万已过期，我这边仍按刚才的价量。',
    'SYN-A 我明天出差，仍按刚才的价量。',
    'SYN-A 本月收入1000万，仍按刚才的价量。',
])
def test_other_reported_numbers_and_nontrading_direction_words_do_not_override_our_referenced_lot(tmp_path, text):
    case, old = desk(tmp_path)
    result, _ = analyze(case, text, lambda ctx, current: [
        observation(ctx, case.member, 'ofr', current,
            price=(old, '3.04', 'YIELD'), volume=(old, '500万', 'QUANTITY'))])
    assert result['accepted'] and not result['rejected'], result
    assert quote(case)['price']['value'] == '3.04' and quote(case)['volume_wan'] == '500'


def test_explicit_later_quantity_correction_does_not_force_the_earlier_quoted_lot(tmp_path):
    case, _ = desk(tmp_path)
    result, _ = analyze(case, 'SYN-A 3.04出1000万，不过量改为500万', lambda ctx, current: [
        observation(ctx, case.member, 'ofr', current,
            price=(current, '3.04', 'YIELD'), volume=(current, '500万', 'QUANTITY'))])
    assert result['accepted'] and not result['rejected'], result
    assert quote(case)['price']['value'] == '3.04' and quote(case)['volume_wan'] == '500'
