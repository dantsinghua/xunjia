# 原始 R01–R40 整改对照

安琳，本表映射原始40项问题。2026-10-13 已从隔离工作树精确集成至原目录 `/home/anlin/work/xunjia-agent`，没有 commit/push。原目录独立完整验证通过：702项Python、52项Node、类型检查/构建，256文件前后零漂移，其中252项与冻结版本一致。唯一最终结果为 `/tmp/xunjia-verification-5mksioj3/result.json`，详见[验收报告](acceptance-report.md)和[交付结果](final-delivery.md)。

## 编号、裁决与证据口径

- **R01–R40** 只指[原始代码评审](../2026-10-13-code-review.md)。下表保留原始问题含义。
- [实施中终审](final-review.md)另起编号，最新已到 R18。本文将其写作 **F01–F18**，例如 F12 是查询增长问题，原始 R12 是全选范围；两者不能互换。
- 当前规格按[整改设计](../2026-10-13-remediation-design.md)、[Chat Agent 设计](../2026-10-13-dify-chat-agent-design.md)及后续明确裁决共同解释。Agent 可以查证和提出自然对话；确定性审核决定能否外发。实价且 gap 大于零可以继续争取，达标或触停才转人工，不能沿用原评审“实价一律停止”的旧建议。
- 后续明确删除 mock 服务：正式行情模块为 `server/market_cache.py`、`server/qb_source.py`、`server/quote_channel.py`；生产前端 `web/src/lib/mock.ts` 已删除，DTO 为 `types.ts`，前端合成夹具仅在 `web/tests/`。旧报告中的 `mock/` 路径与旧命令是当时快照，不可再作为当前启动说明。
- 行内**候选证据**及部分“待同版终验”措辞保留实施时的逐项记录；这些仓库内测试现已纳入上述最终冻结版统一门并通过。现场环境、真实数据及真实账号的缺口仍按各行保留，不能由测试通过推定上线。
- 统一验证由独立 QA 维护的 [`tests/run_verification.py`](../../tests/run_verification.py)执行并检查源码漂移，本表不替代其结果。原始R01–R40的实现映射与最终测试已收口；后续独立终审F01–F18也全部关闭。不同批次计数不相加。

## 安全与真实发送控制

