"""独立聚合成本验收：仅合成时序与本地 SQLite，不调用模型或发送。

依据事前固化的 measured-service-synthetic-arrival-grid-v1 规格。
本文件在 r20 的 47 例量测期间仅编写，须待主负责人放行后执行。
"""
from __future__ import annotations

from importlib import util
import math
from pathlib import Path
import sys

import pytest

from server.negotiation.settings import Aggregation
from server.negotiation.engine import Engine
from server.negotiation.store import Store


@pytest.fixture(autouse=True)
def no_model_or_dispatcher(monkeypatch):
    from server.negotiation.chatflow import ChatflowClient
    from server.negotiation.runtime import Dispatcher

    def forbidden(*args, **kwargs):
        raise AssertionError("聚合基准不得创建真实模型客户端或发送调度器")

    monkeypatch.setattr(ChatflowClient, "__init__", forbidden)
    monkeypatch.setattr(Dispatcher, "__init__", forbidden)


@pytest.fixture
def subject():
    path = Path(__file__).with_name("aggregation_cost.py")
    assert path.is_file(), "聚合成本实现尚未交回"
    name = "xunjia_independent_aggregation_cost"
    spec = util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@pytest.mark.parametrize("reference_ms", [1, 800, 801])
def test_candidates_are_the_declared_grid_with_strict_half_bound(subject, monkeypatch, reference_ms):
    validated = []
    original = Aggregation.__post_init__

    def checked(instance):
        result = original(instance)
        validated.append((instance.quiet_ms, instance.max_wait_ms))
        return result

    monkeypatch.setattr(Aggregation, "__post_init__", checked)
    windows = subject.candidate_windows(reference_ms)
    upper = math.ceil(reference_ms / 2) - 1
    grid = {math.floor(k * upper / 4) for k in range(5)}
    expected = {(quiet, cap) for quiet in grid for cap in grid if quiet <= cap}
    assert isinstance(windows, list)
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in windows)
    assert len(windows) == len(set(windows))
    assert set(windows) == expected
    assert (0, 0) in windows
    assert expected <= set(validated), "每个候选必须经过实际 Aggregation 校验"
    assert all(type(w) is int and type(c) is int and 0 <= w <= c < reference_ms / 2
               for w, c in windows)
    assert (1500, 3000) not in windows, "旧候选不能绕过本轮半窗约束"


@pytest.mark.parametrize("reference_ms", [0, True, float("inf")])
def test_invalid_reference_cannot_create_a_window(subject, reference_ms):
    with pytest.raises(ValueError):
        subject.candidate_windows(reference_ms)


def test_six_arrival_traces_are_fixed_without_window_arguments(subject):
    traces = subject.arrival_traces(2000, 400, 600)
    assert traces == {
        "single": [0],
        "simultaneous": [0, 0, 0, 0],
        "short_burst": [0, 100, 200, 300],
        "spaced_burst": [0, 200, 400, 600],
        "finite_continuous": [100 * index for index in range(21)],
        "inflight": [0, 399, 401, 999, 1001],
    }


def test_inflight_edges_keep_coincident_events_and_fractional_measured_times(subject):
    traces = subject.arrival_traces(20, .5, .5)
    assert traces["inflight"] == [0, 0, 0, 1.5, 2]
    assert traces["short_burst"] == [0, 1, 2, 3]


def test_score_counts_extra_actual_calls_and_mean_window_wait(subject):
    result = subject.score_trace(
        analysis_calls=3, reply_calls=2, analysis_ms=400, reply_ms=600,
        window_waits_ms=[0, 100, 200],
    )
    assert result["D_ms"] == pytest.approx(100)
    assert result["extra_ms"] == pytest.approx(1400)
    assert result["J_ms"] == pytest.approx(1500)


@pytest.mark.parametrize("reply_calls", [0, 1])
def test_score_does_not_credit_negative_extra_work_or_charge_an_uncalled_reply(subject, reply_calls):
    result = subject.score_trace(
        analysis_calls=1, reply_calls=reply_calls, analysis_ms=400, reply_ms=600,
        window_waits_ms=[0, 10],
    )
    assert result["D_ms"] == pytest.approx(5)
    assert result["extra_ms"] == pytest.approx(0)
    assert result["J_ms"] == pytest.approx(5)


