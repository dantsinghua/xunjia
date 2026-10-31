# 企点引用回复传到询价端与 Dify：入站、存储、投影合同（2026-10-28）

安琳，本文件是询价端承接「企点引用回复（`reply`）」的实现合同，供实现方照做、测试与验收方对照。只写已定内容；代码现实与裁决冲突或裁决未覆盖之处列在 §13 张力清单（2026-10-28 总控已逐条裁定，见 `DECISION-2.md` 附录 A；第二轮 N1～N7 与实现期 C1～C3 见附录 B、C，其中 N7 已作废；T10 为开放项，C3 并入），实现方不得自行扩大设计，先回报总控。

## 1. 来源与所有权

- **裁决来源**：采集端裁决 **R6-117**（采集端仓库 `docs/00-共享基线与口径.md` §15g）；总控裁决草案 `DECISION-2.md`（采集端仓库 `.codex/qidian-reply-quote-20261028/`）；前置 R6-116（采集端读库采集 `reply`，已部署并真实验收通过）。本仓库没有 R6 裁决表，本文与上述来源冲突时以 R6-117 / DECISION-2 为准并回报总控。
- **线上字段形状的唯一出处**：采集端仓库 `docs/06-邮件摆渡与消息存取.md` §2.9.5「引用对象 `reply`」条末子条「传到询价端（R6-117）」。本文 §2 是它在询价端的抄录，不另立口径。
- **快照**：本文依据询价端 `main` HEAD `1a248f2`（已跟踪文件干净）撰写，下文行号均为该提交的约略行号。实现开工前若相关文件哈希已变，先重新比对再动手。

| 部分 | owner | 本轮 |
|---|---|---|
| 产出 `reply` 键 | 采集端 `src/qtrade_agent/ossbox/xunjia_v22.py::observe` | 采集端另行实现 |
| 共享 wheel `xunjia_collector_runtime` 2.2.12 的 18 个文件（含 `relay/qtrade_native_port.py`、`relay/institution_archive.py`）、`relay/protocol.py`、ibquote | 共享 | **一律不改** |
| 入站受限校验 | 本仓 `server/negotiation/runtime.py::Inbox._apply` | 改 |
| 存储 | 本仓 `server/negotiation/store.py`、`server/negotiation/schema.py`、`sql/oceanbase/004_message_reply.sql`（新）、`sql/oceanbase/001_xunjia_schema_mysql.sql`（重新生成） | 改 |
| 健康检查（`reply_json` 为可选列，§4.4） | 本仓 `server/health.py` | 改 |
| 消息行解析进上下文 | 本仓 `server/negotiation/store.py`、`server/negotiation/engine.py`、`server/negotiation/commands.py`（`_handoff_evidence`，§13 C1） | 改 |
| Dify 投影、提示词、导出件 | 本仓 `dify/negotiation_v22/node_code.py`、`prompts.py`、`README.md`、`dify/chatflow_0153/v22-*.yml`、`dify/chatflow_1171/v22-*.yml` | 改 |
| 历史归档 `relay/institution_archive.py` | 共享 wheel | 不改、不带引用 |

## 2. 线上字段形状与版本兼容

**位置**：采集端发布的 v2 `kind="event"` 信封的 `payload.reply`。

**存在规则**：只对企点通道事件恒存在（无引用为 `null`）；其它通道不加该键（本仓 `Inbox._apply` 本就只收企点）。

**形状**：`"reply": null`，或恰好七个键的对象（键恒在，除 `state` 外值可为 `null`）：

| 键 | 取值 | 采集端规则 |
|---|---|---|
| `state` | `"ok"` / `"unparsed"` | 取自采集端消息视图 `reply.state` |
| `text` | 字符串或 `null` | 被引用消息的**客户端摘要**，≤ 2000 字符，**可能被截断** |
| `source_text` | 字符串或 `null` | 关联到的原消息全文：只经 `resolved_message_id` 读同账号、同会话、同 `identity_epoch` 的原消息 `text`，超过 4000 字符截到 4000；读不到、校验不符或为空 → `null`；不按文本或时间猜 |
| `source_direction` | `"in"` / `"out"` / `null` | 原消息已关联 → 取其 `dir`；未关联但 `sender_id` 非 `null` → 等于本企点号 `self_uid` 为 `"out"`，否则 `"in"`；都没有 → `null` |
| `sender_id` | 字符串或 `null` | 被引用消息发送者 uin |
| `ts` | ISO 8601 +08:00 字符串或 `null` | 被引用消息时间 |
| `source_ext_msg_id` | `"qd:<uniseq>"` 或 `null` | 被引用消息的原生 ID，与本仓 `xj2_message.native_id` 同一取值空间（`Inbox._apply` 约 77 行 `native = payload.ext_msg_id …`） |

- `state="unparsed"` 时其余六键全部 `null`。
- 采集端补键过程任何异常 → `reply=null`，不影响其原有发布；采集端事件 payload 的 `reply` 既非 `null` 也非对象 → 线上 `reply` 置 `null`，按原因每进程告警一次（§13 T8）。
- `reply` **不进**采集端 `text`、`text_hash`、`native_key`、`event_id`；同一原生消息只因 `reply` 不同不会产生 edit/retract 事件。
- 信封层 `payload_hash`/`content_hash`（共享 wheel `relay/protocol.py::envelope`，`digest(payload)`）覆盖整个 payload、含 `reply`，属传输完整性校验；采集端 `host_event` body 写入后不变，重发同一 body 不受影响。它不是本文所说的 `xj2_message.content_hash`。
- **历史不带**：采集端历史回补（`history_native`/`history_publish`）与本仓历史归档不带引用（理由见 §9）。

**版本兼容**：

