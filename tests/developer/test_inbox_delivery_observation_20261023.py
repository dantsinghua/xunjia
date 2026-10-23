"""Authenticated nonterminal delivery observations never replace native facts."""
from dataclasses import replace
import json

import pytest

from relay.protocol import Binding, Codec, envelope
from server.negotiation.application_store import ApplicationStore
from server.negotiation.prices import InvalidFact
from server.negotiation.runtime import Inbox
from server.negotiation.schema import TABLES
from server.negotiation.service import Inquiry


NOW = 1800000000.0
OWNER = 'synthetic-observation-owner'
B = Binding(OWNER, 'synthetic-account', 'synthetic-workbench', b'k' * 32, b's' * 32)


def items():
    return [{'code': 'SYN-OBSERVATION', 'our_side': 'BUY', 'peer_ids': ['synthetic-peer'],
        'volume_wan': '1000', 'opening_yield_pct': '3.20', 'limit_yield_pct': '3.12',
        'step_bp': '2', 'pricing_basis': 'absolute'}]


def fixture(tmp_path, state):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'observations.db'), initialize=True, clock=lambda: NOW)
    store.bind_account(OWNER, B.account_uid, B.workbench_id)
    service = Inquiry(store)
    first = service.send_direct(OWNER, 'synthetic-original-request', items())
    cid = first['commands'][0]['command_id']
    assert service.commands_core.claim(OWNER, cid)['state'] == 'PENDING'
    command = service.commands_core.get(OWNER, cid)
    if state != 'PENDING':
        service.commands_core.receipt(OWNER, cid, account_uid=B.account_uid, peer_uid='synthetic-peer',
            content_hash=command['content_hash'], status=state, evidence='original exact native evidence',
            native_id='original-native-id' if state == 'SENT' else None, occurred_at=NOW)
    return store, service, Inbox(service, None), service.commands_core.get(OWNER, cid)


def snapshot(store):
    with store.db.transaction(write=False) as tx:
        # Include commands, native messages/quotes, audit, owner control and thread revisions.
        return {table: sorted(json.dumps(dict(row), sort_keys=True) for row in tx.execute('SELECT * FROM ' + table).fetchall())
            for table in TABLES}


def authenticated_receipt(command, status, *, binding=B, **changes):
    payload = {'command_id': command['command_id'], 'account_uid': binding.account_uid,
        'peer_id': 'synthetic-peer', 'content_hash': command['content_hash'], 'status': status,
        'evidence': 'awaiting-command-delivery', 'native_id': 'must-not-be-recorded',
        'executed_at': NOW + 1, **changes}
    data = envelope(binding, 'receipt', payload, message_id='synthetic-observation',
        command_id=payload['command_id'], peer_id=payload['peer_id'], content_hash=payload['content_hash'],
        control_version=1, created_at=NOW, expires_at=NOW + 120)
    codec = Codec(binding, clock=lambda: NOW)
    return codec.unpack(codec.pack(data))


@pytest.mark.parametrize('state', ['PENDING', 'UNKNOWN', 'SENT'])
@pytest.mark.parametrize('observation', ['pending', 'accepted'])
def test_nonterminal_observation_is_pure_noop_for_all_existing_native_states(tmp_path, state, observation):
    store, _, inbox, command = fixture(tmp_path, state)
    before = snapshot(store)
    assert inbox.apply(B, authenticated_receipt(command, observation)) is None
    assert snapshot(store) == before


@pytest.mark.parametrize('observation', ['pending', 'accepted'])
@pytest.mark.parametrize('identity', ['account', 'workbench', 'owner', 'peer', 'content', 'command'])
def test_nonterminal_observation_still_requires_full_existing_command_identity(tmp_path, observation, identity):
    store, service, inbox, command = fixture(tmp_path, 'UNKNOWN')
    binding = B
    changes = {}
    if identity == 'account':
        binding = replace(B, account_uid='foreign-account')
    elif identity == 'workbench':
        binding = replace(B, workbench_id='foreign-workbench')
    elif identity == 'owner':
        binding = replace(B, owner_user_id='foreign-owner')
    elif identity == 'peer':
        changes['peer_id'] = 'foreign-peer'
    elif identity == 'content':
        changes['content_hash'] = 'b' * 64
    else:
        store.bind_account('foreign-owner', 'foreign-account', 'foreign-workbench')
        foreign = service.send_direct('foreign-owner', 'foreign-request', items())
        changes['command_id'] = foreign['commands'][0]['command_id']
    before = snapshot(store)
    with pytest.raises((InvalidFact, PermissionError)):
        inbox.apply(binding, authenticated_receipt(command, observation, binding=binding, **changes))
    assert snapshot(store) == before
