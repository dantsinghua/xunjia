# 安琳：OSS / 邮件双向交付的复用与外部最小改动证据

日期：2026-10-13。本轮只读事实核对；没有实现、联网、连接账号、读取真实数据库/密钥/邮箱/日志。

## 已确认的设计前提

- 企业内网多人；安琳后续已确认登录参考 nanyin-deposit-agent、数据库为同项目的 OceanBase。具体源码由主审核实，见主设计；本分报告只核传输，不重复核认证和数据库。
- 5 人各用 1 个号，批次策略确认后只在边界内自动谈价，拿到实价转人工。
- OSS 和邮件本期都要双向完整交付；每条绑定择一主用，人工切换，unknown 先对账。无需设计同时双发、自动切换或跨席位抢号。

## 结论

**现役启动入口引用的主树已有 OSS 下行消费者与 ACK，不能另起炉灶重写。邮件的 SMTP/IMAP、验签、持久化发件队列和统一命令总线也已有。真正缺的是两通道统一执行/去重与对账、消息上报生产接线，以及批次授权撤销对已排队命令的约束。**

最初只读 10-10 冻结副本时没找到 OSS；主审随后根据运行 wrapper 扩大了确切源码路径的只读核对范围，本报告已用主树事实替代旧快照判断。没有据旧快照宣称现役无 OSS。

## 1. 版本与读取范围

- 询价仓：`X=/home/anlin/work/xunjia-agent`，脏工作树。
- 采集端主树：**`A=/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent`**。以下 `A/...:行` 都可按这个绝对前缀直接定位。
- 运行入口证据：`/home/anlin/work/qtrade-build/real-chain-20261010/run_agent.py:20` 指定 `/mnt/c/Users/anlin/Desktop/work/src`。
- 主树 HEAD 为 `6c5b941482d4f711a4481d18b771f9b48e16d917`，有大量未提交/未跟踪修改，因此事实来源是本次读到的文件，不把 HEAD 当完整部署版本。**没有核验进程已加载的新旧代码**；`A/app.py:398` 也注明在跑进程需下次启动才接到新轮询。读到源码接线不等同现役服务本轮实测。
- 已读主树根 `AGENTS.md`；src 无更具体 AGENTS。主树 resume skill 与已读副本的 skill SHA256 相同，未重复加载。
- 对比确认主树 `bus/bus.py`、`models.py`、`store/schema_agent.sql` 与 `mail/{backends,fetcher,sender,ingest,confirm,sign,service,mail_store,headers}.py` 共 12 文件，逐字节 SHA256 与已读 10-10 副本一致；相关行号和行为可直接用于主树。`app.py`、`ossbox/*` 则直接读取主树。
- 禁止范围保持：未打开 agent.toml、token、agent.db、账号数据、邮箱、日志，也未调用任何采集端函数。仅源码、适用规范和 git/目录元数据。未运行真实或假业务测试，本报告仅作静态设计证据。

## 2. 可直接保留或复用的实现

| 现成部分 | 证据 | 可以省掉的重复工作 |
|---|---|---|
| 询价侧 OSS 密封、上传、ACK 读取 | `X/imlink/oss_send.py:69`、`:77`、`:85`、`:110`、`:297`、`:325` | 保留对象名、命令序列化和网络访问；修复原超时状态机即可，不重写连接器 |
| 采集端 OSS 真实对象箱与下行循环 | `A/ossbox/inbox.py:91`、`:138`、`:172`；`A/app.py:362`、`:397` | 现成列举 in/xunjia、解密校验、账号映射、ACK 写回的外壳可用 |
| 目录/通讯录发布读取 | `A/app.py:364`；`A/ossbox/catalog_publish.py:115`；`X/imlink/oss_catalog.py:352`、`:390` | 目录/通讯录协议保留；不要把其在线时间等同单条命令结果 |
| SMTP/IMAP + 可注入假后端 | `A/mail/backends.py:47`、`:78`、`:348`、`:564` | 使用现成 imaplib/smtplib 实现、SSL/STARTTLS 和错误分类，不新增邮件基础库 |
| IMAP 游标/UIDVALIDITY/只读取信 | `A/mail/fetcher.py:204`、`:211`、`:238`、`:245` | 复用增量/重扫流程，增加询价侧消息/回执解析入口 |
| 邮件头与模板、签名 | `A/mail/headers.py:29`、`:37`；`A/mail/sign.py:20`、`:44`、`:64`、`:80` | 复用构造和 HMAC 规范，避免另一套邮件注入防护/验签算法 |
| 邮件白名单/路由/权限检查 | `A/mail/ingest.py:196`、`:219`、`:227`、`:254`、`:301` | 保留发件人、账号范围、op、时间戳和 nonce 校验；require_signature 的实际配置仍需部署确认 |
| 邮件入站到总线、结果回执 | `A/mail/ingest.py:363`、`:434`、`:474`；`A/app.py:718`、`:725` | 保留命令邮件→统一 Command→IM 执行→签名回执邮件流程 |
| 持久化发件队列 | `A/mail/mail_store.py:408`、`:437`；`A/mail/sender.py:81`、`:114`、`:187` | 保留去重、逐信重试、限流和回执关联，不另建邮件发送引擎 |
| 单账号执行与幂等 claim | `A/bus/bus.py:166`、`:196`、`:229`、`:238`；`A/store/schema_agent.sql:214` | 一个账号一个执行队列、同 key 参数冲突拒绝、事务 claim；是 OSS 应复用的入口 |
| 消息身份与结果查询 | `A/store/schema_agent.sql:338`；`A/api/app.py:778` 的 `/accounts/{account_id}/commands/{trace_id}` | 使用 account+ext_msg_id 消息身份、trace 对具体命令结果，不再按正文全库猜送达 |

