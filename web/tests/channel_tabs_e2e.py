"""意向询价渠道页签：最优有数，QT/做市商为空，经纪商可单选一家。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent / "out"
URL = "http://127.0.0.1:5190/#/quote"
def shot(page, name: str) -> None:
    page.locator(".table-scroll").evaluate("el => { el.scrollLeft = 720 }")
    page.screenshot(path=str(OUT / name))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 2400, "height": 900})
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_selector(".mkt-row", timeout=20000)
        expect(page.get_by_test_id("chan-best")).to_have_class(re.compile(r"\bon\b"))
        expect(page.get_by_test_id("broker-select")).to_have_count(0)
        shot(page, "channel-best.png")

        page.get_by_test_id("chan-qt").click()
        note = page.get_by_test_id("channel-note")
        expect(note).to_contain_text("QT意向报价尚未接入")
        expect(page.locator(".mkt-row")).to_have_count(0)
        page.screenshot(path=str(OUT / "channel-qt.png"))

        page.get_by_test_id("chan-mm").click()
        expect(page.get_by_test_id("channel-note")).to_contain_text("做市商报价尚未接入")
        expect(page.locator(".mkt-row")).to_have_count(0)
        page.screenshot(path=str(OUT / "channel-mm.png"))

        page.get_by_test_id("chan-broker").click()
        expect(page.get_by_test_id("broker-current")).to_have_text("经纪商最优")
        page.wait_for_selector(".mkt-row", timeout=20000)
        expect(page.get_by_test_id("channel-note")).to_have_count(0)
        shot(page, "channel-broker-best.png")

        page.get_by_test_id("broker-current").click()
        guoli = page.locator("[data-testid=broker-option][data-broker-id='9']")
        expect(guoli).to_have_text("国利")
        expect(page.get_by_test_id("broker-option")).to_have_count(7)
        for name in ["经纪商最优", "国际", "中诚", "平安", "上田", "信唐", "国利"]:
            expect(page.get_by_test_id("broker-option").filter(has_text=name)).to_be_visible()
        page.screenshot(path=str(OUT / "channel-broker-menu.png"))
        with page.expect_response(lambda r: "channel=broker" in r.url and "broker=9" in r.url) as resp:
            guoli.click()
        body = resp.value.json()
        assert body["total"] > 0
        assert {x.get("obs") or x.get("oos") for x in body["data"]} == {"国利"}
        expect(page.get_by_test_id("broker-current")).to_have_text("国利")
        page.wait_for_function(
            """() => {
              const pills = [...document.querySelectorAll('.mkt-row .side-pill')]
                .map(el => (el.textContent || '').trim())
                .filter(t => t && t !== '—')
              return pills.length > 0 && pills.every(t => t === '国利')
            }""",
            timeout=10000,
        )
        shot(page, "channel-broker-one.png")
        browser.close()
    print("e2e ok", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
