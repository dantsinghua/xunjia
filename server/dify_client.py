"""Dify 1.17.1 Chatflow transport; complete proposals only, no dispatch or retries."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class DifyClientError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Dify Chat调用失败: {code}')


@dataclass(frozen=True)
class ChatResult:
    proposal: dict
    conversation_id: str
    message_id: str
    task_id: str
    workflow_run_id: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def constant(_value):
        raise ValueError('nonfinite')
    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('nonfinite')
        return parsed
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise DifyClientError('INVALID_JSON') from None
    if not isinstance(value, dict):
        raise DifyClientError('INVALID_JSON')
    return value


def _text(value, limit=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def validate_agent_turn(value: dict) -> dict:
    """Validate the transport contract, never grant price/identity authority."""
    fields = {'schema_version', 'turn_id', 'conversation_epoch', 'source_message_id', 'observations',
              'proposed_actions', 'public_message', 'trader_view', 'memory_patch', 'history_request', 'validation_ref'}
    if not isinstance(value, dict) or set(value) != fields or value['schema_version'] != '1':
        raise DifyClientError('INVALID_AGENT_TURN')
    if not all(_text(value[k]) for k in ('turn_id', 'source_message_id')):
        raise DifyClientError('INVALID_AGENT_TURN')
    if type(value['conversation_epoch']) is not int or value['conversation_epoch'] < 0:
        raise DifyClientError('INVALID_AGENT_TURN')
    if not isinstance(value['trader_view'], dict) or any(value[k] is not None and not isinstance(value[k], dict) for k in ('memory_patch', 'history_request')):
        raise DifyClientError('INVALID_AGENT_TURN')
    for key in ('observations', 'proposed_actions'):
        if not isinstance(value[key], list) or len(value[key]) > 50 or not all(isinstance(item, dict) for item in value[key]):
            raise DifyClientError('INVALID_AGENT_TURN')
    actions = ('SOCIAL_REPLY', 'CLARIFY_BOND', 'ASK_QUOTE', 'COUNTER', 'WAIT', 'REQUEST_HANDOFF')
    for action in value['proposed_actions']:
        if action.get('type') not in actions:
            raise DifyClientError('INVALID_AGENT_TURN')
        for field in ('rfq_session_id', 'ambiguity_issue_id'):
            if field in action and not _text(action[field]):
                raise DifyClientError('INVALID_AGENT_TURN')
        for field in ('yield_pct', 'volume_wan'):
            if field in action and (not isinstance(action[field], str) or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,16})?', action[field])):
                raise DifyClientError('INVALID_AGENT_TURN')
    for observation in value['observations']:
        if observation.get('attribution') not in ('unique', 'ambiguous'):
            raise DifyClientError('INVALID_AGENT_TURN')
        refs = observation.get('evidence_refs')
        if not isinstance(refs, list) or len(refs) > 50 or not all(_text(ref) for ref in refs):
            raise DifyClientError('INVALID_AGENT_TURN')
        peer = observation.get('peer_quote')
        if peer is not None:
            if (not isinstance(peer, dict) or peer.get('side') not in ('bid', 'ofr')
                    or peer.get('kind') not in ('yield', 'bp', 'net_price') or type(peer.get('firm')) is not bool
                    or not _text(peer.get('source_message_id')) or not isinstance(peer.get('value'), str)
                    or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,16})?', peer['value'])):
                raise DifyClientError('INVALID_AGENT_TURN')
    if value['public_message'] is not None and not _text(value['public_message'], 4000):
        raise DifyClientError('INVALID_AGENT_TURN')
    if value['validation_ref'] is not None and not _text(value['validation_ref'], 2048):
        raise DifyClientError('INVALID_AGENT_TURN')
    if value['public_message'] is not None and value['validation_ref'] is None:
        raise DifyClientError('UNVALIDATED_PUBLIC_MESSAGE')
    if value['history_request'] is not None and (value['public_message'] is not None or any(a['type'] not in ('WAIT', 'REQUEST_HANDOFF') for a in value['proposed_actions'])):
        raise DifyClientError('HISTORY_WITH_ACTION')
    return value


class DifyChatClient:
    def __init__(self, base_url: str, app_key: str, *, opener=None, timeout=90,
                 total_timeout=180, max_response_bytes=1048576):
        if not isinstance(base_url, str):
            raise DifyClientError('INVALID_CONFIGURATION')
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in ('', '/v1')):
            raise DifyClientError('INVALID_CONFIGURATION')
        if not _text(app_key, 4096) or '\n' in app_key or '\r' in app_key:
            raise DifyClientError('NOT_CONFIGURED')
        for value in (timeout, total_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise DifyClientError('INVALID_CONFIGURATION')
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 10485760:
            raise DifyClientError('INVALID_CONFIGURATION')
        self.base_url = base_url.rstrip('/') + ('' if parsed.path.rstrip('/') == '/v1' else '/v1')
        self._app_key = app_key
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.timeout, self.total_timeout = timeout, total_timeout
        self.max_response_bytes = max_response_bytes

    def _request(self, path, body):
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, RecursionError):
            raise DifyClientError('INVALID_REQUEST') from None
        if len(data) > 1048576:
            raise DifyClientError('REQUEST_TOO_LARGE')
        return urllib.request.Request(self.base_url + path, data=data, method='POST', headers={
            'Authorization': 'Bearer ' + self._app_key, 'Content-Type': 'application/json',
            'Accept': 'text/event-stream' if body.get('response_mode') == 'streaming' else 'application/json'})

    def _chunks(self, response, started):
        size = 0
        # HTTPResponse.read1 returns currently available bytes and avoids readline's
        # unbounded wait for a newline. Socket timeout bounds each network read.
        read = getattr(response, 'read1', None) or response.read
        while True:
            if time.monotonic() - started >= self.total_timeout:
                raise DifyClientError('TIMEOUT')
            block = read(min(4096, self.max_response_bytes + 1))
            if time.monotonic() - started >= self.total_timeout:
                raise DifyClientError('TIMEOUT')
            if not block:
                return
            if not isinstance(block, bytes):
                raise DifyClientError('INVALID_RESPONSE')
            size += len(block)
            if size > self.max_response_bytes:
                raise DifyClientError('RESPONSE_TOO_LARGE')
            yield block

    def chat(self, *, query, user, inputs, conversation_id=None, response_mode='streaming'):
        if (not _text(query, 16000) or not _text(user, 200) or not isinstance(inputs, dict)
                or response_mode not in ('streaming', 'blocking')
                or conversation_id is not None and not _text(conversation_id)):
            raise DifyClientError('INVALID_REQUEST')
        body = dict(query=query, user=user, inputs=inputs, response_mode=response_mode, auto_generate_name=False)
        if conversation_id is not None:
            body['conversation_id'] = conversation_id
        request = self._request('/chat-messages', body)
        started = time.monotonic()
        try:
            with self.opener(request, timeout=min(self.timeout, self.total_timeout)) as response:
                if getattr(response, 'status', 200) != 200:
                    raise DifyClientError('HTTP_ERROR')
                if response_mode == 'streaming':
                    if 'text/event-stream' not in response.headers.get('Content-Type', '').lower():
                        raise DifyClientError('INVALID_CONTENT_TYPE')
                    return self._stream(self._chunks(response, started), conversation_id)
                envelope = _json(b''.join(self._chunks(response, started)))
                if envelope.get('event') != 'message':
                    raise DifyClientError('UNSUCCESSFUL_COMPLETION')
                return self._result(envelope.get('answer'), envelope, conversation_id)
        except DifyClientError:
            raise
        except (TimeoutError,):
            raise DifyClientError('TIMEOUT') from None
        except urllib.error.HTTPError:
            raise DifyClientError('HTTP_ERROR') from None
        except urllib.error.URLError as exc:
            raise DifyClientError('TIMEOUT' if isinstance(exc.reason, TimeoutError) else 'TRANSPORT_ERROR') from None
        except (OSError, ValueError, UnicodeError):
            raise DifyClientError('TRANSPORT_ERROR') from None

    @staticmethod
    def _events(chunks):
        buffer = b''
        fields = []
        for block in chunks:
            buffer += block
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.rstrip(b'\r')
                if not line:
                    if fields:
                        yield b'\n'.join(fields)
                        fields = []
                elif line.startswith(b'data:'):
                    fields.append(line[5:].removeprefix(b' '))
                # SSE comments, event/id/retry and heartbeat lines are metadata.
        if buffer.strip() or fields:
            raise DifyClientError('TRUNCATED_STREAM')

    def _stream(self, chunks, expected_conversation):
        ids, answer = {}, ''
        ended = succeeded = False
        for raw in self._events(chunks):
            if raw in (b'ping', b'[DONE]'):
                continue
            event = _json(raw)
            kind = event.get('event')
            if not isinstance(kind, str):
                raise DifyClientError('INVALID_EVENT')
            for field in ('conversation_id', 'message_id', 'task_id', 'workflow_run_id'):
                if field not in event:
                    continue
                value = event[field]
                if not _text(value) or field in ids and ids[field] != value:
                    raise DifyClientError('RESPONSE_ID_MISMATCH')
                ids[field] = value
            if expected_conversation is not None and ids.get('conversation_id', expected_conversation) != expected_conversation:
                raise DifyClientError('CONVERSATION_MISMATCH')
            if kind in ('error', 'workflow_paused', 'workflow_failed', 'workflow_stopped'):
                raise DifyClientError('UNSUCCESSFUL_COMPLETION')
            if kind in ('message', 'message_replace'):
                if ended or not isinstance(event.get('answer'), str):
                    raise DifyClientError('INVALID_EVENT')
                answer = event['answer'] if kind == 'message_replace' else answer + event['answer']
            elif kind == 'workflow_finished':
                data = event.get('data')
                if not isinstance(data, dict) or data.get('status') != 'succeeded' or data.get('error'):
                    raise DifyClientError('UNSUCCESSFUL_COMPLETION')
                if data.get('id') and data['id'] != ids.get('workflow_run_id'):
                    raise DifyClientError('RESPONSE_ID_MISMATCH')
                succeeded = True
            elif kind == 'message_end':
                if ended:
                    raise DifyClientError('DUPLICATE_TERMINAL')
                ended = True
        if not ended or not succeeded or not ids.get('workflow_run_id'):
            raise DifyClientError('INCOMPLETE_STREAM')
        return self._result(answer, ids, expected_conversation)

    @staticmethod
    def _result(answer, envelope, expected_conversation):
        if not all(_text(envelope.get(k)) for k in ('conversation_id', 'message_id', 'task_id')):
            raise DifyClientError('INVALID_RESPONSE')
        if expected_conversation is not None and envelope['conversation_id'] != expected_conversation:
            raise DifyClientError('CONVERSATION_MISMATCH')
        if not isinstance(answer, str):
            raise DifyClientError('INVALID_RESPONSE')
        proposal = validate_agent_turn(_json(answer))
        return ChatResult(proposal=proposal, conversation_id=envelope['conversation_id'],
                          message_id=envelope['message_id'], task_id=envelope['task_id'],
                          workflow_run_id=envelope.get('workflow_run_id'))

    def stop(self, task_id, user):
        """Return only Dify's request acknowledgement, never a business stop guarantee."""
        if not _text(user) or not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', task_id):
            raise DifyClientError('INVALID_REQUEST')
        request = self._request('/chat-messages/' + task_id + '/stop', {'user': user})
        started = time.monotonic()
        try:
            with self.opener(request, timeout=min(self.timeout, self.total_timeout)) as response:
                if getattr(response, 'status', 200) != 200:
                    raise DifyClientError('HTTP_ERROR')
                return _json(b''.join(self._chunks(response, started))).get('result') == 'success'
        except DifyClientError:
            raise
        except TimeoutError:
            raise DifyClientError('TIMEOUT') from None
        except urllib.error.HTTPError:
            raise DifyClientError('HTTP_ERROR') from None
        except (urllib.error.URLError, OSError):
            raise DifyClientError('TRANSPORT_ERROR') from None
