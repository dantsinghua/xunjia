"""Public bond tools are stateless, credential-free, and read-only."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.app import create_app


def _calc():
    return {
        "schema_version":"1", "operation":"yield_gap",
        "inputs":{
            "our_side":"BUY", "peer_side":"ofr",
            "limit_yield_pct":"2.40","peer_yield_pct":"2.20",
            "same_basis":True,
        },
    }


@pytest.fixture
def client(tmp_path):
    app=create_app(database_url="sqlite:///"+str(tmp_path/"public.db"),enable_background=False)
    with TestClient(app) as c:
        yield app,c


def test_public_calculation_works_without_app_token_or_conversation_ticket(client):
    app,c=client
    with app.state.repository.db.transaction(write=False) as tx:
        before=tx.execute('SELECT COUNT(*) AS n FROM xj2_analysis').fetchone()['n']
    response=c.post('/api/agent/tools/bond_calc',json=_calc())
    assert response.status_code==200,response.text
    assert response.json()['status']=='ok'
    assert response.json()['usage']=='analysis_only'
    assert 'authorization' not in response.text.lower()
    with app.state.repository.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_analysis').fetchone()['n']==before


def test_existing_dify_json_wrappers_work_without_tokens(client):
    _,c=client
    args={"arguments_json":json.dumps(_calc())}
    assert c.post('/api/agent/tools/bond_calc',json=args).json()['status']=='ok'
    old={**args,"tool_budget_ref":"obsolete-ticket-with-no-security-meaning"}
    result=c.post('/api/agent/tools/bond_calc',json=old)
    assert result.status_code==200 and result.json()['status']=='ok'
    assert 'obsolete-ticket' not in result.text


@pytest.mark.parametrize("tool,query",[
    ("bond_info",{"schema_version":"1","mode":"get","bond_id":"SYN-ONLY"}),
    ("issuer_info",{"schema_version":"1","mode":"get","issuer_id":"SYN-ONLY"}),
    ("similar_bonds",{"schema_version":"1","anchor_bond_id":"SYN-ONLY",
        "tenor_basis":"maturity","remaining_days":{"min":0,"max":365},
        "issuer_scope":"any","compare_fields":["remaining_days"]}),
])
def test_public_fact_tools_are_accessible_and_fail_closed_without_data_source(client,tool,query):
    _,c=client
    result=c.post('/api/agent/tools/'+tool,json=query)
    assert result.status_code==200,result.text
    assert result.json()['status']=='unavailable'
    assert result.json()['data'] is None


def test_only_declared_tools_and_validated_inputs_are_accepted(client):
    _,c=client
    assert c.post('/api/agent/tools/secret_data',json=_calc()).json()['status']=='invalid_input'
    bad=c.post('/api/agent/tools/bond_calc',json={"arguments_json":'{"schema_version":"1","schema_version":"2"}'})
    assert bad.status_code==400


def test_retired_private_context_cannot_execute_old_business_contract(client):
    _,c=client
    assert c.post('/internal/agent/context',json={'turn_ref':'fake-ref'}).status_code==410
    assert c.post('/internal/agent/history',json={'turn_ref':'fake-ref','request':{}}).status_code==410
    assert c.post('/internal/agent/validate',json={'turn_ref':'fake-ref','proposal':{}}).status_code==410
    assert c.post('/api/auth/me').status_code==401
