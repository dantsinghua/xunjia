"""Developer checks for the real-model recovery evidence collector fixture."""
from scripts import measure_dify_recovery as recovery


def test_recovery_fixture_keeps_raw_receipt_pending_and_native_opening(tmp_path):
    spec = recovery.prepare(tmp_path)
    store, service = recovery.open_service(tmp_path)
    before = recovery.state(store, spec, "analysis")
    assert before["quote"]["revision"] == 0
    assert before["message"]["analysis_state"] == "QUEUED"
    assert before["commands"] == []
    context = store.context(spec["owner"], spec["member_id"])
    assert context[0]["direction"] == "out" and context[0]["native_id"]
    assert "3.20" in context[0]["text"]
    assert spec["message_id"] not in {row["message_id"] for row in context}
    assert before["message"]["ingest_revision"] > before["thread"]["analyzed_revision"]
    assert service.calendar.latest_valuation_day(0) == "2026-10-17"
    reopened, _ = recovery.open_service(tmp_path)
    assert recovery.state(reopened, spec, "analysis") == before


def test_recovery_collector_rejects_reusing_existing_output(tmp_path):
    import pytest
    recovery.prepare(tmp_path)
    with pytest.raises(FileExistsError):
        recovery.prepare(tmp_path)
