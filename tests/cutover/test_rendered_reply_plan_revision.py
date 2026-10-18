"""A rendered plan may separate sourced roles, never expand economic authority."""
from copy import deepcopy
from decimal import Decimal
import importlib
import json
import re
from types import SimpleNamespace

import pytest

from server.bond_tools import BondTools
from server.negotiation.commands import Commands
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.reference_data import ReferenceSnapshot


DAY = '2026-10-17'


def renderer():
    try:
        module = importlib.import_module('server.negotiation.reply_render')
    except ModuleNotFoundError:
        pytest.fail('The server-owned reply renderer is not implemented')
    assert callable(getattr(module, 'public_information', None))
    assert callable(getattr(module, 'render_reply_plan', None))
    return module


def context():
    return {'member_id': 'peer-a', 'thread_id': 'thread-a', 'bond_code': 'SYN-A',
        'our_side': 'BUY', 'trade_day': DAY, 'reconfirm_day': None, 'unresolved_messages': [],
        'basis': {'member_id': 'peer-a', 'member_version': 2, 'observed_watermark': 21},
        'boundary': {'kind': 'YIELD', 'value': '3.12'},
        'our_quote': {'price': {'kind': 'YIELD', 'value': '3.20'}, 'native_id': 'synthetic-opening'},
        'plan': {'opening_yield_pct': '3.20', 'step_bp': '2', 'volume_wan': '20000'},
        'group_summary': {'reported_shortfall_wan': '19500', 'goal_ready': False},
        'quotes': {'ofr': {'side': 'ofr', 'price': {'kind': 'YIELD', 'value': '3.10'},
            'volume_wan': '500', 'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'UNKNOWN',
            'confirmed_day': DAY, 'revision': 2, 'evidence': {'price': ['peer-a-message']}}},
        'context': [{'message_id': 'peer-a-message', 'direction': 'in', 'text': 'SYN-A 3.10出500万',
            'trade_day': DAY, 'ingest_revision': 21, 'historical': False}],
        'quote_constraints': {'status': 'AVAILABLE', 'price_kind': 'YIELD', 'tick_size': '0.01',
            'min_yield_pct': '3.18', 'max_yield_pct': '3.22', 'base_yield_pct': '3.20',
            'base_source': 'LAST_SENT', 'max_step_bp': '2', 'reason': None},
        'analysis_tool_results': [], 'analysis_tool_source': None, 'tool_results': []}


def plan(**changes):
    value = {'kind': 'REPLY', 'quote_value': '3.18', 'include_volume': False,
             'information_ids': [], 'question': 'NONE', 'lead': '', 'tail': ''}
    value.update(changes)
    return value


def render(ctx, value):
    before = deepcopy((ctx, value))
    result = renderer().render_reply_plan(ctx, value)
    assert (ctx, value) == before
    assert isinstance(result['proposal'], dict) and isinstance(result['economic_text'], str)
    assert isinstance(result['parts'], list)
    assert result['proposal'].get('render_plan') is not None
    return result


def accepted(ctx, result):
    before = deepcopy(ctx)
    body = Engine.__new__(Engine)._validate_reply(ctx, deepcopy(result['proposal']))
    assert ctx == before, 'Rendering/validating B must not confirm or revise A facts'
    assert body['text'] == result['proposal']['text']
    return body


