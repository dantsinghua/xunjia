"""Current-day presentation never deletes old messages or substitutes market quotes."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect

DAY = "2026-10-22"

def bond(code="TODAY", **changes):
    return {"code": code, "name": code+"合成券", "session_id": "member-"+code,
            "group_id": "group-"+code, "trade_day": DAY, "state": "OPEN",
            "stage": "询价中", "phase": "询价中", "own_side": "BUY", "own_limit": "1.52",
            "member_send_allowed": True, "group_send_allowed": True, "send_allowed": True, **changes}

def fixture_state(ui, bonds):
    page, url, state, _, _ = ui
    state["counterparties_data"] = [{"id": "qa-peer", "name": "对手甲", "bonds": bonds}]
    state["chat_pages"] = {"": {"messages": [{"id": "history-1", "role": "peer",
        "text": "昨日已识别报价，保留历史", "time": "10:00:00", "analysis_state": "ANALYZED",
        "contexts": [{"code": "OLD", "name": "昨日债券", "memberId": "old-member"}],
        "bond": "OLD"}], "hasMore": False}}
    page.clock.install(time=datetime.fromisoformat("2026-10-22T12:00:00+08:00"))
    return page, url, state

def poll(page):
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/monitor/counterparties"):
        page.clock.run_for(3500)

def test_today_inquiring_only_on_both_sides_without_deleting_history(ui):
    items=[bond(),bond("OLD",trade_day="2026-10-21"),bond("FUTURE",trade_day="2026-10-23"),
           bond("PAUSED",stage="已暂停",member_send_allowed=False),bond("HANDOFF",state="HANDOFF",stage="待人工确认"),
           bond("NODATE",trade_day=None),bond("RECEIPT",stage="待回执")]
    page,url,state=fixture_state(ui,items);original=deepcopy(state["counterparties_data"])
    page.goto(url+"/#/monitor")
    expect(page.locator(".cp-bond")).to_have_count(1)
    expect(page.locator(".rb-card")).to_have_count(1)
    expect(page.locator(".cp-bond")).to_contain_text("TODAY合成券")
    expect(page.locator(".bond-panel .panel-hd .cnt")).to_have_text("1 只")
    message=page.locator('[data-message-id="history-1"]')
    expect(message).to_contain_text("昨日已识别报价")
    message.locator(".ctx").filter(has_text="昨日债券").click()
    expect(message.locator(".ctx.active")).to_have_text("昨日债券")
    expect(page.locator(".rb-card")).to_have_count(1)
    expect(page.locator(".rb-card")).not_to_contain_text("OLD合成券")
    expect(page.get_by_test_id("paused-bond-actions")).to_be_visible()
    expect(page.locator(".bilateral-quotes, .group-summary, .quote-evidence, .quote-fact")).to_have_count(0)
    expect(page.locator(".bond-panel")).not_to_contain_text("字段来源")
    assert state["counterparties_data"] == original
    assert not [r for r in ui[3] if r.method not in ("GET","OPTIONS")]

def test_historical_only_counterparty_keeps_chat_access_with_no_bond_cards(ui):
    page,url,_=fixture_state(ui,[bond("OLD",trade_day="2026-10-21")])
    page.goto(url+"/#/monitor")
    expect(page.locator(".cp-card")).to_have_count(1)
    expect(page.locator(".cp-bond, .rb-card")).to_have_count(0)
    expect(page.get_by_test_id("current-bonds-empty")).to_be_visible()
    expect(page.locator('[data-message-id="history-1"]')).to_contain_text("昨日债券")
    page.locator('[data-message-id="history-1"] .ctx').filter(has_text="昨日债券").click()
    expect(page.locator(".rb-card")).to_have_count(0)

def test_china_midnight_removes_old_cards_without_touching_message_associations(ui):
    page,url,_=fixture_state(ui,[bond()])
    page.clock.set_fixed_time(datetime.fromisoformat("2026-10-22T23:59:59+08:00"))
    page.goto(url+"/#/monitor");expect(page.locator(".rb-card")).to_have_count(1)
    page.clock.set_fixed_time(datetime.fromisoformat("2026-10-23T00:00:01+08:00"))
    poll(page)
    expect(page.locator(".cp-bond, .rb-card")).to_have_count(0)
    expect(page.locator('[data-message-id="history-1"]')).to_contain_text("昨日债券")
    assert not [r for r in ui[3] if r.method not in ("GET","OPTIONS")]

def test_newest_parsed_price_volume_refresh_together_and_stale_response_cannot_revert(ui):
    page,url,state=fixture_state(ui,[bond()]);revision={"value":1}
    row=state["counterparties_data"][0]["bonds"][0]
    def q(price,volume):
        return {"side":"ofr","price": None if price is None else {"kind":"YIELD","value":price},
                "volume_wan":volume,"validity":"ACTIVE","confirmed_day":DAY,
                "finality":"NEGOTIABLE","assertion":"QUOTE","revision":revision["value"],"evidence":{}}
    row["quotes"]={"ofr":q("1.52","1000"),"bid":q("9.99","9999")}
    row["market_bid"]="8.88";row["market_ofr"]="8.89"
    def response(route):route.fulfill(json={"ok":True,"revision":revision["value"],"data":state["counterparties_data"]})
    page.route("**/api/monitor/counterparties",response)
    page.goto(url+"/#/monitor");board=page.get_by_test_id("trade-board")
    expect(board.locator('[data-field="peer_quote"]')).to_have_text("1.52%")
    revision["value"]=2;row["quotes"]["ofr"]=q("1.625","1250.5");poll(page)
    expect(board.locator('[data-field="peer_quote"]')).to_have_text("1.625%")
    expect(board.locator('[data-field="peer_volume_wan"]')).to_have_text("1250.5")
    revision["value"]=1;row["quotes"]["ofr"]=q("1.52","1000");poll(page)
    expect(board.locator('[data-field="peer_quote"]')).to_have_text("1.625%")
    expect(board.locator('[data-field="peer_volume_wan"]')).to_have_text("1250.5")
    expect(board).not_to_contain_text("9.99")
    expect(board).not_to_contain_text("版本")

def test_monitor_address_book_buttons_never_render_organization_or_friend_list(ui):
    page,url,state=fixture_state(ui,[bond()]);state["rfq_enabled"]=True
    page.route("**/api/im/contacts**",lambda route:route.fulfill(json={"ok":True,"data":{
        "source":"its-relay","workbench_id":"qa-wb","account_uid":"910001","stale":False,
        "org_state":"ready","org":{"departments":[{"id":"d1","name":"不应展开的组织","parent_id":""}],"members":[]},
        "contacts":[{"uin":"910100","name":"不应展开的好友"}]}}))
    page.goto(url+"/#/monitor")
    for label in ["读取通讯录","刷新通讯录"]:
        button=page.get_by_role("button",name=label,exact=True);expect(button).to_be_enabled();button.click()
        expect(button).to_be_enabled()
        expect(page.locator(".im-book")).to_have_count(0)
        expect(page.locator(".im-bar")).not_to_contain_text("不应展开的组织")
        expect(page.locator(".im-bar")).not_to_contain_text("不应展开的好友")

@pytest.mark.parametrize("route_name",["quote","monitor","chat","link"])
def test_production_csp_typography_is_local_and_consistent_on_all_pages(ui,route_name,tmp_path):
    page,url,state=fixture_state(ui,[bond()])
    dist=Path(os.environ.get("XJ_TEST_WEB_DIST", str(Path(__file__).resolve().parents[2]/"dist"))).resolve()
    external=[];errors=[]
    page.on("request",lambda request:external.append(request.url) if "fonts.googleapis" in request.url or "fonts.gstatic" in request.url else None)
    page.on("pageerror",lambda error:errors.append(str(error)))
    csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:"
    page.route(url+"/",lambda route:route.fulfill(path=str(dist/"index.html"),content_type="text/html",headers={"Content-Security-Policy":csp}))
    def asset(route):
        path=(dist/urlsplit(route.request.url).path.lstrip("/")).resolve()
        assert path.is_relative_to(dist) and path.is_file()
        route.fulfill(path=str(path))
    page.route(url+"/assets/**",asset)
    page.goto(url+"/#/"+route_name)
    expect(page.locator("main").last).to_be_visible()
    page.evaluate("document.fonts.ready")
    body_font=page.locator("body").evaluate("el=>getComputedStyle(el).fontFamily")
    assert "Microsoft YaHei" in body_font and "monospace" not in body_font
    if route_name=="monitor":
        metric=page.locator('[data-field="own_limit"]');expect(metric).to_have_text("1.52")
        assert metric.evaluate("el=>getComputedStyle(el).fontFamily")==body_font
        assert "tabular-nums" in metric.evaluate("el=>getComputedStyle(el).fontVariantNumeric")
        for selector in [".rb-name",".rba",".im-sel",".rm-k"]:
            assert page.locator(selector).first.evaluate("el=>getComputedStyle(el).fontFamily")==body_font
        cdp=page.context.new_cdp_session(page);cdp.send("DOM.enable");cdp.send("CSS.enable")
        node=cdp.send("DOM.querySelector",{"nodeId":cdp.send("DOM.getDocument")["root"]["nodeId"],"selector":'[data-field="own_limit"]'})
        actual=cdp.send("CSS.getPlatformFontsForNode",{"nodeId":node["nodeId"]})["fonts"]
        assert actual and all(not f["isCustomFont"] for f in actual)
        assert not any("Mono" in f["familyName"] for f in actual)
        (tmp_path/"actual-rendered-fonts.json").write_text(json.dumps(actual,ensure_ascii=False))
    assert not external and not errors
    page.screenshot(path=str(tmp_path/(route_name+"-production.png")),full_page=True)
