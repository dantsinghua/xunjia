# 安琳的本轮独立代码与安全评审

## 评审摘要

**最新结论（第四轮）：APPROVE——准予进入冻结同版本终验。** 本报告18项具体复现已取得关闭证据；这是所审实现范围的代码评审结论，**不是全工程0回归/0红、现场验收或部署批准**。前文各轮 REQUEST CHANGES 保留为历史记录，当前状态以末尾第四轮为准。

以下为 2026-10-13 实施中的各轮快照，不是冻结源码后的统一终验。已即时交办不等于已修复，关闭须有独立重验。

**完整阅读的首轮核心文件：20 个。累计发现：18 项，CRITICAL 0、HIGH 14、MEDIUM 4、LOW 0。** 这是本轮发现总数，逐项修复复核状态见末尾追加，不代表这些问题在后续快照都仍存在。

范围：`server/{business,chat_runtime,runtime,inbox,agent_routes,link_routes,transport_config,auth,market,market_routes,quote_runtime,transports,receiver_ledger,app,domain,db,schema,repository}.py`、`relay/{protocol,collector_bridge}.py`。另按调用点查阅 `server/qb_source.py`、Dify client、设计及独立测试，不把这些局部查阅算成完整评审。

唯一根 `/home/anlin/.codex/worktrees/b372/xunjia-agent`，分支 `codex/xunjia-chat-remediation-20261013`；原项目尚未集成。未修改产品代码或测试，未连接真实账号或外部服务。本报告中的价格、账户、券名全部是离线合成案例。

## 规格与根因检查

以整体整改、Chat Agent、STS 三份已确认设计及后续“删除 mock 服务”裁决为依据。已看到正式认证入口、本人隔离、事务批次、Chat proposal 签证、真实回执与公共行情白名单，mock 服务/seed 已删除，正式行情移入 server。Dify 的真实导入和两轮运行由其他角色验证，不能据此替代本报告的安全检查。

硬边界仍不完整：停止条件部分取决于模型是否填 observations；不同公开动作使用不同检查；消息调度和传输恢复存在持久饥饿；采集端人工出站与主通道切换未完全闭环。不能通过扩大 LIMIT、信任模型布尔值、忽略过期事件、吞掉异常或把 unknown 改 failed 来绕过这些主合同。

## HIGH 问题

### R01：省略 observations 可绕过净价、达标停止

位置：`server/chat_runtime.py:162`、`:194`。`handoff` 初值为 false，净价和目标差距只在模型自选 observations 循环中核验；COUNTER 本身不要求当前源报价已完整核对。

已复现：BUY，授权 1000 万，开价 2.50，边界 2.40，每步 2 BP。分别输入 `000100.IB 净价100`、`000100.IB 我卖2.42%，1000万，实价`，合法 COUNTER 2.48 提案只将 observations 设为空，两次均获正文签证并返回 `status=queued`。第二例实际已达目标，第一例本应净价转人工。

修复：源消息中有价格、方向、量或附加条件时，必须得到完整且能关联源证据的观察；缺失/不完整不能进入自动报价。净价、明确非价格条件变化等硬触停在可信运行器判断，不依赖模型主动上报。独立覆盖空观察、遗漏其中一价、WAIT 后下一条恢复谈价等路径。

### R02：ASK_QUOTE 公开正文可反向、超量

位置：`server/chat_runtime.py:236`。方向和数量单位检查只对 COUNTER 执行；ASK_QUOTE 仅核结构化量及数字集合。

已复现：结构化 ASK_QUOTE 指向 BUY/1000 万的会话，正文 `000100.IB 我方卖出1000亿，请问可以吗？`，经真实 validate 获签证，process 返回 queued，命令正文保留上述反方向/超量内容。

修复：所有含交易语义的公开动作统一核对方向、数量与单位、券锚及价格口径；非交易分支不能借数字白名单携带交易承诺。不能只为本反例加字符串拦截，须修复行动与正文的统一合同。

### R03：裸债券代码不识别导致错挂券

位置：`server/chat_runtime.py:146`、`:154`。未知代码检查要求带市场后缀；单活跃会话 fallback 会吞掉原文显式出现的另一只裸代码。

已复现：唯一活跃 `000100.IB`，源文 `240210 我卖2.20%，1000万，实价`，提案把报价挂到 000100.IB，仍自动还价 queued。

