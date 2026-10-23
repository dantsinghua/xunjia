"""Shared current protocol separates native platforms without changing peer identity."""
import hashlib
import json

import pytest

from relay.gateway_paths import CONTRACT, InstitutionKeys, account_paths
from relay.institution_codec import InstitutionCodec
from relay.institution_directory import InstitutionDirectory
from relay.protocol import Codec, ProtocolError, canonical, envelope
from server.institution_transport import InstitutionTransport
from relay.institution_archive import InstitutionArchive
from relay.gateway_paths import CHUNK_BYTES
import base64
from tests.cutover.file_gateway_fakes import AUTH, NOW
from tests.cutover.test_institution_path_contract import Paths


def test_channel_paths_keep_current_qidian_identity_and_separate_wechat():
    q = account_paths('wb', 'same-uid')
    w = account_paths('wb', 'same-uid', channel='wechat')
    assert q == account_paths('wb', 'same-uid', channel='qidian')
    assert len(set(q.values()) | set(w.values())) == 6
    assert all(len(p) == 20 for p in [*q.values(), *w.values()])


def test_wechat_channel_is_signed_and_cannot_cross_into_qidian_codec():
    keys = InstitutionKeys.from_auth(AUTH)
    wx = keys.binding('wb', 'same-uid', channel='wechat')
    qd = keys.binding('wb', 'same-uid')
    frame = envelope(wx, 'event', {'event_id': 'event-wx', 'peer_id': 'wxpeer-Z3JvdXA',
        'channel': 'wechat', 'text': 'synthetic group', 'direction': 'in'},
        message_id='event-wx', created_at=NOW, expires_at=NOW + 60)
    assert frame['channel'] == 'wechat' and wx.account_uid == 'same-uid'
    packed = Codec(wx, clock=lambda: NOW).pack(frame)
    assert Codec(wx, clock=lambda: NOW).unpack(packed)['channel'] == 'wechat'
    with pytest.raises(ProtocolError):
        Codec(qd, clock=lambda: NOW).unpack(packed)
    local = keys.binding('wb', 'same-uid', 'workspace-wx', channel='wechat')
    projected = InstitutionCodec(local, keys.owner_id, clock=lambda: NOW).unpack(packed)
    assert projected['owner_user_id'] == 'workspace-wx' and projected['channel'] == 'wechat'


@pytest.mark.parametrize('channel', ['', 'qq', 'WECHAT', None])
def test_unsupported_platform_is_rejected_before_path_or_binding_creation(channel):
    with pytest.raises(ValueError):
        account_paths('wb', 'uid', channel=channel)


def test_directory_same_native_uid_on_distinct_channels_is_not_a_false_conflict(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH)
    directory = InstitutionDirectory(tmp_path / 'directory.db', keys, clock=lambda: NOW)
    accounts = [{'account_id': channel + '-slot', 'account_uid': 'same-uid', 'channel': channel,
        'online': True, 'paths': account_paths('wb', 'same-uid', channel=channel)}
        for channel in ['qidian', 'wechat']]
    binding = keys.binding('wb', 'directory')
    directory.put(envelope(binding, 'manifest', {'contract': CONTRACT, 'op': 'directory',
        'revision': 1, 'updated_at': NOW, 'accounts': accounts},
        message_id='directory-multi', created_at=NOW, expires_at=NOW + 60))
    assert directory.account('wb', 'same-uid')['available']
    assert directory.account('wb', 'same-uid', channel='wechat')['available']
    wx = keys.binding('wb', 'same-uid', channel='wechat')
    groups = [{'native_id': 'synthetic@chatroom', 'peer_id': 'wxpeer-c3ludGhldGljQGNoYXRyb29t', 'name': 'Synthetic group'}]
    directory.put(envelope(wx, 'contacts', {'contract': CONTRACT, 'op': 'friends', 'revision': 1,
        'updated_at': NOW, 'contacts': [], 'groups': groups}, message_id='friends-wx', created_at=NOW, expires_at=NOW + 60))
    assert directory.contacts('wb', 'same-uid', 'workspace', channel='wechat')['groups'] == groups
    assert directory.contacts('wb', 'same-uid', 'workspace')['contacts'] == []


def test_transport_same_account_platforms_have_separate_durable_scopes(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH); wire = Paths(); scopes = []
    for channel in ['qidian', 'wechat']:
        binding = keys.binding('wb', 'same-uid', channel=channel)
        paths = account_paths('wb', 'same-uid', channel=channel)
        transport = InstitutionTransport(binding, wire.client(paths['downlink_path']),
            wire.client(paths['uplink_path']), tmp_path / 'same-file.db', role='collector',
            institution_owner=keys.owner_id, channel=channel, clock=lambda: NOW)
        scopes.append(transport.store.scope)
    assert len(set(scopes)) == 2


