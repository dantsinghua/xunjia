# QTrade V2.2 宿主源码与接线参考

安琳，本目录交付本批新增的两份运行源码、四份独立验收文件，以及既有 QTrade 文件的定点接线说明。它不是完整 QTrade 源码、安装包或可自动应用的通用补丁。复制两个模块本身不能完成装配；既有文件仍需按下文审核集成。

## 来源与限制

来源为 2026-10-17 的本地 QTrade 工作树，Git HEAD 为 `f87304bfb1f63482e1d999f6b507a346537c28f7`，分支 `claude/lucid-dijkstra-uu5max`。实施前已存在大量未提交改动，尤其 `app.py`、`main.py`；该 HEAD 不是实施前完整工作树。

本轮未保留 QTrade 修改前源码副本。`collector-freeze.json`、`collector-all-final-before.json` 是修改后的冻结证据；本轮 `wire_qtrade_v22.py` 只有定点替换，没有保存前像。因此本目录不提供 `.patch`，不声称可以对任意 QTrade 版本运行 `git apply`，也没有以 HEAD 差异夹带原有改动。清单中既有文件的 `base_sha256: null` 表示未知，不能当成空文件或已通过校验。

`manifest.json` 记录六份新增文件的字节 SHA256，以及五份既有文件在本地终验版本的完整 SHA256 和接线符号。新增文件以 `source/` 后的相对路径映射到 QTrade 源码树，逐字节保留。四份验收文件由独立验收方编写，本目录仅复制，未修改断言或跳过条件。不要直接在 xunjia 项目中运行这些 QTrade 用例；它们依赖目标 QTrade 的 `tests/conftest.py`、源码和开发依赖。

## 依赖与配置

先在用于集成的独立 QTrade 副本中核对既有接口，再装配本目录。协议实现由主交付包 `collector-runtime/xunjia_collector_runtime-2.2.0-py3-none-any.whl` 提供；使用 QTrade 专用 Python 环境离线安装并执行 `pip check`，不能覆盖其他服务共享环境中的 `server`、`relay` 包。运行时需要以下接口：

- `relay.protocol.Binding`；`CollectorBridge.publish_native/run_once/publish_capabilities`。
- `ReceiverLedger.begin_inbound/stage_out/permission`；`QTradeNativePort` 的 `execution_permission` 和只读 `native_reconcile`。
- `OSSTransport` 的 V2 通讯录路径为 `wb/{workbench}/out/xunjia/{account}/contacts.bin`，不能覆盖 deposit 的共享 `contacts.json`。
- QTrade 当前 `Events.on_emit`、持久 `events_outbox` 和 `Store.outbox_seq_bounds`；`OssV1Box` 使用现有 STS provider。

`XUNJIA_COLLECTOR_VERSION=2.2` 显式选择 V2；不设置保持 v1，未知值停用询价消费者。`XUNJIA_COLLECTOR_CONFIG_FILE` 指向私有 JSON：

```json
{
  "owner_user_id": "REPLACE_OWNER",
  "account_uid": "REPLACE_ACCOUNT",
  "workbench_id": "REPLACE_WORKBENCH",
  "seal_key_file": "/private/path/seal-key.hex",
  "sign_key_file": "/private/path/sign-key.hex",
  "allow_native_send": false
}
```

密钥文件内容为十六进制，路径相对私有配置文件解析。配置、账本与已有 OSS 工作台身份必须一致；V2 配置缺失或失败不能回落 v1。默认 `allow_native_send=false`；安装和以下合成测试不需要启动账号或启用真实发送。

## 既有文件接线

以下位置来自冻结版本，行号可能随目标分支变化，应按符号和上下文集成。这里给出本批新增逻辑，不复制含原有 dirty 修改的完整既有文件。

### `src/qtrade_agent/bus/bus.py`

导入 `.native_guard` 中的 `NativePermissionChanged`、`native_send_guard`、`require_native_permission`，并在 `Bus` 类声明 `supports_xunjia_v22_guard = True`。只有后续两处检查实际安装后才能声明该能力。

在 `Bus._execute` 的文本发送分支，保留现有账号/闸门检查；`await self._rate_limit(acct.id)` 和 `job.done.done()` 检查之后、创建出向 `SENDING` 行之前加入：

