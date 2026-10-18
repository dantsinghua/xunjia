"""QA dates stay explicit while lease/backoff elapsed seconds remain real."""
from datetime import datetime
from zoneinfo import ZoneInfo

from dify.negotiation_v22.cases import FixtureClock


def test_fixture_clock_keeps_declared_day_and_measures_elapsed_monotonic_seconds():
    tick = [1234.0]
    clock = FixtureClock(monotonic=lambda: tick[0])
    before = clock()
    assert datetime.fromtimestamp(before, ZoneInfo('Asia/Shanghai')).isoformat() == '2026-10-17T12:00:00+08:00'
    tick[0] += 5.125
    assert clock() - before == 5.125
    reopened = FixtureClock(anchor=clock.anchor, monotonic=lambda: tick[0])
    assert reopened() == clock()
    tick[0] += 2
    assert reopened() - before == 7.125


def test_recovery_reopens_same_clock_without_rewinding_backoff(tmp_path):
    from scripts import measure_dify_recovery as recovery
    spec = recovery.prepare(tmp_path)
    first, service = recovery.open_service(tmp_path)
    before = first.clock()
    second, reopened = recovery.open_service(tmp_path)
    assert first.clock.anchor == second.clock.anchor
    assert before <= second.clock()
    assert service.trade_day() == reopened.trade_day() == '2026-10-17'
    assert (tmp_path / 'clock.json').is_file()
    assert recovery.state(second, spec, 'analysis')['message']['trade_day'] == reopened.trade_day()
