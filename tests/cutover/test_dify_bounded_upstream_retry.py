"""One bounded retry for positively identified upstream service failure only."""
import io,json,urllib.error
import pytest
from server.negotiation.chatflow import ChatflowClient,FlowUnavailable

class Clock:
 def __init__(self):self.now=100.
 def __call__(self):return self.now
 def sleep(self,value):self.now+=value

class Reply(io.BytesIO):
 status=200

def response():return Reply(json.dumps({'answer':json.dumps({'schema_version':'2.2','observations':[]}), 'message_id':'synthetic-result'}).encode())
def failure(message=None,status=400):
 return urllib.error.HTTPError('https://synthetic.invalid/v1/chat-messages',status,'synthetic',{},io.BytesIO(json.dumps({'message':message or 'Run failed: [openai_api_compatible] Error: API request failed with status code 502: upstream unavailable'}).encode()))

def test_recovers_one_upstream_failure_and_keeps_retry_evidence(monkeypatch):
 clock=Clock();monkeypatch.setattr('server.negotiation.chatflow.time.sleep',clock.sleep)
 class Opener:
  calls=[]
  def open(self,request,timeout):
   self.calls.append((request.data,timeout));clock.now+=.2
   if len(self.calls)==1:raise failure()
   return response()
 opener=Opener();client=ChatflowClient('https://synthetic.invalid','synthetic-app',stage='analysis',opener=opener,clock=clock)
 assert client.call({'_stage_budget_seconds':2})['schema_version']=='2.2'
 assert len(opener.calls)==2 and opener.calls[0][0]==opener.calls[1][0]
 assert 0<opener.calls[1][1]<opener.calls[0][1]
 assert client.last_evidence['request_attempts']==2
 assert client.last_evidence['retry_failures'][0]['error_code']=='DIFY_UPSTREAM_UNAVAILABLE'
 assert 'synthetic-app' not in json.dumps(client.last_evidence)

def test_persistent_upstream_failure_stops_after_two_attempts(monkeypatch):
 clock=Clock();monkeypatch.setattr('server.negotiation.chatflow.time.sleep',clock.sleep)
 class Opener:
  count=0
  def open(self,*a,**kw):self.count+=1;raise failure()
 o=Opener();c=ChatflowClient('https://synthetic.invalid','synthetic-app',stage='reply',opener=o,clock=clock)
 with pytest.raises(FlowUnavailable,match='DIFY_UPSTREAM_UNAVAILABLE'):c.call({})
 assert o.count==2 and c.last_evidence['request_attempts']==2

@pytest.mark.parametrize('status,message',[(400,'Every raw message needs an explicit scope'),(401,'invalid key'),(403,'forbidden'),(429,'rate limited')])
def test_contract_and_auth_failures_never_retry(status,message):
 class Opener:
  count=0
  def open(self,*a,**kw):self.count+=1;raise failure(message,status)
 o=Opener();c=ChatflowClient('https://synthetic.invalid','synthetic-app',stage='analysis',opener=o)
 with pytest.raises(FlowUnavailable):c.call({})
 assert o.count==1

def test_retry_is_not_started_when_shared_budget_is_exhausted(monkeypatch):
 clock=Clock();monkeypatch.setattr('server.negotiation.chatflow.time.sleep',lambda _:pytest.fail('No retry delay allowed'))
 class Opener:
  count=0
  def open(self,*a,**kw):self.count+=1;clock.now+=.9;raise failure()
 o=Opener();c=ChatflowClient('https://synthetic.invalid','synthetic-app',stage='analysis',opener=o,clock=clock)
 with pytest.raises(FlowUnavailable):c.call({'_stage_budget_seconds':1})
 assert o.count==1