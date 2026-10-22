"""Current institutional connection HTTP routes and atomic configuration writes."""
from __future__ import annotations
import os
import tempfile
from fastapi import APIRouter,Body,Request

def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def connection_settings_router(settings):
    router = APIRouter(prefix='/api/im/mq-config')

    @router.get('')
    def status(request: Request):
        return {'ok': True, 'data': settings.status(request.state.user.user_id)}

    @router.put('')
    def save(request: Request, body: dict = Body(default_factory=dict)):
        return {'ok': True, 'data': settings.save(request.state.user.user_id, body)}

    @router.post('/test')
    def test(request: Request, body: dict = Body(default_factory=dict)):
        return {'ok': True, 'data': settings.test(request.state.user.user_id, body)}
    return router