修复：券代码、裸代码、简称与别名形成一致的候选集合。存在不能验证的代码样式或未知简称时不得使用单会话 fallback；查证不成走一次中性澄清/人工。新增带后缀与裸代码、简称前缀包含、数字数量与裸代码歧义等正反例。

### R04：首询正文绕过禁成交检查

位置：`server/business.py:52`、`:66`，`server/domain.py:50`、`:66`。API 的 name 只检查非空/长度，直接拼进首询；首询不经过 Chat 的公开正文检查。

已复现：name=`合成债，成交，已确认交易`，preview→confirm→Dispatcher 可产生并交付给离线发送替身：`您好，000100.IB 合成债，成交，已确认交易，我方考虑买入1000万，收益率2.50%，还能帮忙争取吗？`。

修复：代码与名称须匹配可信已接入资料或明确受限的人工券标识；名称不能承载自由交易语句。首询同样执行最终对外语义/授权检查。人工确认批次不是允许绕过系统“不成交”硬边界。

### R05：停止后的旧消息永久占据 Chat 候选

位置：`server/app.py:70`，`server/chat_runtime.py:53`，`server/business.py:171`。后台固定选择最早 5 条 queued；_start 对不可运行消息只返回 None，多处停止/转人工没有清理后续 queued。

已复现：账号 A 队列 5 条消息后关闭机器人，再授权账号 B 并收到第 6 条可处理消息。连续两轮候选仍是 A 的 5 条，每条 not_runnable；B 永远不在候选内。

修复：在控制状态切换时终结旧 queued/running；调度查询只选仍有效的绑定/会话、每会话最早消息，并对阻塞对话跳过而不阻塞其他账号。root 已接单修复，尚未在本轮报告中关闭。

### R06：原生出站 echo 与人工接管没有完整接线

发现时位置：`server/inbox.py:56`（构造入站 event），`server/business.py:267`。协议和 ReceiverLedger 已有精确 native_id→command_id 关联，但 Inbox 最初丢失 source_command_id；ingest 对普通 out 只记录 ignored。

已复现：已有 AUTO 会话，摄入一条无命令关联的真实人工 out，conversation.mode 仍为 AUTO。静态同时确认回执生成 out_<command_id>、echo 另建消息，存在双条及回执丢失不能用原生证据恢复的问题。

修复：精确身份/native_id/正文/原命令关联才能合并出站事实或对账；无关联的新人工外发触发接管和控制版本变更，不能以文本/时间猜测去重。历史出站重放不可反复接管；关联失败不能直接当成功。

动态说明：评审后段已看到 root 正在补 Inbox source_command_id 分支；尚未对新分支及人工 out 路径独立重验，因此保留本项。

### R07：超过信封 TTL 的 OSS 历史永久阻塞恢复

位置：`server/transports.py:391`、`:396`，`relay/protocol.py:127`。消费从连续游标逐条拉取，每条必须仍在当前信封有效期；生产者不会自动重签已上传的旧 event，错误也不形成可恢复缺口状态。

已复现：两端 fake OSS，TTL=10 秒。发 seq1，时钟前移 11 秒，再发 seq2（最新 manifest 有效）。consumer.poll 连续两次在 seq1 报 `ProtocolError 信封认证、身份或有效期校验失败`，cursor 保持 0，seq2 永远收不到。默认七天仅是更长的同一触发条件。

修复：明确历史重签/重发与保留窗口恢复合同，保持相同 event_id/source_seq，历史记录按真实 occurred_at 存储且不触发自动回复。对不可恢复缺口显式呈现并审计；不能关闭时效校验或静默越过旧对象。还须核对本地已 stage 后过期、相同 message_id 续签后的缓存刷新。

### R08：第 51 条未决命令永远得不到对账

位置：`server/runtime.py:40`。每轮固定最早 50 条 pending/unknown，没有重试时间或公平游标；持久 unknown 永久占位。

已复现：授权 51 个对手，离线传输均返回 unknown，51 条都已提交。再运行三轮：150 次 reconcile，只覆盖同样 50 个 command_id，剩余 1 条从未对账。OSS 的回执主要通过 reconcile 拉取，因此该命令即使远端已有结果也无法闭环。

