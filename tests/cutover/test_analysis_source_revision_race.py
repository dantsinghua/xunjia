"""A frozen analysis cannot revive a source invalidated during model repair."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current
from test_analysis_evidence_repair import repair_case, install, run
from test_persistent_retry_http import latest_row


@pytest.mark.parametrize('kind', ['retract', 'edit'])
def test_native_source_revision_during_repair_cannot_restore_quote_or_coverage(desk, kind):
    case = repair_case(desk, siblings=True)
    entered, release = Event(), Event()
    def repair_hook(context):
        entered.set()
        assert release.wait(5), 'Synthetic repair wait was not released'
    calls, _ = install(case, repair_hook=repair_hook)
    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis = pool.submit(run, case)
        try:
            assert entered.wait(3), 'Expected to pause inside the evidence-repair call'
            claimed = latest_row(case.app.state.repository, 'xj2_analysis')
            text = 'SYN-A 此前报价撤回' if kind == 'retract' else 'SYN-A 我方报价改为3.16%收'
            revision_event = pool.submit(raw, case.app, case.peer['thread_id'], text, case.next_seq,
                kind=kind, direction='out', native_id='synthetic-'+kind+'-event',
                target_event_id=case.anchor['event_id']).result(timeout=3)
            page = case.app.state.repository.raw_page(OWNER, case.peer['thread_id'])
            changed = next(message for message in page['messages'] if message['message_id'] == revision_event)
            assert changed['target_id'] == case.anchor['message_id']
            assert changed['ingest_revision'] > claimed['through_revision']
            assert current(case.app, case.peer).revision == 0
        finally:
            release.set()
        saved = analysis.result(timeout=4)

    assert len(calls) == 2 and saved['facts_committed'], saved
    assert revision_event not in {message['message_id'] for message in calls[-1]['messages']}
    page = case.app.state.repository.raw_page(OWNER, case.peer['thread_id'])
    assert page['thread']['analyzed_revision'] == claimed['through_revision']
    assert page['thread']['ingest_revision'] > page['thread']['analyzed_revision']
    good = current(case.app, case.sibling)
    assert good.revision == 1 and good.price.value == '3.10' and good.volume_wan == '700'
    quote = current(case.app, case.peer)
    assert quote.price is None, 'The old analysis revived a native source invalidated after its frozen watermark'
    group = next(group for group in case.app.state.repository.group_rows(OWNER) if group['bond_code'] == 'SYN-A')
    assert group['summary']['final_confirmed_volume_wan'] == '0'
    assert not group['summary']['whole_lots']
    assert any(entry['index'] == 0 for entry in saved['rejected'])
    assert saved['repair']['succeeded'] is False
    assert len(desk[3].sent) == 1