def public_record(name):
    source = 'synthetic-independent-render-source'
    def fact(value):
        return {'value': value, 'quality': 'verified', 'source_ref': source,
                'as_of': DAY, 'missing_reason': None}
    rating = {'agency': 'Synthetic', 'scale': 'synthetic', 'value': 'AAA', 'rating_date': DAY}
    bonds = []
    for code, days in [('SYN-A', 365), ('SYN-B', 335)]:
        bonds.append({'bond_id': code, 'code': code, 'name': code, 'market': 'IB',
            'master': {'issuer_id': fact('SYN-ISSUER'), 'remaining_days': fact(days),
                'maturity_date': fact('2027-10-17' if code == 'SYN-A' else '2027-09-17'),
                'tenor_basis': fact('maturity'), 'issuer_rating': fact(rating)},
            'valuation': {'yield_pct': fact('2.9872'), 'valuation_date': fact(DAY),
                'provider': fact('synthetic'), 'basis': fact('maturity')}, 'quotes': []})
    issuer = {'issuer_id': 'SYN-ISSUER', 'full_name': fact('Synthetic Issuer'),
        'short_name': fact('SYN-ISSUER'), 'registered_region': fact('Synthetic Region'),
        'industry': fact({'scheme': 'synthetic', 'level': '1', 'code': 'SYN-INDUSTRY'}),
        'issuer_rating': fact(rating)}
    snapshot = ReferenceSnapshot('synthetic-reply-render-v1', DAY,
        sources=[{'id': source, 'origin': 'explicit synthetic independent fixture'}],
        bonds=bonds, issuers=[issuer])
    arguments = {
        'bond_info': {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A'},
        'issuer_info': {'schema_version': '1', 'mode': 'get', 'issuer_id': 'SYN-ISSUER'},
        'similar_bonds': {'schema_version': '1', 'anchor_bond_id': 'SYN-A', 'tenor_basis': 'maturity',
            'remaining_days': {'min': 0, 'max': 365}, 'issuer_scope': 'same',
            'compare_fields': ['remaining_days', 'valuation_yield_pct']},
        'bond_calc': {'schema_version': '1', 'operation': 'yield_gap', 'inputs': {
            'hypothetical': True, 'our_side': 'BUY', 'peer_side': 'ofr', 'same_basis': True,
            'quote_type': 'yield', 'yield_basis': 'maturity', 'peer_yield_basis': 'maturity',
            'limit_yield_pct': '3.20', 'peer_yield_pct': '3.10'}},
    }[name]
    result = BondTools(SimpleNamespace(snapshot=lambda: snapshot)).invoke(name, arguments)
    assert result['status'] in ('ok', 'partial'), result
    if name == 'similar_bonds':
        assert [row['bond_id'] for row in result['data']['candidates']] == ['SYN-B']
        comparisons = result['data']['candidates'][0]['comparisons']
        assert comparisons['remaining_days']['anchor_value'] == 365
        assert comparisons['remaining_days']['candidate_value'] == 335
        assert Decimal(comparisons['valuation_yield_pct']['candidate_value']) == Decimal('2.9872')
    return {'request': {'name': name, 'arguments': arguments}, 'result': result}


def with_public(name, *, inherited=True):
    ctx = context()
    ctx['analysis_tool_results' if inherited else 'tool_results'] = [public_record(name)]
    if inherited:
        ctx['analysis_tool_source'] = {'analysis_id': 'owned-analysis-v1', 'through_revision': 21}
    info = renderer().public_information(ctx)
    assert info and all(isinstance(row['id'], str) and isinstance(row['text'], str) for row in info)
    return ctx, info


@pytest.mark.parametrize('value', ['3.18', '3.19', '3.20', '3.21', '3.22'])
def test_all_five_authorized_grid_prices_reach_the_real_reply_validator(value):
    ctx = context()
    body = accepted(ctx, render(ctx, plan(quote_value=value)))
    assert body['our_price']['value'] == value
    assert value+'%' in body['text']


def test_large_authorized_domain_preserves_every_interior_price_not_just_recommendations():
    ctx = context()
    ctx['boundary']['value'] = '1.00'
    ctx['our_quote']['price']['value'] = '3.00'
    ctx['plan'].update(opening_yield_pct='3.00', step_bp='200')
    ctx['quote_constraints'].update(min_yield_pct='1.00', max_yield_pct='5.00',
                                    base_yield_pct='3.00', max_step_bp='200')
    seen = set()
    for tick in range(100, 501):
        value = format(Decimal(tick)/100, '.2f')
        body = accepted(ctx, render(ctx, plan(quote_value=value)))
        seen.add(body['our_price']['value'])
    assert seen == {format(Decimal(tick)/100, '.2f') for tick in range(100, 501)}


@pytest.mark.parametrize('value', ['3.17', '3.23', '3.185', '3.18e0', 'NaN', 3.18, True, None])
def test_illegal_or_non_grid_choice_is_rejected_without_clamping(value):
    with pytest.raises((InvalidFact, ValueError)):
        render(context(), plan(quote_value=value))


@pytest.mark.parametrize('inherited', [False, True])
def test_sourced_similarity_numbers_coexist_with_the_authorized_own_quote(inherited):
    ctx, info = with_public('similar_bonds', inherited=inherited)
    result = render(ctx, plan(information_ids=[row['id'] for row in info]))
    body = accepted(ctx, result)
    for value in ('SYN-B', '2.9872', '335', '365', '30', '3.18%'):
        assert value in body['text'], 'Do not obtain acceptance by deleting the requested comparison'
    assert '2.9872' not in result['economic_text'] and '335' not in result['economic_text']
    assert '3.18' in result['economic_text']
    public = [part for part in result['parts'] if part['role'] == 'PUBLIC_INFO_OR_CALC']
    assert public and all(part.get('source') for part in public)


@pytest.mark.parametrize('tool', ['bond_info', 'issuer_info', 'bond_calc'])
def test_actual_tool_facts_are_rendered_and_calculation_does_not_echo_its_inputs(tool):
    ctx, info = with_public(tool)
    result = render(ctx, plan(information_ids=[row['id'] for row in info]))
    body = accepted(ctx, result)
    if tool == 'bond_info': assert 'SYN-ISSUER' in body['text']
    elif tool == 'issuer_info':
        assert 'Synthetic Issuer' in body['text'] and 'AAA' in body['text']
        assert any(date in body['text'] for date in (DAY, '2026年10月17日', '2026/10/17'))
    else:
        assert re.search(r'10(?:\.0+)?\s*BP', body['text'], re.I)
        assert '3.10' not in body['text'] and '3.20' not in body['text']


def test_unknown_information_id_and_previous_source_version_do_not_authorize_public_numbers():
    ctx, info = with_public('issuer_info')
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, plan(information_ids=['not-an-actual-information-id']))
    old_plan = plan(information_ids=[row['id'] for row in info])
    changed = deepcopy(ctx)
    changed['analysis_tool_results'][0]['result']['snapshot_id'] = 'synthetic-reply-render-v2'
    with pytest.raises((InvalidFact, ValueError)):
        render(changed, old_plan)