| 采集端 | 询价端 | 行为 |
|---|---|---|
| 旧（不发该键） | 新 | 缺键 = `null`，入库 `reply_json` 为 NULL，投影不出 `reply` |
| 新（发该键） | 旧 | 旧 `Inbox._apply` 不读该键，`relay/protocol.py` 不拒未知键（采集端已实测），行为与改前一致 |
| 新 | 新 | 按 §3～§7 处理 |

## 3. `Inbox._apply` 受限校验（`server/negotiation/runtime.py`）

- **位置**：在 `kind == 'event'`、群聊已丢弃（约 68-69 行）、`direction=='out' and source_command` 回执分支（约 79-84 行，该分支不入 `xj2_message`、不处理引用）之后，紧挨 `store.ingest(...)` 调用（约 105 行）之前，从 `payload` 取 `reply`，经下列校验得到 `reply`（对象或 `None`），以新关键字参数 `reply=` 传给 `store.ingest`。
- **校验规则**（2026-10-28 总控裁定 T6、T7；🔴 **不得抛异常、不得隔离或拒收本条事件**）：
  1. **整个 `reply` 为 `None`**：缺键或值为 `None`；值不是 `dict`；`state` 缺失或不在 `{"ok", "unparsed"}` 内。
  2. **只取七键、忽略多余键**：只读 `{state, text, source_text, source_direction, sender_id, ts, source_ext_msg_id}`，其它键丢弃，不传下、不入库；`state` 以外的键缺失 → 该键 `None`。
  3. `state == "unparsed"` → 其余六键一律 `None`（不论传入值为何）。
  4. `state == "ok"` 时逐键校验，**单键类型或长度不符 → 仅该键 `None`**，其余键照常保留：`text`：`str` 且长度 ≤ 2000；`source_text`：`str` 且长度 ≤ 4000；`sender_id`、`source_ext_msg_id`：`str` 且长度 ≤ 128；`ts`：`str` 且长度 ≤ 64（超长视为 `None`）；`source_direction` ∈ `{"in", "out"}`；以上各键值为 `None` 时保持 `None`。
  5. 类型判断用 `type(x) is str`，不接受布尔、数字等冒充。
- 输出：`None`，或**恰为七键**的对象（键恒在）；合格键按原值传下、不改写、不截断，不合格或缺失的键为 `None`。
- 校验函数建议放在 `runtime.py` 模块级（例如 `_reply_wire(value)`），便于单测直接调用；命名不作合同约束。

## 4. 存储

### 4.1 列定义

- `xj2_message` 新增可空列 `reply_json`，内容为经 §3 校验输出的七键对象经 `store.encoded()` 序列化后的 JSON 文本；无引用为 NULL。
- **本地 SQLite / 新建库**：`server/negotiation/schema.py::statements()` 的 `xj2_message` DDL 增加 `reply_json {text}`（SQLite 为 `TEXT`、MySQL 为 `LONGTEXT`），放在唯一约束之前；不加 `NOT NULL`、不加默认值。改后用 `scripts/export_oceanbase_schema.py --write` 重新生成 `sql/oceanbase/001_xunjia_schema_mysql.sql`（验收测试 `tests/acceptance/test_oceanbase_schema_sql_20261015.py` 要求两者逐字一致）。
- **已有 OceanBase 库**：新增 `sql/oceanbase/004_message_reply.sql`，由 DBA 手工执行（先例 `003_thread_workbench.sql`），内容：

```sql
-- Explicit OceanBase MySQL migration; execute only in the verified xunjia_* schema.
-- Preserve a backup and SHOW CREATE TABLE xj2_message before any DDL.
-- On repeat runs, skip this ADD COLUMN if INFORMATION_SCHEMA.COLUMNS already has it:
--   SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
--   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='xj2_message' AND COLUMN_NAME='reply_json';
-- NULL means no captured quote (no quote, legacy row, or row stored before this column). Never backfill.
ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;
```

- 该迁移只加列，不建索引、不改其它列、不回填、不重建表。执行后核对：上面的查询返回一行，`DATA_TYPE=longtext`、`IS_NULLABLE=YES`。

### 4.2 列缺失降级

- 运行库没有 `reply_json` 列（DBA 未执行 004，或已有本地 SQLite 库）时：入库照常、不存引用，**进程内告警一次**（不含引用内容；首次探测发现缺列即告警，不论本条是否带引用，§13 C2）；不得因此拒收、隔离或卡住入站，也不得让 `INSERT` 因列不存在而失败回滚。
- 建议做法（非合同约束）：`Store` 实例首次需要时探测一次列是否存在并缓存——SQLite 用 `PRAGMA table_info(xj2_message)`，MySQL 用 `SHOW COLUMNS FROM xj2_message LIKE 'reply_json'`（与 `server/health.py::schema_health` 同一手法）；探测结果决定 `INSERT`/补写语句是否带该列。按此做法，DBA 执行 004 后需重启询价端进程才开始存引用，部署步骤须写明。
- 列缺失时读出的行没有 `reply_json`，按无引用处理（§5）。
- 已有 SQLite 库不自动 `ALTER`，走本节降级；README 给出手工补列语句 `ALTER TABLE xj2_message ADD COLUMN reply_json TEXT;`（SQLite 须写 `TEXT`，§13 T2）。
- 列缺失不影响就绪：健康检查对 `reply_json` 缺失只报 warning（§4.4）。

### 4.3 写入与重放（`server/negotiation/store.py::ingest`，约 323-392 行）

