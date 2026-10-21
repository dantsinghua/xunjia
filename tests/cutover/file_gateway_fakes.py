"""Explicit in-memory implementation of the attached its-relay HTTP contract."""
import base64
from collections import defaultdict
from email import policy
from email.parser import BytesParser
import io
import json
import time
from urllib.parse import urlsplit, quote
import uuid
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from relay.file_gateway import GatewayAuth, FileGatewayClient

NOW=1800000000
AUTH=GatewayAuth("synthetic-file-gateway", b"0123456789abcdef")

def decode_token(token):
    blob=base64.b64decode(token,validate=True)
    decoder=Cipher(algorithms.SM4(AUTH.sm4_key),modes.CBC(blob[:16])).decryptor()
    raw=decoder.update(blob[16:])+decoder.finalize()
    pad=padding.PKCS7(128).unpadder()
    return json.loads(pad.update(raw)+pad.finalize())

class Response:
    def __init__(self,code,body=b"",headers=None):
        self.code=self.status=code;self.body=io.BytesIO(body)
        self.headers={"Content-Length":str(len(body)),**(headers or {})}
    def __enter__(self):return self
    def __exit__(self,*args):self.body.close()
    def read(self,n=-1):return self.body.read(n)

class Gateway:
    def __init__(self):
        self.queues=defaultdict(list);self.calls=[];self.tokens=[]
        self.lose_upload=False;self.lose_download=False;self.forced_status=None
    def client(self,direction,**kwargs):
        return FileGatewayClient("https://"+direction+".invalid",AUTH,opener=self,clock=lambda:NOW,**kwargs)
    def open(self,request,timeout):
        url=urlsplit(request.full_url);op=url.path.rsplit("/",1)[-1]
        headers={k.lower():v for k,v in request.header_items()};self.calls.append((url.netloc,op))
        token=headers.get("access_token");self.tokens.append(token)
        try:
            decoded=decode_token(token)
            valid=decoded["key"]==AUTH.key and type(decoded["ts"]) is str and abs(int(decoded["ts"])-NOW)<=60
        except Exception:valid=False
        if not valid:return Response(401,b"{}")
        if self.forced_status:return Response(self.forced_status,b"{}")
        q=self.queues[url.netloc]
        if op=="pending":return Response(200,json.dumps({"pending":len(q)}).encode())
        if op=="upload":
            assert request.method=="POST"
            raw=("Content-Type: "+headers["content-type"]+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+request.data
            mail=BytesParser(policy=policy.default).parsebytes(raw)
            part=next(mail.iter_parts());assert part.get_param("name",header="content-disposition")=="file"
            name=part.get_filename();data=part.get_payload(decode=True);q.append((name,data))
            if self.lose_upload:raise TimeoutError("synthetic secret never logged")
            return Response(200,json.dumps({"file_key":str(uuid.uuid4()),"file_name":name}).encode())
        assert op=="download" and request.method=="GET"
        if not q:return Response(204)
        name,body=q.pop(0)
        if self.lose_download:raise TimeoutError("synthetic download consumed before lost response")
        disposition="attachment; filename=\"fallback.bin\"; filename*=UTF-8\x27\x27"+quote(name)
        return Response(200,body,{"Content-Disposition":disposition,"Content-Type":"application/octet-stream"})
