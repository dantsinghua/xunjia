"""Synthetic Cookie/device binding; no real credentials or network requests."""
import asyncio

import pytest

from server.quote_credentials import load_for_probe, merged, save_quote, snapshot
from server.quote_probe import probe_quote


@pytest.mark.parametrize('device_field', [{}, {'device_id': ''}, {'device_id': None}])
def test_new_cookie_device_replaces_implicit_saved_device_for_test_and_save(tmp_path, device_field):
    save_quote({'cookie': 'sid=synthetic-old', 'device_id': 'synthetic-old-device'}, tmp_path)
    body = {'cookie': 'sid=synthetic-new; device_id=synthetic-new-device', **device_field}
    values = merged(body, tmp_path)
    assert values['device_id'] == 'synthetic-new-device'
    assert snapshot(tmp_path)['device_id'] == 'synthetic-old-device'  # Test is read-only.
    result = save_quote(body, tmp_path)
    assert result['device_id_set'] is True
    assert snapshot(tmp_path)['device_id'] == 'synthetic-new-device'
    assert load_for_probe(tmp_path)[:2] == (body['cookie'], 'synthetic-new-device')


def test_first_save_can_derive_device_without_separate_field(tmp_path):
    result = save_quote({'cookie': 'sid=synthetic; device_id="synthetic-device"', 'sid': 'synthetic'}, tmp_path)
    assert result['cookie_set'] and result['device_id_set']
    assert snapshot(tmp_path)['device_id'] == 'synthetic-device'
    assert 'synthetic-device' not in repr(result)


def test_cookie_without_device_retains_saved_contract(tmp_path):
    save_quote({'cookie': 'sid=synthetic-old', 'device_id': 'synthetic-saved'}, tmp_path)
    body = {'cookie': 'sid=synthetic-new', 'device_id': ''}
    assert merged(body, tmp_path)['device_id'] == 'synthetic-saved'
    save_quote(body, tmp_path)
    assert snapshot(tmp_path)['device_id'] == 'synthetic-saved'


@pytest.mark.parametrize('cookie,device', [
    ('device_id=synthetic-cookie-device', 'synthetic-other-device'),
    ('device_id=synthetic-a; device_id=synthetic-b', ''),
    ('device_id=synthetic-a; device_id=synthetic-a', ''),
    ('sid=synthetic-a; sid=synthetic-b; device_id=synthetic-device', ''),
    ('device_id=', ''),
    ('device_id=synthetic invalid', ''),
    ('device_id="synthetic-unclosed', ''),
])
def test_ambiguous_cookie_is_rejected_before_any_save(tmp_path, cookie, device):
    save_quote({'account': 'original', 'cookie': 'sid=synthetic-old', 'device_id': 'synthetic-saved'}, tmp_path)
    before = snapshot(tmp_path)
    body = {'account': 'changed', 'cookie': cookie, 'device_id': device}
    with pytest.raises(ValueError) as failure:
        save_quote(body, tmp_path)
    assert snapshot(tmp_path) == before
    assert 'synthetic-' not in str(failure.value)


def test_matching_explicit_device_is_accepted(tmp_path):
    save_quote({'cookie': 'sid=synthetic; device_id=synthetic-device', 'device_id': 'synthetic-device'}, tmp_path)
    assert snapshot(tmp_path)['device_id'] == 'synthetic-device'


def test_loading_older_saved_cookie_derives_device_without_disk_mutation(tmp_path):
    (tmp_path / 'cookie.txt').write_text('sid=synthetic; device_id=synthetic-current')
    (tmp_path / 'device_id.txt').write_text('synthetic-stale')
    assert load_for_probe(tmp_path)[1] == 'synthetic-current'
    assert (tmp_path / 'device_id.txt').read_text() == 'synthetic-stale'


class Socket:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def recv(self): return b'synthetic'


@pytest.mark.parametrize('device,success', [('', True), ('synthetic-device', True), ('synthetic-conflict', False)])
def test_direct_probe_uses_same_cookie_binding_and_hides_values(device, success):
    calls = []
    def authenticate(cookie, effective_device):
        calls.append((cookie, effective_device))
        return {'t': 'synthetic-token'}
    result = asyncio.run(probe_quote(
        creds=lambda: ('sid=synthetic; device_id=synthetic-device', device, ''),
        authenticate=authenticate, connect=lambda *args: Socket(), ws_url=lambda base, token: base,
        ws_base='wss://synthetic.invalid',
    ))
    assert result['ok'] is success
    assert len(calls) == int(success)
    if success:
        assert calls[0][1] == 'synthetic-device'
    assert 'synthetic-' not in result['note']