```python
native_guard = getattr(cmd, '_xunjia_native_guard', None)
try:
    require_native_permission(native_guard)
except NativePermissionChanged:
    self._finalize(job, _err_result('GATE_BLOCKED', cmd.trace_id or '',
        CommandError('Native send permission changed', reason='xunjia_native_permission_changed')))
    return
```

在已有 `self.store.ingest`、`job.msg_id = r.id` 之后，将单次 `adapter.send` 放入任务局部 guard 上下文，并保留其后的正常结果处理：

```python
try:
    with native_send_guard(native_guard):
        res = await adapter.send(acct, cmd)
except NativePermissionChanged:
    self.store.mark_out_state(job.msg_id, 'FAILED')
    self._finalize(job, _err_result('GATE_BLOCKED', cmd.trace_id or '',
        CommandError('Native send permission changed', reason='xunjia_native_permission_changed')))
    return
```

`_xunjia_native_guard` 只能由已安装协议 wheel 的 `QTradeNativePort` 在 command factory 返回后附加固定 callable，绑定不可变命令身份与本地 `ReceiverLedger.permission`。不能增加 HTTP/JSON 接收该 callable 的接口。该许可只做本地短事务，不允许网络或模型调用持数据库锁。

### `src/qtrade_agent/adapters/qidian/ui.py`

导入 `from ...bus.native_guard import require_native_permission`。在 `QidianUi._tap_anchor` 中，异步 `_dump` 完成后，真实节点和显式坐标 fallback 两条分支都在对应 `await self._tap(...)` **紧前面**加入：

```python
if name == 'chat_send':
    require_native_permission()
```

仅拦截发送按钮，不改变其他 UI 操作。不能只在导航开始或 OSS 上传前检查；限速或 UI 查询期间收到的新消息必须使最后一次点击被拒绝。

### `src/qtrade_agent/app.py`

保留既有 CRLF。集成位置与顺序如下：

| 符号 | 必需改动 |
| --- | --- |
| `AgentApp.open` | 现有 OSS box 建立后调用 `collector_version()`；非法值写 `_xunjia_version="disabled"` 和 `oss_start_error="xunjia_collector_version_invalid"`。保存 `_xunjia_oss_box=oss_box`，初始化 `xunjia_v22_host=None`。只有 `oss_box is not None and self._xunjia_version == "1"` 才构造原 `XunjiaInbox`。保留 deposit 的原 `CatalogPublisher`。 |
| `xunjia_inbox_tick` | 在 `inbox.poll()` 前，若 `getattr(self, "_xunjia_version", "1") != "1"` 则返回。 |
| `_fanout_webhooks` | 保留既有过滤计算；先调用存在的 `host.observe(**kw)`，用 `finally` 保证原 `self.webhooks.fanout(ignore_filters=ignore, **kw)` 仍执行。保留已有 `Events.on_emit` 到此函数的绑定。 |
| `start` | 顺序为 `await self._configure_saved_ossbox()`、`await self._configure_xunjia_v22()`、原 `await self.scheduler.start()`。不另建第二服务，不在安装步骤调用 `start(recover=True)`。 |
| `_configure_saved_ossbox` | 重新装配前清空 `_xunjia_oss_box`；取到 `saved, box` 后保存 box。只有版本为 `"1"` 才构造旧 inbox；V2 仍保留原 deposit catalog publisher。不能让页面保存或重载配置重新开启 v1。 |
| `stop` | scheduler 停止后、webhook/适配器/Bus/Store 关闭前，若存在 host 则 `await host.stop()`；不能取消线程并伪称原生动作未发生。 |

新增以下两个方法，当前版本的完整方法内容如下：

