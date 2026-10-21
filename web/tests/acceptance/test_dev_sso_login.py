"""Independent LoginGate acceptance using the real Vue app and intercepted APIs.

No production services, real credentials, Dify, MQ or IM are contacted.
"""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect


SECRET = "synthetic-browser-only-secret-独立😀"
USER = "qa-dev-a"
NAME = "独立验收甲"
TOKEN = "synthetic-independent-xunjia-token"
MENUS = {"quote": "意向询价", "monitor": "机器人监控", "chat": "智能问答", "link": "采集通道"}


@pytest.fixture
def gate(browser, frontend_url):
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(7000)
    state = {"config_status": 200, "config": {"ok": True, "dev_login": True, "sso_configured": True,
             "dev_user_name": "", "dev_nick_name": "", "ts_skew_seconds": 60},
             "login_status": 200, "existing": False, "requests": [], "config_seen": False}

    def intercept(route):
        request = route.request
        if not request.url.startswith(frontend_url + "/"):
            route.abort()
            return
        path = urlsplit(request.url).path
        if not path.startswith("/api/"):
            route.continue_()
            return
        state["requests"].append({"path": path, "method": request.method, "body": request.post_data,
                                  "url": request.url, "headers": request.headers})
        status, body = 200, {"ok": True, "data": []}
        if path == "/api/auth/sso-config":
            state["config_seen"] = True
            status, body = state["config_status"], state["config"]
        elif path == "/api/auth/sso":
            status = state["login_status"]
            body = ({"ok": True, "token": TOKEN, "user_id": USER, "display_name": NAME} if status == 200
                    else {"ok": False, "hint": "合成门户签名无效，请重新进入"})
        elif path == "/api/auth/me":
            if request.headers.get("authorization") == "Bearer " + TOKEN:
                body = {"ok": True, "data": {"user_id": USER, "display_name": NAME,
                        "account_uid": "910001", "binding": {"account_uid": "910001", "owner_user_id": USER}}}
            else:
                status, body = 401, {"ok": False, "hint": "合成身份已失效"}
        elif path == "/api/monitor/summary":
            body["data"] = {"bot": {"status": "正常"}, "stats": []}
        elif path == "/api/im/links":
            body["data"] = {"active_id": "", "links": []}
        elif path == "/api/bonds":
            body.update(rows=[], total=0)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/*", intercept)
    try:
        yield page, frontend_url, state
    finally:
        context.close()


def open_gate(gate, suffix="/#/quote"):
    page, url, state = gate
    page.goto(url + suffix)
    page.wait_for_function("document.readyState === 'complete'")
    return page, state


def fill_form(page):
    password = page.locator('input[type="password"]')
    expect(password).to_be_visible()
    inputs = page.locator('input:not([type="password"]):not([type="radio"]):not([type="checkbox"]):not([type="hidden"])')
    expect(inputs).to_have_count(2)
    page.get_by_label(re.compile("userName")).fill(USER)
    page.get_by_label(re.compile("nickName")).fill(NAME)
    page.get_by_label(re.compile("约定密钥")).fill(SECRET)


def submit(page):
    page.get_by_role("button", name=re.compile("登录.*进入|进入.*工作台")).click()


@pytest.mark.parametrize("config,status", [
    ({"ok": True, "dev_login": False}, 200),
    ({"ok": False, "dev_login": True}, 200),
    ({"ok": True, "dev_login": True}, 503),
    ({"ok": True}, 200),
])
def test_form_is_hidden_unless_configuration_explicitly_succeeds(gate, config, status):
    page, _, state = gate
    state.update(config=config, config_status=status)
    open_gate(gate)
    expect(page.get_by_role("heading", name="询价工作台登录")).to_be_visible()
    page.wait_for_timeout(400)
    assert state["config_seen"], "The dev gate must consult the public config endpoint"
    expect(page.locator('input[type="password"]')).to_have_count(0)
    assert not [r for r in state["requests"] if r["path"] == "/api/auth/sso"]


def test_pending_configuration_does_not_flash_dev_form(gate):
    page, url, _ = gate
    delayed = []
    page.route("**/api/auth/sso-config", lambda route: delayed.append(route))
    page.goto(url + "/#/quote")
    expect(page.get_by_role("heading", name="询价工作台登录")).to_be_visible()
    page.wait_for_timeout(300)
    assert delayed
    expect(page.locator('input[type="password"]')).to_have_count(0)
    delayed[0].fulfill(status=200, json={"ok": True, "dev_login": True, "sso_configured": True})
    expect(page.locator('input[type="password"]')).to_be_visible()


