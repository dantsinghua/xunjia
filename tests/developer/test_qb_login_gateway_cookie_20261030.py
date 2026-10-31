"""Real HTTPX cookie policy regressions for the confirmed site login defect."""
import httpx
import pytest

from collector.qb_login import QuoteLoginError, signin


@pytest.mark.parametrize('gateway', ['', '172.26.64.36'])
@pytest.mark.parametrize('domain', ['', '; Domain=.innodealing.com', '; Domain=web.innodealing.com'])
def test_real_cookie_policy_keeps_challenge_and_session(monkeypatch, gateway, domain):
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', gateway)
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, headers={'Set-Cookie': 'challenge=probe; Path=/; Secure' + domain})
        assert request.headers['cookie'] == 'challenge=probe'
        assert request.content == b'username=probe-user&password=synthetic-password&redirect_to='
        return httpx.Response(302, headers={
            'Location': '/dashboard', 'Set-Cookie': 'sid=synthetic-session; Path=/; Secure' + domain})

    def factory(**options):
        assert options['trust_env'] is False and options['verify'] is True
        return httpx.Client(transport=httpx.MockTransport(respond), **options)

    result = signin('probe-user', 'synthetic-password', client_factory=factory)
    assert result['sid'] == 'synthetic-session'
    assert 'challenge=probe' in result['cookie'] and 'sid=synthetic-session' in result['cookie']
    assert 'synthetic-password' not in str(result)
    assert len(requests) == 2
    assert all(r.headers['host'] == 'web.innodealing.com' for r in requests)
    if gateway:
        assert all(r.url.host == gateway and r.url.port == 11443 for r in requests)
        assert all(r.extensions['sni_hostname'] == 'web.innodealing.com' for r in requests)
    else:
        assert all(r.url.host == 'web.innodealing.com' for r in requests)


@pytest.mark.parametrize('gateway', ['', '172.26.64.36'])
def test_unrelated_cookie_domain_never_proves_login(monkeypatch, gateway):
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', gateway)

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200)
        return httpx.Response(302, headers={
            'Location': '/dashboard', 'Set-Cookie': 'sid=untrusted; Domain=unrelated.invalid; Path=/'})

    def factory(**options):
        return httpx.Client(transport=httpx.MockTransport(respond), **options)

    with pytest.raises(QuoteLoginError, match='登录未获得会话'):
        signin('probe-user', 'synthetic-password', client_factory=factory)


def test_expired_session_is_not_reused_after_login_response(monkeypatch):
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '172.26.64.36')

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, headers={'Set-Cookie': 'sid=expired-session; Domain=.innodealing.com; Path=/'})
        assert request.headers['cookie'] == 'sid=expired-session'
        return httpx.Response(302, headers={'Location': '/dashboard',
            'Set-Cookie': 'sid=; Max-Age=0; Domain=.innodealing.com; Path=/'})

    def factory(**options):
        return httpx.Client(transport=httpx.MockTransport(respond), **options)

    with pytest.raises(QuoteLoginError, match='登录未获得会话'):
        signin('probe-user', 'synthetic-password', client_factory=factory)
