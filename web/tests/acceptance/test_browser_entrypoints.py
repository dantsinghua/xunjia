"""Independent browser behavior acceptance: actual App/router/page components."""
import re

from playwright.sync_api import expect
import pytest


def ask(ui, answer):
    page, url, state, _, _ = ui
    state["answer"] = answer
    page.goto(url + "/#/chat")
    page.locator(".q-input").fill("合成问题")
    page.locator(".q-input").press("Enter")
    expect(page.locator(".md-body")).to_be_visible()
    return page


def test_markdown_renders_text_but_never_executes_raw_html(ui):
    page = ask(ui, '**独立正常加粗**\n<img src="/not-an-image" onerror="window.__qaXss=1">')
    expect(page.locator(".md-body strong")).to_have_text("独立正常加粗")
    assert page.evaluate("window.__qaXss || 0") == 0
    expect(page.locator(".md-body [onerror]")).to_have_count(0)


def test_markdown_dangerous_link_cannot_execute_script(ui):
    page = ask(ui, "[危险链接](javascript:window.__qaXss=1)")
    expect(page.locator('.md-body a[href^="javascript:"]')).to_have_count(0)
    assert page.evaluate("window.__qaXss || 0") == 0


def test_real_qa_request_uses_this_application_authentication(ui):
    ask(ui, "接口唯一回答标记")
    requests = [request for request in ui[3] if request.url.endswith("/api/qa")]
    assert len(requests) == 1
    assert requests[0].headers.get("authorization") == "Bearer synthetic-qa-token"
    assert requests[0].headers.get("x-auth-token") == "synthetic-qa-token"


@pytest.mark.parametrize("status", [200, 403, 503])
def test_stop_rejection_does_not_display_a_successful_stop(ui, status):
    page, url, state, _, _ = ui
    state["bot_error"] = status
    page.goto(url + "/#/monitor")
    expect(page.locator(".bot-status")).to_have_text("正常")
    page.locator(".bot-sw").click()
    expect(page.get_by_text("独立验收-拒绝停止", exact=True)).to_be_visible()
    expect(page.locator(".bot-status")).to_have_text("正常")
    expect(page.locator(".bot-sw")).to_have_class(re.compile(r"\bon\b"))


def test_chat_refresh_failure_preserves_last_successful_messages(ui):
    page, url, state, _, counts = ui
    page.clock.install()
    page.goto(url + "/#/monitor")
    expect(page.get_by_text("最后成功消息-独立验收", exact=True)).to_be_visible()
    state["chat_error"] = True
    page.clock.run_for(3500)
    assert counts["/api/monitor/chat"] >= 2
    expect(page.get_by_text("最后成功消息-独立验收", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile("消息拉取失败|合成查询故障"))).to_be_visible()


def test_initial_monitor_load_does_not_trigger_a_recursive_request_loop(ui):
    page, url, _, _, counts = ui
    page.clock.install()
    page.goto(url + "/#/monitor")
    expect(page.get_by_text("最后成功消息-独立验收", exact=True)).to_be_visible()
    page.clock.run_for(1000)
    assert 1 <= counts["/api/monitor/summary"] <= 2
    assert 1 <= counts["/api/monitor/counterparties"] <= 2
    assert 1 <= counts["/api/monitor/chat"] <= 2


def test_failed_identity_check_never_mounts_business_pages(ui):
    page, url, state, _, counts = ui
    state["auth_status"] = 401
    page.goto(url + "/#/chat")
    expect(page.get_by_role("heading", name="询价工作台登录")).to_be_visible()
    expect(page.get_by_text("合成身份已失效", exact=True)).to_be_visible()
    expect(page.locator(".q-input")).to_have_count(0)
    assert counts["/api/qa"] == 0


def test_identity_expiry_unmounts_the_active_business_page(ui):
    page, url, state, _, _ = ui
    page.goto(url + "/#/chat")
    expect(page.locator(".q-input")).to_be_visible()
    state["qa_status"] = 401
    page.locator(".q-input").fill("过期后禁止继续")
    page.locator(".q-input").press("Enter")
    expect(page.get_by_role("heading", name="询价工作台登录")).to_be_visible()
    expect(page.locator(".q-input")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('xj_auth_token')") is None


def test_header_displays_the_verified_trader_name(ui):
    page, url, _, _, _ = ui
    page.goto(url + "/#/chat")
    expect(page.locator(".q-input")).to_be_visible()
    expect(page.locator(".online b")).to_have_text("验收交易员")


def open_bond(ui):
    page, url, _, _, _ = ui
    page.goto(url + "/#/monitor")
    expect(page.locator(".rb-name")).to_be_visible()
    page.locator(".rb-name").click()
    expect(page.locator(".bond-mdl")).to_be_visible()
    return page


def test_absent_detail_data_never_generates_chart_or_trades(ui):
    page = open_bond(ui)
    expect(page.get_by_text("暂无已接入的历史成交序列", exact=True)).to_be_visible()
    expect(page.locator(".bond-mdl polyline")).to_have_count(0)
    expect(page.get_by_text("今日 0 笔", exact=True)).to_be_visible()


def test_real_history_curve_is_preserved_when_intraday_data_is_absent(ui):
    ui[2]["bond_detail"] = {"hist": {"avg": "41,73 82,19", "zz": "", "max": "unique-history"}, "trades": []}
    page = open_bond(ui)
    expect(page.locator('.bond-mdl polyline[points="41,73 82,19"]')).to_be_visible()


def test_missing_concession_usage_is_not_invented_from_session_stage(ui):
    page, url, _, _, _ = ui
    page.goto(url + "/#/monitor")
    expect(page.locator(".rb-cap")).to_be_visible()
    assert not re.search(r"\d+(?:\.\d+)?%", page.locator(".rb-cap").inner_text()), (
        "The API supplied no concession usage; stage alone cannot create a percentage"
    )
