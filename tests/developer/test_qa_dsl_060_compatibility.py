"""Protect the deployed Nanyin DSL 0.6.0 import contract."""
from pathlib import Path
import yaml
from dify.qa_workflow import build as generator


def test_generated_and_shipped_dsl_pin_customer_version_even_with_newer_template(monkeypatch):
    original=yaml.safe_load
    def newer_template(text):
        value=original(text)
        value['version']='0.7.0'
        return value
    monkeypatch.setattr(generator.yaml,'safe_load',newer_template)
    generated=generator.build()
    shipped=original(Path('dify/qa_workflow/workflow.yml').read_text())
    assert generated['version']==shipped['version']=='0.6.0'
    assert generated==shipped


def test_http_node_matches_nanyin_060_shape_and_keeps_scoped_authentication():
    doc=generator.build()
    nodes={n['id']:n['data'] for n in doc['workflow']['graph']['nodes']}
    assert doc['app']['mode']=='workflow'
    node=nodes['market']
    assert node['body']=={'type':'json','data':'{{#contract.body#}}'}
    assert node['timeout']=={'max_connect_timeout':10,'max_read_timeout':45,'max_write_timeout':10}
    assert node['url']=='{{#start.tool_url#}}'
    assert 'Bearer {{#start.capability#}}' in node['headers']
    assert nodes['end']['outputs']==[{'variable':'receipt_id','value_selector':['receipt','receipt_id']}]
    assert [v['variable'] for v in nodes['start']['variables']]==['question','tool_url','capability']
