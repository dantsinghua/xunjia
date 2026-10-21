"""Real loopback HTTP framing; all stored objects and credentials are synthetic."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import urllib.request
from file_gateway_fakes import Gateway,AUTH,NOW

@contextmanager
def gateway_http():
    gateway=Gateway()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def serve(self):
            parts=self.path.split("/")
            if parts[1] not in ("up","down"):
                self.send_response(404);self.end_headers();return
            body=self.rfile.read(int(self.headers.get("Content-Length","0"))) if self.command=="POST" else None
            request=urllib.request.Request("https://"+parts[1]+".invalid/"+"/".join(parts[2:]),data=body,headers=dict(self.headers),method=self.command)
            response=gateway.open(request,15);data=response.read()
            self.send_response(response.code)
            for key,value in response.headers.items():self.send_header(key,value)
            self.end_headers();self.wfile.write(data)
        do_GET=serve
        do_POST=serve
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield gateway,"http://127.0.0.1:"+str(server.server_address[1])
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
