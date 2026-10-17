"""First-account strategy drafts are explicit and persist only on user save."""
from urllib.parse import urlsplit

from playwright.sync_api import expect


def test_first_account_can_save_an_explicit_personal_draft_without_authorizing_rfq(ui):
    page, url, _, requests, _ = ui
    page.goto(url + "/#/quote")
    page.get_by_role("button", name="询价策略", exact=True).click()
    expect(page.get_by_test_id("strategy-state")).to_contain_text("未保存草稿")
    assert not any(request.method == "POST" for request in requests)
    page.locator(".strategy-mdl").get_by_role("button", name="保存", exact=True).click()
    expect(page.locator(".strategy-mdl")).to_have_count(0)
    writes = [request for request in requests if request.method == "POST"]
    assert len(writes) == 1 and urlsplit(writes[0].url).path == "/api/strategy"
    body = writes[0].post_data_json
    assert body["template_id"] == "personal"
    assert set(body) == {"template_id", "start_offset_bp", "boundary_bp", "step_bp", "strict_match", "system"}
    page.get_by_role("button", name="询价策略", exact=True).click()
    expect(page.get_by_test_id("strategy-state")).to_contain_text("已保存模板")


def test_rejected_first_save_keeps_the_unsaved_draft_visible(ui):
    page, url, state, requests, _ = ui
    state["strategy_error"] = True
    page.goto(url + "/#/quote")
    page.get_by_role("button", name="询价策略", exact=True).click()
    page.locator(".strategy-mdl").get_by_role("button", name="保存", exact=True).click()
    expect(page.get_by_text("合成策略保存被拒绝", exact=True)).to_be_visible()
    expect(page.get_by_test_id("strategy-state")).to_contain_text("未保存草稿")
    assert all(urlsplit(request.url).path == "/api/strategy" for request in requests if request.method == "POST")
