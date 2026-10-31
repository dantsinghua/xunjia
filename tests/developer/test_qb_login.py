"""HTTP DM login; no browser, no live Innodealing, no real passwords in assertions."""
from urllib.parse import urlsplit

import pytest

from collector.qb_login import QuoteLoginError, cookie_header, signin


class _Jar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain='.innodealing.com'):
        self.items = [item for item in self.items if item.name != name]
        self.items.append(type('C', (), {'name': name, 'value': value, 'domain': domain})())

    def __iter__(self):
        return iter(self.items)


class _Response:
    def __init__(self, status, headers=None, content=b''):
        self.status_code = status
        self.headers = headers or {}
        self.content = content
        self.text = content.decode()


def _client(sequence, calls):
    jar = _Jar()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['proxy'] is None and kwargs['verify'] is True and kwargs['trust_env'] is False
            self.cookies = type('Cookies', (), {'jar': jar})()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url, **kwargs):
            calls.append(('GET', url, kwargs, {c.name: c.value for c in jar}))
            status, headers, content, cookies = sequence.pop(0)
            for name, value, domain in cookies:
                jar.set(name, value, domain)
            return _Response(status, headers, content)

        def post(self, url, **kwargs):
            calls.append(('POST', url, kwargs, {c.name: c.value for c in jar}))
            status, headers, content, cookies = sequence.pop(0)
            for name, value, domain in cookies:
                jar.set(name, value, domain)
            return _Response(status, headers, content)

    return Client


def test_signin_posts_form_and_returns_sid_cookie_without_dashboard():
    calls = []
    client = _client([
        (200, {}, b'<form id="form-signin">', [('acw_tc', 'waf', 'web.innodealing.com')]),
        (302, {'location': '/dashboard'}, b'', [
            ('sid', 'synthetic-sid', '.innodealing.com'),
            ('uid', '1001', '.innodealing.com'),
            ('userid', '1001', '.innodealing.com'),
        ]),
    ], calls)
    result = signin('anlin9', 'synthetic-pass', client_factory=client)
    assert result['sid'] == 'synthetic-sid'
    assert [part.partition('=')[0] for part in result['cookie'].split('; ')] == [
        'acw_tc', 'sid', 'uid', 'userid']
    assert 'synthetic-pass' not in result['cookie']
    assert len(calls) == 2
    assert urlsplit(calls[0][1]).path == '/auth-service/signin'
    assert urlsplit(calls[1][1]).path == '/auth-service/signin'
    assert calls[1][2]['data'] == {'username': 'anlin9', 'password': 'synthetic-pass', 'redirect_to': ''}
    assert calls[1][3]['acw_tc'] == 'waf'


def test_signin_rejected_html_does_not_invent_session():
    client = _client([
        (200, {}, b'<form id="form-signin">', [('acw_tc', 'waf', 'web.innodealing.com')]),
        (200, {}, '<title>DM login</title><span id="errorTip">账号或密码错误</span>'.encode(), []),
    ], [])
    with pytest.raises(QuoteLoginError, match='账号或密码错误'):
        signin('anlin9', 'wrong', client_factory=client)


def test_signin_requires_username_and_password():
    with pytest.raises(QuoteLoginError, match='用户名和密码'):
        signin('', 'x')
    with pytest.raises(QuoteLoginError, match='用户名和密码'):
        signin('anlin9', '  ')


def test_signin_rewrites_tcp_to_site_egress_without_changing_host(monkeypatch):
    import httpx

    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '172.26.64.36')
    calls = []

    def transport(request):
        calls.append(request)
        if request.method == 'GET':
            return httpx.Response(200, headers={
                'Set-Cookie': 'acw_tc=waf; Domain=.innodealing.com; Path=/; Secure',
            }, content=b'<form id="form-signin">')
        return httpx.Response(302, headers={
            'Location': '/dashboard',
            'Set-Cookie': 'sid=synthetic-sid; Domain=.innodealing.com; Path=/; Secure',
        })

    def client(**kwargs):
        assert kwargs['proxy'] is None and kwargs['verify'] is True and kwargs['trust_env'] is False
        return httpx.Client(transport=httpx.MockTransport(transport), **kwargs)

    result = signin('anlin9', 'synthetic-pass', client_factory=client)
    assert [request.url.host for request in calls] == ['172.26.64.36', '172.26.64.36']
    assert [request.url.port for request in calls] == [11443, 11443]
    assert calls[0].headers['Host'] == calls[1].headers['Host'] == 'web.innodealing.com'
    assert calls[1].headers['Cookie'] == 'acw_tc=waf'
    assert result['sid'] == 'synthetic-sid'
    assert result['cookie'] == 'acw_tc=waf; sid=synthetic-sid'


def test_cookie_header_last_value_wins():
    jar = _Jar()
    jar.set('sid', 'old')
    jar.set('sid', 'new')
    jar.set('uid', '1')
    assert cookie_header(jar) == 'sid=new; uid=1'
