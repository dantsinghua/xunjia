"""Synthetic responses exercise real Engine text repair and retained audit traces."""
from copy import deepcopy

import pytest

from dify.negotiation_v22 import measurement as statistics
from dify.negotiation_v22.node_code import compact_context
from scripts import measure_dify_v22 as runner


@pytest.mark.parametrize('first_text', ['SYN-A 3.18收500万', 'SYN-A 3.18%出500万'])
def test_measurement_retains_initial_reply_rejection_and_text_only_repair(monkeypatch, first_text):
    seen = []

    class Client:
        def __init__(self, *args, stage, **kwargs):
            self.stage, self.last_evidence = stage, {}

        def call(self, context):
            seen.append((self.stage, deepcopy(context)))
            self.last_evidence = {'stage': self.stage, 'elapsed_ms': 1, 'message_id': 'synthetic-' + str(len(seen))}
            if self.stage == 'reply':
                feedback = context.get('validation_feedback')
                if feedback:
                    assert feedback['mode'] == 'TEXT_ONLY'
                    assert feedback['attempt'] == feedback['max_attempts'] == 1
                    result = deepcopy(feedback['original_response'])
                    result['proposal']['text'] = 'SYN-A 3.18%收500万'
                    return result
                return {'schema_version': '2.2', 'action': 'SEND',
                        'proposal': {'kind': 'REPLY', 'text': first_text,
                                     'our_price': {'kind': 'YIELD', 'value': '3.18'}},
                        'trader_view': {'reason': '仍按授权步长谈价'}}
            current = context['messages'][-1]
            member = context['members'][0]
            changes = {'price': {'kind': 'YIELD', 'value': '3.10'}, 'volume_wan': '500万',
                       'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'NEGOTIABLE',
                       'confirmed_day': '2026-10-17'}
            return {'schema_version': '2.2', 'scopes': {current['message_id']: [member['member_id']]},
                    'unresolved': {}, 'observations': [{'member_id': member['member_id'], 'side': 'ofr',
                        'changes': changes, 'evidence': {key: [current['message_id']] for key in changes},
                        'segments': [{'message_id': current['message_id'], 'text': current['text']}],
                        'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                            'negotiation_space': 'NEGOTIABLE', 'reason': '本轮出价出量且仍可谈'}}]}

    monkeypatch.setattr(runner, 'ChatflowClient', Client)
    config = {'source': {'base_url': 'https://synthetic.invalid'},
              'apps': {stage: {'key': 'synthetic-only'} for stage in ('analysis', 'reply')}}
    result = runner.run_case(runner.cases()[0], config, 300, stage_budget_seconds=540)
    assert result['success'], result.get('error_code')
    assert [stage for stage, _ in seen] == ['analysis', 'reply', 'reply']
    assert result['reply_outputs'][0]['proposal']['text'] == first_text
    assert result['reply_outputs'][1]['proposal']['text'] == 'SYN-A 3.18%收500万'
    audit = result['reply_repair']
    assert audit['attempted'] is True and audit['succeeded'] is True
    assert audit['initial_errors']
    assert len(result['reply_repair_audits']) == 1
    assert result['reply_repair_audits'][0]['data'] == audit
    assert [row['round_kind'] for row in result['evidence'] if row.get('stage') == 'reply'] == ['initial', 'text_repair']
    feedback = result['reply_inputs'][1]['validation_feedback']
    assert compact_context({**result['reply_inputs'][1], 'stage': 'reply'})['validation_feedback'] == feedback
    assert {'INITIAL_REPLY_REJECTION', 'REPAIR_ATTEMPTED'} <= set(result['calibration_exclusion_reasons'])
    summary = statistics.reply_repair_summary([result])
    assert summary['initial_rejection_cases'] == summary['attempted_cases'] == summary['succeeded_cases'] == 1
    assert summary['reply_http_calls'] == 2
