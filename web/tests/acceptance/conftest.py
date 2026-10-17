"""Real Vue entrypoint against intercepted synthetic APIs; no business server."""
from collections import Counter
import json
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def frontend_url(tmp_path_factory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log = (tmp_path_factory.mktemp("vite") / "server.log").open("w")
    process = subprocess.Popen(
        ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
        cwd=ROOT / "web", stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("Dedicated Vite server exited; inspect pytest Vite log")
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("Dedicated Vite server did not become ready")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def ui(browser, frontend_url):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    context.add_init_script("localStorage.setItem('xj_auth_token', 'synthetic-qa-token')")
    page = context.new_page()
    page.set_default_timeout(10000)
    page.set_default_navigation_timeout(20000)
    requests = []
    counts = Counter()
    state = {"answer": "**真实回答标记**", "bot_error": None, "chat_error": False,
             "auth_status": 200, "qa_status": 200, "bond_detail": {}, "rfq_enabled": False,
             "rfq_status": "queued", "rfq_fail_first": False, "rfq_stale": False,
             "reconciled": False, "reconcile_error": False}
    bond = {"code": "000100.IB", "name": "合成债券", "tags": [], "status": "待询价"}

    def route(request_route):
        request = request_route.request
        parsed = urlsplit(request.url)
        if not request.url.startswith(frontend_url + "/"):
            request_route.abort()
            return
        if not parsed.path.startswith("/api/"):
            request_route.continue_()
            return
        requests.append(request)
        counts[parsed.path] += 1
        if parsed.path == "/api/bonds/export" and state.get("export_bytes") is not None:
            request_route.fulfill(status=200, body=state["export_bytes"], headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Content-Disposition": 'attachment; filename="synthetic-export.xlsx"',
            })
            return
        status = 200
        data = {"ok": True, "data": []}
        if parsed.path == "/api/auth/me":
            status = state["auth_status"]
            data["data"] = {"user_id": "qa-trader", "display_name": "验收交易员", "account_uid": "910001", "binding": {"account_uid": "910001"}}
            if status != 200:
                data = {"ok": False, "hint": "合成身份已失效"}
        elif parsed.path == "/api/qa":
            status = state["qa_status"]
            data["data"] = {"answer": state["answer"]}
            if status != 200:
                data = {"ok": False, "hint": "合成身份已失效"}
        elif parsed.path == "/api/strategy":
            if request.method == "POST":
                if state.get("strategy_error"):
                    data = {"ok": False, "hint": "合成策略保存被拒绝"}
                else:
                    state["saved_strategy"] = request.post_data_json
            if data.get("ok"):
                saved = state.get("saved_strategy")
                data["data"] = {"configured": bool(saved), "templates": [{k: v for k, v in saved.items() if k != "system"}] if saved else [],
                                "active_template_id": saved["template_id"] if saved else None, "system": saved.get("system", {}) if saved else {}}
        elif parsed.path == "/api/monitor/summary":
            data["data"] = {"bot": {"status": "正常"}, "stats": []}
        elif parsed.path == "/api/monitor/counterparties":
            data["data"] = state.get("counterparties_data", [{"id": "qa-peer", "name": "合成对手甲", "bonds": [{"code": "000100.IB", "name": "合成债券", "stage": "询价中"}]}])
        elif parsed.path == "/api/monitor/chat":
            if state["chat_error"]:
                status, data = 503, {"ok": False, "hint": "合成查询故障"}
            elif "chat_pages" in state:
                cursor = parse_qs(parsed.query).get("cursor", [""])[0]
                data["data"] = state["chat_pages"][cursor]
            else:
                data["data"] = [{"id": "msg-1", "role": "peer", "text": "最后成功消息-独立验收", "bond": "000100.IB", "tag": "收到", "time": "10:00:00"}]
        elif parsed.path == "/api/im/commands/unknown":
            data["data"] = [] if state["reconciled"] else [{"cmd_id": "cmd-unknown", "peer_id": "qa-peer", "text": "待核对的唯一合成正文",
                "created_at": 1_800_000_000, "expected_result_hash": "review-hash-original", "conflict": True}]
        elif parsed.path == "/api/im/receipts/cmd-unknown/reconcile":
            if state["reconcile_error"]:
                data = {"ok": False, "hint": "独立核对被拒绝"}
            else:
                state["reconciled"] = True
                data["data"] = {"cmd_id": "cmd-unknown", "status": request.post_data_json["status"], "adjudicated": True}
        elif parsed.path == "/api/bot/toggle":
            status = state["bot_error"] or 503
            data = {"ok": False, "hint": "独立验收-拒绝停止"}
        elif parsed.path == "/api/im/links":
            data["data"] = {"active_id": "", "links": []}
            if state["rfq_enabled"]:
                data["data"] = {"active_id": "qa-link", "links": [{
                    "id": "qa-link", "name": "合成当前号", "mode": "oss", "mode_label": "公网信箱",
                    "account_uid": "910001", "workbench_id": "qa-wb", "active": True,
                    "tested": True, "catalog_stale": state["rfq_stale"], "contacts": [{"uin": "910100", "name": "合成收件人", "remark": ""}],
                    "form": {"oss": {"account_uid": "910001", "workbench_id": "qa-wb"}},
                }]}
        elif parsed.path == "/api/im/contacts":
            data["data"] = {"contacts": [{"uin": "910100", "name": "合成收件人", "remark": ""}],
                            "source": "contacts", "org": {"departments": [], "members": []}}
        elif parsed.path == "/api/im/catalog":
            data["data"] = {"workbenches": []}
        elif parsed.path == "/api/bonds":
            data["data"] = [bond]
            data.update(total=1, hasMore=False, nextBefore=0)
        elif parsed.path == "/api/bonds/resolve":
            data["data"] = [bond]
        elif parsed.path in ("/api/im/rfq/status", "/api/im/inquiry/floor"):
            data["data"] = {"items": []}
        elif parsed.path == "/api/im/rfq/preview":
            body = request.post_data_json
            authorized = [{**item, "peer_id": peer} for item in body["items"] for peer in item["peer_ids"]]
            data["data"] = {"batch_id": "batch-synthetic-fixed", "version": 7, "status": "DRAFT", "items": authorized,
                            "commands_preview": [{"peer_id": "910100", "text": "SERVER-ONLY-服务端最终首句"}]}
        elif parsed.path == "/api/im/rfq/send":
            if state["rfq_fail_first"] and counts[parsed.path] == 1:
                status, data = 503, {"ok": False, "hint": "合成确认响应丢失"}
            else:
                data["data"] = {"batch_id": "batch-synthetic-fixed", "commands": [{
                    "command_id": "cmd-synthetic-fixed", "peer_id": "910100", "status": state["rfq_status"],
                }]}
        elif parsed.path == "/api/bond/000100.IB":
            data["data"] = state["bond_detail"]
        elif parsed.path == "/api/bonds/export":
            data = {"ok": False, "hint": "合成导出不可用"}
        request_route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))

    page.route("**/*", route)
    # An intercepted WebSocket is mocked unless connect_to_server is called.
    # Calling the sync close method inside this callback deadlocks Playwright
    # 1.63, so leave an inert local peer instead of forwarding to a server.
    page.route_web_socket("**/*", lambda websocket: None)
    try:
        yield page, frontend_url, state, requests, counts
    finally:
        context.close()
