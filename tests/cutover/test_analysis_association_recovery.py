"""A association correction uses real generated Code and the original client cap."""
from copy import deepcopy
import json
import traceback
import pytest

from server.negotiation.chatflow import ChatflowClient, FlowUnavailable
from test_analysis_reference_handles import scene, generated, model_for, run_decode, OWNER
from test_dify_output_contract_recovery import Clock, Response, http_error

CODE='DIFY_MODEL_CONTRACT_ANALYSIS_ASSOCIATION'
MARKER='XUNJIA_OUTPUT_CONTRACT_V1:'


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_real_scope_error_gets_one_metadata_only_correction_then_commits_complete_valid_facts(tmp_path,monkeypatch,version):
    case,source,job,context=scene(tmp_path)
    context['_stage_budget_seconds']=5
    before=deepcopy(context);clock=Clock()
    monkeypatch.setattr('server.negotiation.chatflow.time.sleep',clock.sleep)
    project,decode=generated(version)
    class Opener:
        calls=[];views=[];first_body=None
        def open(self,request,timeout):
            raw=json.loads(request.data);ctx=json.loads(raw['inputs']['context_json'])
            self.calls.append({'context':deepcopy(ctx),'timeout':timeout})
            view,model=model_for(project,ctx);self.views.append(view)
            clock.sleep(.25)
            assert len(self.calls)<=2
            if len(self.calls)==1:
                member=view['members'][0]['member_id']
                model['scopes']={view['messages'][0]['message_id']:[member.replace('_','8_',1)]}
                try:run_decode(decode,model,ctx)
                except ValueError:
                    self.first_body={'code':'invalid_param','status':400,
                        'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'}
                    raise http_error(self.first_body)
                raise AssertionError('An invalid scope must not be accepted')
            assert case.quote()['price'] is None, 'The failed first envelope must not commit facts'
            self.result=run_decode(decode,model,ctx)
            return Response(json.dumps({'answer':json.dumps(self.result)}).encode())
    opener=Opener();client=ChatflowClient('https://synthetic.invalid','SYNTHETIC_APP_SECRET',
        stage='analysis',timeout=5,opener=opener,clock=clock)
    result=client.call(context)
    assert result==opener.result and len(opener.calls)==2
    feedback=opener.calls[1]['context']['validation_feedback']
    assert feedback=={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,'reason_code':CODE}
    payload=json.loads(opener.first_body['message'].split(MARKER,1)[1].splitlines()[0])
    assert payload=={'version':1,'origin':'analysis_associations','reason_code':CODE}
    assert opener.calls[0]['timeout']==5 and 0<opener.calls[1]['timeout']<5
    for key in ['messages','members','slot_candidates']:
        assert opener.views[0][key]==opener.views[1][key]
    initial=deepcopy(opener.calls[0]['context']);corrected=deepcopy(opener.calls[1]['context'])
    corrected.pop('validation_feedback');assert initial==corrected
    assert result['scopes']=={source:[case.member]} and context==before
    scopes,observations,unresolved=case.engine._normalize(context,result)
    saved=case.store.commit_analysis(OWNER,job['analysis_id'],job['lease_token'],
        scopes=scopes,observations=observations,unresolved=unresolved)
    assert saved['facts_committed'] and saved['accepted'] and not saved['rejected']
    assert case.quote()['price']['value']=='3.10' and case.quote()['volume_wan']=='500'



def real_failure(context,version='1.17.1',point='scope'):
    project,decode=generated(version);view,model=model_for(project,context)
    member=view['members'][0]['member_id'];message=view['messages'][0]['message_id']
    if point=='scope':model['scopes']={message:[member.replace('_','8_',1)]}
    elif point=='unresolved':model['unresolved']={message:[member]}
    elif point=='missing_scope':model['scopes']={}
    else:raise AssertionError(point)
    try:run_decode(decode,model,context)
    except ValueError:
        return {'code':'invalid_param','status':400,
            'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'}
    raise AssertionError('Malformed analysis must fail real Code')