| 原始问题 | 当前整改点与代码 | 可复核证据 | 仍待终验或边界 |
|---|---|---|---|
| R01 未认证即可发送/改配置 | [`server/app.py`](../../server/app.py)统一 API 认证；[`auth.py`](../../server/auth.py)验证 SSO 并签发独立会话；本人固定绑定；WS 首帧认证；生产入口不挂旧 `imlink.routes`，mock 服务已删除。 | [`test_auth_entrypoint.py`](../../tests/acceptance/test_auth_entrypoint.py)、[`test_business_http.py`](../../tests/acceptance/test_business_http.py)、[`test_market_http_ws.py`](../../tests/acceptance/test_market_http_ws.py)覆盖匿名、越权及 WS 边界。 | 候选证据；真实门户签名、部署来源限制、凭据与账号绑定仍需现场验收。 |
| R02 停止未控制执行链 | [`InquiryService.set_bot/stop_sessions`](../../server/business.py)、[`Dispatcher`](../../server/runtime.py)与接收账本贯穿目标状态、控制版本、queued 取消、在途 cancel/query；unknown 不显示成已停止。 | [`test_dispatcher_safety.py`](../../tests/acceptance/test_dispatcher_safety.py)、[`test_business_http.py`](../../tests/acceptance/test_business_http.py)、双通道报告。 | 候选证据；真实 native 动作不能由本地停止承诺撤回，须部署 v2 账本并取得远端停止证据。 |
| R03 监控 GET 外发、历史默认授权 | 查询与后台分离；[`app.py`](../../server/app.py) lifespan 驱动 Inbox/Chat/Dispatcher；[`business.py`](../../server/business.py)按授权时间、源水位与历史标记接收；[`chat_runtime.py`](../../server/chat_runtime.py)重新核验。 | GET 零业务副作用、历史只展示、关闭浏览器后台仍运行分别见 business HTTP、[`test_chat_runner_safety.py`](../../tests/acceptance/test_chat_runner_safety.py)、[`test_application_lifecycle.py`](../../tests/acceptance/test_application_lifecycle.py)。 | F15/F16 补齐首次历史对话授权与每券授权时间边界；须在最终同版重验。 |
| R04 模型自由回复绕过确定性规则 | [`ChatRunner`](../../server/chat_runtime.py)、[`domain.py`](../../server/domain.py)、[`public_text.py`](../../server/public_text.py)核对源文、券身份、方向、数量、边界、步幅、条件和最终正文；签证后原子写 command。 | Chat 安全门与 [`test_dify_chat_stream.py`](../../tests/acceptance/test_dify_chat_stream.py)；[真实隔离 Dify 导入报告](dify-import-report.md)。 | 按新 Chat 裁决实现；隔离 Dify 使用合成模型，不能证明真实模型质量。F01–F04/F13 的正反例须同版重验。 |
| R05 接管竞态、恢复重放旧消息 | [`business.py`](../../server/business.py)持久接管版本、human_lock 与恢复水位；Chat/Dispatcher 重读控制；原生 out 精确 native ID 关联，人工屏障通过双字段签封上行，新命令必须携带已观察的屏障对。 | Chat takeover/resume 门；独立 collector echo 20 项候选通过；[`receiver_ledger.py`](../../server/receiver_ledger.py)、[`collector_bridge.py`](../../relay/collector_bridge.py)及传输报告。 | F17/F18 的 held 到期终态和人工事件因果屏障已修复，并经独立QA及第四轮终审关闭；更高 control_version 本身不能解除人工屏障。 |
| R06 任务切换到错误账号 | 绑定、会话、turn、command 和 v2 信封冻结 owner/account/workbench/peer；[`TransportRouter`](../../server/transports.py)按本人账号固定路由，主通道切换须停止且无未决。 | auth、batch、dual transport、[`test_manual_switch_http.py`](../../tests/acceptance/test_manual_switch_http.py)身份隔离门。 | 两路上行持续同 ID/seq，切主用只切命令路线；真实多账号及双路部署待现场。 |
| R07 同文回执串确认、券状态互相覆盖 | [`business.py`](../../server/business.py)按原 command 与账号/peer 校验；传输核摘要签封；[`aggregate_sessions`](../../server/market.py)从本人逐会话事实汇总，冲突保持 unknown。 | dispatcher 错身份/迟到 ACK，business HTTP 冲突与人工裁决重放门；F14 后续回归。 | 候选证据；人工裁决不能被无证据 unknown 覆盖，新冲突证据仍须可重开，最终重验。 |
| R08 超时永久失败、重试不收敛 | 稳定 request/command ID；accepted/pending 与 sent 分开；[`Dispatcher`](../../server/runtime.py)按 `next_check_at` 公平对账；人工对账需明确结果、凭证及结果 hash，不重发。 | [`test_dispatcher_safety.py`](../../tests/acceptance/test_dispatcher_safety.py)、[`test_reconciliation.py`](../../web/tests/acceptance/test_reconciliation.py)；终审 F08 记录 51 条未决公平性复跑。 | 候选证据；慢网络和多身份负载仍需最终门；unknown 不允许换路盲补发。 |
| R09 JSON 并发丢状态、安全闸损坏放行 | [`schema.py`](../../server/schema.py)、[`db.py`](../../server/db.py)、[`business.py`](../../server/business.py)事务保存授权/消息/命令/审计；生产 MySQL 使用 READ COMMITTED，接收端持久账本先 claim 后 native。 | [`mysql_contract.py`](../../tests/acceptance/mysql_contract.py)隔离 MySQL 六门；batch/dispatcher 崩溃、并发与唯一性门。 | 隔离 MySQL 通过不等于现场 OceanBase；写满/恢复、迁移与备份仍须目标环境证明。旧 JSON 路径未作为新生产入口。 |

## 功能闭环与前后端契约

