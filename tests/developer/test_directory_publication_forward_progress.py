"""Developer regression: current full snapshots cannot be hidden behind old UNKNOWN."""
import pytest

from relay.protocol import Binding, Codec, ProtocolError, result
from relay.file_gateway import FileGatewayError
from tests.mq_fixtures import MQWire

B = Binding('snapshot-owner', 'snapshot-account', 'snapshot-wb', b'k' * 32, b's' * 32)


def document(kind, stamp):
    return {'account_uid': B.account_uid, 'updated_at': stamp,
            **({'contacts': [{'uin': 'real-shape-fixture', 'name': 'Synthetic'}]} if kind == 'contacts'
               else {'accounts': [{'account_uid': B.account_uid}], 'capabilities': ['negotiation-v22-watermark']})}


def transport(tmp_path, *, ttl=604800):
    wire = MQWire()
    now = [100.0]
    channel = wire.transport(B, tmp_path / 'collector.db', role='collector',
                             clock=lambda: now[0], envelope_ttl=ttl)
    return wire, now, channel


def rows(channel, table):
    with channel.store.db() as db:
        return [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


@pytest.mark.parametrize('kind', ['catalog', 'contacts'])
def test_new_full_snapshot_crosses_wire_without_mutating_or_resending_old_unknown(tmp_path, kind):
    wire, now, channel = transport(tmp_path)
    wire.lose_upload = True
    with pytest.raises(FileGatewayError):
        getattr(channel, 'publish_' + kind)(document(kind, now[0]))
    old_packet, old_publication, old_upload = rows(channel, 'tx_packet'), rows(channel, 'tx_publication'), rows(channel, 'fg_upload')
    wire.lose_upload = False
    now[0] += 60
    latest = document(kind, now[0])
    identity = getattr(channel, 'publish_' + kind)(latest)
    assert len(wire.uploaded) == 2
    assert Codec(B, clock=lambda: now[0]).unpack(wire.uploaded[-1][2])['payload'] == latest
    assert rows(channel, 'tx_packet')[0] == old_packet[0]
    assert rows(channel, 'tx_publication')[0] == old_publication[0]
    assert rows(channel, 'fg_upload')[0] == old_upload[0]
    assert rows(channel, 'tx_publication')[-1][1] == identity
    assert rows(channel, 'tx_publication')[-1][-1] == 1
    calls = len(wire.calls)
    with pytest.raises(FileGatewayError, match='FILE_UPLOAD_OUTCOME_UNKNOWN'):
        channel.flush_publications()
    assert len(wire.calls) == calls


def test_same_snapshot_failure_is_durable_and_cannot_be_retried_with_new_packet(tmp_path):
    wire, now, channel = transport(tmp_path)
    payload = document('contacts', now[0])
    wire.lose_upload = True
    with pytest.raises(FileGatewayError):
        channel.publish_contacts(payload)
    packet, publication, upload = rows(channel, 'tx_packet'), rows(channel, 'tx_publication'), rows(channel, 'fg_upload')
    wire.lose_upload = False
    now[0] += 1
    calls = len(wire.calls)
    with pytest.raises(FileGatewayError, match='FILE_UPLOAD_OUTCOME_UNKNOWN'):
        channel.publish_contacts(payload)
    assert len(wire.calls) == calls
    assert rows(channel, 'tx_packet') == packet
    assert rows(channel, 'tx_publication') == publication
    assert rows(channel, 'fg_upload') == upload


def test_expired_same_snapshot_is_never_resigned_or_republished_as_new(tmp_path):
    wire, now, channel = transport(tmp_path, ttl=10)
    payload = document('contacts', now[0])
    channel.publish_contacts(payload)
    before = [rows(channel, table) for table in ('tx_packet', 'tx_publication', 'fg_upload')]
    now[0] += 11
    calls = len(wire.calls)
    with pytest.raises(ProtocolError):
        channel.publish_contacts(payload)
    assert len(wire.calls) == calls
    assert [rows(channel, table) for table in ('tx_packet', 'tx_publication', 'fg_upload')] == before


def test_new_snapshot_own_failure_stays_unuploaded_and_unknown(tmp_path):
    wire, now, channel = transport(tmp_path)
    wire.lose_upload = True
    for stamp in (100.0, 160.0):
        now[0] = stamp
        with pytest.raises(FileGatewayError):
            channel.publish_contacts(document('contacts', stamp))
    assert len(wire.uploaded) == 2
    assert len(rows(channel, 'fg_upload')) == 2
    assert all(row[2] == 'UNKNOWN' for row in rows(channel, 'fg_upload'))
    assert all(row[-1] == 0 for row in rows(channel, 'tx_publication'))


def test_receipt_fifo_unknown_guard_remains_unchanged(tmp_path):
    wire, now, channel = transport(tmp_path)
    wire.lose_upload = True
    with pytest.raises(FileGatewayError):
        channel.publish_contacts(document('contacts', now[0]))
    wire.lose_upload = False
    command = dict(command_id='receipt-command', owner_user_id=B.owner_user_id,
                   account_uid=B.account_uid, workbench_id=B.workbench_id, peer_id='peer',
                   content_hash='a' * 64, control_version=1, expires_at=200,
                   text='Synthetic test only', transport='oss')
    calls = len(wire.calls)
    with pytest.raises(FileGatewayError, match='FILE_UPLOAD_OUTCOME_UNKNOWN'):
        channel.publish_receipt(command, result(command, 'sent', 'synthetic-native-proof'))
    assert len(wire.calls) == calls
    assert rows(channel, 'tx_publication')[-1][-1] == 0
    assert rows(channel, 'fg_upload')[0][2] == 'UNKNOWN'
