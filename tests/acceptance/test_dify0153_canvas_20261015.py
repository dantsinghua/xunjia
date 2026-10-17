"""Dify 0.15.3 canvas and IF/ELSE compatibility regression.

No API calls, no Dify instance updates, and no real model/IM activity.
"""
from pathlib import Path
import json
import yaml

from scripts.build_dify0153_dsl import ROOT, SOURCE, convert


def _renderable():
    original=yaml.safe_load(SOURCE.read_text(encoding='utf8'))
    current=yaml.safe_load((ROOT/'dify/chatflow_0153/chatflow-0153-safe.yml').read_text(encoding='utf8'))
    generated=convert(original,provider='openai_api_compatible',model_name='kimi-k2.5')
    return original,current,generated


def test_legacy_rendered_dsl_exactly_matches_current_generator():
    original,current,generated=_renderable()
    assert current==generated
    assert current['version']=='0.1.5'
    assert original['version']=='0.7.0'
    nodes=current['workflow']['graph']['nodes']
    edges=current['workflow']['graph']['edges']
    ids={n['id'] for n in nodes}
    assert len(nodes)==len(ids)==31
    assert len(edges)==32
    assert all(e['source'] in ids and e['target'] in ids for e in edges)
    assert not any(n['data']['type']=='agent' for n in nodes)


def test_if_else_canvas_conditions_are_string_typed_with_matching_code_outputs():
    _,current,_=_renderable()
    nodes={n['id']:n['data'] for n in current['workflow']['graph']['nodes']}
    matrix={
        'format_ok':('parse_first','valid'),
        'needs_history':('history_check','needs_history'),
        'needs_public':('allow_public','needs_public_reply'),
    }
    for node_id,(source_id,key) in matrix.items():
        cases=nodes[node_id]['cases']
        assert len(cases)==1
        conditions=cases[0]['conditions']
        assert len(conditions)==1
        condition=conditions[0]
        assert condition['comparison_operator']=='is'
        assert condition['variable_selector']==[source_id,key]
        assert condition['value']=='true'
        assert type(condition['value']) is str
        assert nodes[source_id]['outputs'][key]['type']=='string'


def _main(code):
    namespace={}
    exec(compile(code,'<synthetic-dify-code-node>','exec'),namespace)
    return namespace['main']


def test_condition_emitters_preserve_boolean_branch_semantics_as_strings():
    _,current,_=_renderable()
    nodes={n['id']:n['data'] for n in current['workflow']['graph']['nodes']}
    # Malformed private proposal cannot be promoted to a valid turn.
    result=_main(nodes['parse_first']['code'])('not-json','{}')
    assert result['valid']=='false'
    assert result['proposal']==''
    check=_main(nodes['history_check']['code'])
    assert check(json.dumps({'history_request':None}))['needs_history']=='false'
    assert check(json.dumps({'history_request':{'keywords':[]}}))['needs_history']=='true'
    public=_main(nodes['allow_public']['code'])
    safe=json.dumps({'ok':False,'needs_public_reply':False,'public_context':{}})
    assert public(safe,200,'{}')['needs_public_reply']=='false'
    allowed=json.dumps({'ok':True,'needs_public_reply':True,'public_context':{'allowed_actions':[]}})
    assert public(allowed,200,'{}')['needs_public_reply']=='true'