@pytest.mark.parametrize("menu,label", MENUS.items())
def test_dev_signing_login_enters_selected_menu_without_leaking_secret(gate, menu, label):
    page, state = open_gate(gate)
    fill_form(page)
    # Native select or the visible menu choice: the contract does not prescribe a widget.
    if page.locator("select").count():
        page.get_by_label("进入菜单").select_option(label=label)
    else:
        page.get_by_text(label, exact=True).last.click()
    submit(page)
    expect(page.locator(".online b")).to_have_text(NAME)
    expect(page).to_have_url(re.compile(r"/#/" + menu + r"(?:\?|$)"))
    posts = [r for r in state["requests"] if r["path"] == "/api/auth/sso"]
    assert len(posts) == 1
    assert posts[0]["method"] == "POST"
    body = json.loads(posts[0]["body"])
    assert set(body) == {"userName", "nickName", "ts", "sign"}
    assert body["userName"] == USER and body["nickName"] == NAME
    assert re.fullmatch(r"\d+", body["ts"])
    assert body["sign"] == hashlib.md5(f"{USER}|{NAME}|{body['ts']}|{SECRET}".encode()).hexdigest()
    assert SECRET not in json.dumps(state["requests"], ensure_ascii=False)
    assert SECRET not in page.url
    storage = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
    assert SECRET not in storage
    assert page.evaluate("localStorage.getItem('xj_auth_token')") == TOKEN
    me = [r for r in state["requests"] if r["path"] == "/api/auth/me"]
    assert me and me[-1]["headers"].get("authorization") == "Bearer " + TOKEN
    page.evaluate("localStorage.removeItem('xj_auth_token')")
    page.reload()
    expect(page.locator('input[type="password"]')).to_be_visible()
    expect(page.locator('input[type="password"]')).to_have_value("")


@pytest.mark.parametrize("identity", [
    {"userName": USER, "nickName": NAME}, {"user_name": USER, "nick_name": NAME},
    {"userId": USER, "userName": NAME}, {"user_id": USER, "user_name": NAME},
])
@pytest.mark.parametrize("source", ["search", "hash"])
def test_portal_aliases_are_normalized_posted_and_removed_from_url(gate, identity, source):
    params = {**identity, "timestamp": "1800000000", "sig": "a" * 32, "keep": "safe"}
    query = urlencode(params)
    suffix = "/?" + query + "#/chat" if source == "search" else "/#/chat?" + query
    page, state = open_gate(gate, suffix)
    expect(page.locator(".online b")).to_have_text(NAME)
    posts = [r for r in state["requests"] if r["path"] == "/api/auth/sso"]
    assert len(posts) == 1
    assert json.loads(posts[0]["body"]) == {"userName": USER, "nickName": NAME, "ts": "1800000000", "sign": "a" * 32}
    parsed = urlsplit(page.url)
    remaining = {**parse_qs(parsed.query), **parse_qs(parsed.fragment.partition("?")[2])}
    assert remaining == {"keep": ["safe"]}
    expect(page.locator('input[type="password"]')).to_have_count(0)


@pytest.mark.parametrize("query", ["userName=partial", "nickName=partial", "sig=bad", "userName=a&nickName=b&ts=1800000000&sign=bad"])
def test_invalid_portal_entry_requires_explicit_return_to_dev_login(gate, query):
    page, _, state = gate
    state["login_status"] = 401
    open_gate(gate, "/?" + query + "#/quote")
    expect(page.get_by_role("heading", name="询价工作台登录")).to_be_visible()
    page.wait_for_timeout(400)
    expect(page.locator('input[type="password"]')).to_have_count(0)
    assert page.evaluate("localStorage.getItem('xj_auth_token')") is None
    page.get_by_role("button", name=re.compile("返回.*联调|返回.*开发|使用.*联调")).click()
    expect(page.locator('input[type="password"]')).to_be_visible()


def test_existing_session_survives_configuration_fetch_failure(gate):
    page, _, state = gate
    state["config_status"] = 503
    page.add_init_script("localStorage.setItem('xj_auth_token', " + json.dumps(TOKEN) + ")")
    open_gate(gate, "/#/chat")
    expect(page.locator(".q-input")).to_be_visible()
    expect(page.locator(".online b")).to_have_text(NAME)
    assert page.evaluate("localStorage.getItem('xj_auth_token')") == TOKEN
    expect(page.locator('input[type="password"]')).to_have_count(0)


def test_failed_dev_signature_does_not_mount_business_pages(gate):
    page, state = open_gate(gate)
    state["login_status"] = 401
    fill_form(page)
    submit(page)
    expect(page.get_by_text("合成门户签名无效，请重新进入", exact=True)).to_be_visible()
    assert page.evaluate("localStorage.getItem('xj_auth_token')") is None
    assert not [r for r in state["requests"] if r["path"] in ("/api/bonds", "/api/qa", "/api/monitor/summary")]
