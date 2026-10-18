"""Async reply credentials are scoped and precede idempotent recognition."""
import pytest

from server.negotiation.prices import InvalidFact, StaleDecision
from server.negotiation.store import encoded
from test_v22_reply_text_repair import OWNER, desk, response


def candidate(tmp_path):
    store, engine, _, member = desk(tmp_path)
    basis = engine.commands.basis(OWNER, member, depends_on_group_volume=True)
    with store.write(OWNER) as tx:
        context = engine._reply_context(tx, OWNER, member, basis)
        tx.execute("INSERT INTO xj2_reply_job(job_id,member_id,request_id,basis_json,state,lease_token,lease_until,attempts,result_json,created_at) VALUES(?,?,?,?,'RUNNING',?,?,1,'{}',?)",
            ('synthetic-job', member, 'synthetic-request', encoded(basis), 'synthetic-token',
             store.clock() + 60, store.clock()))
    kwargs = dict(basis=basis, request_id='synthetic-request', proposal=response()['proposal'],
        validator=lambda proposal: engine._validate_reply(context, proposal), expires_at=store.clock()+120,
        reply_job_id='synthetic-job', reply_lease_token='synthetic-token')
    return store, engine.commands, member, kwargs


@pytest.mark.parametrize('field,value', [
    ('member_id', 'different-member'), ('request_id', 'different-request'),
    ('lease_token', 'replacement-token'), ('state', 'DONE'), ('lease_until', 0),
])
def test_async_command_requires_matching_live_job_member_request_and_token(tmp_path, field, value):
    store, commands, member, kwargs = candidate(tmp_path)
    with store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_reply_job SET ' + field + '=? WHERE job_id=?', (value, 'synthetic-job'))
    with pytest.raises(StaleDecision):
        commands.propose(OWNER, member, **kwargs)
    with store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 0
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_decision').fetchone()['n'] == 0


@pytest.mark.parametrize('missing', ['reply_job_id', 'reply_lease_token'])
def test_partial_async_reply_fence_is_not_treated_as_synchronous_permission(tmp_path, missing):
    store, commands, member, kwargs = candidate(tmp_path)
    kwargs.pop(missing)
    with pytest.raises(InvalidFact):
        commands.propose(OWNER, member, **kwargs)
    with store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 0


def test_replaced_async_job_cannot_recognize_even_an_existing_idempotent_command(tmp_path):
    store, commands, member, kwargs = candidate(tmp_path)
    first = commands.propose(OWNER, member, **kwargs)
    replay = commands.propose(OWNER, member, **kwargs)
    assert first['state'] == replay['state'] == 'QUEUED'
    assert replay['replayed'] and replay['command_id'] == first['command_id']
    with store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_reply_job SET lease_token=? WHERE job_id=?', ('new-token', 'synthetic-job'))
    with pytest.raises(StaleDecision):
        commands.propose(OWNER, member, **kwargs)
    with store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 1
