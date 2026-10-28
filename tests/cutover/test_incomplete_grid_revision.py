"""An incomplete projection may ask a question but must not invent quote ticks."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.reply_render import render_reply_plan
from tests.cutover.test_rendered_reply_plan_revision import context, cash_context, plan, settlement_plan


def missing_tick():
    value = context()
    del value['quote_constraints']['tick_size']
    return value


def test_absent_tick_cannot_render_a_literal_without_filling_original_constraints():
    value = missing_tick()
    before = deepcopy(value)
    with pytest.raises(ValueError):
        render_reply_plan(value, plan())
    assert value == before and 'tick_size' not in value['quote_constraints']


@pytest.mark.parametrize('selection', [{'quote_index': 0}, {'quote_value': '3.18'}])
def test_absent_tick_cannot_authorize_index_or_literal_reply(selection):
    value = missing_tick()
    choice = plan()
    del choice['quote_value']
    choice.update(selection)
    before = deepcopy((value, choice))
    with pytest.raises((ValueError, KeyError)):
        render_reply_plan(value, choice)
    assert (value, choice) == before


@pytest.mark.parametrize('tick', [None, '0.02'])
def test_present_invalid_tick_is_still_rejected_instead_of_becoming_missing(tick):
    value = context()
    value['quote_constraints']['tick_size'] = tick
    with pytest.raises(ValueError):
        render_reply_plan(value, plan())


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_projection_retains_cash_facts_without_granting_a_price_domain(version):
    value = {**cash_context(), 'schema_version': '2.2', 'stage': 'reply',
             'context_policy_revision': 'latest_quote_confirmation_v1'}
    value['context'][0].update(native_id='synthetic-cash-native', event_id='synthetic-cash-event',
        thread_id=value['thread_id'], target_id=None, source_seq=21, content_hash='synthetic-source-hash')
    del value['quote_constraints']['tick_size']
    before = deepcopy(value)
    nodes = {node['id']: node['data'] for node in build('reply', version=version,
             provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    projected = json.loads(projection['main'](json.dumps(value))['context'])
    assert projected['quotes'] == value['quotes']
    mechanical = {'message_id', 'native_id', 'event_id', 'thread_id', 'target_id', 'source_seq', 'content_hash'}
    expected_messages = [{key: item for key, item in message.items() if key not in mechanical}
                         for message in value['context']]
    assert projected['context'] == expected_messages
    assert all(not mechanical.intersection(message) for message in projected['context'])
    assert [message['text'] for message in projected['context']] == [message['text'] for message in value['context']]
    assert projected['quote_constraints'] == value['quote_constraints']
    assert 'quote_grid' not in projected
    assert 'tick_size' not in projected['quote_constraints']
    choice = settlement_plan()
    context_input = next(row for row in nodes['contract']['variables'] if row['variable'] == 'context_json')
    assert context_input['value_selector'] == ['start', 'context_json']
    result = json.loads(contract['main'](json.dumps({'schema_version': '2.2', 'reply_plan': choice}),
                                         json.dumps(value))['result'])
    assert result['action'] == 'SEND' and result['proposal']['kind'] == 'CLARIFY'
    assert result['proposal'].get('our_price') is None
    assert value == before
