"""Calendar availability must not silently make yesterday's quote current."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.prices import InvalidFact, LimitPolicy
from server.reference_data import ReferenceSnapshot


def test_no_calendar_still_requires_each_peer_to_reconfirm_after_midnight(tmp_path):
    now=[datetime(2026,10,16,23,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()]
    app=create_app(submission_guard=lambda _owner: None, database_url='sqlite:///'+str(tmp_path/'calendar.db'),enable_background=False,
                   auth_config=AuthConfig(clock=lambda:now[0]))
    store=app.state.repository
    group=store.create_group('owner',bond_code='SYN-A',our_side='BUY',target_volume='1000',
        trade_day='2026-10-16',policy=LimitPolicy('ABSOLUTE','3.12'))
    store.add_member('owner',group,account_uid='synthetic-account',peer_uid='p1')
    store.add_member('owner',group,account_uid='synthetic-account',peer_uid='p2')
    now[0]+=120
    app.state.rollover_once()
    value=store.group_rows('owner')[0]
    assert value['trade_day']=='2026-10-17'
    assert all(peer['reconfirm_day']=='2026-10-17' for peer in value['peers'])


def test_old_snapshot_date_remains_metadata_without_blocking_calendar_free_estimate(tmp_path):
    class Reference:
        def snapshot(self):
            return ReferenceSnapshot('synthetic-old','2026-09-01',bonds=[
                {'code':'SYN-A','valuation_yield_pct':'2.9872','valuation_date':'2026-09-01'}])
    app=create_app(submission_guard=lambda _owner: None, database_url='sqlite:///'+str(tmp_path/'source.db'),enable_background=False,
                   reference_provider=Reference())
    # 安琳新规则只取消估值日期门；不把旧数据伪装为最新发布数据。
    valuation,expected=app.state.inquiry.valuation('SYN-A')
    assert valuation.yield_pct=='2.9872' and valuation.trading_date=='2026-09-01'
    assert valuation.source=='reference' and expected is None
    assert app.state.inquiry.calendar is None


def test_economic_send_is_blocked_at_midnight_before_rollover_poll_runs(tmp_path):
    now=[datetime(2026,10,16,23,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()]
    app=create_app(submission_guard=lambda _owner: None, database_url='sqlite:///'+str(tmp_path/'native-day.db'),enable_background=False,
                   auth_config=AuthConfig(clock=lambda:now[0]))
    store=app.state.repository;commands=app.state.inquiry.commands_core
    group=store.create_group('owner',bond_code='SYN-A',our_side='BUY',target_volume='1000',
        trade_day='2026-10-16',policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
    member,_thread=store.add_member('owner',group,account_uid='synthetic-account',peer_uid='p1')
    basis=commands.basis('owner',member)
    now[0]+=120
    result=commands.propose('owner',member,basis=basis,request_id='midnight-test',
        proposal={'kind':'REPLY','text':'3.20%收'},validator=lambda value:value,expires_at=now[0]+60)
    assert result['state']=='REJECTED'
    assert result['error']['reason']=='DAILY_ROLLOVER_REQUIRED'