- 签名增加关键字参数 `reply=None`（`dict` 或 `None`；非 `dict` 一律按 `None`，`ingest` 不因 `reply` 抛异常）。
- 🔴 **不进 `content_hash`**：约 342-344 行计算 `digest` 的 `payload` 字典**不加** `reply`；有无引用、引用内容不同，`content_hash` 完全相同。
- 🔴 **不进重放比对**：约 349-360 行 `previous['content_hash'] != digest` 判定与历史等价重放的 `fields=('text','direction','kind','native_id','occurred_at','trade_day')` **不加** `reply_json`；只因 `reply` 不同不得触发 `Replayed event ID has different native content`。
- **新行**（约 373-376 行 `INSERT INTO xj2_message`）：列可用时一并写 `reply_json`（`reply` 为 `None` 写 NULL）。
- **NULL 可补写，非 NULL 不改**：同一 `event_id` 已有行并按原逻辑返回既有 `message_id` 时（`content_hash` 相同的直接返回分支，以及约 354-359 行历史等价重放返回分支），若列可用、既有行 `reply_json IS NULL` 且本次 `reply` 非 `None`，执行 `UPDATE xj2_message SET reply_json=? WHERE message_id=? AND reply_json IS NULL`；既有行非 NULL 一律不动。补写只改这一列：不改 `content_hash`、`ingest_revision`、`source_seq`、`xj2_thread` 任何列，不重新排队分析。
- 抛 `Replayed event ID has different native content` 的分支、出向 echo 分支（约 362-367 行按 `native_id` 命中既有 out 行）**不补写**（echo 行来自本系统发出的命令，不带引用；见 §13 T9）。
- `server/negotiation/commands.py` 约 353 行由回执直接插入的 out 行不带引用，保持原样（列为 NULL）。

### 4.4 健康检查：`reply_json` 为可选列（`server/health.py`，§13 T1）

- `server/health.py::schema_health` 以 `schema.statements()` 生成期望列；`reply_json` 进 `schema.py` 后，对 `xj2_message.reply_json` **缺失只报 warning**（在返回结果中列出，键名不作合同约束），**不计入** `missing_columns`、**不计** `SCHEMA_INCOMPLETE`。
- warning 只进 `schema_health` 返回结果的 warnings，**不进** readiness 的 `reasons`（§13 N3）。键名由实现方沿用 `health.py` 既有命名风格：现行为键 `warnings`（列表，键恒在、无 warning 时为空列表；缺列时含 `OPTIONAL_COLUMN_MISSING:xj2_message.reply_json`），见实现交接与 §13 C2。
- 因此在其它条件满足时 `/api/health/ready` 仍返回 200，`python -m server.manage check` 不因此失败。
- 只豁免「缺失」（§13 N1）：`reply_json` 列存在但类型不符（例如 SQLite 手工补列写成 `LONGTEXT`）仍按既有严格判定计 `invalid_columns`、报 `SCHEMA_INCOMPLETE`。
- 其它列（含 `xj2_message` 其它列与其它表）的既有判定不变：缺失仍计 `missing_columns`、类型不符仍计 `invalid_columns`、索引判定照旧。
- 现有调用方 `server/manage.py`（`check`）、`deploy/oceanbase.py`、`deploy/database_upgrade.py::verify_fresh` 直接沿用 `schema_health` 的结果，本合同不改这三个文件（影响见 §13 T1 注）。`deploy/oceanbase.py`、`deploy/database_upgrade.py` 本轮不改（§13 N2）：它们不会因缺 `reply_json` 提示执行 004 或失败；004 作为 DBA 升级步骤显式写在 §4.1 与 §10.2，漏执行时由健康检查 warning 可见。

## 5. 消息行进入上下文：解析 `reply_json`

- 快照与上下文继续沿用 `SELECT *` / `SELECT m.*`。新增一个行转换辅助函数（建议放 `store.py`，例如 `Store.message_row(row)`，命名不作约束）：把行字典中的 `reply_json`（字符串或 NULL，列缺失时不存在）解析为键 `reply`（七键对象或 `None`），并从行字典里删除 `reply_json` 原串。解析失败、不是对象或键集合不是七键 → `reply=None`（不抛异常）；恰为七键时再按 §3 逐键规范化（`state` 非法整体 `None`、单键不符该键 `None`，§13 C2）。
- 必须经过该转换的读取点（即全部会进入 Dify `context_json` 的消息行）：

| 文件 | 函数（约略行号） | 进入的上下文 |
|---|---|---|
| `server/negotiation/store.py` | `_eligible_messages`（109-111） | A 快照 `messages` |
| `server/negotiation/store.py` | `_context`（470-485） | A `members[].context`；B `context` |
| `server/negotiation/store.py` | `context`（497 起，含其调用的 `_history_message` 锚点行） | `history_results[].messages` |
| `server/negotiation/engine.py` | `analyze_thread` 内纠错锚点 `correction_context`（564-567） | A `correction_context` |
| `server/negotiation/engine.py` | `_reply_context` 的 `unresolved`（686） | B `unresolved_messages`（及由其选出的 `current_inbound_message`） |
| `server/negotiation/commands.py` | `_handoff_evidence`（170-176，行取自 `store._history_message`；改走 `Store.message_row`，§13 C1） | B `handoff_evidence`（经 `engine._reply_context`）；服务端上下文不再携带 `reply_json` 原串，`node_code.py` 投影层对 `reply`/`reply_json` 的拦截保留作第二道防线 |

- 快照（`xj2_analysis.snapshot_json`）里保存解析后的对象；租约恢复沿用旧快照，旧快照行无 `reply` 键按 `None` 处理。
- 送 Dify 的 `context_json` 中，消息行带的是七键对象 `reply`；它只供 §6 投影使用，**不直接给模型**。
- 不经 Dify 的读取点（`commands.py` 约 64 行、`application_store.py::raw_page` 约 138 行等）本轮不改（`raw_page` 会随工作台原文接口带出 `reply_json` 原串，本轮不改、登记为开放项，见 §13 T10）。`Store.messages()`（公开方法，`SELECT *`，现只被 `scripts/measure_dify_settlement.py` 使用、不进 Dify）同样带出原串，本轮不改，并入 T10 开放项（§13 C3）。

## 6. Dify 投影（`dify/negotiation_v22/node_code.py`）