def test_wechat_event_roundtrip_keeps_native_group_and_requires_commit_before_ack(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH); wire = Paths()
    paths = account_paths('wb', 'wxid', channel='wechat')
    local = keys.binding('wb', 'wxid', 'workspace', channel='wechat')
    native = keys.binding('wb', 'wxid', channel='wechat')
    s = InstitutionTransport(local, wire.client(paths['uplink_path']), wire.client(paths['downlink_path']),
        tmp_path / 's.db', role='service', institution_owner=keys.owner_id, channel='wechat', clock=lambda: NOW)
    c = InstitutionTransport(native, wire.client(paths['downlink_path']), wire.client(paths['uplink_path']),
        tmp_path / 'c.db', role='collector', institution_owner=keys.owner_id, channel='wechat', clock=lambda: NOW)
    c.publish_event({'event_id': 'wx-event', 'peer_id': 'wxpeer-c3ludGhldGljQGNoYXRyb29t', 'channel': 'wechat',
        'session': {'kind': 'group', 'native_id': 'synthetic@chatroom'}, 'text': 'synthetic original',
        'direction': 'in', 'occurred_at': NOW, 'ext_msg_id': 'native-wx-event'})
    rows = s.poll(); assert len(rows) == 1
    assert rows[0]['envelope']['payload']['session']['native_id'] == 'synthetic@chatroom'
    with c.store.db() as db: assert db.execute('SELECT acked FROM ip_delivery').fetchone()[0] == 0
    s.ack(rows[0]['source_token']); s.poll(); c.poll()
    with c.store.db() as db: assert db.execute('SELECT acked FROM ip_delivery').fetchone()[0] == 1


def test_pending_image_does_not_block_later_authenticated_media_manifest(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH); wire = Paths()
    paths = account_paths('wb', 'wxid', channel='wechat')
    binding = keys.binding('wb', 'wxid', channel='wechat')
    s = InstitutionTransport(binding, wire.client(paths['uplink_path']), wire.client(paths['downlink_path']),
        tmp_path / 'media-s.db', role='service', institution_owner=keys.owner_id, channel='wechat', clock=lambda: NOW)
    c = InstitutionTransport(binding, wire.client(paths['downlink_path']), wire.client(paths['uplink_path']),
        tmp_path / 'media-c.db', role='collector', institution_owner=keys.owner_id, channel='wechat', clock=lambda: NOW)
    c.publish_event({'event_id': 'image-event', 'peer_id': 'wxpeer-c3ludGhldGlj', 'channel': 'wechat',
        'text': '', 'kind': 'image', 'direction': 'in', 'occurred_at': NOW, 'ext_msg_id': 'native-image'})
    assert len(s.poll()) == 1
    seen = []; s.manifest_handler = lambda frame: seen.append(frame['payload']['op'])
    c.publish_manifest({'contract': CONTRACT, 'op': 'native_media', 'event_id': 'image-event', 'attachments': []})
    assert len(s.poll(allow_pending_intake=True)) == 1
    assert seen == ['native_media']
    with s.store.db() as db:
        assert db.execute("SELECT acked FROM tx_message WHERE kind='event'").fetchone()[0] == 0


def test_large_native_media_requires_authenticated_reference_and_survives_reordered_chunks(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH); binding = keys.binding('wb', 'wxid', channel='wechat')
    archive = InstitutionArchive(tmp_path / 'archive', clock=lambda: NOW)
    raw = b'original-synthetic-image\0' * 140000
    h = hashlib.sha256(raw).hexdigest(); count = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    def frame(payload, name):
        return envelope(binding, 'manifest', {'contract': CONTRACT, **payload}, message_id=name,
                        created_at=NOW, expires_at=NOW + 60)
    for position in reversed(range(count)):
        archive.accept(frame({'op': 'media_chunk', 'sha256': h, 'size': len(raw), 'chunks': count,
            'position': position, 'data': base64.b64encode(raw[position*CHUNK_BYTES:(position+1)*CHUNK_BYTES]).decode()}, 'part-'+str(position)))
    with pytest.raises(LookupError): archive.media('wb', 'wxid', h)
    archive.accept(frame({'op': 'native_media', 'event_id': 'image-event', 'ext_msg_id': 'native-image',
        'peer_id': 'wxpeer-c3ludGhldGlj', 'channel': 'wechat', 'media_state': 'chunked',
        'attachments': [{'name': 'original.png', 'sha256': h, 'size': len(raw)}]}, 'image-reference'))
    reopened = InstitutionArchive(tmp_path / 'archive', clock=lambda: NOW)
    assert reopened.media('wb', 'wxid', h).read_bytes() == raw
    with pytest.raises(LookupError): reopened.media('other-wb', 'wxid', h)


def test_full_pending_image_batch_still_processes_later_media_control(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH); wire = Paths()
    paths = account_paths('wb', 'wxid', channel='wechat'); binding = keys.binding('wb', 'wxid', channel='wechat')
    s = InstitutionTransport(binding, wire.client(paths['uplink_path']), wire.client(paths['downlink_path']),
        tmp_path / 'batch-s.db', role='service', institution_owner=keys.owner_id, channel='wechat', clock=lambda: NOW)
    c = InstitutionTransport(binding, wire.client(paths['downlink_path']), wire.client(paths['uplink_path']),
        tmp_path / 'batch-c.db', role='collector', institution_owner=keys.owner_id, channel='wechat', clock=lambda: NOW)
    for i in range(31):
        c.publish_event({'event_id': 'pending-image-'+str(i), 'peer_id': 'wxpeer-c3ludGhldGlj', 'channel': 'wechat',
            'text': '', 'kind': 'message', 'type': 'image', 'direction': 'in', 'occurred_at': NOW, 'ext_msg_id': 'image-'+str(i)})
    assert len(s.poll(limit=40)) == 31
    seen = []; s.manifest_handler = lambda frame: seen.append(frame['payload']['op'])
    c.publish_manifest({'contract': CONTRACT, 'op': 'native_media', 'event_id': 'pending-image-0', 'attachments': []})
    for _ in range(2): s.poll(limit=30, allow_pending_intake=True)
    assert seen == ['native_media']
    with s.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM tx_message WHERE kind='event' AND acked=0").fetchone()[0] == 31
