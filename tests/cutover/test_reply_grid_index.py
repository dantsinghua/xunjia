"""An integer selects every existing legal quote tick, never new authority."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.commands import Commands
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from test_rendered_reply_plan_revision import (
    context, cash_context, plan, renderer, render, accepted,
)


GRID_FIELDS = {'status', 'first_yield_pct', 'last_yield_pct', 'tick_size',
               'index_min', 'index_max', 'count'}


def grid(ctx):
    method = getattr(renderer(), 'quote_grid', None)
    assert callable(method), 'The compact complete quote-grid projection is missing'
    before = deepcopy(ctx)
    value = method(ctx)
    assert set(value) == GRID_FIELDS and ctx == before
    assert value['tick_size'] == '0.01'
    return value


def indexed(index, **changes):
    value = plan()
    value.pop('quote_value')
    value['quote_index'] = index
    value.update(changes)
    return value


def body(ctx, selection):
    return Commands._body(accepted(ctx, render(ctx, selection)))


def wide_context(lower='1.00', upper='5.00', base='3.00', step='200'):
    ctx = context()
    ctx['boundary']['value'] = lower
    ctx['our_quote']['price']['value'] = base
    ctx['plan'].update(opening_yield_pct=base, step_bp=step)
    ctx['quote_constraints'].update(min_yield_pct=lower, max_yield_pct=upper,
                                    base_yield_pct=base, max_step_bp=step)
    return ctx


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_all_five_indexes_map_exactly_to_the_original_legal_prices_and_native_body(side):
    ctx = context()
    values = ['3.18', '3.19', '3.20', '3.21', '3.22']
    if side == 'SELL':
        ctx['our_side'] = 'SELL'
        ctx['quotes'] = {'bid': {**ctx['quotes']['ofr'], 'side': 'bid'}}
        ctx['our_quote']['price']['value'] = ctx['plan']['opening_yield_pct'] = '3.00'
        ctx['quote_constraints'].update(min_yield_pct='2.98', max_yield_pct='3.02', base_yield_pct='3.00')
        values = ['2.98', '2.99', '3.00', '3.01', '3.02']
    assert grid(ctx) == {'status': 'AVAILABLE', 'first_yield_pct': values[0],
        'last_yield_pct': values[-1], 'tick_size': '0.01', 'index_min': 0, 'index_max': 4, 'count': 5}
    for index, value in enumerate(values):
        actual = body(ctx, indexed(index))
        assert actual == body(ctx, plan(quote_value=value))
        assert actual['our_price']['value'] == value
        assert value+'%'+('收' if side == 'BUY' else '出') in actual['text']
        assert 'render_plan' not in actual


def test_every_interior_tick_is_selectable_without_removing_points_from_a_large_domain():
    ctx = wide_context()
    assert grid(ctx)['count'] == 401 and grid(ctx)['index_max'] == 400
    actual = [body(ctx, indexed(index))['our_price']['value'] for index in range(401)]
    assert actual == [format(Decimal(tick)/100, '.2f') for tick in range(100, 501)]


def test_million_point_domain_has_a_constant_size_projection_and_exact_sampled_indexes():
    ctx = wide_context('-5000.00', '5000.00', '0.00', '500000')
    value = grid(ctx)
    assert value['count'] == 1000001 and value['index_max'] == 1000000
    assert len(json.dumps(value)) < 250 and all(not isinstance(v, (dict, list, tuple)) for v in value.values())
    for index in (0, 1, 499999, 500000, 500001, 999999, 1000000):
        expected = format(Decimal('-5000') + Decimal(index)/100, '.2f')
        assert body(ctx, indexed(index))['our_price']['value'] == expected


@pytest.mark.parametrize('lower,upper,base,step,expected', [
    ('3.181', '3.229', '3.20', '2.9', ['3.19', '3.20', '3.21', '3.22']),
    ('-0.019', '0.019', '0.00', '1.9', ['-0.01', '0.00', '0.01']),
])
def test_non_grid_bounds_preserve_all_existing_legal_ticks_without_changing_constraints(lower, upper, base, step, expected):
    ctx = wide_context(lower, upper, base, step)
    original = deepcopy(ctx['quote_constraints'])
    value = grid(ctx)
    assert value['first_yield_pct'] == expected[0] and value['last_yield_pct'] == expected[-1]
    assert value['count'] == len(expected) and value['index_max'] == len(expected)-1
    for index, price in enumerate(expected):
        assert body(ctx, indexed(index)) == body(ctx, plan(quote_value=price))
    assert ctx['quote_constraints'] == original


@pytest.mark.parametrize('index', [True, False, -1, 5, 10**30, 0.0, '0', None])
def test_invalid_index_is_rejected_without_coercion_or_clamping(index):
    ctx = context()
    before = deepcopy(ctx)
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, indexed(index))
    assert ctx == before


@pytest.mark.parametrize('value', ['3.18', None])
def test_index_and_value_fields_are_mutually_exclusive_even_with_a_null_value(value):
    with pytest.raises((InvalidFact, ValueError)):
        render(context(), indexed(0, quote_value=value))


@pytest.mark.parametrize('kind', ['CLARIFY', 'RECONFIRM', 'ASK_VOLUME', 'SOCIAL'])
def test_non_reply_action_cannot_carry_an_economic_index(kind):
    with pytest.raises((InvalidFact, ValueError)):
        render(context(), indexed(0, kind=kind, question='NONE', lead='请确认报价。'))


@pytest.mark.parametrize('status', ['EMPTY', 'UNAVAILABLE', 'AVAILABLE_WITHOUT_TICK'])
def test_empty_or_unavailable_domain_has_no_index_and_cannot_generate_a_quote(status):
    ctx = context()
    if status == 'AVAILABLE_WITHOUT_TICK':
        ctx['quote_constraints'].update(min_yield_pct='3.181', max_yield_pct='3.189')
        expected_status = 'EMPTY'
    else:
        ctx['quote_constraints'].update(status=status, min_yield_pct=None, max_yield_pct=None)
        expected_status = status
    value = grid(ctx)
    assert value == {'status': expected_status, 'first_yield_pct': None, 'last_yield_pct': None,
                    'tick_size': '0.01', 'index_min': None, 'index_max': None, 'count': 0}
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, indexed(0))


@pytest.mark.parametrize('change', [{'tick_size': '0.02'}, {'min_yield_pct': None},
                                  {'max_yield_pct': 'Infinity'}, {'min_yield_pct': 'NaN'}])
def test_invalid_grid_inputs_never_create_a_legal_selected_index(change):
    ctx = context()
    ctx['quote_constraints'].update(change)
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, indexed(0))


@pytest.mark.parametrize('change', ['member', 'side', 'basis', 'grid'])
def test_old_bound_index_cannot_be_reinterpreted_under_new_context(change):
    ctx = context()
    rendered = render(ctx, indexed(0))
    newer = deepcopy(ctx)
    if change == 'member': newer['member_id'] = 'other-member'
    elif change == 'side': newer['our_side'] = 'SELL'
    elif change == 'basis': newer['basis']['member_version'] += 1
    else: newer['quote_constraints'].update(min_yield_pct='3.20', max_yield_pct='3.22')
    with pytest.raises((InvalidFact, ValueError)):
        render(newer, rendered['proposal']['render_plan'])
    with pytest.raises((InvalidFact, ValueError)):
        Engine.__new__(Engine)._validate_reply(newer, rendered['proposal'])


def test_supplied_grid_projection_cannot_widen_the_authoritative_constraints():
    ctx = context()
    ctx['quote_grid'] = {'status': 'AVAILABLE', 'first_yield_pct': '3.10',
        'last_yield_pct': '3.22', 'tick_size': '0.01', 'index_min': 0, 'index_max': 12, 'count': 13}
    assert grid(ctx)['first_yield_pct'] == '3.18'
    assert body(ctx, indexed(0))['our_price']['value'] == '3.18'
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, indexed(12))


@pytest.mark.parametrize('reason', ['cash', 'cross_day', 'unresolved'])
def test_numeric_grid_availability_does_not_authorize_a_reply_before_required_clarification(reason):
    ctx = cash_context() if reason == 'cash' else context()
    if reason == 'cross_day': ctx['reconfirm_day'] = '2026-10-18'
    elif reason == 'unresolved': ctx['unresolved_messages'] = [{'message_id': 'unclear'}]
    assert grid(ctx)['status'] == 'AVAILABLE'
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, indexed(0))


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_code_projects_current_grid_and_maps_index_through_real_engine(version):
    ctx = {**context(), 'schema_version': '2.2', 'stage': 'reply',
           'context_policy_revision': 'latest_quote_confirmation_v1'}
    nodes = {node['id']: node['data'] for node in build('reply', version=version,
             provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    projected = json.loads(projection['main'](json.dumps(ctx))['context'])
    assert projected['quote_grid'] == grid(ctx)
    assert projected['quote_constraints'] == ctx['quote_constraints']
    for index, value in enumerate(('3.18', '3.19', '3.20', '3.21', '3.22')):
        response = {'schema_version': '2.2', 'reply_plan': indexed(index)}
        decoded = json.loads(contract['main'](json.dumps(response), json.dumps(ctx))['result'])
        assert decoded['action'] == 'SEND'
        assert decoded['proposal'] == render(ctx, indexed(index))['proposal']
        checked = Commands._body(Engine.__new__(Engine)._validate_reply(ctx, decoded['proposal']))
        assert checked['our_price']['value'] == value and 'render_plan' not in checked


def test_legacy_quote_value_still_uses_its_original_strict_numeric_gate():
    ctx = context()
    for value in ('3.18', '3.19', '3.20', '3.21', '3.22'):
        assert body(ctx, plan(quote_value=value))['our_price']['value'] == value
    for invalid in ('3.17', '3.23', '3.185', '3.18e0', 'NaN', True, 3.18, None):
        with pytest.raises((InvalidFact, ValueError)):
            render(ctx, plan(quote_value=invalid))
