"""Loopback-only its-relay contract server; test credentials, no external services.
This is a destructive FIFO, not an object-store emulator or a business ACK API.
"""
import base64,collections,contextlib,hashlib,json,re,socket,sqlite3,threading,uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,quote
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

class MockPathGateway:
    def __init__(self,root,auths,clock,*,max_bytes=50*1024*1024):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.dbpath=self.root/'gateway.sqlite3';self.auths=list(auths);self.clock=clock
        self.max_bytes=max_bytes;self.calls=[];self.faults=collections.defaultdict(collections.deque);self.lock=threading.Lock()
        with self.db() as db:db.execute('CREATE TABLE IF NOT EXISTS files(seq INTEGER PRIMARY KEY AUTOINCREMENT,k TEXT,p TEXT,id TEXT,n TEXT,b BLOB,stamp REAL)')
        self.dbpath.chmod(0o600)
        self.server=None;self.thread=None
    @contextlib.contextmanager
    def db(self):
        db=sqlite3.connect(self.dbpath,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def authenticate(self,encoded):
        if not encoded:raise ValueError('missing token')
        try:raw=base64.b64decode(encoded,validate=True)
        except Exception:raise ValueError('invalid token') from None
        for auth in self.auths:
            try:
                decrypt=Cipher(algorithms.SM4(auth.sm4_key),modes.CBC(raw[:16])).decryptor()
                padded=decrypt.update(raw[16:])+decrypt.finalize();unpad=padding.PKCS7(128).unpadder()
                value=json.loads(unpad.update(padded)+unpad.finalize())
                if value.get('key')!=auth.key or type(value.get('ts')) is not str:continue
                if abs(int(value['ts'])-self.clock())>60:raise ValueError('expired token')
                return auth.key
            except Exception:continue
        raise ValueError('invalid or expired token')
    def fault(self,operation,path,mode):
        assert mode in ('502','drop_after_commit','missing_object')
        with self.lock:self.faults[operation,path].append(mode)
    def _fault(self,op,path):
        with self.lock:
            q=self.faults[op,path]
            return q.popleft() if q else None
    def count(self,key,path):
        with self.db() as db:return db.execute('SELECT count(*) FROM files WHERE k=? AND p=?',(key,path)).fetchone()[0]
    def start(self):
        outer=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def answer(self,status,body=b'',headers=None):
                if isinstance(body,dict):body=json.dumps(body).encode()
                self.send_response(status);self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
                for k,v in (headers or {}).items():self.send_header(k,v)
                self.end_headers();self.close_connection=True
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def drop(self):
                self.close_connection=True
                try:self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            def do_GET(self):self.handle_api()
            def do_POST(self):self.handle_api()
            def handle_api(self):
                u=urlsplit(self.path);q=parse_qs(u.query,keep_blank_values=True)
                op=u.path.rsplit('/',1)[-1]
                if u.path!='/its-relay/v1/api/files/'+op or op not in ('pending','upload','download'):
                    return self.answer(404,{'message':'not found'})
                try:key=outer.authenticate(self.headers.get('access_token'))
                except ValueError:return self.answer(401,{'message':'invalid or expired token'})
                values=q.get('path',[])
                if len(values)!=1 or re.fullmatch('[A-Za-z0-9_-]{1,20}',values[0]) is None:
                    return self.answer(400,{'message':'invalid path'})
                path=values[0]
                with outer.lock:outer.calls.append({'op':op,'key_hash':hashlib.sha256(key.encode()).hexdigest()[:12],'path':path,'method':self.command})
                if self.command!=('POST' if op=='upload' else 'GET'):return self.answer(405)
                fail=outer._fault(op,path)
                if fail=='502':return self.answer(502,{'message':'synthetic upstream failure'})
                with outer.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('DELETE FROM files WHERE stamp<?',(outer.clock()-86400,))
                    if op=='pending':return self.answer(200,{'pending':db.execute('SELECT count(*) FROM files WHERE k=? AND p=?',(key,path)).fetchone()[0]})
                    if op=='upload':
                        ct=self.headers.get('Content-Type','');length=self.headers.get('Content-Length','')
                        if not ct.startswith('multipart/form-data'):return self.answer(400,{'message':'multipart required'})
                        if not length.isdecimal() or int(length)>outer.max_bytes+262144:return self.answer(413)
                        raw=self.rfile.read(int(length))
                        if len(raw)!=int(length):return self.answer(400)
                        try:
                            mime=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+ct+'\r\n\r\n').encode()+raw)
                            parts=[p for p in mime.iter_parts() if p.get_param('name',header='content-disposition')=='file']
                            if len(parts)!=1:return self.answer(400,{'message':'file required'})
                            body=parts[0].get_payload(decode=True);name=parts[0].get_filename() or 'file'
                        except Exception:return self.answer(400)
                        if len(body)>outer.max_bytes:return self.answer(413)
                        name=name.replace('\\','/').rsplit('/',1)[-1]
                        name=''.join(c for c in name if ord(c)>=32 and ord(c)!=127 and c not in '\"')[:200] or 'file'
                        identity=str(uuid.uuid4());db.execute('INSERT INTO files(k,p,id,n,b,stamp) VALUES(?,?,?,?,?,?)',(key,path,identity,name,body,outer.clock()));db.commit()
                        if fail=='drop_after_commit':return self.drop()
                        return self.answer(200,{'file_key':identity,'file_name':name})
                    row=db.execute('SELECT * FROM files WHERE k=? AND p=? ORDER BY seq LIMIT 1',(key,path)).fetchone()
                    if row is None:return self.answer(204)
                    db.execute('DELETE FROM files WHERE seq=?',(row['seq'],));db.commit()
                if fail=='drop_after_commit':return self.drop()
                if fail=='missing_object':return self.answer(404,{'message':'file not found'})
                name=row['n'];ascii_name=name.encode('ascii','ignore').decode() or 'file'
                return self.answer(200,bytes(row['b']),{'Content-Type':'application/octet-stream','X-Content-Type-Options':'nosniff','Content-Disposition':"attachment; filename=\""+ascii_name+"\"; filename*=UTF-8''"+quote(name,safe='')})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.base='http://127.0.0.1:'+str(self.server.server_port)+'/its-relay/v1'
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True);self.thread.start();return self
    def close(self):
        if self.server:self.server.shutdown();self.server.server_close()
        if self.thread:self.thread.join(3);assert not self.thread.is_alive()
    def __enter__(self):return self.start()
    def __exit__(self,*args):self.close()