"""Durable client tests over documented destructive FIFO, not a mock OSS map."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import pytest
from relay.protocol import Binding,Codec,envelope,digest
from relay.file_gateway import FileGatewayError
from relay.collector_bridge import CollectorBridge
from server.file_gateway_transport import FileGatewayTransport
from server.receiver_ledger import ReceiverLedger
from file_gateway_fakes import Gateway,AUTH,NOW

B=Binding("synthetic-owner","synthetic-account","synthetic-wb",b"k"*16,b"s"*32)

def setup(tmp_path,gateway=None):
    g=gateway or Gateway();up=g.client("up");down=g.client("down")
    service=FileGatewayTransport(B,up,down,tmp_path/"service.db",role="service",clock=lambda:NOW)
    collector=FileGatewayTransport(B,down,up,tmp_path/"collector.db",role="collector",clock=lambda:NOW)
    return g,service,collector

def event(**changes):
    return {"event_id":"synthetic-event","peer_id":"synthetic-peer","text":"SYN-A 3.10\u51fa500\u4e07","direction":"in","occurred_at":NOW,"ext_msg_id":"synthetic-native",**changes}

def command(**changes):
    return {"owner_user_id":B.owner_user_id,"account_uid":B.account_uid,"workbench_id":B.workbench_id,"command_id":"synthetic-cmd","peer_id":"synthetic-peer","content_hash":"a"*64,"control_version":1,"expires_at":NOW+120,"text":"SYN-A 3.20%\u6536\uff0c500\u4e07","transport":"oss","business_version":"2.2","source_watermark":0,**changes}

def test_same_queue_is_not_a_production_duplex_transport(tmp_path):
    g=Gateway();client=g.client("up")
    with pytest.raises(FileGatewayError,match="SEPARATE_QUEUES"):
        FileGatewayTransport(B,client,client,tmp_path/"bad.db",role="service")
    assert not g.calls

def test_receive_journal_replays_locally_until_business_ack(tmp_path):
    g,s,c=setup(tmp_path);c.publish_event(event())
    assert len(g.queues["up.invalid"])==1
    first=s.poll();assert len(first)==1 and not g.queues["up.invalid"]
    before=len(g.calls)
    restart=FileGatewayTransport(B,g.client("up"),g.client("down"),tmp_path/"service.db",role="service",clock=lambda:NOW)
    assert restart.poll()==first and len(g.calls)==before
    restart.ack(first[0]["source_token"]);assert restart.poll()==[]

def test_zero_lost_input_when_processing_crashes_after_durable_download(tmp_path,monkeypatch):
    g,s,c=setup(tmp_path);c.publish_event(event())
    original=s._stage_downloads;calls=[0]
    def crash():
        calls[0]+=1
        if calls[0]==2:raise OSError("synthetic downstream crashed")
        return original()
    monkeypatch.setattr(s,"_stage_downloads",crash)
    with pytest.raises(OSError):s.poll()
    count=len(g.calls);assert not g.queues["up.invalid"]
    restart=FileGatewayTransport(B,g.client("up"),g.client("down"),tmp_path/"service.db",role="service",clock=lambda:NOW)
    assert restart.poll()[0]["envelope"]["payload"]["text"]==event()["text"]
    assert len(g.calls)==count

def test_uncertain_download_survives_restart_without_blocking_next_distinct_file(tmp_path):
    g,s,c=setup(tmp_path);c.publish_event(event());g.lose_download=True
    with pytest.raises(FileGatewayError):s.poll()
    assert s.receive_ready();count=len(g.calls)
    g.lose_download=False;c.publish_event(event(event_id="second",ext_msg_id="second"))
    count=len(g.calls)
    restart=FileGatewayTransport(B,g.client("up"),g.client("down"),tmp_path/"service.db",role="service",clock=lambda:NOW)
    rows=restart.poll()
    assert len(rows)==1 and rows[0]["envelope"]["payload"]["event_id"]=="second"
    assert len(g.calls)>count and not g.queues["up.invalid"]
    assert any(row["state"]=="UNKNOWN" for row in restart.delivery_diagnostics()["download_issues"])

def test_old_crash_between_request_and_fsync_does_not_globally_stop_future_files(tmp_path):
    g,s,c=setup(tmp_path)
    with s.store.db() as db:db.execute("INSERT INTO fg_download(scope,state) VALUES(?,?)",(s.store.scope,"RECEIVING"))
    assert s.poll()==[] and s.receive_ready()
    assert g.calls==[("up.invalid","pending"),("up.invalid","download")]
    with s.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM fg_download WHERE state='RECEIVING'").fetchone()[0]==1

def test_upload_timeout_is_not_retried_on_restart(tmp_path):
    g,s,c=setup(tmp_path);g.lose_upload=True
    with pytest.raises(FileGatewayError):c.publish_event(event())
    count=len(g.calls);assert len(g.queues["up.invalid"])==1
    restart=FileGatewayTransport(B,g.client("down"),g.client("up"),tmp_path/"collector.db",role="collector",clock=lambda:NOW)
    with pytest.raises(FileGatewayError):restart.flush_events()
    assert len(g.calls)==count and restart.delivery_diagnostics()["upload_unknown"]

def test_corrupt_file_is_preserved_and_distinct_valid_file_continues(tmp_path):
    g,s,c=setup(tmp_path);g.queues["up.invalid"].append(("foreign.bin",b"not an authenticated envelope"));c.publish_event(event())
    rows=s.poll()
    assert len(rows)==1 and rows[0]["envelope"]["payload"]["event_id"]==event()["event_id"]
    assert s.receive_ready() and not g.queues["up.invalid"]
    with s.store.db() as db:
        row=db.execute("SELECT blob,state FROM fg_download WHERE scope=?",(s.store.scope,)).fetchone()
        assert bytes(row[0])==b"not an authenticated envelope" and row[1]=="QUARANTINED"

def test_wrong_direction_authenticated_file_does_not_execute(tmp_path):
    g,s,c=setup(tmp_path);wire=command();env=s._command(wire,"command")
    g.queues["up.invalid"].append(("wrong-direction.bin",s.codec.pack(env)))
    assert s.poll()==[]
    assert not s.delivery_diagnostics()["download_blockers"]
    assert s.delivery_diagnostics()["download_issues"][0]["state"]=="QUARANTINED"

def test_publication_accepted_is_never_native_sent_and_duplicate_send_is_one_upload(tmp_path):
    g,s,c=setup(tmp_path);wire=command()
    assert s.send(wire)["status"]=="accepted"
    assert s.send(wire)["status"]=="pending"
    assert len(g.queues["down.invalid"])==1

def test_concurrent_duplicate_send_has_one_upload(tmp_path):
    g,s,c=setup(tmp_path);wire=command()
    with ThreadPoolExecutor(max_workers=2) as pool:answers=list(pool.map(lambda _:s.send(wire),range(2)))
    assert all(a["status"] in ("accepted","pending") for a in answers)
    assert len(g.queues["down.invalid"])==1

def test_collector_executes_once_then_receipt_reaches_service(tmp_path):
    g,s,c=setup(tmp_path);calls=[];ledger=ReceiverLedger(tmp_path/"receiver.db",B,clock=lambda:NOW)
    def native(wire):
        calls.append(wire);return {"status":"sent","evidence":"synthetic-native-ack","native_id":"synthetic-native-ack"}
    bridge=CollectorBridge(ledger=ledger,adapters={"oss":c},native_send=native,native_reconcile=lambda _: {"status":"unknown","evidence":"synthetic"})
    wire=command();assert s.send(wire)["status"]=="accepted"
    step=bridge.run_once();assert step["processed"]==1 and not step["errors"],step
    answer=s.reconcile(wire);assert answer["status"]=="sent" and answer["native_id"]=="synthetic-native-ack"
    bridge.run_once();assert len(calls)==1

def test_new_native_message_invalidates_old_command_over_new_carrier(tmp_path):
    g,s,c=setup(tmp_path);calls=[];ledger=ReceiverLedger(tmp_path/"receiver.db",B,clock=lambda:NOW)
    bridge=CollectorBridge(ledger=ledger,adapters={"oss":c},native_send=lambda wire:calls.append(wire),native_reconcile=lambda _:None)
    wire=command();s.send(wire);bridge.publish_native(event())
    step=bridge.run_once();assert not calls and step["processed"]==1,step
    rows=s.poll();receipt=next(r for r in rows if r["kind"]=="receipt")
    assert receipt["envelope"]["payload"]["status"] in ("failed","cancelled")

def test_auth_rejection_does_not_consume_or_permanently_block_queue(tmp_path):
    g,s,c=setup(tmp_path);c.publish_event(event());g.forced_status=401
    with pytest.raises(FileGatewayError):s.poll()
    assert s.receive_ready() and len(g.queues["up.invalid"])==1
    g.forced_status=None;assert len(s.poll())==1

def test_catalog_and_contacts_are_authenticated_cached_documents(tmp_path):
    g,s,c=setup(tmp_path)
    c.publish_catalog({"account_uid":B.account_uid,"capabilities":["negotiation-v22-watermark"]})
    c.publish_contacts({"account_uid":B.account_uid,"contacts":[]})
    assert s.catalog()["capabilities"]==["negotiation-v22-watermark"]
    assert s.contacts()["contacts"]==[]

def test_state_cannot_be_silently_rebound_to_different_queue(tmp_path):
    g,s,c=setup(tmp_path)
    with pytest.raises(FileGatewayError,match="STATE_ROUTE_CHANGED"):
        FileGatewayTransport(B,g.client("other"),g.client("down"),tmp_path/"service.db",role="service")

def test_retired_two_url_factory_is_removed():
    assert not hasattr(FileGatewayTransport,'from_config')

def test_current_factory_restores_opposite_paths_with_mtls(tmp_path):
    from server.institution_transport import InstitutionTransport
    from relay.gateway_paths import InstitutionKeys,CONTRACT,account_paths
    from tests.tls_material import tls_config
    auth=tmp_path/'auth.yaml';auth.write_text(json.dumps({'key':AUTH.key,'sm4_key':AUTH.sm4_key.decode()}));auth.chmod(0o600)
    keys=InstitutionKeys.from_auth(AUTH);binding=keys.binding('wb-a','10001')
    cfg={'contract':CONTRACT,'base_url':'https://relay.invalid','auth_file':auth.name,**tls_config()}
    c=InstitutionTransport.from_institution_config(binding,cfg,tmp_path/'db',role='collector',config_dir=tmp_path)
    paths=account_paths('wb-a','10001')
    assert c.inbound.path==paths['downlink_path'] and c.outbound.path==paths['uplink_path']
    assert c.inbound.base_url==c.outbound.base_url=='https://relay.invalid'
    with pytest.raises(FileGatewayError,match='CONFIG_INVALID'):
        InstitutionTransport.from_institution_config(binding,{**cfg,'uplink_url':'https://old.invalid'},tmp_path/'other',role='collector',config_dir=tmp_path)