修复：持久/可恢复的公平重试调度，按账号及 next_attempt 分配有界工作；停止屏障优先，慢网络不能串行占用所有账号。不是把 LIMIT 从 50 改成更大数。

### R09：UI 切换主通道没有切换采集消息上行

位置：`server/link_routes.py:90`，`relay/collector_bridge.py:108`、`:134`。业务库 binding.transport 改为 mail；Bridge 的 primary_transport 是构造时配置，原生事件仍只走原路。

静态已确认路径：OSS 故障时切邮件可以改命令路线，但对手聊天仍只尝试 OSS，谈价消息闭环中断。未调用真实通道。

修复状态：root 已裁决“事件上行投递所有已配置路线，命令出站仍择一主用”，传输实现者已开始 tx_event_route 每通道持久状态改造。须独立证明一条通道异常不阻断另一条、恢复后同 event_id/source_seq 去重、无自动双发命令。本报告尚不批准正在改写的版本。

## MEDIUM 问题

### R10：无效人工对手类型返回 500

位置：`server/business.py:manual_send`（发现时约 327 行，直接把 peer_id 传入 SQL）。

独立既有测试实际失败：`POST /api/monitor/send {cp:[],text:"合成消息",request_id:"bad-peer"}` 返回 500，预期 400/422；本轮已运行测试证明，非推测。

修复：写事务前验证 peer_id 的类型、长度、格式及权限；结构错误不得进入 DB 驱动；保留“不改变状态”独立断言。

### R11：后台失败被吞掉，无法定位真实故障

位置：`server/inbox.py:16`、`:29`；`server/app.py:62`；`server/chat_runtime.py:311`。

Inbox 吞掉 adapter.poll 异常返回正常，repeated 会清掉该任务错误；数据库重试也没有可查类别。Chat 生成、schema、权限、预算、工具及数据库故障都被写成同一个 validation_or_generation_failed。health.ready 只依赖登录配置。实际认证失败或 R07 永久阻塞时，运维无法判断是无消息还是链路坏了。

修复：保留无密钥的错误代码、模块、重试计数、最近成功/失败时间和关联 turn/command；不把异常全文、秘密或内部提示输出给交易对手。失败应继续 fail closed，但不能吞诊断。健康/状态只报告确有证据的就绪范围。

### R12：状态和行情每次读取全历史，增长无界

位置：`server/business.py:140`（_commands 全量命令）、`server/market.py:30`（每个 session 扫 commands）、`:137`（每页重取/过滤/排序所有行情）。

确定性复杂度：每次行情或状态请求至少读取本人全部命令和未归档 session，aggregate_sessions 对每个 session 扫全部命令；再重扫行情并排序。即使 UI 只显示 50 条，后端成本仍随全部历史命令/全部券增长，5 人定时刷新放大负载。未做生产容量压测，因此不宣称已测得具体延迟。

修复：按当前 session/原命令 ID 查询必要状态，预构建会话→未决命令索引消除嵌套遍历；公共行情快照与本人状态分离，分页/筛选在可索引层完成或采用有界快照索引。无须引入新分布式框架。

## 验证与限制

1. 20 个完整阅读文件 AST parse 通过。
2. 在本隔离根执行：

   ```text
   .venv/bin/python -m pytest -q tests/acceptance/test_auth_entrypoint.py tests/acceptance/test_chat_runner_safety.py tests/acceptance/test_dispatcher_safety.py tests/acceptance/test_manual_switch_http.py tests/acceptance/test_market_http_ws.py tests/acceptance/test_dual_transport.py
   ```

   结果 **110 passed、1 failed、0 skipped，10.00 秒，退出码 1**。唯一失败为 R10。执行时仍有并行写者，不能作为冻结版验收。
3. R01–R08 中的明确行为复现使用独立临时 SQLite、合成账户、fake transport/OSS、受控时钟；没有修改测试门。R09 为静态调用链，R11/R12 为已确认代码行为/复杂度，未冒称真实生产复现。
4. 新增缺陷已送 root 和独立 QA，由 QA 维护回归门。实现者不能自行改松用例将本报告关闭。
5. 人工冲突对账入口、聊天复合游标正在由 root 补写，本报告不把“计划补写”当成完成。真实 OceanBase、企业身份门户、采集账号、STS/OSS/邮件网络、真实模型效果仍须现场验收；主数据未配置返回 unavailable 是已授权范围，不应用模拟值补齐。
6. 当前批次未发现 CRITICAL，并不表示全库/所有自然语言边缘已穷尽。禁止成交/泄密属于硬边界，不能仅凭少量禁词案例或提示词证明。

