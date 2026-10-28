"""Timing must expose real operation boundaries without leaking or retrying."""
import json
import hashlib
import logging
from types import SimpleNamespace
import pytest
from relay.file_gateway import FileGatewayError
from file_gateway_fakes import Gateway, AUTH, Response


def timing(caplog):
    return [json.loads(record.message) for record in caplog.records
            if record.name == 'xunjia.timing']


def test_gateway_boundaries_cover_read_and_preserve_payload_privacy(caplog):
    caplog.set_level(logging.INFO, logger='xunjia.timing')
    g = Gateway(); c = g.client('up')
    c.upload('private-contact-name.bin', b'private-message-body')
    assert c.download().content == b'private-message-body'
    rows = timing(caplog)
    assert [(r['stage'], r['operation']) for r in rows] == [
        ('http_started', 'upload'), ('http_completed', 'upload'),
        ('http_started', 'download'), ('http_completed', 'download')]
    assert rows[0]['request_id'] == rows[1]['request_id']
    assert rows[2]['request_id'] == rows[3]['request_id']
    assert rows[0]['request_id'] != rows[2]['request_id']
    assert rows[0]['blob_sha256'] == rows[1]['blob_sha256'] == rows[3]['blob_sha256']
    assert rows[0]['blob_sha256'] == hashlib.sha256(b'private-message-body').hexdigest()
    assert all(r['elapsed_ms'] >= 0 and r['status'] == 200 for r in rows[1::2])
    assert all(r['at'] > 0 for r in rows)
    output = caplog.text
    assert all(value not in output for value in
               ['private-contact-name', 'private-message-body', AUTH.key, AUTH.sm4_key.decode(), *g.tokens])


def test_unknown_upload_is_timed_once_without_retry_or_exception_text(caplog):
    caplog.set_level(logging.INFO, logger='xunjia.timing')
    g = Gateway(); g.lose_upload = True
    with pytest.raises(FileGatewayError) as exc:
        g.client('up').upload('own.bin', b'own')
    assert exc.value.ambiguous and len(g.calls) == 1
    rows = timing(caplog)
    assert [r['stage'] for r in rows] == ['http_started', 'http_completed']
    assert rows[-1]['code'] == 'FILE_GATEWAY_OUTCOME_UNKNOWN'
    assert rows[-1]['ambiguous'] is True
    assert 'synthetic secret never logged' not in caplog.text


def test_timing_handler_failure_cannot_change_upload_result(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='xunjia.timing')
    def broken(*a, **kw):
        raise OSError('diagnostic sink failed')
    monkeypatch.setattr(logging.getLogger('xunjia.timing'), 'info', broken)
    g = Gateway()
    assert g.client('up').upload('own.bin', b'own')['file_name'] == 'own.bin'
    assert len(g.calls) == 1


def test_dify_timing_contains_stage_and_run_but_no_prompt_or_app_key(caplog):
    from server.negotiation.chatflow import ChatflowClient
    caplog.set_level(logging.INFO, logger='xunjia.timing')
    raw = json.dumps({'answer': json.dumps({'schema_version': '2.2', 'action': 'WAIT'}),
                      'workflow_run_id': 'synthetic-run'}).encode()
    client = ChatflowClient('https://dify.invalid', 'private-app-key', stage='reply',
        opener=SimpleNamespace(open=lambda *a, **kw: Response(200, raw)))
    assert client.call({'member_id': 'private-contact', 'text': 'private-prompt'})['action'] == 'WAIT'
    rows = timing(caplog)
    assert [r['stage'] for r in rows] == ['dify_started', 'dify_completed']
    assert rows[-1]['flow_stage'] == 'reply' and rows[-1]['workflow_run_id'] == 'synthetic-run'
    assert rows[-1]['elapsed_ms'] >= 0
    assert rows[0]['request_id'] == rows[1]['request_id']
    assert rows[0]['basis_ref'] == rows[1]['basis_ref']
    client.call({'member_id': 'private-contact', 'text': 'private-prompt'})
    assert timing(caplog)[2]['request_id'] != rows[0]['request_id']
    assert all(value not in caplog.text for value in ['private-contact', 'private-prompt', 'private-app-key'])
