"""One source read per recovery tick, and no projection churn on unchanged prices."""
import json
import pytest
from server.negotiation.prices import LimitPolicy, Valuation
from server.reference_data import ReferenceSnapshot
from test_review_valuation_recovery import setup, OWNER

def multi_bond_setup(tmp_path, count):
    app, ref, now, first, member, thread = setup(tmp_path)
    codes = ["SYN-A"] + ["SYN-" + str(i) for i in range(1, count)]
    groups = [first]
    for code in codes[1:]:
        groups.append(app.state.repository.create_group(OWNER, bond_code=code, our_side="BUY",
            target_volume="1000", trade_day=ref.day, policy=LimitPolicy("CHINABOND_BP", "5"),
            valuation=Valuation(code, ref.day, ref.value, "initial"), expected_valuation_date=ref.day))
    ref.calendar_reads = 0
    def calendar(_):
        ref.calendar_reads += 1
        return ref.day
    def snapshot():
        ref.reads += 1
        if not ref.connected:
            raise ConnectionError("synthetic valuation-table outage")
        return ReferenceSnapshot("tick-" + str(ref.reads), ref.day, bonds=[
            {"code": code, "valuation_date": ref.day, "valuation_yield_pct": ref.value} for code in codes])
    ref.snapshot = snapshot
    ref.latest_valuation_day = calendar
    return app, ref, now, groups

@pytest.mark.parametrize("count", [1, 3, 12])
@pytest.mark.parametrize("connected", [True, False])
def test_one_reference_snapshot_per_tick_without_calendar_dependency(tmp_path, count, connected):
    app, ref, now, groups = multi_bond_setup(tmp_path, count)
    now[0] += 120
    ref.day = "2026-10-17"
    ref.connected = connected
    ref.value = "2.60"
    result = app.state.rollover_once()
    assert result["rolled"] == count
    assert len(result["pending"]) == (0 if connected else count)
    assert ref.reads == 1, "A source outage must not cause one network timeout per bond"
    assert ref.calendar_reads == 0, "发布日历不再参与BP估算或阻塞恢复"
    for gid in groups:
        group = app.state.repository.group(OWNER, gid)
        assert group["trade_day"] == ref.day
        assert json.loads(group["boundary_json"]) == ({"kind": "YIELD", "value": "2.65"} if connected else None)

@pytest.mark.parametrize("entry", ["tick", "store"])
def test_unchanged_valuation_does_not_advance_projection(tmp_path, entry):
    app, ref, now, gid, member, thread = setup(tmp_path)
    store = app.state.repository
    before = store.snapshot(OWNER)
    for _ in range(3):
        if entry == "tick":
            assert app.state.rollover_once()["revalued"] == 0
        else:
            assert not store.refresh_valuation(OWNER, gid,
                valuation=Valuation("SYN-A", ref.day, ref.value, "same-economics"), expected_valuation_date=ref.day)
    assert store.snapshot(OWNER) == before

def test_source_cache_failure_does_not_prevent_cross_day_invalidation(tmp_path):
    app, ref, now, groups = multi_bond_setup(tmp_path, 3)
    now[0] += 120
    ref.day = "2026-10-17"
    def invalidate():
        raise ConnectionError("synthetic cache reset failure")
    ref.invalidate_valuation_cache = invalidate
    result = app.state.rollover_once()
    assert result["rolled"] == 3 and result["pending"] == []
    for gid in groups:
        group = app.state.repository.group(OWNER, gid)
        assert group["trade_day"] == ref.day and json.loads(group["boundary_json"]) == {"kind":"YIELD","value":"2.63"}