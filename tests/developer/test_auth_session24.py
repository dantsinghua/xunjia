"""Synthetic session lifetime boundaries; no real SSO or credentials."""
from dataclasses import replace

from server.auth import AuthConfig, AuthService, AuthUser


USER = AuthUser('synthetic-owner', 'Synthetic User')
SECRET = 'synthetic-session-secret-at-least-thirty-two-bytes'


def config(clock, **kwargs):
    return AuthConfig(sso_secret='synthetic-portal-secret', session_secret=SECRET,
                      clock=lambda: clock[0], **kwargs)


def test_default_session_retains_identity_for_twenty_four_hours():
    clock=[1800000000]
    auth=AuthService(config(clock));token=auth.issue_token(USER)
    for elapsed in (23*3600, 24*3600-1):
        clock[0]=1800000000+elapsed
        assert auth.verify_token(token)==USER
    clock[0]=1800000000+24*3600
    assert auth.verify_token(token) is None


def test_environment_constructor_uses_same_default_without_new_configuration(monkeypatch):
    monkeypatch.setenv('XUNJIA_SSO_SECRET','synthetic-portal-secret')
    monkeypatch.setenv('XUNJIA_AUTH_SECRET',SECRET)
    clock=[1800000000]
    auth=AuthService(replace(AuthConfig.from_env(),clock=lambda:clock[0]))
    token=auth.issue_token(USER);clock[0]+=23*3600
    assert auth.verify_token(token)==USER
    assert auth.config.sso_skew_seconds==60


def test_service_reconstruction_preserves_session_but_key_change_rejects():
    clock=[1800000000]
    token=AuthService(config(clock)).issue_token(USER)
    clock[0]+=23*3600
    assert AuthService(config(clock)).verify_token(token)==USER
    rotated=replace(config(clock),session_secret='synthetic-other-secret-at-least-thirty-two-bytes')
    assert AuthService(rotated).verify_token(token) is None
    encoded,signature=token.split('.')
    assert AuthService(config(clock)).verify_token(encoded+'.'+('0' if signature[0]!='0' else '1')+signature[1:]) is None


def test_existing_short_session_expiry_is_not_extended_by_new_default():
    clock=[1800000000]
    old=AuthService(config(clock,token_ttl_seconds=3600)).issue_token(USER)
    current=AuthService(config(clock))
    clock[0]+=3599
    assert current.verify_token(old)==USER
    clock[0]+=1
    assert current.verify_token(old) is None


def test_explicit_short_lifetime_keeps_exact_expiry_contract():
    clock=[1800000000]
    auth=AuthService(config(clock,token_ttl_seconds=30));token=auth.issue_token(USER)
    clock[0]+=29;assert auth.verify_token(token)==USER
    clock[0]+=1;assert auth.verify_token(token) is None
