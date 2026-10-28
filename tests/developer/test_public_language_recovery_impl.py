"""Same existing public-language policy in backend and generated Code."""
import json
import traceback
import io
import urllib.error
from copy import deepcopy

import pytest

from dify.negotiation_v22.build import build
from server.negotiation import text_roles
from server.negotiation.engine import Engine
from server.negotiation.chatflow import _http_error_code, ChatflowClient
from server.negotiation.prices import InvalidFact
from test_literal_fact_binding_impl import current


CASES=[
    ('我方底线是{own_quote}。','Public reply exposes private policy or internal context'),
    ('{own_quote}，{peer_volume}确认。','Quantity confirmation must explicitly refer to a quotation, not execution'),
    ('{own_quote}，可以成交吗？','Reply promises execution or reserves a counterparty lot'),
]


@pytest.mark.parametrize('template,error',CASES)
def test_original_backend_still_raises_the_exact_same_public_error(template,error):
    text=template.replace('{own_quote}','3.15%收').replace('{peer_volume}','500万')
    with pytest.raises(InvalidFact,match=error):
        Engine.__new__(Engine)._validate_reply(current(),{'kind':'REPLY','text':text,
                                                         'our_price':{'kind':'YIELD','value':'3.15'}})


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
@pytest.mark.parametrize('template,error',CASES)
def test_generated_code_rejects_the_same_public_error_through_the_existing_private_carrier(version,template,error):
    ctx=current();ctx.update(schema_version='2.2',stage='reply',context_policy_revision='latest_quote_confirmation_v1')
    graph=build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
    scope={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='contract'),scope)
    raw={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'3.15','text':template}}
    try:scope['main'](json.dumps(raw),json.dumps(ctx))
    except ValueError:
        trace=traceback.format_exc()
        body={'code':'invalid_param','status':400,'message':'Run failed: '+trace+'error: exit status 255\n'}
        assert error in trace
        assert _http_error_code(400,json.dumps(body).encode())=='DIFY_MODEL_CONTRACT_PUBLIC_SCOPE'
    else:pytest.fail('A forbidden public sentence must fail inside Code before a candidate leaves the workflow')


def test_shared_helper_keeps_the_existing_predicate_local_negation_semantics():
    helper=getattr(text_roles,'public_language_error',None)
    assert callable(helper)
    assert helper('不自动成交、不锁量，只继续报价。') is None
    assert helper('不会自动成交但给你锁量。')=='Reply promises execution or reserves a counterparty lot'


@pytest.mark.parametrize('template,error',CASES)
def test_each_fixed_public_reason_gets_only_the_existing_one_corrective_request(template,error):
    ctx=current();ctx.update(schema_version='2.2',stage='reply',context_policy_revision='latest_quote_confirmation_v1')
    before=deepcopy(ctx);graph=build('reply',version='0.15.3',provider='synthetic',model='synthetic')['workflow']['graph']
    scope={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='contract'),scope)
    raw={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'3.15','text':template}}
    try:scope['main'](json.dumps(raw),json.dumps(ctx))
    except ValueError:
        body={'code':'invalid_param','status':400,'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'}
    else:pytest.fail('Original forbidden text must produce a real Code carrier')
    requests=[]
    class Response(io.BytesIO):status=200
    class Opener:
        def open(self,request,timeout):
            data=json.loads(request.data);requests.append(json.loads(data['inputs']['context_json']))
            if len(requests)==1:
                raise urllib.error.HTTPError('https://synthetic.invalid',400,'synthetic',{},io.BytesIO(json.dumps(body).encode()))
            corrected={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'3.15',
                'text':'我这边{own_quote}，你再看看？'}}
            answer=scope['main'](json.dumps(corrected),data['inputs']['context_json'])['result']
            return Response(json.dumps({'answer':answer}).encode())
    client=ChatflowClient('https://synthetic.invalid','synthetic-private-key',stage='reply',opener=Opener())
    result=client.call(ctx)
    assert len(requests)==2 and ctx==before
    assert requests[1]['validation_feedback']['reason_code']=='DIFY_MODEL_CONTRACT_PUBLIC_SCOPE'
    assert requests[1]['validation_feedback']['previous_output']==raw
    validated=Engine.__new__(Engine)._validate_reply(ctx,result['proposal'])
    assert validated['text']=='我这边3.15%收，你再看看？'
    assert client.last_evidence['request_attempts']==2
    assert client.last_evidence['retry_failures'][0]['error_code']=='DIFY_MODEL_CONTRACT_PUBLIC_SCOPE'
