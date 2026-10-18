"""Recovery collection keeps monotonic evidence without changing model inputs."""
from copy import deepcopy
import io
import json

import pytest

from scripts import measure_dify_recovery as recovery
from server.negotiation.chatflow import ChatflowClient, FlowUnavailable


BOOT = '55a59cca-cf76-4788-8cbd-4c9a084a253c'


class Opener:
    def __init__(self, *, timeout=False):
        self.timeout = timeout
        self.payloads = []
        self.timeouts = []

    def open(self, request, timeout):
        self.payloads.append(json.loads(request.data))
        self.timeouts.append(timeout)
        assert request.get_header('Authorization') == 'Bearer synthetic-key'
        if self.timeout:
            raise TimeoutError('synthetic timeout; no network')
        response = io.BytesIO(json.dumps({'answer': json.dumps({
            'schema_version': '2.2', 'scopes': {}, 'observations': []}),
            'message_id': 'synthetic-message', 'conversation_id': 'synthetic-conversation'}).encode())
        response.status = 200
        return response


def flows(tmp_path, monkeypatch, *, timeout=False):
    opener = Opener(timeout=timeout)
    monkeypatch.setattr(recovery, 'ChatflowClient', lambda *args, **kwargs:
                        ChatflowClient(*args, **kwargs, opener=opener))
    config = {'source': {'base_url': 'http://127.0.0.1:1'},
              'apps': {'analysis': {'key': 'synthetic-key'}}}
    walls = iter([100.0, 99.0, 101.0, 98.0])
    monos = iter([10.0, 12.0, 13.0, 15.0])
    return recovery.TracedFlows(config, tmp_path, 'fault' if timeout else 'recover', 'analysis', 90, 150,
        wall_clock=lambda: next(walls), monotonic_clock=lambda: next(monos), boot_id=BOOT), opener


def test_wall_rollback_keeps_monotonic_trace_and_unique_native_query_without_touching_context(tmp_path, monkeypatch):
    traced, opener = flows(tmp_path, monkeypatch)
    context = {'thread_id': 'synthetic-thread', 'context_policy_revision': 'latest_quote_confirmation_v1',
               'messages': [], 'members': [], '_stage_budget_seconds': 150}
    before = deepcopy(context)
    for _ in range(2):
        assert traced.analyze(context)['schema_version'] == '2.2'
    assert context == before
    rows = [json.loads(line) for line in (tmp_path / 'http-attempts.jsonl').read_text().splitlines()]
    assert len(rows) == 2 and len({row['request_trace_id'] for row in rows}) == 2
    for row, payload in zip(rows, opener.payloads):
        assert row['boot_id'] == BOOT
        assert row['ended_at'] < row['started_at'], 'Keep the actual wall rollback, do not rewrite it'
        assert row['ended_monotonic'] - row['started_monotonic'] == 2
        assert payload['query'] == ('Execute the current versioned analysis stage and return its JSON contract.'
                                   ' [xunjia_recovery_trace=' + row['request_trace_id'] + ']')
        sent = json.loads(payload['inputs']['context_json'])
        assert sent == {key: value for key, value in {**context, 'schema_version': '2.2',
                       'stage': 'analysis'}.items() if key != '_stage_budget_seconds'}
        assert payload['user'].startswith('xunjia-') and row['request_trace_id'] not in payload['user']


def test_timeout_retains_query_link_monotonic_evidence_and_error_without_inventing_a_result(tmp_path, monkeypatch):
    traced, opener = flows(tmp_path, monkeypatch, timeout=True)
    with pytest.raises(FlowUnavailable) as raised:
        traced.analyze({'thread_id': 'synthetic-thread', 'messages': [], 'members': []})
    assert raised.value.code == 'DIFY_TIMEOUT'
    row = json.loads((tmp_path / 'http-attempts.jsonl').read_text())
    assert row['error_code'] == 'DIFY_TIMEOUT' and 'result' not in row
    assert opener.timeouts == [1]
    assert row['request_trace_id'] in opener.payloads[0]['query']
    assert row['boot_id'] == BOOT and row['ended_monotonic'] >= row['started_monotonic']
    assert row['ended_at'] < row['started_at']