| 原始问题 | 当前整改点与代码 | 可复核证据 | 仍待终验或边界 |
|---|---|---|---|
| R10 状态查询和删除缺失 | [`routes.py`](../../server/routes.py)提供本人状态查询；生产按券 DELETE 已移除，改 `POST /api/sessions/{session_id}/archive`，Monitor 按单 session 归档；核本人、已停及无未决，不删消息或审计。 | 独立归档/导出/CSP/构建组合 17 项候选通过，含归档幂等、同对手另一券不受影响、pending/unknown 阻断。 | 原按 code 归档的合同差异已修复；仍待冻结同版终验。 |
| R11 绝对收益底价未进入谈价 | [`market_routes.py`](../../server/market_routes.py)存 `kind/value/revision`；预览明确冻结起始与边界收益率；[`chat_runtime.py`](../../server/chat_runtime.py)按冻结 policy 审核，BP 仅在有可信且新鲜估值时换算。 | batch/Chat 授权和边界门；前端 RFQ 预览门；债券工具报告。 | 候选证据；底价变更要求先停/对账/重确认，不静默改在途策略；现场估值缺口不能猜日期或净价换算。 |
| R12 全选遗漏完整筛选或带隐藏券 | [`market.py`](../../server/market.py)、[`market_cache.py`](../../server/market_cache.py)与 Quote 共用 FilterSpec；列表/count/resolve/export 同筛选，排除使用市场 code；确认只认服务端预览。 | 独立正式 QB→SQLite→HTTP 三门；[分页报告](market-pagination-report.md)；[`test_rfq_authorization.py`](../../web/tests/acceptance/test_rfq_authorization.py)。 | 固定集合一致性有候选证据；批次 resolve 最大 500 券，超限明确拒绝；跨 HTTP 市场变化不是冻结快照。 |
| R13 监控监听自动请求环 | [`Monitor.vue`](../../web/src/pages/Monitor.vue)监听稳定 peer ID，列表刷新 single-flight、聊天独立代次；历史页轮询保留当前复合 cursor。 | 浏览器挂载请求有界门；聊天 HTTP 9 门及实际 Monitor 历史翻页浏览器候选通过。 | 新增历史页/轮询已有独立证据，仍待冻结同版复验。 |
| R14 详情/追踪构造假数据 | [`api.ts`](../../web/src/api.ts)、BondModal/Chart/TraceModal 只映射返回事实；缺曲线、成交、追踪明确不可用；生产 `mock.ts` 删除为 DTO `types.ts`。 | 浏览器缺数据不造曲线/成交、真实历史曲线保留、缺 used 不造百分比；交易板缺值门。 | 部分真实主数据仍未接入；追踪缺事实时为空不等于真实审计可视化已全交付。 |
| R15 写失败包装成功 | [`http.ts`](../../web/src/lib/http.ts)、[`api.ts`](../../web/src/api.ts)保留 HTTP/业务失败；停止、预览、确认、对账仅按服务端结果更新；queued 不冒充 sent。 | 浏览器停止拒绝、预览重试、对账拒绝及导出 JSON 错误门。 | 候选证据；新控制入口须最终同版验证。 |
| R16 策略 DTO 不一致、校验污染保存 | [`market_routes.py`](../../server/market_routes.py)先完整校验再事务更新本人配置/绑定版本；[`api.ts`](../../web/src/api.ts)映射三参数及 S1/S2，前端轮次派生。 | 前端 API/策略开发者门；业务冻结授权及版本门。 | 候选证据；保存配置不会追改已授权 policy，需保留该边界的最终回归。 |
| R17 前端正式构建失败 | [`Link.vue`](../../web/src/pages/Link.vue)收敛本人 OSS/邮件两张固定卡，移除旧模式比较和无定义模板引用；类型与导入迁移。 | [前端报告](frontend-report.md)有各批 vue-tsc/Vite 通过记录。 | 原目录最终vue-tsc/Vite、正式构建产物服务和浏览器门全部通过。 |
| R18 OSS 入站依赖外部共享磁盘 | [`Inbox`](../../server/inbox.py)消费签封事件，业务提交后 ACK；[`OSSTransport`](../../server/transports.py)持久 stage/连续源水位；Bridge 发布原生事件，不读取外部业务库作为生产捷径。 | [`test_dual_transport.py`](../../tests/acceptance/test_dual_transport.py)实际 Codec/适配器/Inbox 与假 OSS；传输开发者端到端。 | 现役 collector 尚未安装 v2；必须停旧 xunjia poller，真实网络与原生 ID 接线待现场。 |
| R19 永久覆盖态、一次回执不收敛 | Quote 从本人 status/floor 恢复；Monitor 按稳定 command 轮询；删第二份人工聊天历史与长期状态 override。 | 前端开发者状态/回执门及生产 DTO 浏览器门；后台持续对账门。 | 候选证据；晚回执、刷新/换页、新历史翻页须同版联验。 |
| R20 邮件模式没有真实传输实现 | [`MailTransport`](../../server/transports.py)使用 SMTP TLS 与 IMAP SSL，固定白名单/单密封附件、UIDVALIDITY、持久去重；与 OSS 共用 ReceiverLedger。 | dual transport 及传输开发者门验证 SMTP 接受非 sent、坏邮件隔离、断点恢复。 | 使用假 SMTP/IMAP；企业 OAuth/网关、证书、账号认证与真实收发未验，不是本地 maildir 伪成功。 |
| R21 1500 字保护未接发送入口 | [`InquiryService.preview`](../../server/business.py)按每 peer 合并最终首句后检查长度，超限整批拒绝；RfqAssignModal 入口同口径。 | business HTTP `test_merged_unicode_text_over_limit_rejects_the_whole_preview`；前端合并/编辑回归。 | 候选证据；按 Unicode 字符计数，不按字节截断，不自动分段发送。 |
| R22 空绝对收益变零、非法数值落盘 | Quote 先拒绝空白；[`domain.decimal`](../../server/domain.py)拒 bool/空/非有限/不合法格式，保留合法零和负收益；policy 检查量、步幅与方向边界。 | HTTP 非法授权/Chat 数值门、前端空 yield 门、行情合法负值门。 | 候选证据；最终需覆盖切换 BP/yield 的完整预览链，不能把格式边界冒充所有业务限额。 |
| R23 聊天导出按钮空实现 | [`Monitor.vue`](../../web/src/pages/Monitor.vue)下载实际文件；[`market_routes.monitor_export`](../../server/market_routes.py)要求 start/end，支持 peer，本人范围分块生成 XLSX；MySQL SSDictCursor、SQLite fetchmany，每批 500 行；硬限 10 万行/2000 万字符。 | 独立导出验证 +08:00、账号/peer 隔离、500/500/1/0 读取、公式文本及超限 400；负载门逐行核对 10 万行本人数据。 | 原范围和批量物化差异已修复；最终 XLSX 仍形成响应文件，不宣称 HTTP 字节流式传输；待冻结终验。 |
| R24 Markdown 直接 v-html | [`Chat.vue`](../../web/src/pages/Chat.vue)使用 DOMPurify；[`app.py`](../../server/app.py)统一安全头，含匿名 401 的 CSP；显式 XUNJIA_WEB_DIST 提供实际同源前端构建。 | 浏览器 HTML/危险链接负例；独立安全头及实际 dist/assets 同源服务门，401 原红已修复重验。 | 原 CSP 待确认项已取得代码和候选独立证据；仍待冻结同版终验及部署响应核验。 |

