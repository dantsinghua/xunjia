"""Current source authority must remain explicit in ordinary and repair views."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from test_verified_literal_fact_binding import quantity_context, reply


def generated(version):
    nodes={row['id']:row['data'] for row in build('reply',version=version,
        provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
    projection,contract={},{}
    exec(nodes['context']['code'],projection);exec(nodes['contract']['code'],contract)
    return projection['main'],contract['main']


def context_for(state,mode):
    context=quantity_context();context.update(schema_version='2.2',stage='reply')
    if state=='QUESTION':context['quotes']['ofr']['assertion']='QUESTION'
    elif state=='WITHDRAWN':context['quotes']['ofr']['validity']='WITHDRAWN'
    if mode=='OUTPUT_CONTRACT':
        context['validation_feedback']={'mode':mode,'attempt':1,'max_attempts':1,
            'reason_code':'DIFY_MODEL_CONTRACT_SLOTS','previous_output':{
                'schema_version':'2.2','reply_plan':reply('这边{own_quote}，500万你再看看？')}}
    return context


def unavailable_hint(system,state):
    # Small semantic anchors, not a fixed sentence or a strategy template.
    return [line for line in system.splitlines() if state in line
            and 'peer_quote' in line and 'peer_volume' in line
            and any(word in line for word in ('不可用','禁用'))]


@pytest.mark.parametrize('state',['QUESTION','WITHDRAWN'])
@pytest.mark.parametrize('mode',['ordinary','OUTPUT_CONTRACT'])
def test_current_nonquotation_has_an_explicit_no_macro_no_bare_number_hint_and_keeps_legal_own_quote(state,mode):
    context=context_for(state,mode);before=deepcopy(context)
    for version in ('0.15.3','1.17.1'):
        project,contract=generated(version)
        result=project(json.dumps(context,ensure_ascii=False))
        hints=unavailable_hint(result['system_prompt'],state)
        assert hints, 'The current peer state must explicitly disable both peer fact slots'
        assert any('裸' in line for line in hints), 'Disabled references cannot be rewritten as raw numbers'
        assert any(any(word in line for word in ('删除','去掉','不提','省略')) for line in hints)
        for text in ['你刚才{peer_quote}，这边{own_quote}。','这边{own_quote}，{peer_volume}你再看看？',
                     '这边{own_quote}，500万你再看看？']:
            with pytest.raises((ValueError,InvalidFact)):render_reply_plan(context,reply(text))
        safe={'schema_version':'2.2','reply_plan':reply('这边{own_quote}，你再看看？')}
        decoded=json.loads(contract(json.dumps(safe),json.dumps(context))['result'])
        body=Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
        assert body['our_price']['value']=='3.15'
        assert '500' not in body['text'] and '3.10' not in body['text']
        if mode=='OUTPUT_CONTRACT':
            # Only the rejected generated sentence is omitted from the model view.
            bad=before['validation_feedback']['previous_output']
            with pytest.raises(ValueError,match='^Economic numbers and facts must use verified slots$'):
                render_reply_plan(context,bad['reply_plan'])
            with pytest.raises(ValueError,match='^XUNJIA_OUTPUT_CONTRACT_V1:') as rejected:
                contract(json.dumps(bad),json.dumps(context))
            assert str(rejected.value.__cause__)=='Economic numbers and facts must use verified slots'
            carrier=json.loads(str(rejected.value).split(':',1)[1])
            assert carrier=={'version':1,'origin':'render_reply_plan',
                'reason_code':'DIFY_MODEL_CONTRACT_SLOTS','previous_output':bad}
            expected_feedback=deepcopy(before['validation_feedback'])
            del expected_feedback['previous_output']['reply_plan']['text']
            assert json.loads(result['context'])['validation_feedback']==expected_feedback
            assert context['validation_feedback']==before['validation_feedback']
    assert context==before


def test_active_current_quote_is_not_disabled_and_keeps_its_source_bound_price_and_whole_lot():
    for mode in ('ordinary','OUTPUT_CONTRACT'):
        context=context_for('ACTIVE_QUOTE',mode);before=deepcopy(context)
        for version in ('0.15.3','1.17.1'):
            project,contract=generated(version)
            system=project(json.dumps(context))['system_prompt']
            assert not unavailable_hint(system,'QUOTE')
            plan=reply('你刚才{peer_quote}，{peer_volume}，这边{own_quote}。')
            decoded=json.loads(contract(json.dumps({'schema_version':'2.2','reply_plan':plan}),json.dumps(context))['result'])
            body=Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
            assert '3.10%出' in body['text'] and '500万' in body['text']
        assert context==before


def test_normalized_peer_yield_uses_the_authoritative_quote_or_omits_raw_valuation_bp():
    for mode in ('ordinary','OUTPUT_CONTRACT'):
        context=context_for('ACTIVE_QUOTE',mode)
        context['context'][0]['text']='SYN-A 中债估值－５出５００万'
        before=deepcopy(context)
        for version in ('0.15.3','1.17.1'):
            project,contract=generated(version)
            system=project(json.dumps(context,ensure_ascii=False))['system_prompt']
            assert any('YIELD' in line and 'peer_quote' in line and '中债' in line
                       and any(word in line for word in ('不重述','不复述','不得','禁止','不要'))
                       for line in system.splitlines()), 'Normalized yield must not be restated as an unbound raw BP offset'
            with pytest.raises((ValueError,InvalidFact)):
                render_reply_plan(context,reply('{own_quote}，考虑中债估值-5出{peer_volume}。'))
            safe=reply('你刚才{peer_quote}，这边{own_quote}。')
            decoded=json.loads(contract(json.dumps({'schema_version':'2.2','reply_plan':safe}),json.dumps(context))['result'])
            assert Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])['our_price']['value']=='3.15'
        assert context==before
