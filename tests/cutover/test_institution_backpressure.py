"""Real path transport cannot flood a FIFO while its receiver is paused."""
import pytest
from relay.gateway_paths import CONTRACT
from tests.cutover.test_institution_path_contract import pair,NOW

def event(number):
    return {'event_id':'event-'+str(number),'peer_id':'peer','text':'synthetic',
            'direction':'in','occurred_at':NOW,'ext_msg_id':'native-'+str(number)}

@pytest.mark.parametrize('op',['activate_context','context_ready'])
def test_identical_context_control_has_one_durable_wire_identity(tmp_path,op):
    gateway,keys,service,collector=pair(tmp_path)
    sender=service if op=='activate_context' else collector
    payload={'contract':CONTRACT,'op':op,'context_id':'synthetic-context','context_version':1}
    ids=[]
    for _ in range(25):
        ids.append(sender.publish_manifest(payload));gateway.now+=2
    assert len(set(ids))==1
    assert len(gateway.queues[sender.outbound.path])==1
    with sender.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM ip_delivery').fetchone()[0]==1


def test_paused_receiver_never_accumulates_periodic_duplicate_uploads(tmp_path):
    gateway,keys,service,collector=pair(tmp_path)
    collector.publish_event(event(1))
    for _ in range(100):
        gateway.now+=31;collector.retry_data()
    assert len(gateway.queues[collector.outbound.path])==1
    with collector.store.db() as db:
        assert db.execute('SELECT attempts FROM ip_delivery').fetchone()[0]==1


def test_data_lost_after_consuming_fifo_still_recovers_once_queue_is_empty(tmp_path):
    gateway,keys,service,collector=pair(tmp_path)
    collector.publish_event(event(1))
    assert service.inbound.download() is not None
    assert not gateway.queues[collector.outbound.path]
    gateway.now+=31;collector.retry_data()
    rows=service.poll();assert len(rows)==1
    service.ack(rows[0]['source_token']);service.poll();collector.poll()
    gateway.now+=31;collector.retry_data()
    assert not gateway.queues[collector.outbound.path]
    with collector.store.db() as db:assert db.execute('SELECT acked FROM ip_delivery').fetchone()[0]==1


def test_backpressure_does_not_suppress_new_original_native_data(tmp_path):
    gateway,keys,service,collector=pair(tmp_path)
    collector.publish_event(event(1));gateway.now+=31;collector.retry_data()
    collector.publish_event(event(2))
    assert len(gateway.queues[collector.outbound.path])==2


def test_different_contexts_never_reuse_control_identity(tmp_path):
    gateway,keys,service,collector=pair(tmp_path)
    ids=[service.publish_manifest({'contract':CONTRACT,'op':'activate_context',
        'context_id':'context-'+str(i),'context_version':i+1}) for i in range(2)]
    assert len(set(ids))==2 and len(gateway.queues[service.outbound.path])==2


def test_idle_retry_does_not_probe_or_upload(tmp_path):
    gateway,keys,service,collector=pair(tmp_path)
    before=list(gateway.calls);collector.retry_data()
    assert gateway.calls==before