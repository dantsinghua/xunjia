"""Isolated V2 RFQ admission: observation metadata is not financial authority.

No transport or dispatcher is created. Confirmation only queues synthetic commands
inside the temporary SQLite database.
"""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from server.negotiation.application_store import ApplicationStore
from server.negotiation.prices import InvalidFact
from server.negotiation.service import Inquiry

OWNER = 'synthetic-preview-owner'


@pytest.fixture
def proposal(tmp_path):
    now = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'rfq-isolated.db'), initialize=True, clock=lambda: now)
    store.bind_account(OWNER, 'synthetic-account-a', 'synthetic-wb-a')
    prefs = {
        'floors': {'SYN-A': {'kind': 'yield', 'value': '2.4'}, 'SYN-B': {'kind': 'bp', 'value': '5'}},
        'strategy': {'template_id': 'synthetic-policy', 'step_bp': '2', 'strict': True},
        'collector_directory_checks': {'oss': {
            'workbench_id': 'synthetic-wb-a', 'account_uid': 'synthetic-account-a', 'version': 1,
            'stale': True, 'updated_at': now - 400, 'publication_id': 'old-book', 'publication_hash': 'a' * 64}},
    }
    store.save_preferences(OWNER, prefs)
    service = Inquiry(store)
    items = [{'code': code, 'name': code, 'peer_ids': ['synthetic-peer'], 'our_side': 'BUY',
        'volume_wan': '1000', 'opening_yield_pct': '2.5', 'limit_yield_pct': '2.4', 'step_bp': '2',
        'pricing_basis': 'absolute'} for code in ['SYN-A', 'SYN-B']]
    preview = service.preview(OWNER, 'synthetic-preview', items)
    assert service.commands(OWNER) == []
    return store, service, prefs, preview


def confirm(case):
    _, service, _, preview = case
    return service.confirm(OWNER, preview['batch_id'], preview['version'], 'synthetic-confirm')


@pytest.mark.parametrize('field,value', [
    ('stale', False), ('updated_at', 1792460000), ('publication_id', 'new-signed-book'),
    ('publication_hash', 'b' * 64),
])
def test_authenticated_directory_observation_changes_do_not_revoke_financial_preview(proposal, field, value):
    store, service, prefs, _ = proposal
    changed = deepcopy(prefs['collector_directory_checks'])
    changed['oss'][field] = value
    store.save_preferences(OWNER, {'collector_directory_checks': changed})
    result = confirm(proposal)
    assert len(result['group_ids']) == 2
    assert len(result['commands']) == 1
    assert result['commands'][0]['status'] == 'queued'
    assert len(service.commands(OWNER)) == 1
    assert {g['bond_code'] for g in store.group_rows(OWNER)} == {'SYN-A', 'SYN-B'}


def test_first_successful_book_observation_after_preview_does_not_require_new_pricing_consent(proposal):
    store, service, _, _ = proposal
    with store.write(OWNER) as tx:
        tx.execute("UPDATE xj2_binding SET config_json=json_remove(config_json,'$.collector_directory_checks') WHERE owner_id=?", (OWNER,))
    preview = service.preview(OWNER, 'preview-before-first-book-read', [{
        'code': 'SYN-C', 'peer_ids': ['synthetic-peer'], 'our_side': 'BUY', 'volume_wan': '1000',
        'opening_yield_pct': '2.5', 'limit_yield_pct': '2.4', 'step_bp': '2'}])
    store.save_preferences(OWNER, {'collector_directory_checks': {'oss': {
        'workbench_id': 'synthetic-wb-a', 'account_uid': 'synthetic-account-a', 'version': 1,
        'stale': False, 'updated_at': store.clock(), 'publication_id': 'first-real-book', 'publication_hash': 'c' * 64}}})
    result = service.confirm(OWNER, preview['batch_id'], preview['version'], 'confirm-after-first-book')
    assert len(result['commands']) == 1 and result['commands'][0]['status'] == 'queued'


@pytest.mark.parametrize('change', [
    {'floors': {'SYN-A': {'kind': 'yield', 'value': '2.45'}}},
    {'floors': {'SYN-A': {'kind': 'bp', 'value': '9'}}},
    {'strategy': {'template_id': 'changed-policy', 'step_bp': '3', 'strict': True}},
    {'strict_match': False},
])
def test_changed_pricing_authority_rejects_confirmation_and_leaves_zero_commands(proposal, change):
    store, service, _, _ = proposal
    store.save_preferences(OWNER, change)
    with pytest.raises(InvalidFact, match='pricing settings changed'):
        confirm(proposal)
    assert service.commands(OWNER) == []
    assert store.group_rows(OWNER) == []


@pytest.mark.parametrize('field,value', [('version', 2), ('account_uid', 'synthetic-account-b'),
                                         ('workbench_id', 'synthetic-wb-b')])
def test_changed_binding_identity_or_version_never_inherits_old_preview(proposal, field, value):
    store, service, _, _ = proposal
    with store.write(OWNER) as tx:
        tx.execute(f'UPDATE xj2_binding SET {field}=?,version=version+1 WHERE owner_id=?' if field != 'version'
            else 'UPDATE xj2_binding SET version=? WHERE owner_id=?', (value, OWNER))
    with pytest.raises(InvalidFact, match='^Stored request belongs to a retired account context; submit a new request id$'):
        confirm(proposal)
    assert service.commands(OWNER) == []
    assert store.group_rows(OWNER) == []