说明：QTrade 的 MailStore 依赖现有 Store/SQLite 表，不是独立零依赖库。企业数据库未知时可复用协议逻辑/数据约束，不应提前把 SQLite DDL 作为新的企业数据库决定。

## 3. 当前真实缺口

### 3.1 OSS 绕过了已有执行总线

- `A/app.py:257` 的 sender 绑定 `self.qidian_ui.send_text`（真实后端），`:362` 直接交给 XunjiaInbox。
- `A/ossbox/inbox.py:233` 直接调用该 sender，未走 `bus.submit`；`:248` 在点击发送之后才 `_write_out`。
- `:87-88` 的 Dedup/outcome 是进程内集合/字典；`:299` 的持久检查只看已有同 idem 消息，不能替代发送前原子 claim；`:33` 还用硬编码 HELD_CMD_IDS 阻止个别旧指令补发。
- `:309-327` 按账号、会话、同文和 120 秒窗口等待 landed；超时仍写普通 failed，已点击但未确认未独立表示 unknown（`:257-263`）。

**外侧必要改动：保留 OSS 拉取/密封/ACK 外壳，统一转为已有 Command 总线执行，使用发送前持久 claim 和持久结果。** 删除硬编码业务特例须有等价持久取消记录，不能直接删列表后让旧对象补发。以上是复用边界事实，具体接口改法交架构通道决定。

### 3.2 OSS 与邮件的消息上报都还缺生产接线

- `A/ossbox/bridge.py:52` 的 `plan_uploads` 只返回计划，不做网络上传（文件 `:1-4` 已明确）。全 src 搜索只见定义/导出，无生产调用。
- `plan_uploads` 默认 seq_start=1（`:60`），Dedup 为内存对象（`:85`）；不足以支撑重启续传。另在计划阶段就标记已见（`:91`），不能直接当“上传成功后提交游标”。
- `A/mail/sender.py:95` 的 enqueue_message 在全 src 中仅定义，无生产调用。
- `A/ossbox/imap_out.py:23` 只判断存款绑定微信群是否发信，`:32` 只渲染 ibquote-163-v1，且注释明确“不发真实邮件”；不是询价企点的完整邮件上报实现。
- 当前应用只装配 OSS inbox/catalog 和邮件取信/回执投递，没有找到新增企点消息驱动上述上报的调度入口：`A/app.py:350-368`。
- 询价侧当前读取采集端本地 SQLite：`X/imlink/dify_inquiry.py:527`、`X/imlink/monitor_feed.py:488`。这不能替代跨网 OSS/邮件入站。

**外侧必要改动：在消息落库后接一条可续传发布流程，按绑定选择 OSS 或邮件；复用已存在的消息规范化/对象命名/模板/发件队列。** 发布失败保留待发，确认落库后才推进游标；重启、切主通道不得重新识别/回复历史消息。内侧另补两个消费者，共同落到同一消息身份去重。

### 3.3 两通道幂等键不相同，人工切换也不能忽略去重

- OSS 原样使用 command.idempotency_key：`A/ossbox/inbox.py:188`；询价侧 payload 对应 `X/imlink/oss_send.py:101`。
- 邮件入口强制改为 `mail:{short}:{req_id}`：`A/mail/ingest.py:271-272`。
- 当前 OSS 的 cmd_id、邮件 req_id/Message-ID、总线 trace_id 没有统一的持久映射。内存 ServiceCall/effect 相等不代表同一业务命令只执行一次：`A/ossbox/services.py:26`、`:169`。
- 总线按账号+key 唯一，留存默认 7 天：`A/store/schema_agent.sql:214-227`。邮件超时容差是接收时验签条件（`A/mail/ingest.py:311`），不等于排队执行前的业务有效期。

**外侧必要改动：同一业务命令无论先 OSS 后邮件，都映射到同一总线幂等身份，校验账号/op/参数摘要并返回已有结果。** 人工切换前 unknown 先按该身份对账；不得换新 UUID 重新报价。无需支持同时双发，但旧主通道的迟到指令必须仍能识别为重复、过期或已撤销。

### 3.4 现有取消能力不覆盖询价授权与已排队发送