@pytest.fixture
def production_calls(monkeypatch):
    audit = {"run_once": [], "claims": [], "commits": [], "ready": []}
    original_run = Engine.run_once
    original_claim = Store.claim_analysis
    original_commit = Store.commit_analysis
    original_ready = Aggregation.ready

    def run_once(engine, *args, **kwargs):
        audit["run_once"].append({"force": kwargs.get("force", False)})
        return original_run(engine, *args, **kwargs)

    def claim(store, *args, **kwargs):
        result = original_claim(store, *args, **kwargs)
        if result is not None:
            snapshot = result["snapshot"]
            audit["claims"].append({
                "through_revision": snapshot["through_revision"],
                "message_ids": [row["message_id"] for row in snapshot["messages"]],
            })
        return result

    def commit(store, *args, **kwargs):
        result = original_commit(store, *args, **kwargs)
        audit["commits"].append(result)
        return result

    def ready(aggregation, **kwargs):
        result = original_ready(aggregation, **kwargs)
        audit["ready"].append(result)
        return result

    monkeypatch.setattr(Engine, "run_once", run_once)
    monkeypatch.setattr(Store, "claim_analysis", claim)
    monkeypatch.setattr(Store, "commit_analysis", commit)
    monkeypatch.setattr(Aggregation, "ready", ready)
    return audit


def simulate(subject, arrivals, **overrides):
    arguments = dict(
        arrivals_ms=arrivals, analysis_ms=1000, reply_ms=500,
        quiet_ms=0, max_wait_ms=100, reference_ms=5000, poll_phase_ms=0,
    )
    arguments.update(overrides)
    return subject.simulate_trace(**arguments)


def assert_drained_without_send(result, message_count):
    assert result["drained"] is True
    assert result["remaining_messages"] == 0
    assert result["external_im_sent"] is False
    assert result["analysis_calls"] == len(result["analysis_events"])
    assert result["reply_calls"] == len(result["reply_events"])
    snapshots = result["analysis_events"]
    identifiers = [identity for event in snapshots for identity in event["message_ids"]]
    assert len(identifiers) == len(set(identifiers)) == message_count
    assert all(event["state"] == "COMMITTED" for event in snapshots)
    assert len(result["window_waits_ms"]) == message_count
    assert all(math.isfinite(wait) and wait >= 0 for wait in result["window_waits_ms"])
    assert math.isfinite(result["end_ms"]) and result["end_ms"] >= 0


def test_single_message_uses_real_store_engine_and_readiness(subject, production_calls):
    result = simulate(subject, [0], quiet_ms=100, max_wait_ms=200)
    assert_drained_without_send(result, 1)
    assert result["analysis_calls"] == result["reply_calls"] == 1
    assert result["window_waits_ms"] == pytest.approx([100], abs=.05)
    assert result["analysis_events"][0]["started_ms"] == pytest.approx(250, abs=.05)
    assert result["reply_events"][0]["state"] == "WAIT"
    assert production_calls["run_once"]
    assert all(row["force"] is False for row in production_calls["run_once"])
    assert False in production_calls["ready"] and True in production_calls["ready"]
    assert len(production_calls["claims"]) == len(production_calls["commits"]) == 1
    saved = production_calls["commits"][0]
    assert saved["facts_committed"] is True and saved["accepted"] and not saved["rejected"]
    assert production_calls["claims"][0]["message_ids"] == result["analysis_events"][0]["message_ids"]
    assert saved["through_revision"] == result["analysis_events"][0]["through_revision"]


def test_simultaneous_arrivals_are_all_ingested_before_first_poll(subject):
    result = simulate(subject, [0, 0, 0, 0])
    assert_drained_without_send(result, 4)
    assert result["analysis_calls"] == result["reply_calls"] == 1
    assert len(result["analysis_events"][0]["message_ids"]) == 4
    assert result["window_waits_ms"] == pytest.approx([0, 0, 0, 0], abs=.05)
    assert result["analysis_events"][0]["started_ms"] == pytest.approx(0, abs=.05)