def client_steps(monkeypatch,context,steps,stage='analysis'):
    from test_dify_output_contract_recovery import ScriptedOpener
    clock=Clock();monkeypatch.setattr('server.negotiation.chatflow.time.sleep',clock.sleep)
    opener=ScriptedOpener(clock,steps)
    client=ChatflowClient('https://synthetic.invalid','SYNTHETIC_APP_SECRET',stage=stage,timeout=5,opener=opener,clock=clock)
    return client,opener


@pytest.mark.parametrize('point',['scope','unresolved'])
def test_association_carrier_has_only_safe_metadata_and_a_real_typed_cause(tmp_path,point):
    _case,_source,_job,context=scene(tmp_path)
    body=real_failure(context,point=point);message=body['message']
    assert 'AnalysisAssociationError: Invalid member association' in message
    assert all(name in message for name in ['_validate_result_strict','decode_stage_result','decode_model_output'])
    payload=json.loads(message.split(MARKER,1)[1].splitlines()[0])
    assert payload=={'version':1,'origin':'analysis_associations','reason_code':CODE}
    assert context['messages'][0]['text'] not in message


@pytest.mark.parametrize('order',['contract_contract','contract_upstream','upstream_contract'])
def test_analysis_association_and_transport_share_two_requests_and_one_absolute_deadline(tmp_path,monkeypatch,order):
    from test_dify_output_contract_recovery import upstream_failure
    _case,_source,_job,context=scene(tmp_path);before=deepcopy(context)
    error=http_error(real_failure(context))
    outcomes={'contract_contract':[error,http_error(real_failure(context))],
        'contract_upstream':[error,upstream_failure()], 'upstream_contract':[upstream_failure(),error]}[order]
    client,opener=client_steps(monkeypatch,context,[(outcomes[0],.4),(outcomes[1],.3)])
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls)==2 and opener.calls[0]['timeout']==5
    assert 0<opener.calls[1]['timeout']<5 and context==before
    assert client.last_evidence['request_attempts']==2


def test_expired_analysis_call_budget_does_not_start_association_correction(tmp_path,monkeypatch):
    _case,_source,_job,context=scene(tmp_path)
    context['_stage_budget_seconds']=1
    client,opener=client_steps(monkeypatch,context,[(http_error(real_failure(context)),1.1)])
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls)==1 and opener.calls[0]['timeout']==1


@pytest.mark.parametrize('mutation',['origin','typed_cause','frame','extra_previous','duplicate_json','oversized','auth','generic400','reason'])
def test_spoofed_or_inexact_analysis_carriers_fail_closed_without_private_leak(tmp_path,monkeypatch,caplog,mutation):
    _case,_source,_job,context=scene(tmp_path);body=real_failure(context)
    message=body['message'];prefix,tail=message.split(MARKER,1);line,rest=tail.split('\n',1)
    payload=json.loads(line);status=400
    if mutation=='origin':payload['origin']='render_reply_plan'
    elif mutation=='typed_cause':prefix=prefix.replace('AnalysisAssociationError:','ValueError:')
    elif mutation=='frame':prefix=prefix.replace('_validate_result_strict','_unrelated_check')
    elif mutation=='extra_previous':payload['previous_output']={'private':'SYNTHETIC_PRIVATE_HTTP_BODY'}
    elif mutation=='auth':status=401
    elif mutation=='generic400':body={'code':'invalid_param','status':400,'message':'Invalid member association'}
    elif mutation=='reason':payload['reason_code']='DIFY_MODEL_CONTRACT_ANALYSIS_OTHER'
    replacement=json.dumps(payload,separators=(',',':'))
    if mutation=='duplicate_json':replacement=replacement[:-1]+',"reason_code":"'+CODE+'"}'
    if mutation=='oversized':prefix='SYNTHETIC_PRIVATE_HTTP_BODY'+'x'*17000+prefix
    if mutation!='generic400':body={**body,'message':prefix+MARKER+replacement+'\n'+rest}
    client,opener=client_steps(monkeypatch,context,[(http_error(body,status),.1)])
    with pytest.raises(FlowUnavailable) as caught:client.call(context)
    assert len(opener.calls)==1
    public=str(caught.value)+repr(caught.value)+json.dumps(client.last_evidence)+caplog.text
    assert 'SYNTHETIC_PRIVATE_HTTP_BODY' not in public and context['messages'][0]['text'] not in public
    assert MARKER not in public


