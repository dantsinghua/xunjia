"""Old file-carrier incidents are local evidence, not a global stream stop.
No uncertain financial command may be replayed to achieve carrier readiness.
"""
import hashlib

import pytest

from relay.file_gateway import FileGatewayError
from relay.file_gateway_journal import Journal, QueueStop
from relay.protocol import Codec, envelope
from test_file_gateway_transport import setup, event, command, B
from file_gateway_fakes import Gateway, NOW


@pytest.mark.parametrize('state', ['UNKNOWN', 'RECEIVING', 'QUARANTINED'])
def test_service_download_history_does_not_block_new_authenticated_packet(tmp_path, state):
    gateway, service, collector = setup(tmp_path)
    with service.store.db() as db:
        seq = db.execute('INSERT INTO fg_download(scope,state,name,blob,code) VALUES(?,?,?,?,?)',
            (service.store.scope, state, 'historical', None, 'historical-error')).lastrowid
    collector.publish_event(event())
    rows = service.poll()
    assert len(rows) == 1 and rows[0]['envelope']['payload']['event_id'] == event()['event_id']
    with service.store.db() as db:
        old = db.execute('SELECT state,name,blob,code FROM fg_download WHERE seq=?', (seq,)).fetchone()
        assert tuple(old) == (state, 'historical', None, 'historical-error')
    service.ack(rows[0]['source_token'])
    assert service.poll() == []


def test_same_uncertain_upload_is_not_repeated_but_distinct_packet_can_progress(tmp_path):
    gateway, service, collector = setup(tmp_path)
    gateway.lose_upload = True
    with pytest.raises(FileGatewayError):
        collector.publish_event(event())
    before = len(gateway.calls)
    gateway.lose_upload = False
    with pytest.raises(FileGatewayError):
        collector.flush_events()
    assert len(gateway.calls) == before, 'No retransmission of same uncertain ciphertext'
    wire = command(command_id='synthetic-distinct-command')
    assert service.send(wire)['status'] == 'accepted'
    assert len(gateway.queues['down.invalid']) == 1


@pytest.mark.parametrize('state', ['UNKNOWN', 'RECEIVING', 'FOREIGN'])
def test_coordinator_old_receive_incident_does_not_block_new_valid_packet(tmp_path, state):
    gateway = Gateway()
    upstream = gateway.client('history-upstream')
    journal = Journal(tmp_path / 'central.sqlite', upstream)
    journal.configure_directory(Codec(B, clock=lambda: NOW), B)
    with journal.db() as db:
        seq = db.execute('INSERT INTO receive(state,name,body) VALUES(?,?,?)', (state, 'historical', None)).lastrowid
    blob = Codec(B, clock=lambda: NOW).pack(envelope(B, 'event', event(event_id='synthetic-new-event'),
        message_id='synthetic-new-event', peer_id='synthetic-peer', created_at=NOW, expires_at=NOW + 120))
    journal.upload('up', 'synthetic-new-event.bin', blob)
    result = journal.download('up')
    assert result == ('synthetic-new-event.bin', blob)
    with journal.db() as db:
        assert tuple(db.execute('SELECT state,name,body FROM receive WHERE id=?', (seq,)).fetchone()) == (state, 'historical', None)
    assert journal.download('up') is None


def test_coordinator_same_unknown_upload_never_repeats_remote_post(tmp_path):
    gateway = Gateway()
    upstream = gateway.client('unknown-upload')
    journal = Journal(tmp_path / 'central.sqlite', upstream)
    journal.configure_directory(Codec(B, clock=lambda: NOW), B)
    blob = Codec(B, clock=lambda: NOW).pack(envelope(B, 'event', event(event_id='synthetic-unknown-event'),
        message_id='synthetic-unknown-event', peer_id='synthetic-peer', created_at=NOW, expires_at=NOW + 120))
    gateway.lose_upload = True
    with pytest.raises(QueueStop):
        journal.upload('up', 'unknown.bin', blob)
    before = len(gateway.calls)
    gateway.lose_upload = False
    with pytest.raises(QueueStop, match='UPLOAD_UNKNOWN'):
        journal.upload('up', 'unknown.bin', blob)
    assert len(gateway.calls) == before
    with journal.db() as db:
        assert db.execute('SELECT state FROM upload WHERE h=?', (hashlib.sha256(blob).hexdigest(),)).fetchone()[0] == 'UNKNOWN'
