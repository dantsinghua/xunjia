"""开始询价：左侧只留可编辑正文，右侧是通讯录。不点确认发送。"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent / "out"
URL = "http://127.0.0.1:5190/#/quote"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(URL, wait_until="domcontentloaded")
        expect(page.get_by_role("combobox")).to_contain_text("3007378246", timeout=20000)
        boxes = page.locator("tbody input[type=checkbox]")
        boxes.nth(0).click()
        boxes.nth(1).click()
        page.get_by_role("button", name="开始询价").click()
        modal = page.get_by_test_id("rfq-modal")
        expect(modal).to_be_visible()
        text = modal.get_by_test_id("rfq-text")
        expect(text).to_be_visible()
        expect(text).not_to_have_value("")
        expect(modal.locator("input[data-field]")).to_have_count(0)
        read = page.get_by_test_id("rfq-read-book")
        if read.count() and read.is_visible() and read.is_enabled():
            read.click()
        expect(modal.get_by_text("当前号通讯录")).to_be_visible()
        expect(modal.get_by_text("组织架构")).to_be_visible(timeout=90000)
        modal.get_by_placeholder("按姓名或号码筛选").fill("415011447")
        person = modal.locator(".person", has_text="415011447")
        expect(person).to_be_visible()
        person.click()
        modal.get_by_test_id("rfq-assign-all").click()
        expect(modal.get_by_text("1 人")).to_have_count(2)
        modal.screenshot(path=str(OUT / "rfq-modal.png"))
        browser.close()
    print(OUT / "rfq-modal.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
