"""No active bond members means an empty association set, not a model retry loop."""
import json
import pytest
from relay.protocol import Binding, envelope
from tests.cutover.test_production_flow import desk,OWNER,NOW,draft,confirm,raw

@pytest.mark.parametrize("historical",[False,True])
def test_unassigned_thread_keeps_raw_messages_without_model_or_send(desk,historical):
    app,client,flows,transport,clock=desk
    binding=Binding(OWNER,"synthetic-account","synthetic-wb",b"s"*32,b"h"*32)
    payload={"event_id":"outside-current-bonds","peer_id":"outside-peer","direction":"in",
        "source_seq":1,"text":"259324.SH 25武空02 1.72出2000万","occurred_at":NOW,"ext_msg_id":"native-unassigned","historical":historical}
    message=app.state.inbox.apply(binding,envelope(binding,"event",payload,peer_id="outside-peer",message_id="outside-packet",created_at=NOW,expires_at=NOW+100))
    with app.state.repository.db.transaction(write=False) as tx:
        tid=tx.execute("SELECT thread_id FROM xj2_message WHERE message_id=?",(message,)).fetchone()["thread_id"]
    def forbidden(_):pytest.fail("A model cannot invent a member when there are no authorized bond members")
    flows.analyze=forbidden
    result=app.state.negotiation_engine.analyze_thread(OWNER,tid,force=True)
    assert result["facts_committed"] is True,result
    assert result["repair"]["mode"]=="NO_ACTIVE_MEMBERS"
    with app.state.repository.db.transaction(write=False) as tx:
        row=tx.execute("SELECT * FROM xj2_message WHERE message_id=?",(message,)).fetchone()
        assert row["text"]==payload["text"] and bool(row["historical"])==historical
        thread=tx.execute("SELECT * FROM xj2_thread WHERE thread_id=?",(tid,)).fetchone()
        assert thread["analyzed_revision"]==thread["ingest_revision"]
        assert tx.execute("SELECT COUNT(*) AS n FROM xj2_group").fetchone()["n"]==0
        assert tx.execute("SELECT COUNT(*) AS n FROM xj2_command").fetchone()["n"]==0
    assert transport.sent==[]

def test_existing_bond_still_requires_real_model_stage(desk):
    app,client,flows,transport,clock=desk
    confirm(client,draft(client))
    member=app.state.repository.group_rows(OWNER)[0]["peers"][0]
    raw(app,member["thread_id"],"SYN-A 3.10出 500万")
    result=app.state.negotiation_engine.analyze_thread(OWNER,member["thread_id"],force=True)
    assert result["facts_committed"] is True and len(flows.analysis_calls)==1
