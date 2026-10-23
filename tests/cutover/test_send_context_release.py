"""Real account-generation admission with only synthetic local institution data."""
import pytest

from tests.institution_test_support import selection
from tests.cutover.test_direct_rfq_send import item, counts


@pytest.mark.parametrize('header', ['', 'synthetic-retired-context'])
@pytest.mark.parametrize('mode', ['direct', 'preview-confirm'])
def test_old_client_is_definitely_rejected_and_current_client_is_idempotent(selection, header, mode):
    s = selection
    api, app = s['api'], s['app']
    current = api.headers['X-Xunjia-Context']
    if mode == 'direct':
        path = '/api/im/rfq/send-direct'
        body = {'request_id': 'synthetic-context-release-direct', 'items': [item()]}
    else:
        preview = api.post('/api/im/rfq/preview', json={'request_id': 'synthetic-preview', 'items': [item()]})
        assert preview.status_code == 200, preview.text
        batch = preview.json()['data']
        path = '/api/im/rfq/send'
        body = {'batch_id': batch['batch_id'], 'expected_version': batch['version'], 'request_id': 'synthetic-confirm'}
    before = counts(app)
    rejected = api.post(path, json=body, headers={'X-Xunjia-Context': header})
    assert rejected.status_code == 400
    assert rejected.json()['code'] == 'ACCOUNT_CONTEXT_CHANGED'
    assert rejected.json()['submitted'] is False
    assert counts(app) == before
    accepted = api.post(path, json=body, headers={'X-Xunjia-Context': current})
    assert accepted.status_code == 200, accepted.text
    replay = api.post(path, json=body, headers={'X-Xunjia-Context': current})
    assert replay.status_code == 200, replay.text
    assert replay.json()['data'] == accepted.json()['data']
    assert counts(app)['xj2_command'] == 1