## 后续评审要求

HIGH 修复后需给出精确文件差异、独立负例与必要正例；先复核根合同，再做冻结源码的全量 pytest/前端/集成及 source_drift 门。保留部署前置条件，不能合并不同快照的通过数宣称“0 回归/0 红”。

本轮建议：**REQUEST CHANGES**。

## 第一轮修复复核追加

本追加是在 root 通知修复后重新阅读最新 `chat_runtime/app/domain/inbox/business` 代码形成，未改产品或测试。

- R01：已看到源文净价/条件/遗漏有价观察的硬触停；COUNTER 现要求当前观察。旧安全绕过路径已修，但带来下述 R13 的正常上下文回归。
- R02：旧“我方卖出1000亿”路径已加共用检查；**仍未关闭**。最新独立复现把正文改为日常省略主语的 `000100.IB 卖出1000万，能帮忙问问吗？`，BUY/1000万 ASK_QUOTE 仍 queued。`expressed` 只匹配“我/我们/我方”，为空就放行，根合同尚未完整修复。
- R03/R04/R05：已看到裸码否决、首询名称约束/共享红线、可运行会话候选及旧 queued 清理；需 QA 新的针对性回归门与正例后关闭。
- R06：已看到精确出站关联核对/回执合并、无关联新人工 out 接管；需 QA 验证历史重放、相反顺序与双路重放后关闭。
- R08：已看到 `next_check_at` 持久重试与最多 5 个并行对账，当前写者仍在收尾，尚未复跑公平性与停止优先场景。
- R09：实现者正在按 root 裁决改为事件双路上行、命令单路出站；未冻结，不作完成声明。
- R10：已在写事务前检查 peer_id 类型；本次独立复跑 `test_chat_runner_safety.py + test_manual_switch_http.py + test_application_lifecycle.py` 为 **59 passed、0 failed、0 skipped，6.02 秒，退出码 0**，包含原 cp=[] 用例。R10 本例关闭，不再描述成最新仍红。
- R07/R11/R12 尚无本评审的关闭证据。

### R13 · MEDIUM：强制本轮新报价切断合法上下文还价

位置：最新 `server/chat_runtime.py::_check` 的 `还价必须基于本轮已核验对手报价` 分支（约 213 行）。

新增修复一律要求 COUNTER 有当前 observations，而 observations 又一律要求价格出现在当前原文，导致“对手已报实价，下一条只说再让一点”的正常多轮场景无法使用事实板。

已在最新代码独立复现：第一条 `000100.IB 我卖2.20%，1000万，实价` 以 WAIT+观察合法提交，peer_quote 已持久保存 2.20/ofr/firm/源消息 ID；第二条 `再让一点`，在 BUY/1000万/2.50→2.48/边界2.40/步幅2BP 的原授权内提出 COUNTER，无新 observations，结果 handoff。第一轮真实已核对报价仍存在，未撤价、未跨日、未变更权限。

修复：区分“本轮出现新价，必须完整核验”和“本轮没有新价，可引用当前会话仍有效且未撤回的已核报价”。旧价来源、方向、授权和控制版本仍由服务端检查；不能允许模型挑更有利的历史价，也不能通过禁止全部上下文还价绕开 R01。该事项已交 root 和 QA。

当前仍为 **REQUEST CHANGES**，尤其 R02 后续变体与 R07/R08/R09 的恢复闭环须取得新的关闭证据。

## 第二轮独立复核

本轮重新阅读 root 的最新改动，没有以旧红测判断新代码。复核过程中新增 R14–R16；均已即时交实现方与 QA。仍有并行写者，不是最终源码冻结结果。

### R14 · HIGH：迟到 unknown 擦掉人工裁决，旧证据重放重新制造冲突

位置：`server/business.py::receipt`，发现时约 206–235 行；新入口 `reconcile_manually`。