### 6.1 `reply` 子对象

消息行的 `reply` 为七键对象时，模型视图中该行增加 `reply` 子对象；`reply` 为 `None` 或缺失时模型视图中**不出现** `reply` 键。七键原对象（含 `source_text`、`source_direction`、`sender_id`、`source_ext_msg_id`）**不得出现在模型视图任何位置**。

| 投影键 | 取值 |
|---|---|
| `text` | `source_text` 非 `null` → `source_text`；否则 → 摘要 `text`；都为 `null` → `null` |
| `text_is_summary` | 用了摘要 `text` 时为 `true`；用了 `source_text`、或两者都为 `null` 时为 `false` |
| `from` | 由 `source_direction` 映射到询价端既有「我方/对方」口径，即消息行 `direction` 的同一取值：`"out"` → `"out"`（我方，本企点号发出）；`"in"` → `"in"`（对方发出）；`null` → `null` |
| `ts` | 原样取 `ts`（ISO 8601 +08:00 字符串或 `null`） |
| `message_id` | 别名引用，按 §6.2，可能不出现 |

`state="unparsed"` 的引用投影为 `{text: null, text_is_summary: false, from: null, ts: null}`（不给 `message_id`）。

### 6.2 别名引用

- **匹配**：在本阶段送入模型的全部消息行里，找 `native_id == reply.source_ext_msg_id` 且 `kind == "message"` 的行；**恰好对应 1 个不同的 `message_id`** 时才算命中；`source_ext_msg_id` 为 `null`、0 个或 ≥ 2 个 → 不给 `message_id`，只给原文（§13 T11）。
- 「本阶段全部消息行」：A = `messages` + `correction_context` + 各 `members[].context` + 各 `history_results[].messages`；B = `context` + `unresolved_messages`（`current_inbound_message` 取自二者）+ `history_results[].messages`。
- **A 阶段（沿用既有别名机制）**：投影时先把命中行的规范 `message_id` 放进 `reply.message_id`，再由 `analysis_reference_metadata(..., kind='view')` 内部的 `message(row)`（约 115-117 行）对它做与行 `message_id` 相同的 `refs(..., 'm')` 映射，输出同一 namespace 的 `m<namespace>_<n>` 别名。命中行本身就在 `analysis_reference_maps`（约 27-69 行）的 `rows` 里，因此一定有别名；`reply` 不参加 namespace 哈希与「Conflicting canonical message metadata」比对，加 `reply` 不改变任何既有别名。
- **B 阶段（按既有 B 投影规则）**：生产策略 `production_reply`（`context_policy_revision == 'latest_quote_confirmation_v1'`，约 191-195 行）下消息行本就省去 `message_id`/`native_id`/`target_id`/`source_seq`，`reply` **也不给 `message_id`，只给原文**；旧 `explicit_anchors_v0` 回归策略下消息行保留规范 `message_id`，`reply.message_id` 同样给命中行的规范 ID。

### 6.3 落点与覆盖面

- 投影写在 `compact_context` 内部（约 184-304 行）：建议在函数内部新增嵌套辅助函数（命中索引 + 子对象生成），由 `message(row)`（约 193-196 行）统一调用——先去掉行中的七键 `reply`，再按 §6.1/§6.2 附上投影后的 `reply`。🔴 辅助函数必须是 `compact_context` 的**嵌套函数**，或在 `dify/negotiation_v22/build.py` 的 `projection` 拼接里追加对应 `inspect.getsource(...)`；否则导出的 Dify 业务投影 Code 节点会缺函数（`build.py` 只拼接 `analysis_reference_maps`、`analysis_reference_metadata`、`compact_context`、两个 feedback 校验函数与 `select_system_prompt`）。
- 必须覆盖模型视图中的**全部**消息行，包括现有代码不经 `message()` 的位置：`history_results[].messages`（经 `result.update(...)` 原样带入）、非生产 B 的 `current_inbound_message`（约 230-235 行直接 `dict(recent[-1])`）与 `unresolved_messages`。验收以「模型视图 JSON 中不存在 `source_text`、`source_direction`、`source_ext_msg_id`、`sender_id` 键」为准。
- A 阶段别名映射只需在 `analysis_reference_metadata` 的 view 分支 `message(row)` 里加一处：行有 `reply` 对象且含 `message_id` 时做 `refs(row['reply'], 'message_id', 'm')`。结果解码方向（`encode=False`）不涉及 `reply`。
- 槽位候选 `collect_candidates`、`literal_member_hints`、`window_reference_gaps`（`server/negotiation/slots.py`）仍只读各消息 `text`，不读 `reply`。

## 7. 提示词（`dify/negotiation_v22/prompts.py`）

两条规则原句如下，实现方逐字加入（措辞调整须经总控）。

**A 规则**——追加到 `ANALYSIS`（放在现有约 85 行 `ANALYSIS += …` 之后）与 `ANALYSIS_DIRECT` 末尾。`_SYSTEMS['analysis']['ordinary']` 即 `ANALYSIS`，`select_system_prompt` 中以前缀拼接 `ordinary` 的各路由（结构重生成、引用待确认、问价提示、明确券码、识券请求）随之带上；`ANALYSIS_ACK`（原样输出固定 JSON）与 `ANALYSIS_REPAIR`（仅补证据）不加。

```text
消息若带reply，表示该条引用了一条原消息：reply.text是原消息内容，text_is_summary=true时只是客户端摘要、可能不完整；from=out为我方发出、in为对方发出；reply.message_id若给出，即该原消息在本轮上下文中的ID。引用是上下文，不是本条新报价；引用中的价格、券名、数量归属原发送者，不能当作本条发送者的报价。判断本条报价对象时可结合引用确定指代。若本条明确采用被引用的我方已发报价（from=out且给出reply.message_id），按上文窗口内指代规则选该原消息自身的价格候选；其余情况报价数值只取本条text。slot候选、evidence和segments只来自各消息自身text，不来自reply对象。
```