def test_busy_loop_can_exceed_cap_preserving_first_facts_and_skipping_uncalled_b(subject, production_calls):
    result = simulate(subject, [0, 300])
    assert_drained_without_send(result, 2)
    assert result["analysis_calls"] == 2 and result["reply_calls"] == 1
    first, second = result["analysis_events"]
    assert first["finished_ms"] == pytest.approx(1000, abs=.05)
    assert second["started_ms"] == pytest.approx(1250, abs=.05)
    assert second["started_ms"] - 300 > 100, "C 不包含前一个 A 的忙时与轮询等待"
    assert first["through_revision"] < second["through_revision"]
    assert all(saved["facts_committed"] and saved["accepted"] and not saved["rejected"]
               for saved in production_calls["commits"])
    assert len(production_calls["commits"]) == 2, "后来入站不得抹掉正常首批 A 事实"
    assert result["reply_events"][0]["started_ms"] == pytest.approx(2250, abs=.05)
    score = subject.score_trace(
        analysis_calls=result["analysis_calls"], reply_calls=result["reply_calls"],
        analysis_ms=1000, reply_ms=500, window_waits_ms=result["window_waits_ms"],
    )
    assert score["extra_ms"] == pytest.approx(1000), "B 前门阻断不应多计 500ms"


def test_inflight_reply_rejection_still_costs_a_full_b_call(subject):
    result = simulate(subject, [0, 1200])
    assert_drained_without_send(result, 2)
    assert result["analysis_calls"] == result["reply_calls"] == 2
    rejected, final = result["reply_events"]
    assert rejected["state"] == "REJECTED" and rejected["reason"] == "WAITING_ANALYSIS"
    assert rejected["started_ms"] == pytest.approx(1000, abs=.05)
    assert rejected["finished_ms"] == pytest.approx(1500, abs=.05)
    assert final["state"] == "WAIT"
    assert result["analysis_events"][1]["started_ms"] == pytest.approx(1750, abs=.05)
    score = subject.score_trace(
        analysis_calls=result["analysis_calls"], reply_calls=result["reply_calls"],
        analysis_ms=1000, reply_ms=500, window_waits_ms=result["window_waits_ms"],
    )
    assert score["extra_ms"] == pytest.approx(1500), "B 已调用再被拒绝仍必须计耗时"


def test_poll_phase_is_preserved_separately_from_window_wait(subject):
    result = simulate(subject, [0], quiet_ms=125, max_wait_ms=200, poll_phase_ms=125)
    assert_drained_without_send(result, 1)
    assert result["window_waits_ms"] == pytest.approx([125], abs=.05)
    assert result["analysis_events"][0]["started_ms"] == pytest.approx(125, abs=.05)


def test_cap_triggers_at_equality_before_the_reset_quiet_window(subject):
    result = simulate(subject, [0, 150], quiet_ms=200, max_wait_ms=250, reference_ms=1000)
    assert_drained_without_send(result, 2)
    assert result["analysis_calls"] == result["reply_calls"] == 1
    assert result["analysis_events"][0]["started_ms"] == pytest.approx(250, abs=.05)
    assert result["window_waits_ms"] == pytest.approx([250, 100], abs=.05)


def test_continuous_finite_arrivals_drain_with_no_loss(subject):
    result = simulate(subject, [100 * index for index in range(21)], quiet_ms=200, max_wait_ms=400)
    assert_drained_without_send(result, 21)
    assert 1 <= result["analysis_calls"] <= 21
    assert 1 <= result["reply_calls"] <= 21
    assert result["end_ms"] < 50000, "有限输入必须有有限收尾，不能等待下一次真实入站"
    assert result["reply_events"][-1]["state"] == "WAIT"


def test_simulation_rejects_a_cap_at_exact_half_of_reference(subject):
    with pytest.raises(ValueError):
        simulate(subject, [0], quiet_ms=0, max_wait_ms=400, reference_ms=800)