独立复现：`sent(e1) → cancelled(e2) → 人工裁决 failed → unknown(e3) → 重放已裁决 sent(e1)`。unknown 虽保留 command.status=failed，却重建 result_json，丢失 adjudicated、人工凭证及裁决元数据；最后的旧 e1 因而再次把命令置 unknown。

修复及复核：最新代码对已有终态的非结论 unknown 只写审计，不覆盖事实；已裁决 seen_receipts 幂等，新的实质矛盾仍可重新待核对。QA 新增真实 HTTP 三门已通过，本评审后续 108 项运行也包含这些门。**此已复現问题关闭。**

相邻状态收尾仍交 root 检查：非冲突 unknown 人工核对后，不能留下 session=WAITING_SEND 且 pending=NULL；若约定核对后不自动复谈，须显式 HUMAN/HANDOFF，已发送的真实价格与人工引用仍应保留。

### R15 · HIGH：已有历史对话挡住首次批次授权

位置：`server/business.py::_conversation`，约 92–104 行。

独立复现：新对手先收到一条历史“您好”，ingest 创建 HUMAN/social_authorized=0；本人再明确 preview→confirm 首次询价，原实现一概将 HUMAN 判为已接管，拒绝授权。真实上线先同步历史联系人会普遍触发。

修复及复核：最新代码只允许 control_version=1、social_authorized=0 的初始 HUMAN 在无在途命令时被首次确认授权，并推进 resume_seq。评审独立正反例：普通历史对话确认成功，AUTO、resume_seq=10；显式 takeover 后版本已变化，仍拒绝同样批次确认。**本项关闭**，不能据此放开所有 HUMAN。

### R16 · HIGH：旧券授权时间被拿来授权新券的迟到消息

位置：`server/business.py::ingest` 的 first_authorization 判断；`server/chat_runtime.py::_start/_check` 的 session 授权边界。

独立复现：t0 授权并发出 000100；停止机器人；t0+100 新确认并发出 000200；t0+101 收到 source_seq 新、historical=false 的 000200 报价，但 occurred_at=t0+50，早于新券授权。原实现取整段对话未归档 session 的 MIN(created_at)，旧 STOPPED 的 000100 使消息被 queued，ChatRunner 又对 000200 生成还价 queued。

修复及复核：最新 snapshot 包含 session.authorized_at，观察和交易动作分别检查源时间；入站授权时间判断也收紧。相同独立场景复跑为 not_runnable、总命令仍为两条首询，无第三条自动还价。**该复现关闭**；多券并行正例与恢复水位继续由 QA 保留。

### 原 R01–R13 的当前状态

| 项目 | 第二轮结论 | 证据/范围 |
|---|---|---|
| R01 | 已修复本轮绕过 | 净价/条件/源文有价漏观察硬触停；独立负例通过 |
| R02 | 已修复已报变体 | ASK 与 COUNTER 共同检查显式/省主语中文及英文方向，原省主语负例通过 |
| R03 | 已修复已报输入及输出券身份变体 | 裸代码输入否决；QA 另发现公开 `000100.SH` 藏在授权简称后，已加完整市场代码核对，最新门通过 |
| R04 | 已修复已报首询注入 | 名称范围及共享禁成交/秘密红线；首询注入门通过 |
| R05 | 已修复已报全局饥饿 | 可运行绑定/会话 + 每会话首条候选，控制切换终结旧 queued；后台行为门通过 |
| R06 | 已修复已报接管/echo缺线 | 精确原命令/账号/对手/正文/native ID 核对；新人工 out 接管。新历史重签路径仍需与 R07 联验 |
| R07 | **仍待传输恢复闭环** | event 可续签、已 stage 缓存可刷新已实现；采集方正在补有界 replay_events 与 transport_replay 标记，尚未完成本评审的端到端关闭 |
| R08 | 已修复固定前50饥饿 | next_check_at、公平有界查询；独立 51 未决 / 11 轮复跑，51 次对账覆盖51个唯一命令，0遗漏。慢网络停止优先仍应留正式负载门 |
| R09 | **实现中，待冻结独立复验** | 采集端每route事件队列与双路上行已见改动；单路故障、恢复重放及命令单路不变由传输角色收尾 |
| R10 | 已关闭 | peer 类型在事务前校验；独立HTTP用例通过 |
| R11 | **未关闭** | root将继续补结构化无敏感诊断，当前不声称已完成 |
| R12 | 部分修复，**未关闭** | 已看到状态聚合以索引替代嵌套遍历；公共行情/历史读取增长仍需最终方案和查询证据 |
| R13 | 已修复已报正常回归 | 无新价可引用服务端仍有效、未撤回的本会话报价，300秒有界有效期；当前新价仍须完整观察。合法“再让一点”正例通过 |

