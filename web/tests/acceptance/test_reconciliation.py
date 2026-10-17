from playwright.sync_api import expect


def open_review(ui):
    page, url, _, _, _ = ui
    page.goto(url + "/#/monitor")
    page.get_by_role("button", name="核对未决指令", exact=True).click()
    dialog = page.locator(".ant-modal-content").filter(has_text="核对发送结果未知的指令")
    expect(dialog).to_be_visible()
    dialog.locator(".pending-list button").first.click()
    return page, dialog


def test_reconciliation_requires_explicit_result_evidence_and_confirmation_and_never_resends(ui):
    page, dialog = open_review(ui)
    save = dialog.get_by_role("button", name="保存人工核对结果", exact=True)
    expect(save).to_be_disabled()
    dialog.locator("select").select_option("failed")
    dialog.locator("textarea").fill("synthetic-native-record-reference")
    expect(save).to_be_disabled()
    dialog.locator('input[type="checkbox"]').check()
    expect(save).to_be_enabled()
    save.click()
    expect(page.get_by_text("已记录人工核对结果；未重发消息，未恢复自动谈价", exact=True)).to_be_visible()
    expect(dialog).to_contain_text("当前没有待核对指令")
    requests = [request for request in ui[3] if request.url.endswith("/cmd-unknown/reconcile")]
    assert len(requests) == 1
    assert requests[0].post_data_json == {"status": "failed", "evidence": "synthetic-native-record-reference",
                                           "expected_result_hash": "review-hash-original", "confirmed": True}
    assert ui[4]["/api/im/rfq/send"] == ui[4]["/api/monitor/send"] == ui[4]["/api/bot/toggle"] == 0


def test_business_rejection_keeps_the_unknown_command_and_never_announces_success(ui):
    ui[2]["reconcile_error"] = True
    page, dialog = open_review(ui)
    dialog.locator("select").select_option("failed")
    dialog.locator("textarea").fill("synthetic-native-record-reference")
    dialog.locator('input[type="checkbox"]').check()
    dialog.get_by_role("button", name="保存人工核对结果", exact=True).click()
    expect(page.get_by_text("独立核对被拒绝", exact=True)).to_be_visible()
    expect(dialog).to_contain_text("待核对的唯一合成正文")
    expect(page.get_by_text("已记录人工核对结果；未重发消息，未恢复自动谈价", exact=True)).to_have_count(0)
    assert ui[4]["/api/im/rfq/send"] == ui[4]["/api/monitor/send"] == ui[4]["/api/bot/toggle"] == 0
