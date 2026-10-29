"""Password-only Innodealing configuration; login is injected, never live."""
import asyncio

import pytest

from collector.qb_gateway import QuoteAuthenticationError
from server.quote_control import QuoteControl
from server.quote_credentials import save_quote, snapshot


def test_save_password_logs_in_and_hides_password(tmp_path):
    seen = []

    def login(username, password):
        seen.append((username, password))
        return {'cookie': 'sid=synthetic-fresh; uid=1; userid=1', 'sid': 'synthetic-fresh'}

    control = QuoteControl(directory=tmp_path, reader=object(), login=login,
                           db_check=lambda: {'ok': True})

    async def run():
        status = await control.save('manager', {'account': 'anlin9', 'password': 'synthetic-pass'})
        assert status['account'] == 'anlin9'
        assert status['password_set'] and status['configured']
        assert status['cookie_set'] and status['device_id_set']
        dumped = str(status)
        assert 'synthetic-pass' not in dumped
        assert 'synthetic-fresh' not in dumped
        saved = snapshot(tmp_path)
        assert saved['password'] == 'synthetic-pass'
        assert saved['cookie'] == 'sid=synthetic-fresh; uid=1; userid=1'
        assert saved['sid'] == 'synthetic-fresh'
        assert saved['device_id']
        assert seen == [('anlin9', 'synthetic-pass')]
        again = await control.save('manager', {'account': 'anlin9', 'password': ''})
        assert again['account'] == 'anlin9'
        assert seen == [('anlin9', 'synthetic-pass')]
        assert snapshot(tmp_path)['password'] == 'synthetic-pass'

    asyncio.run(run())


def test_save_keeps_stable_device_id_across_logins(tmp_path):
    def login(username, password):
        return {'cookie': 'sid=' + password, 'sid': password}

    control = QuoteControl(directory=tmp_path, reader=object(), login=login,
                           db_check=lambda: {'ok': True})

    async def run():
        await control.save('manager', {'account': 'anlin9', 'password': 'first'})
        device = snapshot(tmp_path)['device_id']
        await control.save('manager', {'account': 'anlin9', 'password': 'second'})
        assert snapshot(tmp_path)['device_id'] == device
        assert snapshot(tmp_path)['sid'] == 'second'

    asyncio.run(run())


def test_auth_rejection_relogs_with_saved_password(tmp_path):
    logins = []
    rounds = {'n': 0}

    def login(username, password):
        logins.append(username)
        return {'cookie': 'sid=after-' + str(len(logins)), 'sid': 'after-' + str(len(logins))}

    async def session(values, update):
        rounds['n'] += 1
        if rounds['n'] == 1:
            raise QuoteAuthenticationError('QUOTE_TOKEN_MISSING')
        assert values['cookie'] == 'sid=after-1'
        update(state='streaming')
        await asyncio.Event().wait()

    save_quote({'account': 'anlin9', 'password': 'synthetic-pass',
                'cookie': 'sid=stale', 'sid': 'stale', 'device_id': 'synthetic-device'}, tmp_path)
    control = QuoteControl(directory=tmp_path, reader=object(), session=session, login=login,
                           db_check=lambda: {'ok': True})

    async def run():
        await control.start('manager')
        for _ in range(200):
            await asyncio.sleep(0.01)
            if control.status('manager')['collector_state'] == 'streaming':
                break
        assert control.status('manager')['collector_state'] == 'streaming'
        assert logins == ['anlin9']
        assert snapshot(tmp_path)['cookie'] == 'sid=after-1'
        await control.stop('manager')

    asyncio.run(run())


def test_blank_password_retains_secret_in_public_view(tmp_path):
    save_quote({'account': 'anlin9', 'password': 'kept'}, tmp_path)
    view = save_quote({'account': 'anlin9', 'password': ''}, tmp_path)
    assert view['password_set'] is True
    assert 'kept' not in str(view)
    assert snapshot(tmp_path)['password'] == 'kept'