### 第二轮验证记录

- 初次最新 7 组验收：**137 passed、1 failed，13.56秒**；失败为 QA 新发现的公开错误市场后缀，已即时交 root。本条是修复前历史证据。
- 之后重新运行 Chat/manual-switch/lifecycle/business-http/dispatcher 五组：**108 passed、0 failed、0 skipped，11.11秒，退出码0**，包含上述公开错券修复和人工裁决相关门。仍不代表全量终验。
- 另以临时SQLite/fake transport独立复跑 R08 公平性、R15 初始授权/真实接管正反例、R16 新券授权前迟到消息；结果如上。

第二轮整体仍为 **REQUEST CHANGES**：R07/R09 尚在联动实施，R11/R12 未完成最终复核；原已关闭的具体复现不再计作当前未修复问题。所有最终通过声明仍需以冻结版本的完整验证为准。

## 第三轮独立关闭复核

重新阅读传输账本/Bridge、Inbox、诊断、人工核对及行情 SQL 改动，并运行既有独立验收。没有把前两轮的旧红测当作最新问题。原 R01–R16 的具体复现现可关闭；原现场限制、尚未冻结全量的限制仍保留。新引入的原生 echo 暂存/held 机制中另发现 R17、R18，**因此本轮整体仍不能准予冻结终验**。

### 原问题的剩余关闭证据

- **R07**：可信管理员 `CollectorBridge.replay_events(first_seq,last_seq)` 只允许真实 journal 内连续最多200条；同 event_id/source_seq/occurred_at/业务 hash 重新签封。`transport_replay=true` 经签封认证、单向保留，Inbox 将其作为只读执行属性而不写进 native 事实 hash。旧 stage 未 ack 缓存可用新 expiry 刷新；过期 send 不延长授权。既有独立重放门通过。恢复是明确管理员操作，不冒称任意保留窗口可自动恢复。
- **R09**：上行事件先持久排入所有已配置 route，然后各 route 独立提交/重试。命令与回执仍使用原 command.transport；原生 ID 和源序列共享，不自动双发业务命令。独立双路故障/恢复及去重门通过。
- **R11**：已读 `Diagnostics`，按 owner/component 保留无异常原文的 code、error_type、计数、成功/失败时间；owner 过滤 API 不显示其他交易员实体。Inbox 捕获异常后明确记录类别，Chat 区分阶段/turn，health 区分数据库和自动化配置。不能把无异常正文误称没有诊断，也不能把配置就绪误称真实通道验收完成。
- **R12**：正式 QB provider 走参数化完整 FilterSpec、事务快照 COUNT、二元 keyset `LIMIT 51` 及精确代码 IN；SQL未由用户输入拼字段。状态命令缩到活跃 pending/unknown，聚合改为 O(S+U)，批次命令按列/索引查询，监控只 resolve 在谈券。14项 SQL 开发回归和既有独立行情门在本轮执行中通过。COUNT/文本筛选仍可能扫描；broker 是明确2万券有界快照；跨页实时变化没有冻结快照语义，均已在实现报告声明，不额外承诺生产容量。
- **R14 相邻状态收尾**：人工对账现在同事务 HUMAN/HANDOFF、推进水位、终结旧 queued/running、取消候选；STOPPED/CLOSED 不被恢复。迟到 unknown 只审计，旧证据幂等，新实质矛盾可重新核对。原“人工核对后旧社交自动续跑”的独立门通过。
- **R01–R06、R08、R10、R13、R15、R16**：本轮相关既有独立门通过，未发现原复现回归。原生 echo 精确归属与延迟分类则以以下新增问题单独评审。

### R17 · HIGH：已过期 held 命令无法经查询取得终态

位置：`server/receiver_ledger.py::reconcile`，本轮发现时约260行，`if row and row['status']=='held': return pending`。