@pytest.mark.parametrize('mutation', ['text', 'price', 'plan', 'economic_text', 'parts'])
def test_engine_rebuilds_the_plan_instead_of_trusting_client_rendered_fields(mutation):
    ctx, info = with_public('similar_bonds')
    result = render(ctx, plan(information_ids=[row['id'] for row in info]))
    body = deepcopy(result['proposal'])
    if mutation == 'text': body['text'] += ' 另收800万。'
    elif mutation == 'price': body['our_price']['value'] = '3.12'
    elif mutation == 'plan': body['render_plan']['quote_value'] = '3.19'
    elif mutation == 'economic_text': body['economic_text'] = ''
    else: body['parts'] = [{'role': 'PUBLIC_INFO_OR_CALC', 'text': '3.12%收800万', 'source': 'invented'}]
    with pytest.raises((InvalidFact, ValueError)):
        Engine.__new__(Engine)._validate_reply(ctx, body)


@pytest.mark.parametrize('position', ['lead', 'tail'])
@pytest.mark.parametrize('text', ['另收800万。', '公开估值2.9872%。', '我方底价是3.12。', '500万可以成交，请确认。'])
def test_free_text_never_obtains_numeric_or_execution_permission_from_its_position(position, text):
    ctx = context()
    with pytest.raises((InvalidFact, ValueError)):
        accepted(ctx, render(ctx, plan(**{position: text})))