**B 规则**——作为新的 `B_PROTOCOL_NOTES += …` 放在现有约 88 行之后、约 89-90 行 `REPLY += B_PROTOCOL_NOTES` / `REPLY_NONQUOTE += B_PROTOCOL_NOTES` 之前，使 `REPLY`、`REPLY_NONQUOTE` 及约 241 行使用 `_protocol_notes` 的路由同时带上；`REPLY_REPAIR`（仅改正文）不加。🔴 不得改动约 276-280 行 `.replace(...)` 依赖的 `REPLY` 原句。

```text
消息若带reply，表示该条引用了一条原消息：reply.text是原消息内容，text_is_summary=true时只是客户端摘要、可能不完整；from=out为我方发出、in为对方发出。引用是上下文，不是本条新报价；引用中的价格、券名、数量归属原发送者，只代表原消息发出时的报价，不自动成为当前报价。理解本条所说的券和价时可结合引用确定指代；对方引用我方报价并表示认可时，按上文接受语义与已发报价核验规则判断。不在回复中复述引用原文。
```

## 8. 证据与片段校验不放宽

- `node_code.py` 结果校验（约 408-420 行：`evidence` 须引用已知消息且至少一条当前入站；`segments[].text` 必须是 `known[message_id]['text']` 的连续子串）与 `server/negotiation/validation.py` 仍只认各消息自身 `text`，**不得**把 `reply.text`/`source_text` 加入 `known` 文本或子串来源。
- 引用中的旧价格不能作为证据通过：片段若只出现在某条消息的 `reply` 原文、不在该消息 `text` 中，必须被拒；若模型引用的是被引用原消息本身（其 `message_id`/别名），则按该原消息自身的 `text` 与 `direction` 走原有校验，不因引用关系获得额外许可。

## 9. 历史归档不带引用

- 共享 wheel `relay/institution_archive.py` 约 51 行对归档消息做字段白名单：`set(row) - {"native_id","peer_id","direction","occurred_at","text","type","session_kind","media","historical"}` 非空即 `ProtocolError("ARCHIVE_MESSAGE_FIELDS_INVALID")`。采集端历史回补若加 `reply` 会被本仓拒收；且该文件属共享 wheel，本轮不改。
- 采集端 `history_publish` 以 `digest(message)` 判是否已发，加键会改变全部摘要、致近 7 天历史整体重发。
- 因此历史归档与历史回补本轮不带引用；本仓历史线程中的消息 `reply_json` 保持 NULL，不回填。

## 10. Dify 重新生成、客户重新导入与回退

### 10.1 重新生成（本轮在本机做）

`dify/negotiation_v22/README.md` 中的 `scripts/build_dify0153_dsl.py` 已在提交 `279c923` 删除；仓库内导出件以 `build(stage, version=…, provider='CONFIGURE_MODEL_PROVIDER', model='CONFIGURE_MODEL_NAME')` 为准（`tests/cutover/test_prompt_modes.py::test_saved_dsl_actually_executes_the_same_isolated_modes_as_the_builder` 逐个比对四个文件的 `yaml.safe_load` 结果）。改完 `node_code.py`/`prompts.py` 后在仓库根目录执行：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import yaml
from dify.negotiation_v22.build import build
for version, folder in (('0.15.3', 'chatflow_0153'), ('1.17.1', 'chatflow_1171')):
    for stage in ('analysis', 'reply'):
        doc = build(stage, version=version, provider='CONFIGURE_MODEL_PROVIDER', model='CONFIGURE_MODEL_NAME')
        Path('dify', folder, 'v22-' + stage + '.yml').write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding='utf8')
