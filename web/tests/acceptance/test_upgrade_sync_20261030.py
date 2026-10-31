"""Real upgrade UI interaction with synthetic APIs; never executes an installer."""
import pytest
from playwright.sync_api import expect

from test_subpath_deployment import subpath_dist, subpath_site


def upgrade_status(**overrides):
    return {"enabled": True, "can_upgrade": True, "current_build": "a" * 64,
            "retention_days": 30, "job": None, **overrides}


def job(state, identity="synthetic-new-job"):
    return {"id": identity, "state": state, "message": "synthetic " + state,
            "created_at": 1_800_000_000, "updated_at": 1_800_000_001,
            "target_build": "b" * 64}


@pytest.fixture
def upgrade(ui):
    page, base, *_ = ui
    page.clock.install()
    state = upgrade_status()
    behavior = {"post": "queued", "get": "ok", "held": []}
    calls = []

    def route(intercept):
        request = intercept.request
        calls.append({"method": request.method, "headers": request.headers,
                      "body": request.post_data_json if request.post_data else None})
        if request.method == "POST":
            if behavior["post"] == "lost":
                intercept.abort("connectionreset")
                return
            if behavior["post"] == "hold":
                behavior["held"].append(intercept)
                return
            if behavior["post"] == "forbidden":
                intercept.fulfill(status=403, json={"ok": False, "hint": "合成升级权限拒绝"})
                return
            state["job"] = job("queued")
        elif behavior["get"] == "lost":
            intercept.abort("connectionreset")
            return
        elif behavior["get"] == "hold":
            behavior["held"].append(intercept)
            return
        elif behavior["get"] == "unauthorized":
            intercept.fulfill(status=401, json={"ok": False, "hint": "合成登录失效"})
            return
        intercept.fulfill(json={"ok": True, "data": state})

    page.route(base + "/api/system/upgrade", route)
    return page, base, state, behavior, calls


def open_upgrade(upgrade):
    page, base, *_ = upgrade
    page.goto(base + "/#/link")
    page.locator('#settings-nav-upgrade').click()
    panel = page.get_by_test_id("upgrade-sync-settings")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("a" * 64)
    return page, panel


def writes(upgrade):
    return [call for call in upgrade[4] if call["method"] == "POST"]


def test_upgrade_read_is_authenticated_and_never_auto_applies(upgrade):
    page, panel = open_upgrade(upgrade)
    expect(page.get_by_test_id("upgrade-sync")).to_be_enabled()
    assert writes(upgrade) == []
    assert upgrade[4][0]["headers"]["authorization"] == "Bearer synthetic-qa-token"
    expect(panel).to_contain_text("30 天")


@pytest.mark.parametrize("override", [
    {"enabled": False}, {"can_upgrade": False}, {"job": job("unknown")},
    {"job": job("queued")}, {"job": job("downloading")},
    {"job": job("verifying")}, {"job": job("installing")},
])
def test_upgrade_disabled_or_unfinished_state_cannot_submit(upgrade, override):
    upgrade[2].update(override)
    page, _ = open_upgrade(upgrade)
    button = page.get_by_test_id("upgrade-sync")
    expect(button).to_be_disabled()
    button.evaluate("button => button.click()")
    page.clock.run_for(5001)
    assert writes(upgrade) == []


def test_upgrade_double_click_sends_exactly_one_empty_post(upgrade):
    upgrade[3]["post"] = "hold"
    page, _ = open_upgrade(upgrade)
    button = page.get_by_test_id("upgrade-sync")
    expect(button).to_be_enabled()
    with page.expect_request(lambda request: request.url.endswith("/api/system/upgrade") and request.method == "POST"):
        button.evaluate("button => { button.click(); button.click() }")
    expect(button).to_be_disabled()
    assert len(writes(upgrade)) == 1
    assert writes(upgrade)[0]["body"] == {}
    assert writes(upgrade)[0]["headers"]["authorization"] == "Bearer synthetic-qa-token"
    upgrade[2]["job"] = job("queued")
    upgrade[3]["held"].pop().fulfill(json={"ok": True, "data": upgrade[2]})
    expect(button).to_have_text("升级进行中…")
    page.clock.run_for(10001)
    assert len(writes(upgrade)) == 1


def test_lost_submission_survives_reload_until_new_durable_job_is_observed(upgrade):
    page, panel = open_upgrade(upgrade)
    upgrade[3]["post"] = "lost"
    button = page.get_by_test_id("upgrade-sync")
    button.click()
    expect(panel).to_contain_text("已暂停再次同步")
    assert page.evaluate("sessionStorage.getItem('xj_upgrade_pending:qa-trader')") == ""
    page.reload()
    page.locator('#settings-nav-upgrade').click()
    expect(page.get_by_test_id("upgrade-sync-settings")).to_contain_text("已暂停再次同步")
    expect(button).to_be_disabled()
    page.clock.run_for(10001)
    assert len(writes(upgrade)) == 1
    upgrade[2]["job"] = job("queued")
    page.clock.run_for(5001)
    expect(button).to_have_text("升级进行中…")
    assert page.evaluate("sessionStorage.getItem('xj_upgrade_pending:qa-trader')") is None
    upgrade[2].update(job=job("succeeded"), current_build="b" * 64)
    page.clock.run_for(5001)
    expect(panel).to_contain_text("synthetic succeeded")
    expect(panel).to_contain_text("b" * 64)
    expect(button).to_be_enabled()
    assert len(writes(upgrade)) == 1


