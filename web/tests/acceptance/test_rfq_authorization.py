"""Actual RFQ modal: explicit policy -> server preview -> stable confirmation."""
from playwright.sync_api import expect
import pytest


def open_rfq(ui):
    page, url, state, _, _ = ui
    state["rfq_enabled"] = True
    page.goto(url + "/#/quote")
    page.locator(".mkt-row input[type=checkbox]").first.check()
    page.get_by_role("button", name="开始询价", exact=True).click()
    modal = page.get_by_test_id("rfq-modal")
    expect(modal).to_be_visible()
    return page, modal


def fill_authorization(ui):
    page, modal = open_rfq(ui)
    expect(modal.get_by_test_id("rfq-side")).to_have_value("")
    expect(modal.get_by_test_id("rfq-volume")).to_have_value("")
    modal.get_by_test_id("rfq-side").select_option("BUY")
    for name, value in [("volume", "1000"), ("opening", "2.50"), ("limit", "2.40"), ("step", "2")]:
        modal.get_by_test_id("rfq-" + name).fill(value)
    modal.locator(".person", has_text="910100").click()
    expect(modal.get_by_test_id("rfq-preview")).to_be_enabled()
    return page, modal


def test_opening_rfq_requires_explicit_direction_and_volume_before_preview(ui):
    _, modal = open_rfq(ui)
    expect(modal.get_by_test_id("rfq-side")).to_have_value("")
    expect(modal.get_by_test_id("rfq-volume")).to_have_value("")
    expect(modal.get_by_test_id("rfq-preview")).to_be_disabled()
    expect(modal.get_by_test_id("rfq-confirm")).to_be_disabled()
    assert ui[4]["/api/im/rfq/preview"] == ui[4]["/api/im/rfq/send"] == 0


def test_server_preview_is_the_only_confirmation_content_and_queue_is_not_sent(ui):
    page, modal = fill_authorization(ui)
    modal.get_by_test_id("rfq-text").fill("人工编辑行情原文-不得充当自动授权")
    modal.get_by_test_id("rfq-preview").click()
    expect(modal.get_by_test_id("rfq-server-preview")).to_contain_text("SERVER-ONLY-服务端最终首句")
    assert ui[4]["/api/im/rfq/send"] == 0
    preview_request = [request for request in ui[3] if request.url.endswith("/api/im/rfq/preview")][-1].post_data_json
    assert preview_request["items"][0]["our_side"] == "BUY"
    assert preview_request["items"][0]["volume_wan"] == "1000"
    assert "text" not in preview_request["items"][0]
    modal.get_by_test_id("rfq-confirm").click()
    expect(modal.get_by_test_id("rfq-submission")).to_contain_text("已入队，尚未发送")
    sent = [request for request in ui[3] if request.url.endswith("/api/im/rfq/send")][-1].post_data_json
    assert sent["batch_id"] == "batch-synthetic-fixed"
    assert sent["expected_version"] == 7
    assert sent["request_id"]
    assert "items" not in sent and "text" not in sent
    expect(page.locator(".mkt-row")).not_to_contain_text("询价中")


@pytest.mark.parametrize("field,value", [("volume", "2000"), ("limit", "2.41"), ("step", "1")])
def test_editing_an_authorization_field_invalidates_the_previously_confirmable_preview(ui, field, value):
    _, modal = fill_authorization(ui)
    modal.get_by_test_id("rfq-preview").click()
    expect(modal.get_by_test_id("rfq-confirm")).to_be_enabled()
    modal.get_by_test_id("rfq-" + field).fill(value)
    expect(modal.get_by_test_id("rfq-server-preview")).to_have_count(0)
    expect(modal.get_by_test_id("rfq-confirm")).to_be_disabled()
    assert ui[4]["/api/im/rfq/send"] == 0


def test_removing_recipient_invalidates_preview_and_prevents_confirmation(ui):
    _, modal = fill_authorization(ui)
    modal.get_by_test_id("rfq-preview").click()
    expect(modal.get_by_test_id("rfq-confirm")).to_be_enabled()
    modal.locator(".person", has_text="910100").click()
    expect(modal.get_by_test_id("rfq-confirm")).to_be_disabled()
    expect(modal.get_by_test_id("rfq-preview")).to_be_disabled()
    assert ui[4]["/api/im/rfq/send"] == 0


def test_confirm_retry_after_response_failure_keeps_the_same_request_identity(ui):
    ui[2]["rfq_fail_first"] = True
    _, modal = fill_authorization(ui)
    modal.get_by_test_id("rfq-preview").click()
    expect(modal.get_by_test_id("rfq-confirm")).to_be_enabled()
    modal.get_by_test_id("rfq-confirm").click()
    expect(modal).to_contain_text("合成确认响应丢失")
    modal.get_by_test_id("rfq-confirm").click()
    expect(modal.get_by_test_id("rfq-submission")).to_contain_text("已入队，尚未发送")
    sent = [request.post_data_json for request in ui[3] if request.url.endswith("/api/im/rfq/send")]
    assert len(sent) == 2
    assert sent[0] == sent[1]


def test_stale_directory_does_not_block_zero_send_preview_for_a_bound_trader(ui):
    ui[2]["rfq_stale"] = True
    _, modal = fill_authorization(ui)
    modal.get_by_test_id("rfq-preview").click()
    expect(modal.get_by_test_id("rfq-server-preview")).to_contain_text("SERVER-ONLY-服务端最终首句")
    assert ui[4]["/api/im/rfq/send"] == 0