@pytest.mark.parametrize('stage,feedback',[('analysis',True),('reply',False)])
def test_existing_feedback_and_other_stage_cannot_use_an_analysis_carrier(tmp_path,monkeypatch,stage,feedback):
    _case,_source,_job,context=scene(tmp_path);body=real_failure(context)
    if feedback:context['validation_feedback']={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,'reason_code':CODE}
    before=deepcopy(context);client,opener=client_steps(monkeypatch,context,[(http_error(body),.1)],stage=stage)
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls)==1 and context==before


def test_missing_scope_and_observation_errors_do_not_gain_whole_analysis_recovery(tmp_path,monkeypatch):
    _case,_source,_job,context=scene(tmp_path)
    client,opener=client_steps(monkeypatch,context,[(http_error(real_failure(context,point='missing_scope')),.1)])
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls)==1
    project,decode=generated();_view,model=model_for(project,context)
    model['observations'][0]['slots']['price']=model['observations'][0]['slots']['volume_wan']
    partial=run_decode(decode,model,context)
    assert partial['observations'][0].get('validation_error')
    client,opener=client_steps(monkeypatch,context,[(json.dumps({'answer':json.dumps(partial)}).encode(),.1)])
    assert client.call(context)==partial and len(opener.calls)==1


def test_analysis_recovery_keeps_complete_scopes_and_runs_original_price_expression_decoder(tmp_path):
    for version in ['0.15.3','1.17.1']:
        directory=tmp_path/version.replace('.','-');directory.mkdir()
        _case,source,_job,context=scene(directory)
        project,decode=generated(version);_view,model=model_for(project,context)
        canonical=run_decode(decode,model,context)
        context['validation_feedback']={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,'reason_code':CODE}
        before=deepcopy(context)
        observation=canonical['observations'][0]
        changes=observation.pop('changes');evidence=observation.pop('evidence')
        observation['fields']=[{'field':field,'value':({'basis':'收益率','value':'3.10'} if field=='price' else value),
            'evidence':evidence[field]} for field,value in changes.items()]
        result=run_decode(decode,canonical,context)
        assert result['scopes']=={source:[context['members'][0]['member_id']]}
        assert result['observations'][0]['changes']['price']['kind']=='YIELD'
        assert result['observations'][0]['changes']['price']['value']=='3.10' and context==before


def test_recovery_requires_explicit_scopes_even_for_known_out_and_cannot_invent_confirmation_ids(tmp_path):
    _case,source,_job,context=scene(tmp_path)
    context['messages'].append({'message_id':'synthetic-known-out','direction':'out','text':'SYN-A 3.20收1000万',
        'native_id':'synthetic-nonnative-proof','trade_day':context['messages'][0]['trade_day'],
        'historical':False,'ingest_revision':2})
    context['through_revision']=2;member=context['members'][0]['member_id']
    context['known_outbound_scopes']={'synthetic-known-out':[member]}
    context['validation_feedback']={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,'reason_code':CODE}
    before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        project,decode=generated(version);view,model=model_for(project,context)
        with pytest.raises(ValueError):run_decode(decode,model,context)
        model['scopes'][view['messages'][1]['message_id']]=[view['members'][0]['member_id']]
        assert run_decode(decode,model,context)['scopes']=={source:[member],'synthetic-known-out':[member]}
        missing={'schema_version':'2.2','scopes':model['scopes'],'confirmation_requests':[{}]}
        with pytest.raises(ValueError):run_decode(decode,missing,context)
    assert context==before
