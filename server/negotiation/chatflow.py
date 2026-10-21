"""V2.2 two-stage Dify Chatflow client. App API keys only, no console/admin API.
Every call gets a full, versioned context. Dify conversation memory is not a
second source of truth and never supplies authorization or current quantities.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import ssl
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
from .prices import InvalidFact
from .store import encoded
class FlowUnavailable(RuntimeError):
    def __init__(self,code,status=None):
        self.code,self.status=code,status
        super().__init__(code+(f' HTTP {status}' if status else ''))
def _http_error_code(status,raw):
    # Return safe codes only. Dify may wrap a provider TLS failure in HTTP 400.
    # Do not expose arbitrary upstream text, URLs, prompts or credentials.
    if status==401:return "DIFY_AUTH_FAILED"
    if status==403:return "DIFY_ACCESS_DENIED"
    if status==429:return "DIFY_RATE_LIMITED"
    if not isinstance(raw,bytes) or len(raw)>16384:return "DIFY_HTTP_ERROR"
    try:
        value=json.loads(raw)
        message=value.get("message","") if isinstance(value,dict) else ""
        if not isinstance(message,str):return "DIFY_HTTP_ERROR"
        upstream=re.match(r"^(?:Run failed:\s*)?\[[a-zA-Z0-9_.-]+\] Error: API request failed with status code (\d{3}):",message)
        if upstream:
            body=message[upstream.end():].lower()
            if any(marker in body for marker in ("[ssl:","certificate_verify_failed","tls handshake")):
                return "DIFY_UPSTREAM_TLS_ERROR"
            code=int(upstream[1])
            if code in (401,403):return "DIFY_UPSTREAM_AUTH_FAILED"
            if code==429:return "DIFY_UPSTREAM_RATE_LIMITED"
            if code>=500:return "DIFY_UPSTREAM_UNAVAILABLE"
    except (ValueError,TypeError,UnicodeError):pass
    return "DIFY_HTTP_ERROR"

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,message,headers,newurl):
        # An App API key is scoped to the exact configured origin. Require the
        # operator to configure the final endpoint instead of forwarding it.
        return None
def json_object(text):
    if not isinstance(text,str) or len(text)>1024*1024:raise InvalidFact('Chatflow JSON response exceeds contract')
    text=text.strip()
    if text.startswith('```'):
        rows=text.splitlines()
        if len(rows)<3 or rows[-1].strip()!='```':raise InvalidFact('Incomplete JSON fence')
        text='\n'.join(rows[1:-1])
    def pairs(rows):
        out={}
        for k,v in rows:
            if k in out:raise InvalidFact('Repeated Chatflow JSON field')
            out[k]=v
        return out
    def invalid(_):raise InvalidFact('Nonfinite Chatflow JSON number')
    try:result=json.loads(text,object_pairs_hook=pairs,parse_constant=invalid)
    except (ValueError,TypeError) as exc:raise InvalidFact('Chatflow did not return one valid JSON object') from exc
    if not isinstance(result,dict):raise InvalidFact('Expected a structured Chatflow object')
    return result
class ChatflowClient:
    def __init__(self,base_url,app_key,*,stage,timeout=90,opener=None,clock=time.monotonic):
        url=urlsplit(base_url)
        if url.scheme not in ('http','https') or not url.hostname or url.query or url.fragment:
            raise ValueError('Dify API URL must be a full http(s) address without query or fragment')
        if not isinstance(app_key,str) or not app_key.strip():raise ValueError('Dify stage App API Key missing')
        if stage not in ('analysis','reply'):raise ValueError('Unknown Chatflow stage')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=300:raise ValueError('Invalid Dify request timeout')
        base=base_url.rstrip('/')
        self.url=(base if base.endswith('/v1') else base+'/v1')+'/chat-messages'
        self._key=app_key;self.stage=stage;self.timeout=timeout
        self.opener=opener or urllib.request.build_opener(_NoRedirect());self.clock=clock
        self.last_evidence={}
    def call(self,context):
        context={**context,'schema_version':'2.2','stage':self.stage}
        budget=context.pop('_stage_budget_seconds',self.timeout)
        timeout=min(self.timeout,max(.1,float(budget)))
        payload={'inputs':{'context_json':encoded(context)},'query':'Execute the current versioned '+self.stage+' stage and return its JSON contract.',
                 'response_mode':'blocking','user':'xunjia-'+hashlib.sha256((self.stage+str(context.get('thread_id',context.get('member_id','')))).encode()).hexdigest()[:32]}
        request=urllib.request.Request(self.url,data=encoded(payload).encode('utf8'),method='POST',
              headers={'Authorization':'Bearer '+self._key,'Content-Type':'application/json'})
        start=self.clock()
        self.last_evidence={}
        try:
            try:
                with self.opener.open(request,timeout=timeout) as response:
                    raw=response.read(2*1024*1024+1)
                    if len(raw)>2*1024*1024:raise FlowUnavailable('DIFY_RESPONSE_TOO_LARGE')
                    if response.status!=200:raise FlowUnavailable('DIFY_HTTP_ERROR',response.status)
            except urllib.error.HTTPError as exc:
                status=exc.code
                try:body=exc.read(16385)
                except (OSError,ValueError):body=b""
                finally:exc.close()
                raise FlowUnavailable(_http_error_code(status,body),status) from None
            except TimeoutError:raise FlowUnavailable('DIFY_TIMEOUT') from None
            except ssl.SSLError:raise FlowUnavailable("DIFY_TLS_ERROR") from None
            except urllib.error.URLError as exc:
                if isinstance(exc.reason,ssl.SSLError):raise FlowUnavailable("DIFY_TLS_ERROR") from None
                raise FlowUnavailable('DIFY_TIMEOUT' if isinstance(exc.reason,TimeoutError) else 'DIFY_NETWORK_ERROR') from None
            try:
                wrapper=json_object(raw.decode('utf8'))
                result=json_object(wrapper.get('answer'))
            except (UnicodeError,InvalidFact):raise FlowUnavailable('DIFY_INVALID_STAGE_JSON') from None
            if result.get('schema_version')!='2.2':raise FlowUnavailable('DIFY_INCOMPATIBLE_CONTRACT')
        except FlowUnavailable as exc:
            self.last_evidence={'stage':self.stage,'elapsed_ms':round((self.clock()-start)*1000,3),
                                'error_code':exc.code,'http_status':exc.status}
            raise
        self.last_evidence={'stage':self.stage,'elapsed_ms':round((self.clock()-start)*1000,3),
                            'message_id':wrapper.get('message_id'),'conversation_id':wrapper.get('conversation_id'),
                            'workflow_run_id':wrapper.get('workflow_run_id')}
        return result
class Chatflows:
    def __init__(self,analysis,reply):self.analysis,self.reply=analysis,reply
    def analyze(self,snapshot):return self.analysis.call(snapshot)
    def decide(self,context):return self.reply.call(context)