```python
    def _xunjia_v22_contacts(self, row):
        from .adapters.qidian.contacts import list_private_contacts
        if row.get("state") != "running" or not row.get("self_uid"):
            raise RuntimeError("collector_contacts_account_not_running")
        account = Account(id=row["id"], channel="qidian", state=row["state"], self_uid=row["self_uid"],
            extra={"adb_serial": row.get("adb_serial") or f"127.0.0.1:{16000 + int(row['seq'])}"})
        source = self._maindb_for_uid(str(row["self_uid"]), account)
        if not source.exists():
            raise RuntimeError("collector_contacts_source_unavailable")
        return {"contacts": list_private_contacts(source.list_contact_source_rows())}

    async def _configure_xunjia_v22(self) -> None:
        if getattr(self, "_xunjia_version", "1") != "2.2":
            return
        from .ossbox.xunjia_v22 import XunjiaV22Host
        try:
            if self.xunjia_inbox is not None:
                raise RuntimeError("legacy_xunjia_inbox_must_be_disabled")
            if self.xunjia_v22_host is None:
                self.xunjia_v22_host = XunjiaV22Host.from_env(
                    store=self.store, bus=self.bus, loop=asyncio.get_running_loop(),
                    box=self._xunjia_oss_box, data_dir=self.data_dir,
                    legacy_inbox_disabled=lambda: self.xunjia_inbox is None,
                    contacts_provider=self._xunjia_v22_contacts)
            if self.xunjia_v22_host is not None:
                saved = self.oss_settings.saved()
                if saved and saved.get("workbench_id") != self.xunjia_v22_host.binding.workbench_id:
                    self.xunjia_v22_host = None
                    raise ValueError("collector_workbench_binding_mismatch")
                await self.xunjia_v22_host.start()
        except Exception:
            self.oss_start_error = "xunjia_v22_configuration_unavailable"
            log.error("V2.2 collector unavailable; legacy inbox remains disabled")
```

`_maindb_for_uid` 使用现有企点旁路只读来源；通讯录不可用时保留先前目录，不能启动/恢复账号来完成安装。

### `src/qtrade_agent/ossbox/routes.py`

`register_ossbox_settings` 内 `view()` 返回值继续保留原条件，分别加 `or getattr(agent, 'xunjia_v22_host', None) is not None`：

```python
"inquiry_configured": agent.xunjia_inbox is not None or getattr(agent, 'xunjia_v22_host', None) is not None,
"catalog_configured": agent.catalog_publisher is not None or getattr(agent, 'xunjia_v22_host', None) is not None
```

### `src/qtrade_agent/main.py`

本批全仓测试还修复 `init_db` 新目录权限被 umask 裁剪的问题。只修改创建 parent 的局部逻辑，保留原外层 `OSError` 错误返回和后续 Store 初始化：

```python
try:
    os.makedirs(parent, mode=DB_DIR_MODE)
except FileExistsError:
    if not os.path.isdir(parent):
        raise
else:
    os.chmod(parent, DB_DIR_MODE)
```

`DB_DIR_MODE` 为既有 `0o750`；不修改进程 umask，不 chmod 既有目录。对应独立测试 `test_init_db_permissions.py` 必须保留四个场景。

## 集成验证及验收边界

在独立 QTrade 集成副本中逐个核对 `manifest.json` 的文件哈希。已有文件的完整 result hash 只标识本地终验版本；目标若使用不同合法基线，不应为了凑 hash 而覆盖其他修改，应单独评审、测试并记录新 hash。

完成上述接线并安装协议 wheel 后，在 QTrade 根目录使用其隔离测试 Python 运行：

```sh
python -m pytest tests/acceptance/test_xunjia_v22_native_guard.py tests/acceptance/test_xunjia_v22_host.py tests/acceptance/test_xunjia_v22_app_wiring.py tests/acceptance/test_init_db_permissions.py -q
python -m pytest tests/test_init_db.py -q
python -m pytest tests -q
```

本地冻结版本历史结果为：独立采集门 22 项、目录权限 4 项及实际安装协议 wheel 的跨仓联合 3 项通过；完整 Agent 总门 3309 passed / 0 failed / 0 skipped，输入前后零漂移。该全量结论针对当时的完整本地工作树，不能直接转移为“本目录可在任意 QTrade 版本通过”。

本目录这次交付验证只证明六份新增文件与已验冻结版本逐字节一致、Python 语法可解析及既有五份文件仍匹配冻结 hash，没有执行自动应用，也没有更改 QTrade 工作树。缺少修改前完整副本，故没有 `git apply --check` 证据。正式 QTrade 安装 EXE、真实 STS/OSS、真实账号和原生发送仍需在另行授权环境验收。