## 行情准确性与恢复

| 原始问题 | 当前整改点与代码 | 可复核证据 | 仍待终验或边界 |
|---|---|---|---|
| R25 撤价未完整传播 | [`qb_source.py`](../../server/qb_source.py)写全撤 null 快照；[`quote_runtime.py`](../../server/quote_runtime.py)仅按字段存在性传递，前端 null 清空，缺字段保持。 | [`test_quote_truth.py`](../../tests/acceptance/test_quote_truth.py)单双侧撤价；市场 HTTP/WS 与前端 delta 门。 | 候选证据；真实源撤价与重连快照仍需现场。 |
| R26 净价冒充收益率 | [`qb_apollo.py`](../../collector/qb_apollo.py)按 quote_type 解释字段；净价缺换算保留净价，不进入收益率比较；未知类型不猜。 | quote_truth 净价/缺类型/未知枚举/负收益门。 | 真实协议字段及迁移未现场验；历史 NULL 类型不补猜值。 |
| R27 估值不更新、跨日簿串价 | [`qb_source.py`](../../server/qb_source.py)刷新估值/期限，按源日期清旧簿；缺估值日保持 null；持久 checkpoint 支持重启。 | quote_truth 估值更新、未知日期、新日无昨日最优；market_safety 重启回放门。 | 真实 cnbd_date 未核定协议字段号，不能补“今天”；目标数据源与跨日现场验证待办。 |
| R28 写库失败后游标前进漏行情 | [`market_cache.py`](../../server/market_cache.py)保存点/事务；静态、流水、latest、checkpoint 一起提交，失败恢复内存，提交结果未知从持久水位续读。 | quote_truth 与 [`test_market_safety.py`](../../tests/developer/test_market_safety.py)故障注入、提交响应丢失、线程串行。 | 候选证据；[`migrate_quote_truth.sql`](../../collector/migrate_quote_truth.sql)为显式迁移，未在现场执行。 |
| R29 同价不更新鲜度、delta 漏字段 | 去重纳入 issue_time；PUBLIC_DELTA 包含 fm/cl，附静态 s；按旧/新组并集发布，Quote 更新报价时间和组归属。 | quote_truth 同价新时点；前端静态/盘面/清算 delta 门。 | 候选证据；生产持续推送与组迁移重连须现场/最终联验。 |
| R30 超 2000 行新页被裁掉 | [`Quote.vue`](../../web/src/pages/Quote.vue)50 行服务端页、前后游标及跨页选中，去除头部 2000 行裁剪；SQL keyset。 | 分页报告 2051 券、同 flow_id 无漏重；独立正式 SQL 页集合门。 | 固定数据集已验；连续请求期间市场变动不提供不可变结果集语义。 |
| R31 凭据加载/连通判据不一致 | [`qb_gateway.py`](../../collector/qb_gateway.py)统一配置目录和 SID 合并；[`quote_probe.py`](../../imlink/quote_probe.py)握手后异常断开失败；app 仅显式 QB 部署配置启用，缺环境拒绝。 | quote_source/quote_probe 原测试与 market_safety；当前 app 显式配置路径；[部署说明](deployment.md)。 | **范围收窄**：本期采用管理员静态部署配置，普通交易员只读健康；不交付待测版本→热应用状态机。核心配置分裂与隐式凭据启用已整改，真实 QB 仍现场留口。 |

