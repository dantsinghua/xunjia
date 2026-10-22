"""A timed-out call may later have an exact trace-bound native delivery; never resend."""
from copy import deepcopy
import pytest
from tests.acceptance.test_qtrade_native_port_20261014 import binding,loop,Store,Bus,port,command

class LateStore(Store):
    def __init__(self):super().__init__();self.late=None;self.reads=[]
    def confirmed_outbound_for_trace(self,trace):self.reads.append(trace);return deepcopy(self.late)

def world(binding,loop,code="TIMEOUT"):
    s=LateStore();bus=Bus();p=port(binding,loop,store=s,bus=bus);c=command(binding)
    s.idem[("local-acct-01",c["command_id"])]=dict(status="DONE",trace_id="exact-trace",op="send_text",args_hash=p._args_hash(c))
    s.results["exact-trace"]={"ok":0,"code":code,"data":{}}
    s.late=dict(trace_id="exact-trace",account_id="local-acct-01",session_id="local-acct-01:"+c["peer_id"],dir="out",type="text",text=c["text"],state="DELIVERED",confirmed_by="ingest_merge",ext_msg_id="qd:late-exact-native",ts_ms=1800000000000)
    return s,bus,p,c

@pytest.mark.parametrize("code",["TIMEOUT","UNCONFIRMED","SEND_FAILED"])
def test_late_real_readback_resolves_unknown_without_bus_submit(binding,loop,code):
    s,b,p,c=world(binding,loop,code)
    out=p.native_reconcile(c)
    assert out["status"]=="sent" and out["native_id"]=="qd:late-exact-native"
    assert out["evidence"]=="qtrade-native-readback:exact-trace"
    assert out["executed_at"]==1800000000
    assert b.calls==[] and s.results["exact-trace"]["code"]==code

@pytest.mark.parametrize("changes",[{"account_id":"other"},{"session_id":"local-acct-01:other-peer"},{"trace_id":"other-trace"},{"text":"different"},{"dir":"in"},{"state":"SENDING"},{"ext_msg_id":None},{"confirmed_by":None}])
def test_late_readback_must_match_all_original_native_facts(binding,loop,changes):
    s,b,p,c=world(binding,loop);s.late.update(changes)
    assert p.native_reconcile(c)["status"]=="unknown" and b.calls==[]

def test_wrong_idempotency_never_reads_or_matches_text(binding,loop):
    s,b,p,c=world(binding,loop);s.idem[("local-acct-01",c["command_id"])]["args_hash"]="wrong"
    assert p.native_reconcile(c)["status"]=="unknown" and s.reads==[] and b.calls==[]

def test_expired_cancel_recovers_late_native_sent_instead_of_endless_unknown(tmp_path):
    from tests.cutover.test_file_gateway_transport import setup,B,command,NOW
    from server.receiver_ledger import ReceiverLedger
    from relay.collector_bridge import CollectorBridge
    clock=[NOW];g,s,c=setup(tmp_path);cmd=command(kind="send_text",expires_at=NOW+1)
    ledger=ReceiverLedger(tmp_path/"receiver.db",B,clock=lambda:clock[0]);calls=[]
    def ambiguous(v):calls.append(v);return {"status":"unknown","evidence":"timed-out-after-native"}
    assert ledger.execute(cmd,ambiguous)["status"]=="unknown"
    clock[0]+=2
    def reconcile(v):return {"status":"sent","native_id":"qd:late","evidence":"trace-bound-native-record"}
    bridge=CollectorBridge(ledger=ledger,adapters={"oss":c},native_send=lambda _:pytest.fail("cancel cannot send"),native_reconcile=reconcile)
    s.cancel(cmd);result=bridge.run_once();assert result["errors"]==[],result
    receipts=[row["envelope"]["payload"] for row in s.poll() if row["envelope"]["kind"]=="receipt"]
    assert len(receipts)==1 and receipts[0]["status"]=="sent",receipts
    assert ledger.reconcile(cmd,reconcile)["status"]=="sent" and len(calls)==1
