"""Existing local file coordinator handler, shared by runtime and regression tests.

No account startup, background consumer, or network operation occurs on import.
"""
import base64
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from email.parser import BytesParser
from email import policy
from urllib.parse import quote, urlsplit, parse_qs, unquote
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from .file_gateway import FileGatewayError
from .file_gateway_connection import load_connection, connection_identity
from .file_gateway_journal import QueueStop
from .protocol import token, ProtocolError


class CoordinatorHTTPServer(ThreadingHTTPServer):
 """The existing server loop also services its authenticated directory subscription."""
 def service_actions(self):
  if getattr(self,'journal',None) is not None:self.journal.follow_directory_once()

def valid(token,auth):
 try:
  raw=base64.b64decode(token,validate=True)
  if len(raw)<32 or len(raw)>8192 or len(raw)%16: return False
  c=Cipher(algorithms.SM4(auth.sm4_key),modes.CBC(raw[:16])).decryptor();u=padding.PKCS7(128).unpadder()
  data=c.update(raw[16:])+c.finalize();v=json.loads(u.update(data)+u.finalize())
  return isinstance(v,dict) and isinstance(v.get("key"),str) and hmac.compare_digest(v["key"].encode(),auth.key.encode()) and isinstance(v.get("ts"),str) and v["ts"].isdecimal() and abs(time.time()-int(v["ts"]))<=60
 except Exception: return False
