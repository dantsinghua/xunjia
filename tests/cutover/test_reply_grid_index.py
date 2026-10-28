"""The retired index grid cannot return; literal quotes retain exact safeguards."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.commands import Commands
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from test_rendered_reply_plan_revision import context, cash_context, plan, renderer, render, accepted


def body(ctx, selection):
    return Commands._body(accepted(ctx, render(ctx, selection)))


def wide_context(lower='1.00', base='3.00', step='2'):
    ctx = context()
    ctx['quotes'] = {}  # Grid/rounding contract is independent of current-offer protection.
    ctx['boundary']['value'] = lower
    ctx['our_quote']['price']['value'] = base
    ctx['plan'].update(opening_yield_pct=base, step_bp=step)
    ctx['quote_constraints'] = Engine._quote_constraints(ctx)
    return ctx


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_literal_prices_inside_and_beyond_the_old_step_neighborhood_reach_the_native_body(side):
    ctx = context()
    if side == 'BUY':
        values = ['3.12', '3.13', '3.18', '3.20', '3.25', '4.00']
    else:
        ctx['our_side'] = 'SELL'
        ctx['quotes'] = {'bid': {**ctx['quotes']['ofr'], 'side': 'bid',
            'price': {'kind': 'YIELD', 'value': '3.12'}}}
        ctx['context'][0]['text'] = 'SYN-A 3.12收500万'
        ctx['our_quote']['price']['value'] = ctx['plan']['opening_yield_pct'] = '3.00'
        values = ['2.00', '2.95', '3.00', '3.02', '3.11', '3.12']
    ctx['quote_constraints'] = Engine._quote_constraints(ctx)
    assert not hasattr(renderer(), 'quote_grid'), 'The superseded grid must be removed'
    for value in values:
        actual = body(ctx, plan(quote_value=value))
        assert actual['our_price']['value'] == value
        assert value+'%'+('收' if side == 'BUY' else '出') in actual['text']
        assert 'render_plan' not in actual


def test_every_interior_tick_remains_selectable_without_index_or_recommendation_filtering():
    ctx = wide_context()
    actual = [body(ctx, plan(quote_value=format(Decimal(tick)/100, '.2f')))['our_price']['value']
              for tick in range(100, 501)]
    assert actual == [format(Decimal(tick)/100, '.2f') for tick in range(100, 501)]
    assert ctx['quote_constraints']['max_yield_pct'] is None


def test_wide_domain_has_constant_size_and_does_not_materialize_or_cap_a_million_prices():
    ctx = wide_context('-5000.00', '0.00', '2')
    value = ctx['quote_constraints']
    assert value['min_yield_pct'] == '-5000.00' and value['max_yield_pct'] is None
    assert len(json.dumps(value)) < 400 and all(not isinstance(v, (dict, list, tuple)) for v in value.values())
    for amount in ('-5000.00', '-4999.99', '-0.01', '0.00', '0.01', '4999.99', '5000.00'):
        assert body(ctx, plan(quote_value=amount))['our_price']['value'] == amount


@pytest.mark.parametrize('lower,first,below', [('3.181', '3.19', '3.18'), ('-0.019', '-0.01', '-0.02')])
def test_non_tick_hard_limit_rounds_inward_without_clamping_the_model_price(lower, first, below):
    ctx = wide_context(lower, '3.20')
    before = deepcopy(ctx)
    assert body(ctx, plan(quote_value=first))['our_price']['value'] == first
    assert ctx['quote_constraints']['min_yield_pct'] == first
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, plan(quote_value=below))
    assert ctx == before


@pytest.mark.parametrize('index', [True, False, -1, 0, 1, 5, 10**30, 0.0, '0', None])
def test_every_retired_index_form_is_rejected_without_coercion_or_compatibility(index):
    ctx = context()
    before = deepcopy(ctx)
    selection = plan()
    selection.pop('quote_value')
    selection['quote_index'] = index
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, selection)
    assert ctx == before


@pytest.mark.parametrize('kind', ['CLARIFY', 'RECONFIRM', 'ASK_VOLUME', 'SOCIAL'])
@pytest.mark.parametrize('economic_field', [{'quote_value': '3.18'}, {'quote_index': 0}])
def test_non_reply_actions_cannot_carry_a_hidden_economic_selection(kind, economic_field):
    with pytest.raises((InvalidFact, ValueError)):
        render(context(), {'kind': kind, 'text': '请确认当前条件？', **economic_field})


@pytest.mark.parametrize('status', ['EMPTY', 'UNAVAILABLE'])
def test_unavailable_domain_cannot_generate_a_literal_quote(status):
    ctx = context()
    ctx['quote_constraints'].update(status=status, min_yield_pct=None, max_yield_pct=None)
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, plan())


@pytest.mark.parametrize('change', [{'tick_size': '0.02'}, {'min_yield_pct': 'Infinity'}, {'min_yield_pct': 'NaN'}])
def test_invalid_domain_never_creates_a_legal_literal_quote(change):
    ctx = context()
    ctx['quote_constraints'].update(change)
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, plan())


@pytest.mark.parametrize('change', ['member', 'side', 'basis', 'domain'])
def test_bound_literal_plan_cannot_be_reinterpreted_after_its_context_changes(change):
    ctx = context()
    rendered = render(ctx, plan())
    newer = deepcopy(ctx)
    if change == 'member': newer['member_id'] = 'other-member'
    elif change == 'side': newer['our_side'] = 'SELL'
    elif change == 'basis': newer['basis']['member_version'] += 1
    else: newer['quote_constraints']['min_yield_pct'] = '3.19'
    with pytest.raises((InvalidFact, ValueError)):
        render(newer, rendered['proposal']['render_plan'])
    with pytest.raises((InvalidFact, ValueError)):
        Engine.__new__(Engine)._validate_reply(newer, rendered['proposal'])


def test_fabricated_retired_grid_projection_cannot_widen_the_hard_authoritative_domain():
    ctx = context()
    ctx['quote_grid'] = {'status': 'AVAILABLE', 'first_yield_pct': '3.10',
        'last_yield_pct': '3.22', 'tick_size': '0.01', 'index_min': 0, 'index_max': 12}
    assert body(ctx, plan(quote_value='3.12'))['our_price']['value'] == '3.12'
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, plan(quote_value='3.11'))


@pytest.mark.parametrize('reason', ['cash', 'cross_day', 'unresolved'])
def test_legal_literal_price_does_not_skip_a_required_clarification(reason):
    ctx = cash_context() if reason == 'cash' else context()
    if reason == 'cross_day': ctx['reconfirm_day'] = '2026-10-18'
    elif reason == 'unresolved': ctx['unresolved_messages'] = [{'message_id': 'unclear'}]
    with pytest.raises((InvalidFact, ValueError)):
        render(ctx, plan())


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_code_projects_a_soft_reference_and_decodes_exact_literal_prices(version):
    ctx = {**context(), 'schema_version': '2.2', 'stage': 'reply',
           'context_policy_revision': 'latest_quote_confirmation_v1'}
    nodes = {node['id']: node['data'] for node in build('reply', version=version,
             provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    projected = json.loads(projection['main'](json.dumps(ctx))['context'])
    assert 'quote_grid' not in projected
    assert projected['quote_constraints'] == ctx['quote_constraints']
    assert projected['quote_constraints']['reference_step_bp'] == '2'
    for value in ('3.12', '3.15', '3.18', '3.20', '3.25'):
        response = {'schema_version': '2.2', 'reply_plan': plan(quote_value=value)}
        decoded = json.loads(contract['main'](json.dumps(response), json.dumps(ctx))['result'])
        assert decoded['action'] == 'SEND'
        assert decoded['proposal'] == render(ctx, plan(quote_value=value))['proposal']
        checked = Commands._body(Engine.__new__(Engine)._validate_reply(ctx, decoded['proposal']))
        assert checked['our_price']['value'] == value and 'render_plan' not in checked


@pytest.mark.parametrize('invalid', ['3.11', '3.185', '3.18e0', 'NaN', True, 3.18, None])
def test_literal_value_has_strict_numeric_spelling_tick_and_final_limit(invalid):
    with pytest.raises((InvalidFact, ValueError)):
        render(context(), plan(quote_value=invalid))
