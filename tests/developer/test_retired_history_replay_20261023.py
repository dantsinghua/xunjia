"""Offline Inbox regressions for immutable native history across workspace retirement."""
import json

import pytest

from relay.protocol import Binding, Codec, envelope
from server.fresh_state import clear_workspace
from server.negotiation.application_store import ApplicationStore
from server.negotiation.prices import InvalidFact, LimitPolicy
from server.negotiation.runtime import Inbox
from server.negotiation.service import Inquiry


NOW = 1792641600.0
OWNER = 'synthetic-history-owner'


def fixture(tmp_path, *, kind='message', direction='in', retired=True, original_historical=False):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'replay.db'), initialize=True, clock=lambda: NOW)
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
    store.save_preferences(OWNER, {'context_id': 'old-context', 'context_started_at': NOW - 100})
    group = store.create_group(OWNER, bond_code='SYN-HISTORY', our_side='BUY', target_volume='1000',
        trade_day='2026-10-22', policy=LimitPolicy('ABSOLUTE', '3.12'))
    member, thread = store.add_member(OWNER, group, account_uid='synthetic-account', peer_uid='synthetic-peer')
    binding = Binding(OWNER, 'synthetic-account', 'synthetic-workbench', b'k' * 32, b's' * 32)
    codec = Codec(binding, clock=lambda: NOW)
    inbox = Inbox(Inquiry(store), None)
    if kind != 'message':
        store.ingest(OWNER, thread, event_id='native-target', source_seq=1, text='original target',
            trade_day='2026-10-22', occurred_at=NOW - 5)
        store.ingest(OWNER, thread, event_id='other-target', source_seq=2, text='another target',
            trade_day='2026-10-22', occurred_at=NOW - 4)
    payload = {'event_id': 'native-replayed', 'source_seq': 9, 'text': 'immutable native text',
        'direction': direction, 'event_kind': kind, 'ext_msg_id': 'exact-native-id',
        'peer_id': 'synthetic-peer', 'occurred_at': NOW - 3, 'trade_day': '2026-10-22',
        'historical': original_historical}
    if kind != 'message':
        payload['target_event_id'] = 'native-target'

    def apply(value):
        # Exercise actual envelope authentication and the production Inbox path.
        data = envelope(binding, 'event', value, message_id=value['event_id'], peer_id='synthetic-peer',
            created_at=NOW, expires_at=NOW + 120)
        return inbox.apply(binding, codec.unpack(codec.pack(data)))

    original_id = apply(payload)
    if retired:
        with store.write(OWNER) as tx:
            clear_workspace(tx, OWNER, clock=store.clock)
            tx.execute('UPDATE xj2_binding SET version=version+1,config_json=? WHERE owner_id=?',
                (json.dumps({'context_id': 'new-context', 'context_started_at': NOW - 1}), OWNER))
    with store.db.transaction(write=False) as tx:
        original = dict(tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (original_id,)).fetchone())
        original_thread = dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread,)).fetchone())
    return store, apply, payload, original, original_thread, thread


@pytest.mark.parametrize('kind', ['message', 'edit', 'retract'])
@pytest.mark.parametrize('direction', ['in', 'out'])
@pytest.mark.parametrize('original_historical', [False, True])
def test_retired_historical_native_replay_preserves_original_bytes(tmp_path, kind, direction, original_historical):
    store, apply, payload, original, original_thread, thread = fixture(tmp_path,
        kind=kind, direction=direction, original_historical=original_historical)
    replay = {**payload, 'source_seq': 109, 'historical': True}
    assert apply(replay) == original['message_id']
    with store.db.transaction(write=False) as tx:
        assert dict(tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (original['message_id'],)).fetchone()) == original
        assert dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread,)).fetchone()) == original_thread
    assert store.snapshot(OWNER)['groups'] == [] and store.snapshot(OWNER)['threads'] == []
    assert store.claim_analysis(OWNER, thread, force=True) is None


@pytest.mark.parametrize('field,value', [
    ('text', 'different native body'), ('direction', 'out'), ('ext_msg_id', 'another-native-id'),
    ('occurred_at', NOW - 2), ('trade_day', '2026-10-23'),
    ('target_event_id', 'other-target'), ('event_kind', 'retract'),
])
def test_retired_replay_still_rejects_any_changed_immutable_field(tmp_path, field, value):
    store, apply, payload, original, _, thread = fixture(tmp_path, kind='edit')
    replay = {**payload, 'source_seq': 109, 'historical': True, field: value}
    with pytest.raises(InvalidFact, match='different native content'):
        apply(replay)
    with store.db.transaction(write=False) as tx:
        assert dict(tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (original['message_id'],)).fetchone()) == original


@pytest.mark.parametrize('retired,historical', [(False, True), (True, False)])
def test_metadata_mismatch_needs_both_retirement_cut_and_historical_input(tmp_path, retired, historical):
    _, apply, payload, _, _, _ = fixture(tmp_path, retired=retired)
    with pytest.raises(InvalidFact, match='different native content'):
        apply({**payload, 'source_seq': 109, 'historical': historical})


def test_retired_replay_without_native_timestamp_cannot_assert_immutable_match(tmp_path):
    store, _, payload, _, _, thread = fixture(tmp_path)
    store.clock = lambda: NOW - 3  # A guessed receive clock must not supply missing native time.
    with pytest.raises(InvalidFact, match='different native content'):
        store.ingest(OWNER, thread, event_id=payload['event_id'], source_seq=109,
            text=payload['text'], trade_day=payload['trade_day'], historical=True,
            direction=payload['direction'], native_id=payload['ext_msg_id'])


@pytest.mark.parametrize('direction', ['in', 'out'])
def test_retired_replay_with_only_historical_flag_changed_is_idempotent(tmp_path, direction):
    store, apply, payload, original, original_thread, thread = fixture(tmp_path, direction=direction)
    assert apply({**payload, 'historical': True}) == original['message_id']
    with store.db.transaction(write=False) as tx:
        assert dict(tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (original['message_id'],)).fetchone()) == original
        assert dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread,)).fetchone()) == original_thread