def test_qualitative_quote_confirmation_is_allowed_without_promising_execution():
    ctx = context()
    result = render(ctx, plan(lead='您好，', tail='请确认报价。', include_volume=True))
    body = accepted(ctx, result)
    assert '您好' in body['text'] and '请确认报价' in body['text']
    assert '500' in body['text'] and '成交' not in body['text']


def cash_context(*, member='peer-a', value='34.19'):
    ctx = context()
    ctx['member_id'] = member
    ctx['basis']['member_id'] = member
    ctx['quotes']['ofr']['price'] = {'kind': 'NET_PRICE', 'value': value,
        'settlement_date': None, 'computed_yield': None, 'calculation_ref': None}
    ctx['quotes']['ofr']['evidence'] = {'price': [member+'-cash']}
    ctx['context'] = [{'message_id': member+'-cash', 'direction': 'in', 'trade_day': DAY,
        'ingest_revision': 21, 'historical': False, 'text': 'SYN-A 净价'+value+'出500万'}]
    return ctx


def settlement_plan():
    return plan(kind='CLARIFY', quote_value=None, question='ASK_SETTLEMENT')


def test_same_peer_current_cash_reference_can_ask_settlement_without_becoming_a_new_quote():
    ctx = cash_context()
    result = render(ctx, settlement_plan())
    body = accepted(ctx, result)
    assert body['kind'] == 'CLARIFY' and body.get('our_price') is None
    assert '34.19' in body['text'] and '净价' in body['text']
    assert '34.19' not in result['economic_text']
    assert any(part['role'] == 'COUNTERPARTY_REFERENCE' and part.get('source') for part in result['parts'])


@pytest.mark.parametrize('change', [{'validity': 'WITHDRAWN'}, {'confirmed_day': '2026-10-16'}, {'assertion': 'REPORTED'}])
def test_withdrawn_old_day_or_reported_cash_is_not_revived_as_current_settlement_basis(change):
    ctx = cash_context()
    ctx['quotes']['ofr'].update(change)
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, settlement_plan())
    result = render(ctx, plan(kind='CLARIFY', quote_value=None, question='ASK_CURRENT_QUOTE'))
    body = accepted(ctx, result)
    assert body.get('our_price') is None
    if '34.19' in body['text']:
        assert '上次' in body['text'] or '历史' in body['text']


def test_another_peers_cash_reference_cannot_be_submitted_under_this_member():
    first = cash_context(member='peer-a', value='34.19')
    second = cash_context(member='peer-b', value='99.32')
    other_reply = render(second, settlement_plan())['proposal']
    with pytest.raises((InvalidFact, ValueError)):
        Engine.__new__(Engine)._validate_reply(first, other_reply)


@pytest.mark.parametrize('status', ['unavailable', 'error', 'not_authorized'])
def test_non_successful_tool_status_cannot_publish_payload_facts(status):
    ctx = context()
    record = public_record('issuer_info')
    record['result']['status'] = status
    ctx['analysis_tool_results'] = [record]
    texts = ' '.join(row['text'] for row in renderer().public_information(ctx))
    assert 'Synthetic Issuer' not in texts and 'AAA' not in texts


def test_partial_tool_keeps_available_identity_without_inventing_missing_rating():
    ctx = context()
    record = public_record('issuer_info')
    record['result']['status'] = 'partial'
    record['result']['data']['issuer_rating']['value'] = None
    record['result']['missing_fields'] = ['issuer_rating']
    ctx['tool_results'] = [record]
    info = renderer().public_information(ctx)
    assert info
    texts = ' '.join(row['text'] for row in info)
    assert 'Synthetic Issuer' in texts and 'AAA' not in texts and 'None' not in texts
    accepted(ctx, render(ctx, plan(information_ids=[row['id'] for row in info])))