PY
```

产物为 `dify/chatflow_0153/v22-analysis.yml`、`v22-reply.yml` 与 `dify/chatflow_1171/` 同名两件。README 的「生成及导入」节按此改写，并加入下述客户重新导入与回退步骤。

### 10.2 客户重新导入（只写步骤，本轮不导入、不碰客户环境）

1. 导出并保存客户现有 A、B 两个应用的 DSL 备份，记录已发布 workflow ID、graph 哈希与当前配置的两个 App API key（key 不写入仓库、日志或交接）。
2. 按客户 Dify 版本取对应目录（0.15.3 → `chatflow_0153`，1.17.1 → `chatflow_1171`）的 `v22-analysis.yml`、`v22-reply.yml`，按 README 既有方式导入，选择该工作区已配置的 provider/model 后发布。导入前须在真实 Dify 做引用场景冒烟（对方引用我方报价表示认可、对方引用后给新价两类）。
3. 若导入为新应用：取得新 A/B App API key，更新 `XUNJIA_DIFY_ANALYSIS_APP_KEY`、`XUNJIA_DIFY_REPLY_APP_KEY` 后重启询价端；原应用保留不删，供回退。
4. 🔴 **上线顺序**：**先**完成新 A/B 导入，**再**升级询价端服务端。新 Dify 配旧服务端时消息行没有 `reply`，投影不出该键，行为与改前一致；反之，旧 Dify 配新服务端时，旧 `compact_context` 的 `message()` 会把七键原对象（含 `source_text`、原生 ID）原样带进模型视图且没有提示词规则（§13 T12）。推荐完整顺序（§13 N5）：执行 004（§4.1，已有 OceanBase 库）→ 导入新 A/B → 升级服务端；004 放在前后均可（未加列时按 §4.2 降级，执行后需重启询价端才开始存引用）。004 先于服务端升级执行时，旧服务端 `SELECT *` 会带出 `reply_json: null`，值为空、不含引用内容，接受（§13 N5）。
5. 用合成会话做一次 A/B 冒烟（不对外发询价），确认模型视图含投影后的 `reply`、不含七键原对象。

### 10.3 回退

回退顺序与上线相反（§13 T12、N4）：先停询价端服务（停入站）→ 清空引用值 → 部署旧服务端 → 回退 Dify。

1. **停服务（§13 N4）**：停止询价端服务，使新服务端不再入站。若新服务端仍在入站时清值，停机前新到的引用会再写入 `reply_json`，旧代码启动后仍会把原串带进 Dify。
2. **清空引用值（服务停止后、部署旧服务端前，§13 T13、N6）**：旧代码 `SELECT *` 会把 `reply_json` 原串带入 Dify 上下文（旧投影不过滤未知列），因此执行：

   ```sql
   UPDATE xj2_message SET reply_json = NULL WHERE reply_json IS NOT NULL;
   ```

   OceanBase 由 DBA 执行；本地 SQLite 库同样执行该语句，由运维执行（本机即安琳授权的执行者）。只清值、**不删列**。丢失的仅是可再生的引用上下文，消息本身（`text`、`content_hash` 等其它列）不受影响。执行后核对 `SELECT COUNT(*) FROM xj2_message WHERE reply_json IS NOT NULL;` 为 0。
3. **服务端**：部署旧服务端代码并启动询价端。旧代码 `SELECT *` 会带出 `reply_json: null`（值为空、不含引用内容），接受（§13 N5）。
4. **Dify**：切回 10.2 步骤 1 的原应用（恢复原 key 配置并重启询价端），或导入备份 DSL。须在服务端已回退之后再回退 Dify（理由同 10.2 第 4 条）。
- **OceanBase 004**：只加列，旧代码 `INSERT` 用显式列清单，不受多一列影响；`server/health.py` 只检查期望列存在，多一列不报错。

## 11. 改动文件与 wheel 文件清单核对

`scripts/build_collector_runtime.py` 约 19-26 行 `FILES` 共 18 个文件：`relay/__init__.py`、`relay/protocol.py`、`relay/collector_bridge.py`、`relay/site_egress.py`、`relay/qtrade_native_port.py`、`server/__init__.py`、`server/receiver_ledger.py`、`server/transports.py`、`relay/file_gateway.py`、`server/file_gateway_transport.py`、`relay/file_gateway_connection.py`、`relay/gateway_paths.py`、`relay/institution_codec.py`、`relay/institution_directory.py`、`relay/institution_archive.py`、`server/institution_transport.py`、`relay/wechat_capture.py`、`server/wechat_capture_transport.py`。

本合同要求改动或新增的文件，逐个核对（2026-10-28，HEAD `1a248f2`，脚本化取 `FILES` 求交集；T1 裁定把 `server/health.py` 列入需改文件后同日再核一次，结果相同）：

| 文件 | 在 wheel 清单内？ |
|---|---|
| `server/negotiation/runtime.py` | 否 |
| `server/negotiation/store.py` | 否 |
| `server/negotiation/schema.py` | 否 |
| `server/negotiation/engine.py` | 否 |
| `server/negotiation/commands.py`（§13 C1 后列入；实现方 2026-10-28 对照 `FILES` 核对，见实现交接「C1 修改」节） | 否 |
| `sql/oceanbase/004_message_reply.sql`（新） | 否 |
| `sql/oceanbase/001_xunjia_schema_mysql.sql`（重新生成） | 否 |
| `dify/negotiation_v22/node_code.py` | 否 |
| `dify/negotiation_v22/prompts.py` | 否 |
| `dify/negotiation_v22/README.md` | 否 |
| `dify/negotiation_v22/build.py`（仅当投影辅助函数不嵌套时才改） | 否 |
| `dify/chatflow_0153/v22-analysis.yml`、`v22-reply.yml` | 否 |
| `dify/chatflow_1171/v22-analysis.yml`、`v22-reply.yml` | 否 |
| `server/health.py`（§4.4，`reply_json` 为可选列；2026-10-28 T1 裁定后再核一次） | 否 |
| `docs/implementation/2026-10-28-qidian-reply-context.md`（本文） | 否 |

**结果：交集为空**。18 个 wheel 文件中只有 `relay/collector_bridge.py`（能力字符串 `negotiation-v22-*`）与 `relay/institution_archive.py`（文档字符串）含 `negotiation` 字样，均不导入 `server.negotiation` 或 `dify`。实现若需要改动上表以外的文件，先对照 `FILES`；有交集即停下回报总控。

## 12. 验证要求（DECISION-2 §3 询价端与跨端部分）

测试数据全部合成，不得出现真实聊天内容、真实 uin 或真实样本值。实现方不改自己的测试；验收用例由只读规格的独立验收方撰写。

**询价端**

- `Inbox` 受限校验（§3）：缺 `reply` 键、`null`、非对象、`state` 缺失、`state` 非法 → 入库成功且 `reply_json IS NULL`；多余键 → 被丢弃、其余七键照常入库；`state` 以外的键缺失 → 该键为 `null`；单键超长（`text` 2001、`source_text` 4001、ID 129、`ts` 65）或类型冒充（布尔/数字）、`source_direction` 非法 → 仅该键为 `null`、其它键保留；`unparsed` 带非空值 → 其余六键一律 `null`；合法 `ok`/`unparsed` → 原样入库；入库值恒为七键；任何形状不符都不隔离、不拒收、不产生 `INBOUND_QUARANTINED`。
- 入库与列缺失降级：有列时新行写入；无列库（模拟未执行 004）入库照常、不存引用、告警只一次、不回滚。
- 健康检查（§4.4）：无 `reply_json` 列的库 `schema_health` 不报 `SCHEMA_INCOMPLETE`、给出 warning（不进 readiness `reasons`），`/api/health/ready` 200、`manage check` 成功；`reply_json` 列存在但类型不符、其它列缺失或类型不符仍按原判定报 `SCHEMA_INCOMPLETE`。
- 回退（§10.3）：执行清值语句后 `reply_json` 全为 NULL、列仍在，消息其它列不变。
- `_handoff_evidence`（§13 C1）：服务端 `handoff_evidence` 行不含 `reply_json` 原串，带解析后的 `reply`。
- `content_hash` 与重放：同一事件有/无引用、引用不同时 `content_hash` 相同；同 `event_id` 重放不因 `reply` 不同报错；既有 NULL 可补写、非 NULL 不改；补写不改 `ingest_revision`/`content_hash`；历史等价重放分支同规则。
- A/B 投影：含 `reply` 子对象；`text` 原文优先、摘要兜底、`text_is_summary` 正确；`from` 映射三值；`unparsed` 投影；A 别名与行别名同一 namespace、命中 0/1/≥2 行三种情况；生产 B 不出任何消息 ID；模型视图无七键原对象；`reply=None` 时视图与改前逐字节一致（指新代码；旧代码在列存在时带出 `reply_json: null` 属已接受差异，§13 N5）。
- 证据校验仍只认 `text`：片段只在引用原文中出现 → 拒；引用中的旧价格不能作为证据通过。
- build 产物：四个 yml 与 `build()` 一致（既有 `test_prompt_modes` 比对）；导出的 Code 节点可独立执行带 `reply` 的上下文；README 步骤与产物一致；`export_oceanbase_schema.py --check` 通过。

**跨端（本机联调）**

- 采集端产出的 `host_event` body 经本仓 `Inbox` 入库后，`reply_json` 解析结果与 body 中的 `reply` 七键逐值一致。使用隔离库与假文件网关/现有 QA 夹具；不连客户环境、不连真实 Dify、不对外发询价、不重启运行中采集端。

## 13. 张力清单（2026-10-28 总控已逐条裁定，裁定原文见采集端仓库 `.codex/qidian-reply-quote-20261028/DECISION-2.md` 附录 A；新张力仍不得自行决定，回报总控）

| 编号 | 位置与原句 | 张力 | 裁定 |
|---|---|---|---|
| T1 | `server/health.py::contract/schema_health` 以 `schema.statements()` 生成期望列；DECISION-2「列缺失降级：入库照常……不得因此拒收或卡住入站」 | `reply_json` 进 `schema.py` 后，未执行 004 的库 `schema_health` 报 `SCHEMA_INCOMPLETE`，`/api/health/ready` 返回 503、`python -m server.manage check` 失败；DECISION-2 未说就绪探针如何处理 | **已裁定**：`reply_json` 为可选列，`schema_health` 对它缺失只报 warning，不计 `SCHEMA_INCOMPLETE`，`/api/health/ready` 仍 200、`manage check` 不因此失败；其它列的既有判定不变。`server/health.py` 列入需改文件（§4.4、§11）。注：`deploy/oceanbase.py`、`deploy/database_upgrade.py::verify_fresh` 同用 `schema_health`，随之也不因该列缺失失败 |
| T2 | DECISION-2「本地 SQLite 由 `schema.py` 承接」 | `schema.py` 只用 `CREATE TABLE IF NOT EXISTS`，`manage.py migrate` 不给已有 `xj2_message` 加列；已有 SQLite 库只能走列缺失降级。若手工补列，SQLite 须写 `TEXT`（写 `LONGTEXT` 会被健康检查判 `invalid_columns`） | **已裁定**（同暂行口径）：新建库由 `schema.py` 承接；已有 SQLite 库不自动 `ALTER`，走列缺失降级；README 给手工补列语句（类型 `TEXT`，§4.2） |
| T3 | `deploy/build_incremental.py` `SCHEMA_FILES` 含 `001_xunjia_schema_mysql.sql` | 本改动必然重生 001，增量包会报 `SCHEMA_CHANGE_REQUIRES_EXPLICIT_MIGRATION`，发布只能出完整包并附 004 | **已裁定**（同暂行口径）：本轮不出包 |
| T4 | `dify/negotiation_v22/README.md`「`.venv/bin/python scripts/build_dify0153_dsl.py …`」 | 该脚本已于 `279c923` 删除，README 生成命令过时 | **已裁定**（同暂行口径）：按 §10.1 用 `build()` 重新生成并改写 README |
| T5 | DECISION-2「`from` = 由 `source_direction` 映射到询价端既有「我方/对方」口径」「给出该消息在本阶段的别名引用」 | 未给具体取值与键名，也未说 `unparsed` 是否投影 | **已裁定**（同暂行口径）：`from` ∈ `"out"`/`"in"`/`null`（与消息 `direction` 同域）；别名键 `reply.message_id`；`unparsed` 仍给子对象、各值 `null`（§6.1） |
| T6 | DECISION-2「字符串长度上限：`text` 2000、`source_text` 4000、ID 128」 | 未给 `ts` 上限 | **已裁定**：`ts` 为字符串或 `null`，长度 ≤ 64，超长视为 `null`（§3） |
| T7 | DECISION-2「仅接受上节七键」 | 「恰为七键」使将来采集端加第八键时旧询价端整体丢引用 | **已裁定（改暂行口径）**：只取七键、忽略多余键；`state` 缺失或不在枚举内 → 整个 `reply` 视为 `null`；其它键缺失 → 该键 `null`；单键类型/长度不符 → 该键 `null`；`state="unparsed"` 时其余一律 `null`（§3） |
| T8 | 采集端 06 §2.9.5 子条 | 事件 payload 的 `reply` 既非 `null` 也非对象时，采集端未单列规则 | **已裁定**：采集端对事件 payload 的 `reply` 既非 `null` 也非对象 → 线上 `reply` 置 `null`，按原因每进程告警一次（已写入采集端 06 §2.9.5 子条）；本仓兜底同暂行口径，按 §3 视为 `null` |
| T9 | DECISION-2「同一 native 重放时既有行为 NULL 可补写」 | 「同一 native」可读作 `event_id` 命中，也可含 `ingest` 出向 echo 分支（`native_id` 命中） | **已裁定**（同暂行口径）：只在 `event_id` 命中返回既有行的两个分支补写；echo 分支不补写；补写不重新触发分析（§4.3） |
| T10 | `application_store.py::raw_page` 约 138 行 `SELECT *`，逐行 `dict(row)` 返回 HTTP | 加列后 `reply_json` 原串（含 `source_text`）会随工作台原文接口返回前端；前端是否展示引用不在 DECISION-2 范围 | **已裁定（开放项）**：本轮不改 `application_store.py::raw_page`，登记为开放项（工作台为内部页面，内容与已可见聊天同源）；是否过滤或展示另行裁定 |
| T11 | DECISION-2「若 `source_ext_msg_id` 等于当前上下文某条消息的 `native_id`」 | edit/retract 行与原消息共用 `native_id`，可能命中多行 | **已裁定**（同暂行口径）：只认 `kind="message"` 且恰对应 1 个 `message_id`，否则不给别名（§6.2） |
| T12 | DECISION-2「改完重新生成 A/B 两个 yml；客户重新导入步骤写进 README」 | 新服务端配旧 Dify 时，旧投影把七键原对象原样给模型且无提示词规则 | **已裁定**（同暂行口径）：先导入新 A/B，再升级服务端；回退反序（§10.2 第 4 条、§10.3） |
| T13 | DECISION-2 未涉及服务端回退 | 服务端回退而 `reply_json` 列保留数据时，旧代码 `SELECT *` 把原串带入 Dify 上下文 | **已裁定**：回退服务端前，由 DBA 执行 `UPDATE xj2_message SET reply_json = NULL WHERE reply_json IS NOT NULL;`（只清值、不删列）；丢失的仅是可再生的引用上下文，消息本身不受影响。已写入 §10.3 回退步骤（先停服务见 N4，本地 SQLite 执行者见 N6） |

**第二轮与实现期裁定**（2026-10-28 总控，裁定原文见同一 `DECISION-2.md` 附录 B、C；N 系列针对文档回写第 4 批交接 `handoff-docs-4.md` §4，C 系列针对询价端实现交接 `handoff-r117-xunjia-impl.md` §6）：

| 编号 | 张力 | 裁定 |
|---|---|---|
| N1 | T1 只说「缺失只报 warning」，`reply_json` 存在但类型不符是否仍严格判定未明说 | **已裁定**：T1 只豁免「列缺失」；列存在但类型不符仍按既有严格判定（`invalid_columns`、`SCHEMA_INCOMPLETE`）（§4.4） |
| N2 | `deploy/oceanbase.py`、`deploy/database_upgrade.py::verify_fresh` 以 `schema_health()['ready']` 为闸，T1 后不再提示需执行 004 | **已裁定**：这两个文件本轮不改；004 作为 DBA 升级步骤显式写入本合同（§4.1、§10.2），缺失时由健康检查 warning 可见（§4.4） |
| N3 | warning 键名与是否进 readiness `reasons` 未定 | **已裁定**：warning 只进 warnings、**不进** readiness 的 `reasons`；键名由实现方沿用 `health.py` 既有命名风格并在交接中写明（现行为 `warnings`，见 C2）（§4.4） |
| N4 | T13「回退服务端前」清值时，若新服务端仍在入站，清值后新到的引用会再写入 | **已裁定**：回退顺序为先停询价端服务（停入站）→ DBA 清值 → 部署旧服务端 → 回退 Dify（§10.3） |
| N5 | 列存在而值为 NULL 时旧代码 `SELECT *` 带出 `reply_json: null`，与「改前逐字节一致」字面有出入；004 相对服务端升级的时机未写 | **已裁定**：新代码 `reply` 为 `None` 时消息视图不出现 `reply` 键，与改前一致；旧代码回退后带出 `reply_json: null` 无害，接受。推荐上线顺序：执行 004 → 导入新 A/B → 升级服务端；004 先后均可（有降级）（§10.2 第 4 条、§10.3、§12） |
| N6 | T13 只写了 OceanBase 的 DBA，本地 SQLite 回退未写 | **已裁定**：本地 SQLite 回退同样执行清值语句，由运维（本机即安琳授权的执行者）执行（§10.3 第 2 步） |
| N7 | 手工补列语句写在哪份 README | **作废**（与 T2 冲突，总控笔误）：本合同 §4.2 与 `dify/negotiation_v22/README.md` 都保留手工补列语句（README 现状即可） |
| C1 | `commands.py::_handoff_evidence` 以 `dict(row)` 返回，B `handoff_evidence` 行带 `reply_json` 原串，§5 读取点清单未列 | **已裁定**：`_handoff_evidence` 改走 `Store.message_row`（与另外五个读取点一致），服务端上下文不再携带 `reply_json` 原串；`node_code.py` 投影层拦截保留作第二道防线（§1、§5、§11、§12） |
| C2 | 读侧「恰为七键」与 T7 逐键降级的关系、告警时机、`warnings` 键 | **已裁定**：读回恰为七键再逐键校验；首次发现缺列即告警；`schema_health` 的 `warnings` 键恒在——接受实现方取值（§4.2、§4.4、§5） |
| C3 | `Store.messages()`（`SELECT *`）与 `raw_page` 一样带出 `reply_json` 原串 | **已裁定（开放项）**：本轮不改，并入 T10 开放项（§5） |

附录 F：提示词引用规则修订（安琳 2026-10-28 拍板，针对终审 P2-1：原「报价数值只取本条text」与窗口内指代规则冲突、B 把被引用价一律称旧价）

**开放项**：T10（`application_store.py::raw_page` 带出 `reply_json` 原串；C3 `Store.messages()` 同类并入）本轮不改，留待后续裁定。