## 性能、交付与页面补充

| 原始问题 | 当前整改点与代码 | 可复核证据 | 仍待终验或边界 |
|---|---|---|---|
| R32 监控每次全历史重算 | summary SQL 聚合；status 限当前相关命令；聊天默认 50/上限 200、复合游标；只 resolve 在谈券；行情筛选/count/keyset 下推 SQL；聊天导出按 500 行读取。 | 独立聊天 HTTP 9 门、浏览器历史页；5 身份各 10 万历史、5 路只读与一路导出负载：SQLite P95 0.051745 秒，隔离 MySQL P95 0.627508 秒，候选测试均过 1 秒门。 | 原未压测/导出 fetchall 差异已闭合；数据/机器/并发规模固定，不承诺现场 OceanBase 容量。负载已纳入原目录最终同版runner且通过；最新数值见下方最终证据。 |
| R33 交付不可复现、文档偏离运行 | 新 [`server/manage.py`](../../server/manage.py)与生产 factory；requirements直接依赖声明及完整传递依赖lock、package-lock；[部署说明](deployment.md)明确迁移/配置；mock 服务删；测试与运行数据分离。 | [STS 更新](sts-mq-update.md)、[Dify 导入](dify-import-report.md)、独立 QA 记录；最终 runner 检查源码漂移。 | 干净环境按完整依赖lock安装、pip check、原根npm ci、同版完整回归和无冲突精确集成均完成；lock固定版本，未提供各发行包内容hash，其他平台另验。 |
| R34 多套状态/弃用实现并存 | 新生产 app 只装配 server 的事务链；mock 服务及生产前端合成构造删除；旧 imlink/relay PoC 不作为生产保护证据。 | [`test_retired_entry.py`](../../tests/developer/test_retired_entry.py)与生产 HTTP/后台调用链门；当前 imports 可核对。 | 旧 `imlink/dify_inquiry.py`、`relay/side_b` 等仍存在，不能宣称整库死代码已清空；现场禁止旧 poller 与新桥并行消费同前缀。 |
| R35 单经纪商行情停止更新 | Quote 每 3 秒 REST headSync；QBMarket 单 broker 读取当前书簿，独立筛选分页，不混用跨 broker 最优。 | 前端组件刷新门；分页报告 broker 103 券及容量门。 | 候选证据；broker 为最多 2 万券有界内存快照，真实新价时延与容量未现场压测。 |
| R36 旧范围响应覆盖新筛选 | Quote 共享 scope 与请求代次；账号/渠道/经纪商/筛选变化使旧读取结果失效，选择清理与加载状态同范围维护。 | 前端开发者旧请求反序、切筛选/翻页门；完整 FilterSpec 独立 HTTP 门。 | 候选证据；源码最终冻结后复验所有读取入口。 |
| R37 券简称/代码混用导致聚焦错位 | [`Monitor.vue`](../../web/src/pages/Monitor.vue)聚焦及消息 contexts 比较 code，name 仅展示；DTO 保留 sessionId/code。 | 前端开发者稳定身份门与生产 DTO 浏览器门。 | 后端聊天 DTO 当前仍需与真实 contexts/归属数据联验；同名券不能以名称降级合并。 |
| R38 读取故障伪成功空值、清聊天 | API 保持失败，Monitor 保留最后成功内容并提示；ImLinkBar 周期更新；[`diagnostics.py`](../../server/diagnostics.py)按本人返回结构化诊断，不泄漏异常原文。 | 浏览器失败保留内容、独立运行诊断门；终审第三轮已关闭 F11 的原复现。 | 候选证据；最终F01–F18全部关闭；原目录同版完整验证通过，错误仍按本人可见结构化诊断呈现。 |
| R39 人工发送中切对手写错显示 | Monitor 捕获账号、peer、券、编辑代次及 request_id；回执更新原任务，新输入不被清空；服务端 journal 为唯一历史。 | 前端开发者延迟响应门；manual HTTP 幂等及隔离门。 | 候选证据；真实原生 echo 与服务端回执去重须和 F06 同版联验。 |
| R40 离线账号判通过、诊断被刷新清空 | 新 Link 仅本人固定 OSS/mail；[`link_routes.py`](../../server/link_routes.py)结果明确“只读目录检查”，不声明账号可发送；Link 保留测试结果。 | 前端报告与 manual/switch HTTP 门；界面说明可直接核对。 | **语义收窄**：只读检查不证明 native running 或发送成功，外发以实际执行回执为准；真实登录状态/native 自检仍需 collector 接线与现场验收。 |

