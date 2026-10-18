"""The command write transaction must fence a replaced reply-job lease."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from server.negotiation.engine import Engine
from test_production_flow import OWNER, desk
from test_reply_text_repair import reply_case, install, run, replies, unchanged_facts
from test_persistent_retry_http import latest_row


def test_new_worker_after_final_lease_read_fences_old_cached_candidate_at_command_commit(desk, monkeypatch):
    case = reply_case(desk)
    calls, _ = install(case)
    # A supported short stage makes lease expiry occur before the command's
    # 120-second expiry, so command TTL cannot accidentally hide the lease race.
    engine = Engine(case.app.state.inquiry, case.flows, case.app.state.bond_tools, stage_budget_seconds=30)
    entered, release = Event(), Event()
    original = engine.commands.propose
    def pause_before_real_transaction(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Synthetic pre-commit pause was not released'
        return original(*args, **kwargs)
    monkeypatch.setattr(engine.commands, 'propose', pause_before_real_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_worker = pool.submit(run, case, engine)
        try:
            assert entered.wait(3), 'Expected candidate to pass the outer lease read'
            old = latest_row(case.app.state.repository, 'xj2_reply_job')
            case.clock.value = old['lease_until'] + .01
            flows = SimpleNamespace(analyze=case.flows.analyze,
                decide=lambda context: {'schema_version': '2.2', 'action': 'WAIT', 'proposal': None})
            newer = Engine(case.app.state.inquiry, flows, case.app.state.bond_tools, stage_budget_seconds=30)
            new_result = newer.decide_member(OWNER, case.peer['member_id'])
            replacement = latest_row(case.app.state.repository, 'xj2_reply_job')
            assert new_result['state'] == 'WAIT'
            assert replacement['job_id'] == old['job_id'] and replacement['lease_token'] != old['lease_token']
        finally:
            release.set()
        result = stale_worker.result(timeout=4)

    assert len(calls) == 2
    assert result['state'] in ('REJECTED', 'STALE_LEASE'), 'The old worker committed after its lease token was replaced'
    assert not replies(case), 'No durable command may be created by the superseded worker'
    final = latest_row(case.app.state.repository, 'xj2_reply_job')
    assert final['lease_token'] == replacement['lease_token']
    assert final['state'] == replacement['state'] == 'DONE'
    assert final['result_json'] == replacement['result_json']
    case.app.state.dispatcher.run_once()
    assert len(case.native.sent) == 1
    unchanged_facts(case)


@pytest.mark.parametrize('old_action', ['WAIT', 'HANDOFF'])
def test_replaced_lease_before_final_non_send_transaction_cannot_record_or_stop_peer(desk, monkeypatch, old_action):
    case = reply_case(desk)
    entered, release = Event(), Event()
    stage = {'thread': None, 'model_returned': False, 'paused': False}
    def decide(context):
        stage.update(thread=get_ident(), model_returned=True)
        return {'schema_version': '2.2', 'action': old_action, 'proposal': None}
    case.flows.decide = decide
    engine = Engine(case.app.state.inquiry, case.flows, case.app.state.bond_tools, stage_budget_seconds=30)
    real_write = case.app.state.repository.write
    @contextmanager
    def after_model_lease_transaction(*args, **kwargs):
        pause = get_ident() == stage['thread'] and stage['model_returned'] and not stage['paused']
        if pause: stage['paused'] = True
        with real_write(*args, **kwargs) as tx:
            yield tx
        if pause:
            # The real initial lease read has completed and its DB/owner lock
            # is released; do not fake or replace the subsequent write gate.
            entered.set()
            assert release.wait(5), 'Synthetic non-send commit pause was not released'
    monkeypatch.setattr(case.app.state.repository, 'write', after_model_lease_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_worker = pool.submit(run, case, engine)
        try:
            assert entered.wait(3)
            old = latest_row(case.app.state.repository, 'xj2_reply_job')
            with case.app.state.repository.db.transaction(write=False) as tx:
                before = tx.execute('SELECT COUNT(*) AS n FROM xj2_decision WHERE member_id=?',
                                    (case.peer['member_id'],)).fetchone()['n']
            case.clock.value = old['lease_until'] + .01
            flows = SimpleNamespace(analyze=case.flows.analyze,
                decide=lambda context: {'schema_version': '2.2', 'action': 'WAIT', 'proposal': None})
            newer = Engine(case.app.state.inquiry, flows, case.app.state.bond_tools, stage_budget_seconds=30)
            assert newer.decide_member(OWNER, case.peer['member_id'])['state'] == 'WAIT'
            replacement = latest_row(case.app.state.repository, 'xj2_reply_job')
            assert replacement['job_id'] == old['job_id'] and replacement['lease_token'] != old['lease_token']
        finally:
            release.set()
        result = stale_worker.result(timeout=4)
    assert result['state'] in ('REJECTED', 'STALE_LEASE'), 'A replaced worker must not write WAIT/HANDOFF decisions'
    with case.app.state.repository.db.transaction(write=False) as tx:
        count = tx.execute('SELECT COUNT(*) AS n FROM xj2_decision WHERE member_id=?',
                           (case.peer['member_id'],)).fetchone()['n']
    assert count == before + 1, 'Only the replacement worker may record its decision'
    peer = case.app.state.repository.group_rows(OWNER)[0]['peers'][0]
    assert peer['send_allowed'] == 1, 'A stale HANDOFF must not stop the replacement worker peer'
    final = latest_row(case.app.state.repository, 'xj2_reply_job')
    assert final['lease_token'] == replacement['lease_token'] and final['result_json'] == replacement['result_json']
    assert not replies(case)
    unchanged_facts(case)
