"""V2.2 two-stage Dify Chatflow client. App API keys only, no console/admin API.
Every call gets a full, versioned context. Dify conversation memory is not a
second source of truth and never supplies authorization or current quantities.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import uuid
from relay.file_gateway import timing_event
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
    def __init__(self,code,status=None,*,_previous_output=None):
        self.code,self.status=code,status
        self._previous_output=deepcopy(_previous_output)
        super().__init__(code+(f' HTTP {status}' if status else ''))
_MODEL_CONTRACT_ERRORS={
    'Own quote slot repeats or conflicts with an adjacent direction':'DIFY_MODEL_CONTRACT_SLOTS',
    'Economic numbers and facts must use verified slots':'DIFY_MODEL_CONTRACT_SLOTS',
    'Settlement clarification must actually ask for settlement':'DIFY_MODEL_CONTRACT_SLOTS',
    'Cash quote basis prefix conflicts with verified field':'DIFY_MODEL_CONTRACT_SLOTS',
    'Ambiguous adjacent quote direction word':'DIFY_MODEL_CONTRACT_SLOTS',
    'Quote percent suffix conflicts with verified price kind':'DIFY_MODEL_CONTRACT_SLOTS',
    'Quote direction suffix conflicts with verified field':'DIFY_MODEL_CONTRACT_SLOTS',
    'Ambiguous adjacent quote percent component':'DIFY_MODEL_CONTRACT_SLOTS',
    'Ambiguous adjacent quantity unit word':'DIFY_MODEL_CONTRACT_SLOTS',
    'Quantity unit suffix conflicts with verified whole lot':'DIFY_MODEL_CONTRACT_SLOTS',
    'Equivalent fact-macro text exceeds 1500 characters':'DIFY_MODEL_CONTRACT_SLOTS',
    'Public reference is attributed to the wrong speaker':'DIFY_MODEL_CONTRACT_SLOTS',
    'Current quotation cannot be described as historical':'DIFY_MODEL_CONTRACT_SLOTS',
    'Selected quote is outside the authorized final boundary':'DIFY_MODEL_CONTRACT_BOUNDARY',
    'A quote reply must bind its own structured quotation':'DIFY_MODEL_CONTRACT_OWN_QUOTE',
    'Nonquote plan cannot insert an own price':'DIFY_MODEL_CONTRACT_OWN_QUOTE',
    'Public reply exposes private policy or internal context':'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE',
    'Quantity confirmation must explicitly refer to a quotation, not execution':'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE',
    'Reply promises execution or reserves a counterparty lot':'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE',
    'Counterparty reference is not an actual quotation':'DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT',
    'Unknown or unavailable public fact slot':'DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT',
}
_OUTPUT_CONTRACT_MARKER='XUNJIA_OUTPUT_CONTRACT_V1:'
_ANALYSIS_ASSOCIATION_CODE='DIFY_MODEL_CONTRACT_ANALYSIS_ASSOCIATION'


def _validated_previous_output(previous):
    if not isinstance(previous,dict) or set(previous)!={'schema_version','reply_plan'} or previous.get('schema_version')!='2.2':return None
    plan=previous['reply_plan']
    if (not isinstance(plan,dict) or not {'kind','text'}<=set(plan)
            or set(plan)-{'kind','text','quote_value','question','context_ref','acceptance'}
            or plan.get('kind') not in ('REPLY','RECONFIRM','ASK_VOLUME','CLARIFY','SOCIAL','HANDOFF_NOTICE')
            or not isinstance(plan.get('text'),str) or not 1<=len(plan['text'].strip())<=1500):return None
    if plan['kind']=='REPLY' and 'quote_value' not in plan:return None
    if plan['kind']=='HANDOFF_NOTICE':
        if plan.get('acceptance')!={'meaning':'ACCEPTED_CURRENT_QUOTE','source':'LATEST_INBOUND'} or 'quote_value' in plan:return None
    elif 'acceptance' in plan:return None
    if 'quote_value' in plan and (not isinstance(plan['quote_value'],str)
            or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,8})?',plan['quote_value'])):return None
    if ('question' in plan and plan['question'] not in ('NONE','NEGOTIATE','ASK_SETTLEMENT','ASK_CURRENT_QUOTE','ASK_MORE_VOLUME')
            or 'context_ref' in plan and (not isinstance(plan['context_ref'],str) or not re.fullmatch(r'[a-f0-9]{64}',plan['context_ref']))):return None
    try:
        raw=json.dumps(previous,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf8')
        if len(raw)>6144:return None
    except (ValueError,TypeError,UnicodeError):return None
    return deepcopy(previous)


def _output_contract_failure(status,raw):
    if status!=400 or not isinstance(raw,bytes) or len(raw)>16384:return None
    try:
        body=raw.decode('utf8')
        if not body.lstrip().startswith('{'):return None
        value=json_object(body)
        if (set(value)!={'code','message','status'} or value.get('code')!='invalid_param'
                or type(value.get('status')) is not int or value['status']!=400 or not isinstance(value.get('message'),str)):
            return None
        trace=value['message'].replace('\r\n','\n')
        if not trace.startswith('Run failed: Traceback (most recent call last):\n'):return None
        match=re.search(r'\nValueError: '+re.escape(_OUTPUT_CONTRACT_MARKER)+r'([^\n]+)\n(?:error: exit status 255\n?)?\Z',trace)
        if match is None or not match[1].startswith('{') or len(match[1].encode('utf8'))>8192:return None
        payload=json_object(match[1])
        if payload.get('origin')=='analysis_associations':
            if (set(payload)!={'version','origin','reason_code'}
                    or type(payload.get('version')) is not int or payload['version']!=1
                    or payload.get('reason_code')!=_ANALYSIS_ASSOCIATION_CODE):return None
            for function in ('_validate_result_strict','decode_stage_result','decode_model_output'):
                if not re.search(r'^  File "[^"\n]+", line \d+, in '+function+r'[ \t]*$',trace,re.M):return None
            if not re.search(r'^AnalysisAssociationError: Invalid member association$',trace[:match.start()],re.M):return None
            return (_ANALYSIS_ASSOCIATION_CODE,None)
        for function in ('decode_stage_result','render_reply_plan','decode_model_output'):
            if not re.search(r'^  File "[^"\n]+", line \d+, in '+function+r'[ \t]*$',trace,re.M):return None
        if (set(payload)!={'version','origin','reason_code','previous_output'}
                or type(payload.get('version')) is not int or payload['version']!=1
                or payload.get('origin')!='render_reply_plan' or payload.get('reason_code') not in _MODEL_CONTRACT_ERRORS.values()):return None
        reasons=[text for text,code in _MODEL_CONTRACT_ERRORS.items() if code==payload['reason_code']]
        if not any(re.search(r'^ValueError: '+re.escape(reason)+r'$',trace[:match.start()],re.M) for reason in reasons):return None
        previous=_validated_previous_output(payload['previous_output'])
        return (payload['reason_code'],previous) if previous is not None else None
    except (InvalidFact,ValueError,TypeError,UnicodeError,RecursionError):return None


def _http_error_code(status,raw):
    # Return safe codes only. Dify may wrap a provider TLS failure in HTTP 400.
    # Do not expose arbitrary upstream text, URLs, prompts or credentials.
    if status==401:return "DIFY_AUTH_FAILED"
    if status==403:return "DIFY_ACCESS_DENIED"
    if status==429:return "DIFY_RATE_LIMITED"
    if not isinstance(raw,bytes) or len(raw)>16384:return "DIFY_HTTP_ERROR"
    correction=_output_contract_failure(status,raw)
    if correction:return correction[0]
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
    except (ValueError,TypeError,UnicodeError,RecursionError):pass
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
    def finite(raw):
        value=float(raw)
        if not math.isfinite(value):raise InvalidFact('Nonfinite Chatflow JSON number')
        return value
    try:result=json.loads(text,object_pairs_hook=pairs,parse_constant=invalid,parse_float=finite)
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
    def _request_once(self,request,timeout):
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
            correction=_output_contract_failure(status,body)
            if correction:raise FlowUnavailable(correction[0],status,_previous_output=correction[1]) from None
            raise FlowUnavailable(_http_error_code(status,body),status) from None
        except TimeoutError:raise FlowUnavailable('DIFY_TIMEOUT') from None
        except ssl.SSLError:raise FlowUnavailable("DIFY_TLS_ERROR") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason,ssl.SSLError):raise FlowUnavailable("DIFY_TLS_ERROR") from None
            raise FlowUnavailable('DIFY_TIMEOUT' if isinstance(exc.reason,TimeoutError) else 'DIFY_NETWORK_ERROR') from None
        return raw
    def call(self,context):
        context={**deepcopy(context),'schema_version':'2.2','stage':self.stage}
        budget=context.pop('_stage_budget_seconds',self.timeout)
        timeout=min(self.timeout,max(.1,float(budget)))
        payload={'inputs':{'context_json':encoded(context)},'query':'Execute the current versioned '+self.stage+' stage and return its JSON contract.',
                 'response_mode':'blocking','user':'xunjia-'+hashlib.sha256((self.stage+str(context.get('thread_id',context.get('member_id','')))).encode()).hexdigest()[:32]}
        request=urllib.request.Request(self.url,data=encoded(payload).encode('utf8'),method='POST',
              headers={'Authorization':'Bearer '+self._key,'Content-Type':'application/json'})
        start=self.clock();deadline=start+timeout;attempts=0;failures=[]
        recovery_reason=None;recovery_attempted=False
        self.last_evidence={}
        context_ref=hashlib.sha256(str(context.get('thread_id',context.get('member_id',''))).encode()).hexdigest()
        request_id=uuid.uuid4().hex
        basis_ref=hashlib.sha256(encoded(context.get('basis',{})).encode()).hexdigest()
        timing_event('dify_started',flow_stage=self.stage,context_ref=context_ref,
                     request_id=request_id,basis_ref=basis_ref)
        try:
            for index in range(2):
                remaining=timeout if index==0 else deadline-self.clock()
                if remaining<=0:raise FlowUnavailable('DIFY_TIMEOUT')
                if index and recovery_reason:recovery_attempted=True
                attempts+=1
                try:
                    raw=self._request_once(request,remaining)
                    break
                except FlowUnavailable as exc:
                    failures.append({'error_code':exc.code,'http_status':exc.status,
                                     'elapsed_ms':round((self.clock()-start)*1000,3)})
                    # All recovery reasons share this same two-request loop
                    # and absolute deadline. Native sends are never involved.
                    if index:raise
                    if exc.code=='DIFY_UPSTREAM_UNAVAILABLE':
                        if deadline-self.clock()<=.6:raise
                        time.sleep(.5)
                    elif (self.stage=='analysis' and 'validation_feedback' not in context
                            and exc.status==400 and exc.code==_ANALYSIS_ASSOCIATION_CODE):
                        if deadline-self.clock()<=0:raise
                        recovery_reason=exc.code
                        corrected={**context,'validation_feedback':{'mode':'OUTPUT_CONTRACT',
                            'attempt':1,'max_attempts':1,'reason_code':recovery_reason}}
                        corrected_payload={**payload,'inputs':{'context_json':encoded(corrected)}}
                        request=urllib.request.Request(self.url,data=encoded(corrected_payload).encode('utf8'),method='POST',
                            headers={'Authorization':'Bearer '+self._key,'Content-Type':'application/json'})
                    elif (self.stage=='reply' and 'validation_feedback' not in context
                            and exc.status==400 and exc.code in _MODEL_CONTRACT_ERRORS.values()
                            and _validated_previous_output(exc._previous_output) is not None):
                        if deadline-self.clock()<=0:raise
                        recovery_reason=exc.code
                        corrected={**context,'validation_feedback':{'mode':'OUTPUT_CONTRACT',
                            'attempt':1,'max_attempts':1,'reason_code':recovery_reason,
                            'previous_output':deepcopy(exc._previous_output)}}
                        corrected_payload={**payload,'inputs':{'context_json':encoded(corrected)}}
                        request=urllib.request.Request(self.url,data=encoded(corrected_payload).encode('utf8'),method='POST',
                            headers={'Authorization':'Bearer '+self._key,'Content-Type':'application/json'})
                    else:raise
            try:
                wrapper=json_object(raw.decode('utf8'))
                result=json_object(wrapper.get('answer'))
            except (UnicodeError,InvalidFact):raise FlowUnavailable('DIFY_INVALID_STAGE_JSON') from None
            if result.get('schema_version')!='2.2':raise FlowUnavailable('DIFY_INCOMPATIBLE_CONTRACT')
        except FlowUnavailable as exc:
            self.last_evidence={'stage':self.stage,'elapsed_ms':round((self.clock()-start)*1000,3),
                                'error_code':exc.code,'http_status':exc.status}
            if attempts>1:self.last_evidence.update(request_attempts=attempts,retry_failures=failures)
            if recovery_reason:self.last_evidence['output_contract_recovery']={'attempted':recovery_attempted,'reason_code':recovery_reason}
            timing_event('dify_completed',flow_stage=self.stage,context_ref=context_ref,
                         request_id=request_id,basis_ref=basis_ref,
                         elapsed_ms=self.last_evidence['elapsed_ms'],code=exc.code,http_status=exc.status,
                         request_attempts=attempts)
            raise
        self.last_evidence={'stage':self.stage,'elapsed_ms':round((self.clock()-start)*1000,3),
                            'message_id':wrapper.get('message_id'),'conversation_id':wrapper.get('conversation_id'),
                            'workflow_run_id':wrapper.get('workflow_run_id')}
        if attempts>1:self.last_evidence.update(request_attempts=attempts,retry_failures=failures)
        if recovery_reason:self.last_evidence['output_contract_recovery']={'attempted':recovery_attempted,'reason_code':recovery_reason}
        timing_event('dify_completed',flow_stage=self.stage,context_ref=context_ref,
                     request_id=request_id,basis_ref=basis_ref,
                     elapsed_ms=self.last_evidence['elapsed_ms'],workflow_run_id=wrapper.get('workflow_run_id'),
                     request_attempts=attempts,code='OK')
        return result
class Chatflows:
    def __init__(self,analysis,reply):self.analysis,self.reply=analysis,reply
    def analyze(self,snapshot):return self.analysis.call(snapshot)
    def decide(self,context):return self.reply.call(context)