## 当前证据边界与交付停止条件

1. [独立验收报告](acceptance-report.md)保留候选批次、历史失败及修复后重验。诊断相关 20 红为 INSERT 回归，随后同相关三文件 105 通过，不能继续描述为当前仍有这 20 红，也不能由 105 通过推断全量绿。
2. Dify 1.17.1 新隔离环境在最终冻结后重新加载实际后端，23个加载模块hash匹配；原生run、SSE工具结果和签证digest经独立只读核验。使用合成模型/FakeTransport，结果不代替真实模型及账号质量验收，见[冻结版报告](dify-final-freeze-report.md)。
3. 原目录最终runner含MySQL六门及两数据库各5×10万历史负载，读P95为SQLite0.063928秒/MySQL0.489910秒，10万行导出5.298/6.667秒。规模、数据和机器固定，未验证现场OceanBase。
4. 新 STS 仓库只读核验提交为 `ac26e9ed72f816e2a19b9e1d01c9572bd8d214a4`。MQ 仅允许完整 HTTP 地址 `http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts`；SM4 协议未变。没有实际 auth.key/SM4 key、没有真实 MQ 请求；用户贴出的临时凭据不得进入报告或夹具。
5. 现场主数据保留 [`reference_data.py`](../../server/reference_data.py)薄接口，未配置返回不可用；真实行情、STS/OSS、企业邮件、门户、采集端 native 执行和 OceanBase 仍有现场边界。详见[采集端集成边界](collector-integration.md)。
6. 按券归档、导出范围/分块、CSP、聊天翻页和负载已纳入最终全门；R31以管理员静态部署的范围收窄收口。终审第四轮APPROVE，F01–F18全部关闭，F17/F18的held到期和人工因果屏障有独立正反例。
7. 合同差异、实现、独立门、源码冻结、完整统一验证、终审和原目录精确集成均已完成。结论是当前版本在所列测试合同内0红/未检出回归；本表不把现场留口或未知缺陷包装成已验证。