def test_install_restart_disconnect_recovers_by_get_without_post_replay(upgrade):
    page, panel = open_upgrade(upgrade)
    button = page.get_by_test_id("upgrade-sync")
    button.click()
    expect(button).to_have_text("升级进行中…")
    upgrade[3]["get"] = "lost"
    page.clock.run_for(5001)
    expect(panel).to_contain_text("等待服务恢复")
    expect(button).to_be_disabled()
    upgrade[3]["get"] = "ok"
    upgrade[2].update(job=job("succeeded"), current_build="b" * 64)
    page.clock.run_for(5001)
    expect(panel).to_contain_text("synthetic succeeded")
    expect(button).to_be_enabled()
    assert len(writes(upgrade)) == 1


def test_http_rejection_shows_failure_without_automatic_second_post(upgrade):
    upgrade[3]["post"] = "forbidden"
    page, panel = open_upgrade(upgrade)
    page.get_by_test_id("upgrade-sync").click()
    expect(panel.get_by_role("alert")).to_contain_text("合成升级权限拒绝")
    page.clock.run_for(10001)
    assert len(writes(upgrade)) == 1
    assert page.evaluate("sessionStorage.getItem('xj_upgrade_pending:qa-trader')") is None


def test_leaving_collection_page_aborts_inflight_status_and_stops_polling(upgrade):
    page, panel = open_upgrade(upgrade)
    upgrade[3]["get"] = "hold"
    with page.expect_request(lambda request: request.url.endswith("/api/system/upgrade")):
        panel.get_by_role("button", name="刷新升级状态").click()
    with page.expect_event("requestfailed", predicate=lambda request: request.url.endswith("/api/system/upgrade")):
        page.evaluate("location.hash = '#/quote'")
    expect(panel).to_have_count(0)
    before = len(upgrade[4])
    page.clock.run_for(15001)
    assert len(upgrade[4]) == before
    assert writes(upgrade) == []


def test_leaving_during_post_preserves_unknown_submission_on_return(upgrade):
    page, panel = open_upgrade(upgrade)
    upgrade[3]["post"] = "hold"
    with page.expect_request(lambda request: request.url.endswith("/api/system/upgrade") and request.method == "POST"):
        page.get_by_test_id("upgrade-sync").click()
    assert page.evaluate("sessionStorage.getItem('xj_upgrade_pending:qa-trader')") == ""
    with page.expect_event("requestfailed", predicate=lambda request: request.url.endswith("/api/system/upgrade") and request.method == "POST"):
        page.evaluate("location.hash = '#/quote'")
    expect(panel).to_have_count(0)
    page.evaluate("location.hash = '#/link'")
    page.locator('#settings-nav-upgrade').click()
    expect(panel).to_contain_text("已暂停再次同步")
    expect(page.get_by_test_id("upgrade-sync")).to_be_disabled()
    page.clock.run_for(10001)
    assert len(writes(upgrade)) == 1


def test_expired_login_clears_token_and_does_not_start_upgrade(upgrade):
    page, panel = open_upgrade(upgrade)
    upgrade[3]["get"] = "unauthorized"
    panel.get_by_role("button", name="刷新升级状态").click()
    page.wait_for_function("localStorage.getItem('xj_auth_token') === null")
    page.clock.run_for(10001)
    assert writes(upgrade) == []


def test_production_upgrade_requests_remain_under_selected_application_prefix(browser, subpath_site):
    site = subpath_site
    context = browser.new_context()
    context.add_init_script("localStorage.setItem('xj_auth_token','synthetic-subpath-token')")
    page = context.new_page()
    calls = []
    status = upgrade_status()
    target = site["url"] + "api/system/upgrade"

    def route(intercept):
        request = intercept.request
        calls.append((request.method, request.url, request.headers.get("authorization")))
        if request.method == "POST":
            assert request.post_data_json == {}
            status["job"] = job("queued")
        intercept.fulfill(json={"ok": True, "data": status})

    page.route(target, route)
    page.route_web_socket("**/*", lambda websocket: None)
    try:
        page.goto(site["url"] + "#/link")
        page.locator('#settings-nav-upgrade').click()
        button = page.get_by_test_id("upgrade-sync")
        expect(button).to_be_enabled()
        button.click()
        expect(button).to_have_text("升级进行中…")
        assert [call[0] for call in calls].count("POST") == 1
        assert all(call[1] == target and call[2] == "Bearer synthetic-subpath-token" for call in calls)
        if site["prefix"] != "/":
            assert not any(path == "/api/system/upgrade" for _, path in site["requests"])
    finally:
        context.close()
