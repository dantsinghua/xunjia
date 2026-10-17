"""采集端通道的 HTTP 接口。挂在询价后端 /api/im 下。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Query

from relay.side_b.phrase import guard

from . import client
from .address_book import load_entry, save_entry
from .friends import merge_people, private_friends
from .catalog import book_of, contacts_of, org_of, probe_link, status_of
from .commands import key_configured, log, prepare_send
from .uplink import upload_command
from .rfq_assign import RfqAssignError, build_batches
from .oss_catalog import CatalogError, catalog_body, load_qidian_book
from .quote_source import load_for_probe, public_view as quote_public, save_quote
from .store import MODE_LABEL, Store, public_view

router = APIRouter(prefix="/api/im")
store = Store()

_HINTS = {
    "oss": "公网信箱：选一台已上报目录的工作台，再选该工作台上的一个企点号。不用填采集端地址、令牌或回呼地址。同一个企点号不能绑两条公网信箱。",
    "local": "本机：采集端与询价面板在同一台机器。地址一般是 http://127.0.0.1:17600，用 Bearer 令牌。适合单机联调。",
    "lan": "局域网：每台采集机一条。同一台机器可以登录 01，也可以改登 02。但同一个企点号同一时间只能在一台机器上在线——A 机登入 02，会踢掉 B 机已登录的 02。采集端须监听 0.0.0.0 并放行 17600，用 Bearer 令牌。",
    "public": "互联网：每台采集机一条，地址用 https，鉴权是 AppId + HMAC 密钥。账号互斥与局域网相同：后登录的机器踢掉先登录的同一企点号。",
}


def _fail(hint: str, code: int = 400):
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": False, "hint": hint}, status_code=code)


def _blocked(reason: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"ok": False, "blocked": True, "hint": reason, "data": {"blocked": True}},
        status_code=400,
    )


def _oss_fields(link: dict) -> dict:
    block = link.get("oss") or {}
    return {
        "workbench_id": str(block.get("workbench_id") or ""),
        "account_uid": str(block.get("account_uid") or ""),
    }


@router.get("/catalog")
def catalog():
    return catalog_body(store.catalog_rows(), store.catalog_error(), key_ready=key_configured())


@router.get("/links")
def list_links():
    return {"ok": True, "data": store.list_public(), "hints": _HINTS}


@router.post("/links")
def create_link(body: dict = Body(default_factory=dict)):
    try:
        link = store.create(body)
    except ValueError as e:
        return _fail(str(e))
    data = store.list_public()
    created = next(x for x in data["links"] if x["id"] == link["id"])
    return {"ok": True, "data": created}


@router.put("/links/{link_id}")
def update_link(link_id: str, body: dict = Body(default_factory=dict)):
    try:
        store.update(link_id, body)
    except KeyError:
        return _fail("通道不存在", 404)
    except ValueError as e:
        return _fail(str(e))
    data = store.list_public()
    row = next((x for x in data["links"] if x["id"] == link_id), None)
    return {"ok": True, "data": row}


@router.delete("/links/{link_id}")
def delete_link(link_id: str):
    store.delete(link_id)
    return {"ok": True, "data": store.list_public()}


@router.post("/links/{link_id}/test")
def test_link(link_id: str, body: dict = Body(default_factory=dict)):
    link = store.get(link_id)
    if link is None:
        return _fail("通道不存在", 404)
    if link.get("mode") != "oss" and body.get("account_id"):
        try:
            link = store.update(link_id, {"account_id": body["account_id"]})
        except ValueError as e:
            return _fail(str(e))
    if link.get("mode") == "oss":
        result = probe_link(link, store.catalog_rows(), key_ready=key_configured())
    else:
        result = client.probe(link)
    adopted = result.get("adopted_account_id") or ""
    saved = store.save_test(link_id, result, account_id=adopted)
    view = public_view(saved, store.list_public()["active_id"], store.catalog_rows())
    return {"ok": True, "data": {"passed": result["passed"], "steps": result["steps"], "accounts": result["accounts"], "link": view}}


@router.post("/links/{link_id}/activate")
def activate_link(link_id: str):
    try:
        store.activate(link_id)
    except KeyError:
        return _fail("通道不存在", 404)
    except ValueError as e:
        return _fail(str(e))
    return {"ok": True, "data": store.list_public()}


@router.get("/contacts")
def contacts(refresh: int = Query(0)):
    link = store.active()
    if link is None:
        return _fail("还没有测试通过的当前通道", 409)
    if link.get("mode") == "oss":
        fields = _oss_fields(link)
        rows = store.catalog_rows()
        wid = fields["workbench_id"]
        uid = fields["account_uid"]
        # 总索引只有对象路径。通讯录正文在当前企点号自己的对象里，不读微信群。
        cached = load_entry(store.address_book_path, wid, uid)
        cached_org = cached.get("org") or {}
        has_book = bool(cached.get("contacts") or cached_org.get("departments") or cached_org.get("members"))
        if has_book and not refresh:
            got = {"source": "catalog", "contacts": cached["contacts"], "org": cached_org}
        else:
            rel = book_of(rows, wid, uid)
            if rel:
                try:
                    loaded = load_qidian_book(wid, uid, rel)
                except CatalogError as exc:
                    return _fail(str(exc), 502)
                people = loaded["contacts"]
                org = loaded["org"]
            else:
                people = contacts_of(rows, wid, uid)
                org = org_of(rows, wid, uid)
            saved = save_entry(store.address_book_path, wid, uid, people, org)
            got = {"source": "catalog", "contacts": saved["contacts"], "org": saved["org"]}
    else:
        try:
            got = client.fetch_contacts(link)
        except client.ProbeError as e:
            return _fail(str(e), 502)
        got = {**got, "org": {"departments": [], "members": []}}
    if link.get("mode") == "oss":
        fields = _oss_fields(link)
        merged = merge_people(got["contacts"], private_friends(fields["account_uid"]))
        before = [str(item.get("uin") or "") for item in got["contacts"] if isinstance(item, dict)]
        if [item["uin"] for item in merged] != before:
            saved = save_entry(
                store.address_book_path,
                fields["workbench_id"],
                fields["account_uid"],
                merged,
                got.get("org") or {"departments": [], "members": []},
            )
            got["contacts"] = saved["contacts"]
            got["org"] = saved["org"]
    view = store.list_public()
    current = next(x for x in view["links"] if x["id"] == link["id"])
    return {"ok": True, "data": {
        "link_id": link["id"],
        "name": link.get("name"),
        "mode_label": MODE_LABEL.get(link.get("mode"), ""),
        "account_id": "" if link.get("mode") == "oss" else link.get("account_id"),
        "account_uid": current.get("account_uid") or "",
        "account_nick": current.get("account_nick") or "",
        "endpoint": current["endpoint"],
        "source": got["source"],
        "contacts": got["contacts"],
        "org": got.get("org") or {"departments": [], "members": []},
    }}


@router.get("/status")
def status():
    data = store.list_public()
    current = next((x for x in data["links"] if x["active"]), None)
    return {"ok": True, "data": {"current": current, "ready": [x for x in data["links"] if x["tested"]]}}


@router.get("/receipts/{cmd_id}")
def receipt(cmd_id: str):
    cmd = log.get(cmd_id)
    if cmd is None:
        return _fail("指令不存在", 404)
    link = {
        "mode": "oss",
        "oss": {
            "workbench_id": cmd.get("workbench_id") or "",
            "account_uid": cmd.get("account_uid") or "",
        },
    }
    info = status_of(link, store.catalog_rows())
    try:
        view = log.pull_receipt(
            cmd_id,
            stale=bool(info.get("stale")),
            present=bool(info.get("present")),
            demo_ack=info.get("demo_ack"),
        )
    except KeyError:
        return _fail("指令不存在", 404)
    return {"ok": True, "data": view}


def _send_failure(cmd: dict) -> str:
    """对象没写上才是没有上行。写上了但回执失败，用回执里的原因。"""
    ack = cmd.get("ack") if isinstance(cmd.get("ack"), dict) else {}
    reason = str(ack.get("reason") or "").strip()
    if str(cmd.get("status") or "") == "failed" and reason:
        return reason
    return "没有上行"


def _wait_uploaded(cmd: dict) -> tuple[dict, bool]:
    """已提交的指令交给公网信箱。没有上传函数或上传失败时，第二项为假。"""
    from .uplink import upload_command

    uploaded = True
    if cmd.get("status") == "submitted" and not cmd.get("ack"):
        uploaded = bool(upload_command(cmd))
        cmd = log.get(str(cmd.get("cmd_id") or "")) or cmd
    return cmd, uploaded


def _write_oss(link: dict, native_id: str, text: str, idempotency_key: str, session_kind: str = "private"):
    info = status_of(link, store.catalog_rows())
    allow_write = bool(info.get("present") and not info.get("stale"))
    fields = _oss_fields(link)
    return prepare_send(
        log,
        text,
        allow_write=allow_write,
        workbench_id=fields["workbench_id"],
        account_uid=fields["account_uid"],
        native_id=native_id,
        idempotency_key=idempotency_key,
        session_kind=session_kind,
    )


@router.post("/send")
def send(body: dict = Body(default_factory=dict)):
    """向当前通道上指定的人发私聊。公网模式写指令后等到采集端回执。"""
    link = store.active()
    if link is None:
        return _fail("还没有测试通过的当前通道", 409)
    uin = str(body.get("uin") or "").strip()
    text = str(body.get("text") or "").strip()
    if not uin or not text:
        return _fail("须指定联系人和正文")
    key = str(body.get("idempotency_key") or "").strip() or f"xj-{link['id']}-{uin}"
    if link.get("mode") == "oss":
        result = _write_oss(link, uin, text, key, "private")
        if result.get("blocked"):
            return _blocked(result.get("reason") or "红线闸拦截")
        if not result.get("ok"):
            return _fail(result.get("reason") or "工作台未上报")
        cmd, uploaded = _wait_uploaded(result["command"])
        if not uploaded:
            return _fail(_send_failure(cmd))
        return {"ok": True, "data": {
            "status": cmd.get("status") or "submitted",
            "cmd_id": cmd["cmd_id"],
            "account_uid": _oss_fields(link)["account_uid"],
            "uin": uin,
        }}
    judged = guard(text)
    if not judged.allowed:
        return _blocked(judged.reason or "红线闸拦截")
    try:
        sent = client.send_text(link, uin, judged.text, key)
    except client.ProbeError as e:
        return _fail(str(e), 502)
    payload = sent["payload"]
    return {"ok": True, "data": {
        "status": sent["status"],
        "trace_id": payload.get("trace_id"),
        "account_id": link.get("account_id"),
        "uin": uin,
    }}


@router.post("/rfq/send")
def rfq_send(body: dict = Body(default_factory=dict)):
    """按收件人合并报价后写入公网信箱。只走 prepare_send，不直连企点。"""
    try:
        batches = build_batches(body.get("items"))
    except RfqAssignError as exc:
        return _fail(str(exc))
    link = store.active()
    if link is None:
        return _fail("还没有测试通过的当前通道", 409)
    if link.get("mode") != "oss":
        return _fail("公网信箱才写询价指令")
    for batch in batches:
        allowed, reason = log.accept_text(batch["text"])
        if not allowed:
            return _blocked(reason or "红线闸拦截")
    written = []
    for batch in batches:
        key = "rfq-" + uuid.uuid4().hex
        result = _write_oss(link, batch["uin"], batch["text"], key, "private")
        if result.get("blocked"):
            return _blocked(result.get("reason") or "红线闸拦截")
        if not result.get("ok"):
            return _fail(result.get("reason") or "工作台未上报")
        cmd = result["command"]
        uploaded = upload_command(cmd)
        fresh = log.get(str(cmd.get("cmd_id") or "")) or cmd
        status = str(fresh.get("status") or "submitted")
        from .rfq_status import book_for
        if status == "delivered":
            book_for(log.path).mark(batch.get("codes") or [], "待回执")
        elif status == "failed" or not uploaded:
            book_for(log.path).mark(batch.get("codes") or [], "发送失败")
        else:
            book_for(log.path).mark(batch.get("codes") or [], "待回执")
        written.append({
            "uin": batch["uin"],
            "text": batch["text"],
            "status": status,
            "cmd_id": cmd.get("cmd_id") or "",
            "codes": batch["codes"],
            "_uploaded": uploaded,
            "_reason": _send_failure(fresh) if status == "failed" else "",
        })
    visible = [{key: value for key, value in item.items() if not key.startswith("_")} for item in written]
    accepted = bool(visible) and all(item["_uploaded"] for item in written) and all(
        item["status"] == "delivered" for item in visible
    )
    if accepted:
        return {"ok": True, "data": {"batches": visible}}
    if any(item["status"] == "failed" for item in visible):
        hint = next((item["_reason"] for item in written if item["_reason"]), "发送失败")
    elif not all(item["_uploaded"] for item in written):
        hint = "没有上行，也没有回执" if all(item["status"] == "submitted" for item in visible) else "没有上行"
    else:
        hint = "没有回执"
    return {"ok": False, "hint": hint, "data": {"batches": visible}}


@router.get("/rfq/status")
def rfq_status():
    """Legacy tooling projection; production applies owner-scoped session state."""
    from .quote_last import quotes_for_status
    from .rfq_status import book_for

    states = book_for(log.path).ensure(log.list_all())
    quotes = quotes_for_status(list(states))
    return {"ok": True, "data": {"items": [
        {"code": code, "status": status, "quote": quotes.get(code)}
        for code, status in states.items()
    ]}}


@router.delete("/rfq/status/{code}")
def delete_legacy_rfq_status(code: str):
    """Compatibility for offline tooling; not mounted by the production app."""
    from .rfq_status import book_for

    book_for(log.path).delete(code)
    return rfq_status()


@router.get("/quote")
def get_quote():
    """innodealing 行情配置。不返回 sid、cookie、device_id。"""
    return {"ok": True, "data": quote_public()}


@router.put("/quote")
def put_quote(body: dict = Body(default_factory=dict)):
    try:
        saved = save_quote(body)
    except ValueError as exc:
        return _fail(str(exc))
    return {"ok": True, "data": saved}


@router.post("/quote/test")
async def test_quote(body: dict = Body(default_factory=dict)):
    """保存非空机密后，换令牌并试连行情 WebSocket。说明里不带凭据。"""
    try:
        if body:
            save_quote(body)
    except ValueError as exc:
        return _fail(str(exc))
    from collector.qb_gateway import WS, authenticate
    from collector.qb_ws import ws_url
    from .quote_probe import market_connect, probe_quote
    result = await probe_quote(
        creds=load_for_probe,
        authenticate=authenticate,
        connect=market_connect,
        ws_url=ws_url,
        ws_base=WS,
    )
    return {"ok": True, "data": result}



def _floors():
    from .inquiry_rules import FloorBook
    return FloorBook(log.path.with_name("rfq_floors.json"))


def _sessions():
    from .inquiry_rules import SessionGate
    return SessionGate(log.path.with_name("rfq_sessions.json"))


@router.post("/inquiry/floor")
def set_inquiry_floor(body: dict = Body(default_factory=dict)):
    codes = body.get("codes") if isinstance(body.get("codes"), list) else []
    kind = str(body.get("kind") or "bp").strip()
    if kind == "yield":
        try:
            value = round(float(body.get("value")), 2)
        except (TypeError, ValueError):
            return _fail("绝对收益不是数字")
        _floors().set_yield([str(c) for c in codes], value)
        return {"ok": True, "data": {"kind": "yield", "value": value, "codes": codes}}
    try:
        bp = float(body.get("bp"))
    except (TypeError, ValueError):
        return _fail("底价不是数字")
    _floors().set_many([str(c) for c in codes], bp)
    return {"ok": True, "data": {"kind": "bp", "bp": bp, "codes": codes}}


@router.post("/inquiry/floor/widen")
def widen_inquiry_floor(body: dict = Body(default_factory=dict)):
    code = str(body.get("code") or "").strip()
    if not code:
        return _fail("没有债券代码")
    try:
        before, after = _floors().widen(code)
    except KeyError:
        return _fail("还没有底价")
    return {"ok": True, "data": {"before": before, "after": after, "code": code}}


@router.post("/inquiry/session/pause")
def pause_inquiry_session(body: dict = Body(default_factory=dict)):
    peer = str(body.get("peer") or "").strip()
    try:
        _sessions().pause(peer)
    except ValueError as exc:
        return _fail(str(exc))
    return {"ok": True, "data": {"peer": peer, "paused": True}}


@router.post("/inquiry/session/resume")
def resume_inquiry_session(body: dict = Body(default_factory=dict)):
    peer = str(body.get("peer") or "").strip()
    try:
        _sessions().resume(peer)
    except ValueError as exc:
        return _fail(str(exc))
    return {"ok": True, "data": {"peer": peer, "await_inbound": True}}


@router.post("/inquiry/status")
def mark_inquiry_status(body: dict = Body(default_factory=dict)):
    from .rfq_status import book_for
    code = str(body.get("code") or "").strip()
    status = str(body.get("status") or "").strip()
    if not code:
        return _fail("没有债券代码")
    try:
        book_for(log.path).mark([code], status)
    except ValueError as exc:
        return _fail(str(exc))
    return {"ok": True, "data": {"code": code, "status": status}}
