"""Missing quote modules must never masquerade as database or auth failures."""
import asyncio
import builtins

from server.quote_control import QuoteControl, check_database


def reject_collector(monkeypatch):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == 'collector' or name.startswith('collector.'):
            raise ModuleNotFoundError('synthetic missing collector')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', importing)


def test_missing_collector_is_not_database_unavailable(monkeypatch):
    reject_collector(monkeypatch)
    result = check_database({'host': 'synthetic-do-not-connect'})
    assert result['ok'] is False
    assert result['code'] == 'QUOTE_RUNTIME_UNAVAILABLE'
    assert 'collector' in result['note']


def test_missing_collector_probe_returns_actionable_result(monkeypatch, tmp_path):
    reject_collector(monkeypatch)
    checks = []
    control = QuoteControl(directory=tmp_path, db_check=lambda: checks.append(True))
    result = asyncio.run(control.test('synthetic-user', {}))
    assert result['ok'] is False and result['wss_open'] is False
    assert result['code'] == 'QUOTE_RUNTIME_UNAVAILABLE'
    assert result['saved'] is False
    assert checks == []
    assert not list(tmp_path.iterdir())


def test_missing_runtime_stops_supervisor_without_network_retry(tmp_path):
    calls = []

    async def session(values, update):
        calls.append(True)
        raise ModuleNotFoundError('synthetic missing collector dependency')

    async def run():
        control = QuoteControl(directory=tmp_path, session=session)
        await asyncio.wait_for(control._supervise({}), timeout=0.1)
        assert control.status('synthetic-user')['code'] == 'QUOTE_RUNTIME_UNAVAILABLE'
        assert control.status('synthetic-user')['collector_state'] == 'error'

    asyncio.run(run())
    assert calls == [True]