def make_handler(journal,auth,connection_path, *, directory_codec=None, default_binding=None):
 if directory_codec is not None:
  journal.configure_directory(directory_codec, default_binding)
 def switch_safe():
  with journal.db() as db:
   return not db.execute("SELECT 1 FROM upload LIMIT 1").fetchone() and not db.execute("SELECT 1 FROM receive LIMIT 1").fetchone()
 def pending_count(lane, pair=None):
  # Diagnostics remain readable when intake is halted.
  return journal.pending(lane, pair, diagnostic=True)
 def reload_connection():
  raw=connection_path.read_bytes();revision=hashlib.sha256(raw).hexdigest()
  cached=getattr(journal,"_mq_connection_meta",None)
  if cached and cached["connection_config"]==str(connection_path.resolve()) and cached["connection_revision"]==revision:
   return {**cached,"switch_safe":bool(switch_safe())}
  proposed=load_connection(connection_path,allow_http=True,raw=raw);identity=connection_identity(proposed)
  with journal.db() as db:
   db.execute("CREATE TABLE IF NOT EXISTS connection_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)")
   current=db.execute("SELECT identity FROM connection_identity WHERE singleton=1").fetchone()
   if current is None and not switch_safe(): raise QueueStop("MQ_EXISTING_JOURNAL_IDENTITY_REQUIRED")
   if current and current[0]!=identity and not switch_safe(): raise QueueStop("MQ_QUEUE_CHANGE_REQUIRES_RECONCILIATION")
   db.execute("INSERT INTO connection_identity VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET identity=excluded.identity",(identity,))
  journal.upstream=proposed
  journal._mq_connection_meta={"connection_config":str(connection_path.resolve()),"connection_revision":revision,"connection_identity":identity,"switch_safe":bool(switch_safe())}
  return dict(journal._mq_connection_meta)
 metadata=reload_connection()

 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args): pass
  def answer(self,code,body=b"",typ="application/json",name=None):
   self.send_response(code);self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store")
   if code!=204: self.send_header("Content-Type",typ)
   if name is not None: self.send_header("Content-Disposition","attachment; filename*=UTF-8"+chr(39)*2+quote(name,safe=""))
   self.end_headers()
   if body: self.wfile.write(body)
  def handle_op(self):
   if not valid(self.headers.get("access_token",""),auth): return self.answer(401,b"{}")
   if urlsplit(self.path).path=="/up/api/files/history":
    return self.files_reply()
   with journal.lock:
    nonlocal metadata
    try: metadata=reload_connection()
    except QueueStop:
     if urlsplit(self.path).path=="/up/api/files/directory":
      return self.directory_reply(refresh_error="MQ_QUEUE_CHANGE_REQUIRES_RECONCILIATION")
     if self.command=="GET" and self.path.endswith("/pending") and len(urlsplit(self.path).path.split("/"))==5:
      return self.answer(200,json.dumps({"pending":pending_count(self.path.split("/")[1]),**metadata,"switch_safe":False,"note":"MQ_QUEUE_CHANGE_REQUIRES_RECONCILIATION"}).encode())
     return self.answer(409,b'{"message":"MQ_QUEUE_CHANGE_REQUIRES_RECONCILIATION"}')
    except (OSError,ValueError,KeyError,FileGatewayError):
     if urlsplit(self.path).path=="/up/api/files/directory":
      return self.directory_reply(refresh_error="MQ_CONNECTION_CONFIG_INVALID")
     return self.answer(503,b'{"message":"MQ_CONNECTION_CONFIG_INVALID"}')
    return self.route_op()
  def directory_reply(self,refresh_error=None):
   if self.command!="GET": return self.answer(405,b"{}")
   query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
   if any(len(values)!=1 for values in query.values()): return self.answer(400,b"{}")
   if set(query)-{"workbench_id","account_uid","refresh"}: return self.answer(400,b"{}")
   if query.get("refresh",["0"])[0] not in ("0","1"): return self.answer(400,b"{}")
   try:
    result=journal.directory(query.get("workbench_id",[None])[0],query.get("account_uid",[None])[0],
                             refresh=refresh_error is None and query.get("refresh",["0"])[0]=="1")
    if refresh_error is not None: result.update(refresh_error=refresh_error,refresh_requested=query.get("refresh",["0"])[0]=="1")
    body=json.dumps(result,ensure_ascii=False).encode()
    if len(body)>4*1024*1024: return self.answer(413,b'{"message":"DIRECTORY_TOO_LARGE"}')
    return self.answer(200,body)
   except LookupError: return self.answer(404,b'{"message":"CONTACTS_NOT_AVAILABLE"}')
   except QueueStop as e: return self.answer(503,json.dumps({"message":str(e)}).encode())
   except (ValueError,TypeError): return self.answer(400,b"{}")
  def files_reply(self):
   if self.command!="GET": return self.answer(405,b"{}")
   query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
   if any(len(values)!=1 for values in query.values()) or set(query)-{"workbench_id","account_uid","before","limit"}:
    return self.answer(400,b"{}")
   try:
    result=journal.files(query.get("workbench_id",[None])[0],query.get("account_uid",[None])[0],
                         before=int(query['before'][0]) if 'before' in query else None,
                         limit=int(query.get('limit',['50'])[0]))
    return self.answer(200,json.dumps(result,ensure_ascii=False).encode())
   except (ValueError,TypeError,ProtocolError): return self.answer(400,b"{}")
   except QueueStop as e: return self.answer(503,json.dumps({"message":str(e)}).encode())
  def route_op(self):
   p=urlsplit(self.path).path.split("/")
   if p==["","up","api","files","directory"]:
    return self.directory_reply()
   if p==["","up","api","files","history"]:
    return self.files_reply()
   pair=None
   if len(p)==5 and p[1] in ("up","down") and p[2:4]==["api","files"]:
    lane,op=p[1],p[4]
   elif len(p)==7 and p[1] in ("up","down") and p[4:6]==["api","files"]:
    if directory_codec is None: return self.answer(404,b"{}")
    try: pair=tuple(token(unquote(value)) for value in p[2:4])
    except ProtocolError: return self.answer(400,b"{}")
    lane,op=p[1],p[6]
   else: return self.answer(404,b"{}")
   if directory_codec is not None and journal.default_pair is None and pair is None and op in ('upload','download'):
    return self.answer(409,b'{"message":"COLLECTOR_ACCOUNT_REQUIRED"}')
   try:
    if self.command=="GET" and op=="pending": return self.answer(200,json.dumps({"pending":pending_count(lane,pair),**metadata}).encode())
    if self.command=="GET" and op=="download":
     v=journal.download(lane,pair)
     return self.answer(204) if v is None else self.answer(200,v[1],"application/octet-stream",v[0])
    if self.command=="POST" and op=="upload":
     size=int(self.headers.get("Content-Length","0"))
     if not 0<size<=journal.upstream.max_bytes+4096: return self.answer(413,b"{}")
     typ=self.headers.get("Content-Type","")
     if not typ.lower().startswith("multipart/form-data;"): return self.answer(400,b"{}")
     self.connection.settimeout(10);raw=self.rfile.read(size)
     if len(raw)!=size: return self.answer(400,b"{}")
     m=BytesParser(policy=policy.default).parsebytes(("Content-Type: "+typ+"\r\n\r\n").encode()+raw)
     parts=[x for x in m.iter_parts() if x.get_param("name",header="content-disposition")=="file"]
     if len(parts)!=1 or not parts[0].get_filename(): return self.answer(400,b"{}")
     v=journal.upload(lane,parts[0].get_filename(),parts[0].get_payload(decode=True),pair)
     return self.answer(200,json.dumps(v).encode())
    return self.answer(405,b"{}")
   except (QueueStop,FileGatewayError) as e: return self.answer(502,json.dumps({"message":str(e)}).encode())
   except (ValueError,TypeError): return self.answer(400,b"{}")
  do_GET=handle_op
  do_POST=handle_op
 return Handler