held 是已经持久接受、但从未进入 native 的命令。此分支不检查 expires_at、取消墓碑或控制屏障。若等待 echo 期间命令到期，原 command 签封会被 Bridge 拒收，不能再调用 execute；query 却仍无条件返回 pending，已知未执行的命令永久停留在途。

评审独立复现：t100 首个 native 回调内 stage_out，第二命令 expires_at=101 被置 held；首个命令成功并完成精确 native ID 映射，echo 已变 ready；t102 连续两次 reconcile 第二命令，均返回 `pending/native-echo-classification-held`，未调用 native。QA 随后新增对应独立红门，三轮统一定向运行也复现失败。

修复要求：held 查询需检查持久墓碑/版本屏障和原有效期；确定从未进入 native 的 expired held 可持久 failed，已取消/旧版本可 cancelled，并给可核对回执。真正 pending/unknown 不能沿用这个“未执行”推断。实现方已收到窄项修复任务，报告时仍待关闭证据。

### R18 · HIGH：基于已见版本的人工屏障挡不住此前授权但延迟到达的新版本

位置：`server/receiver_ledger.py::_manual_barrier`，本轮约141行，以及 execute 的 min_version 判断。

人工外发建立的屏障仅为 receiver 已见命令的 MAX(control_version)+1。这个值无法证明下一条更高版本命令是在人工接管以后授权，还是接管前已授权但网络尚未送达。

评审独立复现：receiver 执行过 v1；后台此前已授权 v2 命令但未送达；采集到 `role=human` 的新原生 out 后屏障变2；延迟的 v2 到达仍调用 native，返回 sent。这是人工外发之后才开始的新动作，并非已 claim 动作不能撤回的固有限制。

修复要求：解除人工屏障需要可核实的恢复因果证据，例如绑定本次原生人工事件的明确恢复确认/水位；单纯“数字大于 receiver 已见旧版本”不足。不能把 MAX+1 改 MAX+2 猜版本，也不能以真实 native 回调将来可能另做检查替代当前合同。此项已经报 root 进行窄合同裁决，实现者不应自行扩散成新平台。

### 第三轮执行证据与当前结论

本评审运行 auth/chat/dispatcher/manual-switch/market/lifecycle/business-http/dual-transport 和 SQL分页共9组：**202 passed、3 failed、0 skipped，26.44秒**。其中1红为 R17，另2红是新增安全响应头在未认证早返回401遗漏。后者 root 已统一 secure() 返回路径，本评审再次独立运行该门，**3 passed、0 failed，0.69秒**，不再把其作为当前未解决问题。

所有以上测试均在隔离根、临时数据及假网络执行；未读取或记录真实临时凭据，未触碰 MQ/STS/OSS/企点的生产发送。传输重放与新 held 机制的原生集成仍是可注入桥，不代表現役采集端已部署。

**当前建议：REQUEST CHANGES。原 R01–R16 可按上述证据关闭；R17、R18 是新阻断。** 两项取得独立负例、必要正例和根链证明后，才能给“准予冻结终验”的范围内结论；最终0红还须冻结源码、全量及source_drift门。

## 第四轮：R17/R18 关闭与进入终验结论

本轮严格限定 R17/R18 的两端实现、恢复因果关系及必要回归；未扩充功能范围，未修改产品或测试。

### R17 关闭

`ReceiverLedger.reconcile` 在短事务内识别 held，再调用与执行前共享的 `_unexecuted_failure`：取消墓碑、旧控制版本、未观察人工屏障均持久 cancelled；原 expires_at 已过则持久 failed。仅对 absent/held 的“从未进入 native”事实适用；已 claim 的 pending/unknown 保留真实核对路径，不能仅因过期伪称未发送。

独立原红门 `test_expired_held_command_reaches_failed_without_ever_calling_native` 已通过；开发正反门也在本评审运行中通过，包含真正 pending 到期不能被误判未执行。

### R18 关闭

已核对字段实际接线：

