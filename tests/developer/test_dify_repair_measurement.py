"""Real Engine with a synthetic model proves repair measurement is not hidden."""
from copy import deepcopy

from dify.negotiation_v22 import measurement as statistics
from dify.negotiation_v22.node_code import compact_context
from scripts import measure_dify_v22 as runner


def test_measurement_preserves_initial_response_and_one_evidence_only_repair(monkeypatch):
    seen = []

    class Client:
        def __init__(self, *args, stage, **kwargs):
            self.stage = stage
            self.last_evidence = {}

        def call(self, context):
            seen.append((self.stage, deepcopy(context)))
            self.last_evidence = {'stage': self.stage, 'elapsed_ms': 1, 'message_id': 'synthetic-' + str(len(seen))}
            if self.stage == 'reply':
                return {'schema_version': '2.2', 'action': 'WAIT', 'proposal': None}
            member = context['members'][0]
            current = context['messages'][-1]
            if context.get('validation_feedback'):
                feedback = context['validation_feedback']
                assert feedback['mode'] == 'EVIDENCE_ONLY' and feedback['attempt'] == feedback['max_attempts'] == 1
                result = deepcopy(feedback['original_response'])
                anchor = next(row for row in reversed(member['context']) if row['direction'] == 'out')
                result['observations'][0]['evidence']['price'].append(anchor['message_id'])
                result['observations'][0]['segments'].append({'message_id': anchor['message_id'], 'text': anchor['text']})
                return result
            changes = {'price': {'kind': 'YIELD', 'value': '3.20', 'notation': 'ABSOLUTE'},
                       'volume_wan': '500万', 'assertion': 'QUOTE', 'validity': 'ACTIVE',
                       'finality': 'FINAL', 'confirmed_day': '2026-10-17'}
            return {'schema_version': '2.2', 'scopes': {current['message_id']: [member['member_id']]},
                    'unresolved': {}, 'observations': [{'member_id': member['member_id'], 'side': 'ofr',
                        'changes': changes, 'evidence': {field: [current['message_id']] for field in changes},
                        'segments': [{'message_id': current['message_id'], 'text': current['text']}],
                        'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                            'negotiation_space': 'FINAL', 'reason': '本轮明确引用我方已发价出量并声明不能改善'}}],
                    'tool_requests': []}

    monkeypatch.setattr(runner, 'ChatflowClient', Client)
    config = {'source': {'base_url': 'https://synthetic.invalid'},
              'apps': {stage: {'key': 'synthetic-only'} for stage in ('analysis', 'reply')}}
    case = next(row for row in runner.cases() if row['id'] == 'referenced-confirmation')
    result = runner.run_case(case, config, 300, stage_budget_seconds=540)
    assert result['success'], result.get('error_code')
    assert [stage for stage, _ in seen] == ['analysis', 'analysis', 'reply']
    assert result['analysis_outputs'][0] == result['fact_commit']['repair']['initial_response']
    assert len(result['analysis_outputs'][0]['observations'][0]['evidence']['price']) == 1
    assert len(result['analysis_outputs'][1]['observations'][0]['evidence']['price']) == 2
    assert result['analysis_repair']['attempted'] and result['analysis_repair']['succeeded']
    assert result['analysis_repair']['initial_errors'][0]['reason'] == 'Price is not supported by its classified raw evidence'
    assert [row['round_kind'] for row in result['evidence'] if row.get('stage') == 'analysis'] == ['initial', 'evidence_repair']
    feedback = result['analysis_inputs'][1]['validation_feedback']
    assert compact_context(result['analysis_inputs'][1])['validation_feedback'] == feedback
    summary = statistics.analysis_repair_summary([result])
    assert summary == {'initial_rejection_cases': 1, 'initial_rejected_observations': 1,
                       'attempted_cases': 1, 'succeeded_cases': 1, 'fallback_cases': 0,
                       'analysis_http_calls': 2, 'reply_http_calls': 1}


def test_repair_summary_preserves_fallback_as_a_failed_repair():
    sample = {'success': False, 'analysis_inputs': [{}, {}], 'reply_inputs': [],
              'analysis_repair': {'initial_errors': [{'index': 0}], 'attempted': True,
                                  'succeeded': False, 'fallback': True}}
    summary = statistics.analysis_repair_summary([sample])
    assert summary['initial_rejection_cases'] == summary['attempted_cases'] == summary['fallback_cases'] == 1
    assert summary['succeeded_cases'] == 0
