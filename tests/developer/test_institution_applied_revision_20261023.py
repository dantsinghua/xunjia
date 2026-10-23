"""Offline loaded-revision status: a living worker is not proof of a reload."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from server.institution_runtime import InstitutionRuntime
from tests.institution_test_support import OWNER, selection


class ThreadState:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.alive = False


def test_live_thread_cannot_claim_changed_disk_revision_and_same_settings_save_reloads(selection):
    s = selection
    runtime = s['runtime']
    runtime._thread = ThreadState()
    original_revision = runtime.applied_revision
    assert runtime.settings_status(OWNER)['applied'] is True
    # Persist equivalent settings with different bytes: the old worker has not loaded them.
    runtime.path.write_text(json.dumps(json.loads(runtime.path.read_text()), indent=4) + '\n')
    revision = hashlib.sha256(runtime.path.read_bytes()).hexdigest()
    assert revision != original_revision
    status = runtime.settings_status(OWNER)
    assert status['revision'] == revision and status['applied'] is False
    assert runtime.applied_revision == original_revision
    calls = list(s['wire'].calls)
    result = runtime.settings_save(OWNER, {'base_url': runtime.config['base_url'],
        'config_yaml': '', 'expected_revision': revision})
    assert result['saved'] is True and result['applied'] is True
    assert result['revision'] == runtime.applied_revision == revision
    assert s['wire'].calls == calls
    rendered = json.dumps(result)
    assert runtime.auth.key not in rendered and runtime.auth.sm4_key.decode() not in rendered


def test_failed_load_keeps_applied_false_even_if_disk_is_restored_to_loaded_revision(selection):
    runtime = selection['runtime']
    runtime._thread = ThreadState()
    original_bytes = runtime.path.read_bytes()
    original_revision = runtime.applied_revision
    runtime.path.write_text('invalid JSON SYNTHETIC_PRIVATE_SENTINEL')
    failed = runtime._apply_saved_settings(OWNER)
    assert failed['saved'] is True and failed['applied'] is False
    assert runtime.applied_revision == original_revision
    assert runtime.errors['configuration'] == 'INSTITUTION_CONFIG_LOAD_FAILED'
    runtime.path.write_bytes(original_bytes)
    status = runtime.settings_status(OWNER)
    assert status['revision'] == runtime.applied_revision == original_revision
    assert status['applied'] is False
    assert 'SYNTHETIC_PRIVATE_SENTINEL' not in json.dumps(failed)
    restored = runtime._apply_saved_settings(OWNER)
    assert restored['applied'] is True and 'configuration' not in runtime.errors


@pytest.mark.parametrize('alive', [False, True])
def test_unconfigured_runtime_never_reports_applied(tmp_path, alive):
    runtime = InstitutionRuntime(SimpleNamespace(binding_for=lambda owner: None), None,
        tmp_path / 'missing-config.json', tmp_path / 'state')
    runtime._thread = ThreadState(alive)
    status = runtime.settings_status('synthetic-owner')
    assert status['configured'] is False and status['applied'] is False
    assert status['revision'] == '' and runtime.applied_revision == ''
    assert status['credentials_set'] is False


def test_matching_loaded_revision_still_requires_a_living_thread(selection):
    runtime = selection['runtime']
    runtime._thread = ThreadState(False)
    status = runtime.settings_status(OWNER)
    assert status['revision'] == runtime.applied_revision
    assert status['applied'] is False