- Command 只含 account/op/args/key/confirm/timeout/origin/trace/submitted：`A/models.py:72-82`。OSS schema 必填字段同样没有批次、有效期或授权代次：`A/ossbox/schema.py:14`、`:131`。
- 总线 gate 在入队前（`A/bus/bus.py:200`）；实际执行入口（`:238`）有适配器 write_guard，但无询价批次授权的重新校验。
- `A/app.py:614` 的 cancel_job 取消注册 job task；API 还有 workflow/login cancel。没有找到按 cmd_id/trace_id 取消队列 send_text，或取消/授权版本写入 OSS/邮件的通用协议。
- 邮件高危 approve/reject（`A/mail/confirm.py:67`、`:93`）有参数摘要、时限和控制台授权，是可复用零件；但它不是已确认询价批次在一定价格边界内持续自动谈价的授权模型。

**外侧必要改动：最终发送前能判断该绑定/批次授权是否仍有效，持久记录取消和终态，并让两个入口共用检查。** 询价端停止、转人工、切换主通道不能只改本地按钮。取消在异步链路尚未确认时，页面不得宣称外侧已停；已执行/不可确定的结果保持真实可对账状态。无需外侧复制谈价策略引擎，只需执行最终授权/身份/期限检查。

## 4. 最小外部采集端变更清单

供架构/实现通道估算，不新增宏观组件：

1. **收敛命令入口**：现成 XunjiaInbox 转已有 bus；邮件和 OSS 共用命令身份、原子 claim、执行和持久结果，保留现有账号队列/适配器。
2. **补消息发布接线**：从已落库 IM 消息到一个按绑定择一的发布流程，OSS 使用现有 plan/密封/对象箱，邮件使用 MailSender；补持久游标、失败重试、重启续传与5号隔离。
3. **统一对账/回执**：cmd_id ↔ req_id ↔ trace_id 映射与持久结果可通过选定通道查询；ACK 区分已受理、已执行且确认、未执行拒绝、unknown、已取消，不能用 transport 200/250 冒充 IM 送达。
4. **补最终授权失效检查**：停止/转人工/主通道切换对未执行指令生效，旧主通道迟到命令不继续报价；替换硬编码 HELD_CMD_IDS。字段和原子边界由架构通道与采集端协议负责人定案。

5 人 5 号意味着每人权限直接限制到自己的账号绑定；无需加跨席位分配、账号抢占或分布式故障切换机制。多用户 SSO 与本方批次确认仍由询价应用接企业现有能力；采集端只接明确的身份/授权结果，不另建身份提供方。

## 5. 尚需确认的协议前提

- **主树代码何时被进程加载**：本轮只证实源码及装配；不通过重启或日志/数据库探查来替代部署方确认。
- **同一命令的身份映射**：现成邮件 req_id 改写策略是否保留前缀并让 OSS映射，或升级为兼容公共键；由采集端协作，不能仅在内侧改名字。
- **消息序号/确认与保留期**：out对象目前命名 `wb/{workbench}/out/{consumer}/{seq}.bin`（`A/ossbox/keys.py:102`），seq 必须持久且作用域确定；明确补取、历史与实时边界、过期对象和跨通道重复消息处理。
- **命令有效期、取消确认与unknown对账**：用户已定“unknown先对账”，双方还需明确可查询状态、迟到ACK更新规则，以及旧主通道何时确认停止接受新的/迟到的命令。
- **ACK 成功定义**：OSS当前只含 cmd_id/ok/code（`A/ossbox/inbox.py:55`），邮件含 req_id/account/op/status/trace/time。需固定同一业务状态含义，原文不进回执；价格正文从原授权命令取，不信任回执回带价格。
- **账号映射**：OSS用 workbench+account_uid，邮件使用内部 qdXX account_id。绑定变化不能使既有命令改从另一个号执行；每人固定账号的权限与设备变更需可核对。
- **企业邮件认证**：现成邮件连接器使用user/password；是否需要OAuth/企业网关，需测试环境核实。SSO和OceanBase选择已由安琳确认且由主审核代码；实际目标库事务验收尚未执行。本轮不新增产品选型。

## 6. 避免重复建设与误用

- `X/relay/mailbus.py:52` 只是本地maildir直投，保留为离线夹具，不扩写成第二套生产邮件框架。
- `A/ossbox/imap_out.py` 的“IMAP发送”只是旧命名的纯渲染函数；真实外发用已有 SMTP，入站用 IMAP，不能凭文件名判断链路已接通。
- `A/ossbox/bridge.py`、`X/imlink/relay_equiv.py` 的纯函数可复用字段规则，但不能继续用内存去重/默认序号代替持久化交付。
- 复用已有 bus/mail 不代表它们已经通过本轮全面安全审查；例如不确定结果回放、跨通道切换和停止竞态必须有独立验收。此次只确认复用位置和必要边界，不做实现、不替外部部署出具已验收结论。