1. 采集账本按 peer 持久 `rx_manual_fence` 当前 ID/单调版本，并为原生事件固定 `rx_event_fence`。新人工事实更新控制对；同事件重复不新建版本。
2. 两路事件签封顶层携 `manual_fence_id/manual_fence_version`；Codec 校验 out、peer、无机器人 source_command_id 及签名。控制元数据与原生 payload/hash 分离，源 journal/续签/重放保持该事件原控制对。
3. 根 Inbox 将控制对传入受事务保护的 ingest。新版本即使作为只读历史补达也会建立 HUMAN/停止候选；旧版本不倒退，同版本不同 ID 拒绝。未授权初始会话用 social_authorized/human_lock 区别于实际接管，保留首次授权正例。
4. 明确 resume、批次确认或人工发送之后，命令从可信 conversation 快照携 `receiver_fence_id/receiver_fence_version`。浏览器及 Dify 不控制这些字段。
5. receiver 在 native claim 前要求控制对精确等于当前屏障，并同时校验原控制版本、期限、墓碑。仅更高 epoch 不再构成恢复证明；已提交旧人工草稿也不能绕过。

本评审额外运行真实对象链的离线替身验证，经过 `OSSTransport→Codec→CollectorBridge→ReceiverLedger` 和 `Inbox→InquiryService→Dispatcher`，未绕过字段装配：

```text
OLD_HIGH_EPOCH cancelled
UPLINK_APPLIED 2
BACKEND_CONTROL HUMAN 1 True
NEW_COMMAND_MATCH True True
EXPLICIT_MANUAL_RESULT sent NATIVE_CALLS 2
HISTORICAL_NEW_CONTROL HUMAN 2
OLDER_REPLAY_NO_ROLLBACK True True
```

其中2次 native 分别是原首询和看到屏障后明确授权的新人工消息；旧高 epoch 命令的回调带失败断言且未触发。第二个人工事件先作为带 transport_replay 标记的恢复消息到达，根仍正确更新控制事实为版本2/HUMAN；再重放版本1没有倒退。

### 本轮验证与版本

执行：

```text
.venv/bin/python -m pytest -q tests/acceptance/test_dual_transport.py tests/acceptance/test_manual_switch_http.py tests/acceptance/test_chat_runner_safety.py tests/acceptance/test_batch_lifecycle.py tests/developer/test_transports_bridge.py tests/developer/test_transports_ledger.py tests/developer/test_transports_protocol.py tests/developer/test_transports_io.py
```

结果 **170 passed、0 failed、0 skipped，24.26秒，退出码0**；1条既有 StarletteDeprecationWarning。另8个本轮关键 Python 文件 AST parse 通过。

本轮读取并核验的文件哈希：

| 文件 | SHA256 |
|---|---|
| server/receiver_ledger.py | 4ce7e258eabbca9bb921bd5e2dcfa1ec24daa10886d413fba7889e8d4be3e609 |
| server/transports.py | 2a4cfeecb668266bf94812a3b27fa72a762e550517f3c8600ff26b042e0f2b50 |
| relay/protocol.py | 8f2e95c27c99e33be33d3b349e6828b103e812b14cea25cffca80e109b27910e |
| relay/collector_bridge.py | bb2108c8f05b51e0b9c3268bcf7ed8c0d450ac3b3a4ebf7f9caedec24210bf7d |
| server/business.py | a2cfb52cd25622e3e53d66bebce0fa4b7d0075a4835c87fc0a8c29f5977914d9 |
| server/inbox.py | b02465db907e70e4427ffa3698088d343c7bdedb2ff95c8abca4f3b1856bee47 |
| server/chat_runtime.py | a9ff61f8d2560fdc67d807668106d0d9a7a12967e90738b5084c92575fa71528 |
| server/schema.py | e8ac3030b00fed2160bf4f9808f9f5ef269fae7f84edf72ba7fd3a7ec7e2fd42 |

**第四轮建议：APPROVE，准予冻结同版本终验。** 本报告18项具体问题均已按各轮证据关闭，在本轮所审范围内没有遗留 CRITICAL/HIGH 发现。此后业务/传输核心发生变化须按差异重新核验，不能自动沿用本结论。

最终仍由主执行方完成：冻结所有写者，同一源码根运行全量 pytest、前端/集成/MySQL等既定门及 source_drift 核验；对原目录集成先核 baseline 冲突。OceanBase现场、真实身份/账号、邮件/OSS/STS网络及采集端安装、Windows文件锁、真实模型效果等原有现场限制不因本轮离线绿而消失。本结论不授权真实外发或生产部署，不声称数学意义 exactly-once 或穷尽所有自然语言表达。
