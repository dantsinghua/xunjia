"""Backlog reception is concurrent but every download is durably journaled."""
import threading
import time
import pytest
from relay.protocol import envelope
from relay.file_gateway import DownloadedFile,FileGatewayError
from tests.cutover.test_institution_path_contract import pair,NOW


def test_service_prefetches_known_backlog_and_preserves_native_sequence(tmp_path,monkeypatch):
    gateway,keys,service,collector=pair(tmp_path)
    bodies=[]
    for i in range(1,17):
        value=envelope(collector.binding,'event',{'event_id':'e'+str(i),'peer_id':'peer',
            'source_seq':i,'direction':'in','text':'synthetic '+str(i),'occurred_at':NOW+i},
            message_id='e'+str(i),peer_id='peer',created_at=NOW,expires_at=NOW+600)
        bodies.append(DownloadedFile('native-'+str(i)+'.bin',collector.codec.pack(value)))
    lock=threading.Lock();active=0;maximum=0;index=0
    def download():
        nonlocal active,maximum,index
        with lock:
            if index>=len(bodies):return None
            at=index;index+=1;active+=1;maximum=max(maximum,active)
        time.sleep(.01*(1+(at%3)))
        with lock:active-=1
        return bodies[at]
    monkeypatch.setattr(service.inbound,'pending',lambda:len(bodies)-index)
    monkeypatch.setattr(service.inbound,'download',download)
    rows=service.poll(limit=16)
    assert 1<maximum<=8
    assert [r['envelope']['payload']['source_seq'] for r in rows]==list(range(1,17))
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM fg_download WHERE state='STAGED' AND blob IS NOT NULL").fetchone()[0]==16
    # A caller crash before ack must redeliver the locally persisted originals.
    monkeypatch.setattr(service.inbound,'download',lambda:pytest.fail('Stored input must be returned before more remote reads'))
    again=service.poll(limit=16)
    assert [r['envelope']['message_id'] for r in again]==[r['envelope']['message_id'] for r in rows]


def test_parallel_failure_keeps_other_downloads_durable_and_records_uncertainty(tmp_path,monkeypatch):
    gateway,keys,service,collector=pair(tmp_path)
    lock=threading.Lock();index=0
    def download():
        nonlocal index
        with lock:at=index;index+=1
        if at==0:raise FileGatewayError('FILE_GATEWAY_OUTCOME_UNKNOWN',ambiguous=True)
        value=envelope(collector.binding,'event',{'event_id':'e'+str(at),'peer_id':'peer','source_seq':at,
            'direction':'in','text':'synthetic','occurred_at':NOW},message_id='e'+str(at),peer_id='peer',created_at=NOW,expires_at=NOW+600)
        return DownloadedFile('e'+str(at)+'.bin',collector.codec.pack(value))
    monkeypatch.setattr(service.inbound,'pending',lambda:8)
    monkeypatch.setattr(service.inbound,'download',download)
    with pytest.raises(FileGatewayError):service.poll(limit=8)
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM fg_download WHERE state='UNKNOWN'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM fg_download WHERE state IN ('STORED','STAGED') AND blob IS NOT NULL").fetchone()[0]==7
    monkeypatch.setattr(service.inbound,'download',lambda:pytest.fail('Recover persisted data first'))
    assert len(service.poll(limit=8))==7