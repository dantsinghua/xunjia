"""Only normal business authorization gates control dispatch; synthetic DB/wire."""
import inspect
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.runtime import Dispatcher
from test_dispatch_expiry_offline import RecordedTransport

OWNER = 'synthetic-default-dispatch'
NOW = datetime(2026, 10, 20, 11, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def boot(tmp_path):
    now = [NOW]
    wire = RecordedTransport()
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'default.sqlite'),
        auth_config=AuthConfig(clock=lambda: now[0]), enable_background=False, transport=wire)
    app.state.repository.bind_account(OWNER, 'synthetic-account', 'synthetic-wb')
    return SimpleNamespace(app=app, store=app.state.repository, service=app.state.inquiry,
                           wire=wire, now=now, dispatcher=app.state.dispatcher)


def preview(case):
    return case.service.preview(OWNER, 'synthetic-draft', [{
        'code': 'SYN-DEFAULT', 'peer_ids': ['synthetic-peer'], 'our_side': 'BUY',
        'volume_wan': '1000', 'opening_yield_pct': '3.20',
        'limit_yield_pct': '3.12', 'step_bp': '2'}])


def confirmed(case):
    draft = preview(case)
    result = case.service.confirm(OWNER, draft['batch_id'], draft['version'], 'synthetic-confirm')
    assert len(result['commands']) == 1
    row = case.service.commands_core.get(OWNER, result['commands'][0]['command_id'])
    assert row['state'] == 'QUEUED'
    return row


def test_no_extra_live_dispatch_switch_remains_in_public_constructors():
    assert 'enable_live_transport' not in inspect.signature(create_app).parameters
    assert 'live_enabled' not in inspect.signature(Dispatcher).parameters


@pytest.mark.parametrize('obsolete_env', [None, '0', '1', 'invalid-old-value'])
def test_default_dispatch_submits_confirmed_unexpired_once_without_extra_env_flag(tmp_path, monkeypatch, obsolete_env):
    if obsolete_env is None:
        monkeypatch.delenv('XUNJIA_ENABLE_LIVE_TRANSPORT', raising=False)
    else:
        monkeypatch.setenv('XUNJIA_ENABLE_LIVE_TRANSPORT', obsolete_env)
    case = boot(tmp_path)
    row = confirmed(case)
    case.dispatcher.run_once()
    current = case.service.commands_core.get(OWNER, row['command_id'])
    assert current['state'] == 'PENDING'
    assert [op for op, _ in case.wire.calls] == ['send']
    case.dispatcher.run_once()
    assert [op for op, _ in case.wire.calls] == ['send', 'reconcile']
    assert case.service.commands_core.get(OWNER, row['command_id'])['state'] == 'PENDING'
    peer = case.store.group_rows(OWNER)[0]['peers'][0]
    assert peer['our_quote'] == {}, 'Accepted upload is not native delivery'


def test_preview_without_confirm_creates_no_command_and_sends_nothing(tmp_path):
    case = boot(tmp_path)
    preview(case)
    assert case.dispatcher.run_once() == 0
    assert case.service.commands(OWNER) == [] and case.wire.calls == []


@pytest.mark.parametrize('scope', ['owner', 'group', 'member'])
def test_business_pause_still_fences_already_authorized_command(tmp_path, scope):
    case = boot(tmp_path)
    row = confirmed(case)
    kwargs = {} if scope == 'owner' else {scope + '_id': row[scope + '_id']}
    case.store.set_sending(OWNER, False, **kwargs)
    case.dispatcher.run_once()
    assert case.service.commands_core.get(OWNER, row['command_id'])['state'] == 'CANCELLED'
    assert case.wire.calls == []


def test_expired_confirmed_command_never_submits_when_default_dispatch_enabled(tmp_path):
    case = boot(tmp_path)
    row = confirmed(case)
    case.now[0] = row['expires_at']
    case.dispatcher.run_once()
    current = case.service.commands_core.get(OWNER, row['command_id'])
    assert current['state'] == 'CANCELLED'
    assert json.loads(current['result_json']) == {'reason': 'EXPIRED_BEFORE_DISPATCH'}
    assert case.wire.calls == []


def test_existing_uncertain_command_is_reconciled_not_submitted_again(tmp_path):
    case = boot(tmp_path)
    row = confirmed(case)
    claim = case.service.commands_core.claim(OWNER, row['command_id'])
    case.service.commands_core.receipt(OWNER, row['command_id'], account_uid='synthetic-account',
        peer_uid=claim['peer_uid'], content_hash=row['content_hash'], status='UNKNOWN', evidence='synthetic-lost-native-result')
    case.dispatcher.run_once()
    assert [op for op, _ in case.wire.calls] == ['reconcile']
    assert case.service.commands_core.get(OWNER, row['command_id'])['state'] == 'UNKNOWN'
