"""Exercise the actual SSE generator with signed synthetic sessions and clocks."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers
from server.auth import AuthConfig, AuthService, AuthUser
from server.negotiation.http import router

NOW=1800000000
SECRET='synthetic-sse-session-secret-over-thirty-two-bytes'


def scenario(*,ttl=3,snapshot=None):
    clock=[NOW];calls=[]
    auth=AuthService(AuthConfig(sso_secret='synthetic-sso',session_secret=SECRET,
                                token_ttl_seconds=ttl,clock=lambda:clock[0]))
    app=SimpleNamespace(state=SimpleNamespace(auth=auth))
    def read(who):
        calls.append(who)
        return snapshot(clock,who) if snapshot else {'revision':len(calls)}
    service=SimpleNamespace(store=SimpleNamespace(snapshot=read),commands_core=None)
    endpoint=next(r.endpoint for r in router(service,None,None,None).routes if r.path=='/api/negotiation/updates')
    async def stream(user='alice',headers=None,token=None):
        who=AuthUser(user,'Synthetic '+user)
        supplied=token or auth.issue_token(who)
        async def connected():return False
        request=SimpleNamespace(state=SimpleNamespace(user=who),app=app,
            headers=Headers(headers if headers is not None else {'Authorization':'Bearer '+supplied}),is_disconnected=connected)
        response=await endpoint(request,after=0)
        return response.body_iterator,supplied
    return clock,calls,auth,app,stream


def test_expired_stream_stops_before_another_snapshot():
    async def run():
        clock,calls,_,_,stream=scenario();events,token=await stream()
        try:
            frame=await anext(events);assert 'event: changed' in frame and token not in frame
            clock[0]=NOW+3
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(events),2)
            assert calls==['alice']
        finally:await events.aclose()
    asyncio.run(run())


def test_expiry_during_snapshot_discards_that_revision():
    async def run():
        def snapshot(clock,who):clock[0]=NOW+3;return {'revision':99}
        _,calls,_,_,stream=scenario(snapshot=snapshot);events,_=await stream()
        try:
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(events),2)
            assert calls==['alice']
        finally:await events.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('change',['missing','noncallable','rotated-secret','other-owner'])
def test_missing_or_changed_authentication_fails_closed_without_query(change):
    async def run():
        _,calls,auth,app,stream=scenario();events,_=await stream()
        if change=='missing':del app.state.auth
        elif change=='noncallable':app.state.auth=SimpleNamespace(verify_token=None)
        elif change=='rotated-secret':app.state.auth=AuthService(replace(auth.config,session_secret='different-synthetic-secret-over-thirty-two-bytes'))
        else:app.state.auth=SimpleNamespace(verify_token=lambda _:AuthUser('other-owner','Other'))
        try:
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(events),2)
            assert calls==[]
        finally:await events.aclose()
    asyncio.run(run())


def test_existing_alternate_header_is_valid_but_invalid_bearer_has_precedence():
    async def run():
        _,calls,auth,_,stream=scenario();token=auth.issue_token(AuthUser('alice','Synthetic alice'))
        events,_=await stream(headers={'X-Auth-Token':token})
        try:assert 'event: changed' in await anext(events)
        finally:await events.aclose()
        assert calls==['alice'];calls.clear()
        events,_=await stream(headers={'Authorization':'Bearer malformed','X-Auth-Token':token})
        try:
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(events),2)
            assert calls==[]
        finally:await events.aclose()
    asyncio.run(run())


def test_one_expired_user_does_not_stop_another_valid_owner_stream():
    async def run():
        clock,calls,auth,_,stream=scenario(ttl=2)
        first,_=await stream('alice')
        second_token=AuthService(replace(auth.config,token_ttl_seconds=5)).issue_token(AuthUser('bob','Synthetic bob'))
        second,_=await stream('bob',token=second_token)
        try:
            assert 'changed' in await anext(first);assert 'changed' in await anext(second)
            clock[0]=NOW+2
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(first),2)
            assert 'changed' in await asyncio.wait_for(anext(second),2)
            assert calls==['alice','bob','bob']
        finally:await first.aclose();await second.aclose()
    asyncio.run(run())


def test_expired_session_stops_heartbeat_stream_too():
    async def run():
        clock,calls,_,_,stream=scenario(snapshot=lambda _clock,_who:{'revision':0})
        events,token=await stream()
        try:
            frame=await anext(events)
            assert frame==': keepalive\n\n' and token not in frame
            clock[0]=NOW+3
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(events),2)
            assert calls==['alice']
        finally:await events.aclose()
    asyncio.run(run())


def test_auth_verifier_exception_never_queries_or_emits():
    async def run():
        _,calls,_,app,stream=scenario();events,_=await stream()
        def broken(_token):raise RuntimeError('synthetic-private-detail-must-not-escape')
        app.state.auth=SimpleNamespace(verify_token=broken)
        try:
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(anext(events),2)
            assert calls==[]
        finally:await events.aclose()
    asyncio.run(run())
