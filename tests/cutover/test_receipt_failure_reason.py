"""The page receives the concrete original execution rejection, not an empty reason."""
import pytest
from tests.cutover.test_production_flow import desk,OWNER,draft,confirm

@pytest.mark.parametrize("state,evidence",[("FAILED","qtrade-rejected-before-native:anchor_not_found:search_entry"),("UNKNOWN","qtrade-native-result-timeout")])
def test_receipt_exposes_latest_matching_native_evidence(desk,state,evidence):
    app,client,*_=desk
    value=confirm(client,draft(client));cid=value["commands"][0]["command_id"]
    commands=app.state.inquiry.commands_core;row=commands.get(OWNER,cid)
    commands.receipt(OWNER,cid,account_uid="synthetic-account",peer_uid=value["commands"][0]["peer_id"],content_hash=row["content_hash"],status=state,evidence=evidence)
    response=client.get("/api/im/receipts/"+cid)
    assert response.status_code==200,response.text
    assert response.json()["data"]["reason"]==evidence