@pytest.mark.parametrize('change', ['member', 'thread', 'basis', 'quote_revision', 'constraints'])
def test_bound_plan_cannot_replay_after_its_member_or_fact_basis_changes(change):
    ctx = context()
    rendered = render(ctx, plan())
    bound = rendered['proposal']['render_plan']
    assert isinstance(bound.get('context_ref'), str) and bound['context_ref']
    newer = deepcopy(ctx)
    if change == 'member': newer['member_id'] = 'peer-b'
    elif change == 'thread': newer['thread_id'] = 'other-thread'
    elif change == 'basis': newer['basis']['member_version'] += 1
    elif change == 'quote_revision': newer['quotes']['ofr']['revision'] += 1
    else: newer['quote_constraints']['max_yield_pct'] = '3.21'
    with pytest.raises((InvalidFact, ValueError)):
        renderer().render_reply_plan(newer, bound)
    with pytest.raises((InvalidFact, ValueError)):
        Engine.__new__(Engine)._validate_reply(newer, rendered['proposal'])


def test_same_cash_value_for_two_peers_still_has_a_member_bound_reference():
    first = cash_context(member='peer-a', value='34.19')
    second = cash_context(member='peer-b', value='34.19')
    reply = render(second, settlement_plan())['proposal']
    with pytest.raises((InvalidFact, ValueError)):
        Engine.__new__(Engine)._validate_reply(first, reply)


@pytest.mark.parametrize('cash', [False, True])
def test_validated_rendered_reply_can_enter_the_unchanged_native_command_body_contract(cash):
    ctx = cash_context() if cash else context()
    value = settlement_plan() if cash else plan()
    rendered = render(ctx, value)
    validated = accepted(ctx, rendered)
    command_body = Commands._body(validated)
    assert command_body['text'] == rendered['proposal']['text']
    assert command_body['kind'] == ('CLARIFY' if cash else 'REPLY')
    assert 'render_plan' not in command_body, 'The internal planning record is not part of the native message body'


def test_sell_domain_preserves_all_five_prices_and_the_authorized_direction():
    ctx = context()
    ctx['our_side'] = 'SELL'
    ctx['quotes'] = {'bid': ctx['quotes']['ofr']}
    ctx['quotes']['bid']['side'] = 'bid'
    ctx['our_quote']['price']['value'] = '3.00'
    ctx['plan']['opening_yield_pct'] = '3.00'
    ctx['quote_constraints'].update(min_yield_pct='2.98', max_yield_pct='3.02', base_yield_pct='3.00')
    for value in ('2.98', '2.99', '3.00', '3.01', '3.02'):
        body = Commands._body(accepted(ctx, render(ctx, plan(quote_value=value))))
        assert body['our_price']['value'] == value and value+'%出' in body['text']


def test_an_authorized_domain_crossing_zero_keeps_negative_zero_and_positive_prices():
    ctx = context()
    ctx['boundary']['value'] = '-0.10'
    ctx['our_quote']['price']['value'] = '0.00'
    ctx['plan'].update(opening_yield_pct='0.00', step_bp='10')
    ctx['quote_constraints'].update(min_yield_pct='-0.10', max_yield_pct='0.10',
                                    base_yield_pct='0.00', max_step_bp='10')
    actual = set()
    for tick in range(-10, 11):
        value = format(Decimal(tick)/100, '.2f')
        body = Commands._body(accepted(ctx, render(ctx, plan(quote_value=value))))
        actual.add(body['our_price']['value'])
    assert actual == {format(Decimal(tick)/100, '.2f') for tick in range(-10, 11)}


