"""An owned local SSE connection must not hold a deployment restart indefinitely."""
import ast
from contextlib import suppress
import http.client
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time


def test_deployment_launcher_has_finite_stream_connection_drain():
    source=(Path(__file__).resolve().parents[2]/'deploy/runtime.py').read_text()
    tree=ast.parse(source)
    candidates=[node for node in ast.walk(tree) if isinstance(node,ast.List)
        and any(isinstance(e,ast.Constant) and e.value=='--timeout-graceful-shutdown' for e in node.elts)]
    assert len(candidates)==1
    values=[e.value if isinstance(e,ast.Constant) else None for e in candidates[0].elts]
    index=values.index('--timeout-graceful-shutdown')
    assert 1<=int(values[index+1])<=30


def test_open_sse_is_cancelled_then_application_lifespan_is_closed(tmp_path):
    (tmp_path/'sse_probe.py').write_text(r"""from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
@asynccontextmanager
async def lifespan(app):
    yield
    Path(__file__).with_name('lifespan-closed').write_text('closed')
app=FastAPI(lifespan=lifespan)
@app.get('/events')
async def events():
    async def stream():
        while True:
            yield ': synthetic heartbeat\n\n'
            await asyncio.sleep(60)
    return StreamingResponse(stream(),media_type='text/event-stream')
""")
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    log=(tmp_path/'server.log').open('w')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','sse_probe:app','--app-dir',str(tmp_path),
        '--host','127.0.0.1','--port',str(port),'--timeout-graceful-shutdown','1','--log-level','error'],
        stdout=log,stderr=subprocess.STDOUT)
    connection=None
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            assert process.poll() is None, (tmp_path/'server.log').read_text()
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
            try:
                connection.request('GET','/events');response=connection.getresponse();break
            except OSError:
                connection.close();connection=None;time.sleep(.05)
        else:raise AssertionError('Owned streaming fixture did not start')
        assert response.status==200 and response.readline()==b': synthetic heartbeat\n'
        process.terminate()
        assert process.wait(timeout=8) in (0, -signal.SIGTERM)
        assert (tmp_path/'lifespan-closed').read_text()=='closed'
    finally:
        if connection:connection.close()
        if process.poll() is None:
            process.kill();process.wait(timeout=5)
        log.close()
