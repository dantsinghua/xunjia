"""A fresh quote is retained even before the day's actual confirmation reply."""
from types import SimpleNamespace

import pytest

from dify.negotiation_v22.node_code import decode_stage_result
from server.negotiation.engine import Engine
from server.negotiation.slots import collect_candidates


def commit_selected(world, member, thread, incoming):
    job = world.store.claim_analysis(world.owner, thread, force=True)
    context = {**job['snapshot'], 'schema_version': '2.2', 'stage': 'analysis'}
    candidates = [c for c in collect_candidates(context) if c['message_id'] == incoming]
    def select(value, role):
        return {'candidate_id': next(c['id'] for c in candidates if c['value'] == value and role in c['roles']),
                'role': role}
    wire = {'schema_version': '2.2', 'scopes': {m['message_id']: [member] for m in context['messages']},
            'unresolved': {}, 'observations': [{'member_id': member, 'source_ids': [incoming], 'side': 'ofr',
                'slots': {'price': select('3.15', 'YIELD'), 'volume_wan': select('500万', 'QUANTITY')},
                'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': 'FINAL',
                            'reason': '本人本券出价，明确没有改善空间。'}}]}
    decoded = decode_stage_result(wire, context, 'analysis')
    engine = Engine(SimpleNamespace(store=world.store, commands_core=world.commands, reference_provider=None),
                    SimpleNamespace(), None)
    scopes, observations, unresolved = engine._normalize(context, decoded)
    saved = world.store.commit_analysis(world.owner, job['analysis_id'], job['lease_token'],
        scopes=scopes, observations=observations, unresolved=unresolved, reconfirm_current_quotes=True)
    return context, saved


@pytest.mark.parametrize('question', ['none', 'queued', 'unknown', 'conflicting_receipt', 'sent_after_inbound', 'sent'])
def test_cross_day_quote_survives_until_actual_sent_question_and_later_inbound(world, question):
    group, member, thread = world.make()
    world.quote(member, thread)
    world.store.rollover(world.owner, group, trade_day='2026-10-19')
    command = None
    if question != 'none':
        command = world.queue(member, kind='RECONFIRM')['command_id']
        if question != 'queued':
            assert world.commands.claim(world.owner, command)
        if question in ('sent', 'conflicting_receipt'):
            world.receipt(command, 'SENT', 'synthetic-reconfirm')
        if question == 'conflicting_receipt':
            assert world.receipt(command, 'FAILED') == 'UNKNOWN'
        if question == 'unknown':
            world.receipt(command, 'UNKNOWN')
    incoming = world.message(thread, 'SYN-A 3.15出500万，不能再改善了', day='2026-10-19')
    if question == 'sent_after_inbound':
        world.clock.tick(1)
        world.receipt(command, 'SENT', 'synthetic-later-question')
    context, saved = commit_selected(world, member, thread, incoming)
    assert saved['accepted'] and not saved['rejected'], saved['rejected']
    quote = world.store.quote(world.owner, member, 'ofr')
    assert quote.price.value == '3.15' and quote.volume_wan == '500'
    assert quote.finality == 'FINAL'
    peer = next(p for p in context['members'] if p['member_id'] == member)
    if question == 'sent':
        assert peer['reconfirm_sent_at'] is not None
        assert quote.confirmed_day == '2026-10-19'
        assert world.member(member)['reconfirm_day'] is None
    else:
        assert quote.confirmed_day is None
        assert world.member(member)['reconfirm_day'] == '2026-10-19'
        assert world.store.group(world.owner, group)['summary']['final_confirmed_volume_wan'] == '0'