@pytest.mark.parametrize('price_kind,value', [('NET_PRICE', '34.19'), ('DIRTY_PRICE', '99.32')])
@pytest.mark.parametrize('missing', ['all', 'settlement_date', 'computed_yield', 'calculation_ref'])
def test_controlled_plan_cannot_skip_required_cash_clarification_by_selecting_a_legal_yield(
        price_kind, value, missing):
    ctx = cash_context(value=value)
    ctx['context_policy_revision'] = 'latest_quote_confirmation_v1'
    cash = ctx['quotes']['ofr']['price']
    cash.update(kind=price_kind, settlement_date=DAY, computed_yield='3.10',
                calculation_ref='synthetic-server-verified-calculation')
    for field in ('settlement_date', 'computed_yield', 'calculation_ref'):
        if missing in ('all', field): cash[field] = None
    before = deepcopy(ctx)
    assert ctx['quote_constraints']['status'] == 'AVAILABLE'
    assert (ctx['quote_constraints']['min_yield_pct'], ctx['quote_constraints']['max_yield_pct']) == ('3.18', '3.22')
    with pytest.raises((InvalidFact, ValueError)):
        accepted(ctx, render(ctx, plan(quote_value='3.19')))
    assert ctx == before, 'Required clarification must not shrink the authorized numeric domain or revise facts'


@pytest.mark.parametrize('price_kind,value,label', [('NET_PRICE', '34.19', '净价'), ('DIRTY_PRICE', '99.32', '全价')])
def test_controlled_cash_clarification_and_legacy_independent_yield_permission_remain_distinct(
        price_kind, value, label):
    ctx = cash_context(value=value)
    ctx['quotes']['ofr']['price']['kind'] = price_kind
    result = render(ctx, settlement_plan())
    body = Commands._body(accepted(ctx, result))
    assert body['kind'] == 'CLARIFY' and value in body['text'] and label in body['text']
    assert body.get('our_price') is None
    # The newly authorized decision rule is a controlled-plan requirement.
    # Preserve the explicitly retained old bare-proposal independent-yield contract.
    legacy = {'kind': 'REPLY', 'text': 'SYN-A 3.18%收',
              'our_price': {'kind': 'YIELD', 'value': '3.18'}}
    assert Engine.__new__(Engine)._validate_reply(ctx, legacy)['our_price']['value'] == '3.18'


@pytest.mark.parametrize('price_kind,value', [('NET_PRICE', '34.19'), ('DIRTY_PRICE', '99.32')])
def test_current_cash_with_all_verified_conversion_inputs_can_use_the_controlled_yield_plan(price_kind, value):
    ctx = cash_context(value=value)
    ctx['quotes']['ofr']['price'].update(kind=price_kind, settlement_date=DAY,
        computed_yield='3.10', calculation_ref='synthetic-server-verified-calculation')
    body = Commands._body(accepted(ctx, render(ctx, plan())))
    assert body['kind'] == 'REPLY' and body['our_price']['value'] == '3.18'


