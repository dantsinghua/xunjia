"""Real Monitor navigation and polling against bounded synthetic chat pages."""
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect


def page_payload(prefix, count, next_cursor):
    return {"messages": [{"id": f"{prefix}-{i}", "role": "peer", "dir": "in", "text": f"{prefix}-{i}",
                          "time": "10:00:00", "landed": True} for i in range(count)],
            "nextCursor": next_cursor, "hasMore": bool(next_cursor)}


def test_monitor_keeps_one_history_page_during_polling_and_can_return_to_latest(ui):
    page, url, state, requests, _ = ui
    state["chat_pages"] = {"": page_payload("最新页", 50, "opaque-page-two"),
                           "opaque-page-two": page_payload("历史第二页", 50, "opaque-page-three"),
                           "opaque-page-three": page_payload("历史末页", 5, None)}
    page.clock.install()
    page.goto(url + "/#/monitor")
    expect(page.locator(".bubble")).to_have_count(50)
    expect(page.get_by_text("最新页-49", exact=True)).to_be_visible()
    page.get_by_role("button", name="更早50条", exact=True).click()
    expect(page.get_by_text("历史第二页-49", exact=True)).to_be_visible()
    expect(page.locator(".bubble")).to_have_count(50)
    expect(page.get_by_text("最新页-49", exact=True)).to_have_count(0)
    before_poll = len(requests)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/monitor/chat"):
        page.clock.run_for(3500)
    expect(page.get_by_text("历史第二页-49", exact=True)).to_be_visible()
    polled = [request for request in requests[before_poll:] if urlsplit(request.url).path == "/api/monitor/chat"]
    assert polled and all(parse_qs(urlsplit(request.url).query).get("cursor") == ["opaque-page-two"] for request in polled)
    page.get_by_role("button", name="更早50条", exact=True).click()
    expect(page.get_by_text("历史末页-4", exact=True)).to_be_visible()
    expect(page.locator(".bubble")).to_have_count(5)
    expect(page.get_by_role("button", name="更早50条", exact=True)).to_be_disabled()
    page.get_by_role("button", name="回到最新消息", exact=True).click()
    expect(page.get_by_text("最新页-49", exact=True)).to_be_visible()
    expect(page.locator(".bubble")).to_have_count(50)
    assert not any(request.method == "POST" for request in requests)
