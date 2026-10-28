"""Inquiry-only alias routing over an independently loaded, pinned shim module."""

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import sys
from time import monotonic, time
from types import ModuleType
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from fastapi.responses import JSONResponse


A_ALIAS = 'xunjia-qa-qwen-max-20261018-1245'
B_ALIAS = 'xunjia-qa-reply-gpt4o-20261025'
ROUTE_INPUT = 'xunjia_text_route'
CONTRACT = 'xunjia-stage-bridge-v1'
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 150
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
_TIMING = logging.getLogger('xunjia.dify_stage_bridge')
_ADAPTER_SOURCE = {'path': str(Path(__file__).resolve()),
                   'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
_OVERRIDES = frozenset(('route', ROUTE_INPUT, 'provider', 'service', 'url',
    'base_url', 'endpoint', 'api_key', 'key', 'headers', 'models', 'model_id'))


class StageRouteError(ValueError):
    status_code = 400

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BridgeUpstreamError(RuntimeError):
    def __init__(self, code, http_status=None):
        self.code = code
        self.http_status = http_status if type(http_status) is int else None
        super().__init__(code + ' status=' + str(self.http_status))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _remaining(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise BridgeUpstreamError('DIFY_UPSTREAM_TIMEOUT')
    return remaining


def _read_upstream(response, deadline):
    result = bytearray()
    read = getattr(response, 'read1', response.read)
    while len(result) <= MAX_UPSTREAM_BYTES:
        remaining = _remaining(deadline)
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read(min(65536, MAX_UPSTREAM_BYTES + 1 - len(result)))
        _remaining(deadline)
        if not isinstance(chunk, bytes):
            raise BridgeUpstreamError('DIFY_UPSTREAM_INVALID_RESPONSE')
        if not chunk:
            return bytes(result)
        result.extend(chunk)
    raise BridgeUpstreamError('DIFY_UPSTREAM_RESPONSE_TOO_LARGE')


@dataclass(frozen=True)
class ShimSource:
    path: Path
    sha256: str


def load_pinned_shim(source):
    """Load exactly the reviewed bytes; source location is a startup setting."""
    path = Path(source.path).resolve(strict=True)
    raw = path.read_bytes()
    if not path.is_file() or hashlib.sha256(raw).hexdigest() != source.sha256:
        raise ValueError('SHIM_SOURCE_DRIFT')
    module = ModuleType('_xunjia_pinned_shim_' + source.sha256[:16])
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    module._xunjia_source = ShimSource(path, source.sha256)
    return module


def select_route(model):
    if model is None or model == A_ALIAS:
        return 'qwen'
    if model == B_ALIAS:
        return 'reply_gpt'
    raise StageRouteError('UNKNOWN_MODEL_ALIAS')


def _has_image(messages):
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if isinstance(content, list) and any(isinstance(part, dict) and
                (part.get('type') == 'image_url' or 'image_url' in part) for part in content):
            return True
    return False


def validate_chat_route(payload):
    if not isinstance(payload, dict) or _OVERRIDES.intersection(payload):
        raise StageRouteError('ROUTE_OVERRIDE_FORBIDDEN')
    if 'model' in payload and (not isinstance(payload['model'], str) or not payload['model']):
        raise StageRouteError('INVALID_MODEL_ALIAS')
    route = select_route(payload.get('model'))
    if route == 'reply_gpt' and _has_image(payload.get('messages')):
        raise StageRouteError('REPLY_ROUTE_IS_TEXT_ONLY')
    return route


def build_workflow_inputs(system_prompt, user_prompt, image_id=None, route='qwen'):
    if route not in ('qwen', 'reply_gpt'):
        raise StageRouteError('UNKNOWN_TEXT_ROUTE')
    if route == 'reply_gpt' and image_id:
        raise StageRouteError('REPLY_ROUTE_IS_TEXT_ONLY')
    result = {'system_prompt': system_prompt or '', 'user_prompt': user_prompt or '',
              ROUTE_INPUT: route}
    if image_id:
        result['image'] = {'type': 'image', 'transfer_method': 'local_file',
                           'upload_file_id': image_id}
    return result


def create_app(shim_module, *, open_request=None):
    """Adapt only this private module instance; never change shared source files."""
    source = getattr(shim_module, '_xunjia_source', None)
    if not isinstance(source, ShimSource):
        raise ValueError('PINNED_SHIM_SOURCE_REQUIRED')
    path = Path(source.path).resolve(strict=True)
    if (Path(shim_module.__file__).resolve(strict=True) != path or
            hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256):
        raise ValueError('SHIM_RUNTIME_SOURCE_DRIFT')
    if shim_module.BASE != 'https://dify-test.frontnode.net':
        raise ValueError('UNEXPECTED_INQUIRY_UPSTREAM')
    timeout = shim_module.TIMEOUT
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS):
        raise ValueError('INVALID_UPSTREAM_TIMEOUT')
    selected = ContextVar('xunjia_stage_route', default='qwen')
    request_deadline = ContextVar('xunjia_stage_deadline', default=None)
    send = open_request or urllib.request.build_opener(_NoRedirect()).open

    def routed_call(system_prompt, user_prompt, image_id):
        route = selected.get()
        started = monotonic()
        deadline = request_deadline.get() or started + timeout
        request_id = str(uuid4())
        event = {'event': 'upstream_completed', 'route': route, 'status': 'unknown',
                 'request_id': request_id}
        _TIMING.info(json.dumps({'event': 'upstream_started', 'route': route,
                                'request_id': request_id, 'at': time()}))
        try:
            inputs = build_workflow_inputs(system_prompt, user_prompt, image_id, route)
            body = json.dumps({'inputs': inputs, 'response_mode': 'blocking',
                               'user': 'local-dify'}).encode()
            request = urllib.request.Request(shim_module.BASE + '/v1/workflows/run',
                data=body, method='POST', headers={'Authorization': 'Bearer ' + shim_module.KEY,
                                                  'Content-Type': 'application/json'})
            with send(request, timeout=_remaining(deadline)) as response:
                status = getattr(response, 'status', 200)
                if getattr(response, 'geturl', lambda: request.full_url)() != request.full_url:
                    raise BridgeUpstreamError('DIFY_UPSTREAM_REDIRECT_REFUSED', status)
                result = json.loads(_read_upstream(response, deadline).decode())
            if not isinstance(result, dict) or not isinstance(result.get('data'), dict):
                raise BridgeUpstreamError('DIFY_UPSTREAM_INVALID_RESPONSE', status)
            data = result['data']
            candidate = result.get('workflow_run_id') or data.get('id')
            if isinstance(candidate, str) and len(candidate) == 36:
                try:
                    event['run_id'] = str(UUID(candidate))
                except ValueError:
                    pass
            if data.get('status') != 'succeeded':
                raise BridgeUpstreamError('DIFY_UPSTREAM_WORKFLOW_FAILED', status)
            text = (data.get('outputs') or {}).get('text')
            if not isinstance(text, str):
                raise BridgeUpstreamError('DIFY_UPSTREAM_INVALID_RESPONSE', status)
            _remaining(deadline)
            event.update(status='succeeded', http_status=status)
            return text
        except Exception as exc:
            if isinstance(exc, BridgeUpstreamError):
                safe = exc
            elif isinstance(exc, urllib.error.HTTPError):
                safe = BridgeUpstreamError('DIFY_UPSTREAM_HTTP_ERROR', exc.code)
            elif (isinstance(exc, TimeoutError) or isinstance(exc, urllib.error.URLError)
                  and isinstance(exc.reason, TimeoutError)):
                safe = BridgeUpstreamError('DIFY_UPSTREAM_TIMEOUT')
            else:
                safe = BridgeUpstreamError('DIFY_UPSTREAM_REQUEST_FAILED')
            event.update(status='failed', code=safe.code, http_status=safe.http_status)
            raise safe from None
        finally:
            event['at'] = time()
            event['elapsed_ms'] = round(max(0, monotonic() - started) * 1000, 3)
            _TIMING.info(json.dumps(event))

    shim_module._call = routed_call
    app = shim_module.app

    @app.middleware('http')
    async def stage_route(request, call_next):
        if request.method == 'GET' and request.url.path == '/v1/models':
            return JSONResponse({'object': 'list', 'data': [
                {'id': alias, 'object': 'model', 'created': 0, 'owned_by': 'xunjia'}
                for alias in (A_ALIAS, B_ALIAS)]})
        if request.method == 'GET' and request.url.path == '/_xunjia/stage-bridge':
            return JSONResponse({'contract': CONTRACT, 'aliases': [A_ALIAS, B_ALIAS],
                'shim_source': {'path': str(path), 'sha256': source.sha256},
                'adapter_source': dict(_ADAPTER_SOURCE),
                'model_verified': False})
        if request.method != 'POST' or request.url.path != '/v1/chat/completions':
            return await call_next(request)
        try:
            payload = await request.json()
            route = validate_chat_route(payload)
        except (ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, StageRouteError) else 'INVALID_CHAT_JSON'
            return JSONResponse({'error': {'type': 'invalid_request_error', 'code': code,
                                          'message': code}}, status_code=400)
        token = selected.set(route)
        deadline_token = request_deadline.set(monotonic() + timeout)
        try:
            return await call_next(request)
        finally:
            request_deadline.reset(deadline_token)
            selected.reset(token)

    return app