def generated_reply_nodes(version):
    from dify.negotiation_v22.build import build
    document = build('reply', version=version, provider='synthetic', model='synthetic')
    nodes = {row['id']: row['data'] for row in document['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    return projection['main'], contract['main']


def wire_context(ctx, policy='latest_quote_confirmation_v1'):
    return {**deepcopy(ctx), 'schema_version': '2.2', 'stage': 'reply',
            'context_policy_revision': policy}


def test_generated_reply_nodes_preserve_source_catalog_and_enforce_cash_requirements_before_engine():
    for version in ('0.15.3', '1.17.1'):
        project, decode = generated_reply_nodes(version)
        public, _ = with_public('similar_bonds')
        for ctx, selection in ((wire_context(public), plan()),
                               (wire_context(cash_context()), settlement_plan())):
            before = deepcopy(ctx)
            projected = json.loads(project(json.dumps(ctx, ensure_ascii=False))['context'])
            catalog = renderer().public_information(ctx)
            assert projected['public_information_candidates'] == catalog
            assert projected['reply_requirements'] == renderer().reply_requirements(ctx)
            assert projected['quotes'] == ctx['quotes']
            assert projected['quote_constraints'] == ctx['quote_constraints']
            if catalog:
                selection = {**selection, 'information_ids': [row['id'] for row in catalog]}
            else:
                assert projected['reply_requirements']['allowed_kinds'] == ['CLARIFY']
                assert projected['reply_requirements']['question'] == 'ASK_SETTLEMENT'
                with pytest.raises(ValueError):
                    decode(json.dumps({'schema_version': '2.2', 'action': 'SEND',
                                       'reply_plan': plan()}), json.dumps(ctx))
            response = {'schema_version': '2.2', 'action': 'SEND', 'reply_plan': selection,
                        'trader_view': {'reason': 'Synthetic source-bound decision'}}
            result = json.loads(decode(json.dumps(response), json.dumps(ctx))['result'])
            implicit_send = {key: value for key, value in response.items() if key != 'action'}
            assert json.loads(decode(json.dumps(implicit_send), json.dumps(ctx))['result']) == result
            with pytest.raises(ValueError):
                decode(json.dumps({**response, 'action': 'REPLY'}), json.dumps(ctx))
            expected = render(ctx, selection)
            assert result['proposal'] == expected['proposal']
            body = Commands._body(accepted(ctx, {'proposal': result['proposal']}))
            assert body['text'] == expected['proposal']['text'] and 'render_plan' not in body
            if catalog:
                assert all(value in body['text'] for value in ('2.9872', '335', '365', '30', '3.18'))
            assert ctx == before


def test_latest_policy_cannot_fall_back_to_bare_or_split_text_send_but_legacy_contract_remains():
    bare = {'kind': 'REPLY', 'text': 'SYN-A 3.18%收',
            'our_price': {'kind': 'YIELD', 'value': '3.18'}}
    split = {key: value for key, value in bare.items() if key != 'text'}
    split.update(information_text='', negotiation_text=bare['text'])
    for version in ('0.15.3', '1.17.1'):
        _, decode = generated_reply_nodes(version)
        for proposal in (bare, split):
            response = {'schema_version': '2.2', 'action': 'SEND', 'proposal': proposal}
            latest = wire_context(cash_context())
            with pytest.raises(ValueError):
                decode(json.dumps(response), json.dumps(latest))
            legacy = wire_context(cash_context(), 'explicit_anchors_v0')
            decoded = json.loads(decode(json.dumps(response), json.dumps(legacy))['result'])
            assert decoded['proposal'] == bare
            body = Commands._body(Engine.__new__(Engine)._validate_reply(legacy, decoded['proposal']))
            assert body['kind'] == bare['kind'] and body['text'] == bare['text']
            assert body['our_price']['kind'] == 'YIELD' and body['our_price']['value'] == '3.18'
            assert all(body['our_price'].get(field) is None for field in
                       ('settlement_date', 'computed_yield', 'calculation_ref'))


def test_latest_policy_keeps_non_sending_decisions_and_rejects_foreign_bound_plans():
    ctx = wire_context(context())
    foreign = deepcopy(ctx)
    foreign['member_id'] = 'another-peer'
    foreign['basis']['member_id'] = 'another-peer'
    foreign_plan = render(foreign, plan())['proposal']['render_plan']
    for version in ('0.15.3', '1.17.1'):
        _, decode = generated_reply_nodes(version)
        for action in ('WAIT', 'HANDOFF'):
            response = {'schema_version': '2.2', 'action': action,
                        'trader_view': {'reason': 'Synthetic non-sending decision'}}
            assert json.loads(decode(json.dumps(response), json.dumps(ctx))['result']) == response
        with pytest.raises(ValueError):
            decode(json.dumps({'schema_version': '2.2', 'action': 'SEND',
                               'reply_plan': foreign_plan}), json.dumps(ctx))
