# 现券询价平台代码评审与整改方案

安琳，本次评审结论为 **REQUEST CHANGES**，独立架构结论为 **BLOCK**。当前代码已包含真实行情采集及 OSS 外发能力，但停止、接管、账号隔离、价格规则、回执恢复等关键约束没有贯穿实际执行链。应先修复发送控制与行情准确性，再处理体验和代码清理。

评审日期：2026-10-13。基线：`main`，HEAD `26acaa0`，**包含当前未提交修改和未跟踪源码**，不是只审 HEAD 或 git diff。评审不修改业务实现、配置、正式数据或测试，不提交代码，也没有向真实企点、OSS、Dify 或行情服务发请求。

以下是本次合并重复发现后定位的 **40 项问题：CRITICAL 1、HIGH 22、MEDIUM 16、LOW 1**，不代表穷尽所有缺陷。代码位置针对本次工作树。严重度：CRITICAL 为真实外发安全阻断；HIGH 为主要逻辑错误或交付阻断；MEDIUM 为功能、恢复或维护问题；LOW 为有证据的清理项。尚未证明的生产影响、外部部署条件和原型范围均单独说明。

**评审与验证证据**

- 三条独立审查：代码审查覆盖 `imlink/`、`mock/`、`collector/`、`relay/`；架构审查核对规格、通道边界、状态与恢复；前端审查覆盖 `web/src/` 页面、组件、接口及行情链。主审复核关键证据并补充行情与底价探针。
- 后端测试在临时源码副本运行：**131 项，121 通过、10 失败，1.74 秒**。清除了业务环境变量，配置及数据指向隔离位置，拦截非回环网络。沙箱内最小 TestClient 同样阻塞，获准在沙箱外验证后正常完成；不将该沙箱阻塞计作代码问题。
- `npm run build`：**失败，8 个 TypeScript 错误**；类型检查失败后未执行 Vite 构建。前端两个 TS 测试文件 **8/8 通过**，不能据此认为页面闭环通过。
- 运行了合成发送、暂停、账号切换、回执、Vue 页面响应式逻辑及行情探针；未运行真实发送 E2E、真实 Dify 工作流、真实跨网收信、RPA 或生产数据库验收。
- `git diff --check` 通过；它仅说明空白格式检查通过。后端审查对 61 个 Python 文件进行 AST 解析通过；这不证明业务逻辑正确。
- 证据附件：[后端测试结果](2026-10-13-review-evidence/pytest.txt)、[前端构建结果](2026-10-13-review-evidence/frontend-build.txt)、[行情与底价探针](2026-10-13-review-evidence/market-probes.json)。

**安全与真实发送控制**

1. **R01 · CRITICAL · 真实发送及配置接口没有调用方认证授权。**

   位置：[mock/mock_server.py:122-126](/home/anlin/work/xunjia-agent/mock/mock_server.py:122)，[imlink/routes.py:57-126,258](/home/anlin/work/xunjia-agent/imlink/routes.py:57)，[mock/start.sh:22](/home/anlin/work/xunjia-agent/mock/start.sh:22)。API 没有身份校验，CORS 放行任意来源；启动示例监听 `0.0.0.0`。已用内存 ASGI 替身验证：无认证、携带外部 Origin 的 POST `/api/im/send` 请求返回 200，并调用一次发送替身。网络上能访问后端的调用方可以利用后端已有通道权限操作；是否已对外暴露未探测。整改：认证中间件、角色与账号/通道权限、受限来源、写操作审计；本机演示模式显式关闭真实发送。验收：匿名请求和越权账号请求不能读取敏感业务数据、改配置或进入发送函数；合法请求才通过。

2. **R02 · HIGH · “停止机器人”和“停止询价”没有控制真实执行链。**

   位置：[mock/mock_server.py:881-891,1070-1077](/home/anlin/work/xunjia-agent/mock/mock_server.py:881)，[imlink/dify_inquiry.py:252-290,542-553](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:252)，[web/src/pages/Quote.vue:602-608](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:602)，[web/src/api.ts:322](/home/anlin/work/xunjia-agent/web/src/api.ts:322)。前者只修改内存 `BOT`，后者只修改旧 mock `SESSIONS`；真实发送走 `imlink`，不读这些状态。前端还将选中行覆盖为已停止效果。开关传 `{on:false}`，后端只认 status，否则翻转当前值；重复“停止”甚至可能翻为正常，前端又不从 summary 回填权威状态。合成探针在 BOT=停止时仍触发发送。整改：建立真实会话的持久停止状态和幂等目标状态接口，模型调用前和投递前均检查，按账号、对手和券取消待执行任务。验收：停止后新消息、在途模型结果、缓存补发、页面轮询均不能外发；重复stop不会start，多标签和刷新状态一致。

3. **R03 · HIGH · 读取监控会触发外发，历史和未发起会话默认获准自动回复。**

   位置：[mock/mock_server.py:978-1015](/home/anlin/work/xunjia-agent/mock/mock_server.py:978)，[imlink/dify_inquiry.py:331-398,513-553](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:331)，[imlink/inquiry_rules.py:261-268](/home/anlin/work/xunjia-agent/imlink/inquiry_rules.py:261)，[imlink/monitor_feed.py:509-519](/home/anlin/work/xunjia-agent/imlink/monitor_feed.py:509)。监控 GET 启动 Dify/发信线程；入站查询读全历史，未知 peer 的闸门默认放行，没有必须存在“已授权询价”的条件。合成的 time=1、无券、无上一条外发的“你好”仍进入模型和 sender。整改：GET 改为纯查询；独立后台消费入站，维护账号游标和显式会话授权。历史导入默认只展示。验收：读页面不产生副作用；旧消息和未授权对话零外发；浏览器关闭后已授权后台处理仍工作。

4. **R04 · HIGH · 模型自由文本直接外发，确定性谈价规则未接入真实链。**

   位置：[imlink/dify_inquiry.py:45-52,279-290,389-397](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:45)，[relay/side_b/engine.py:76-129](/home/anlin/work/xunjia-agent/relay/side_b/engine.py:76)，[relay/side_b/run_b.py:17-29](/home/anlin/work/xunjia-agent/relay/side_b/run_b.py:17)，[docs/06-dify-workflow.md:3-5](/home/anlin/work/xunjia-agent/docs/06-dify-workflow.md:3)。真实链取模型 reply/answer 后只过词语红线；价格边界、方向、实价转人工等规则在另一条旧邮件 PoC 链。无禁词的越界报价原文可通过发送替身。整改：模型只给结构化抽取；规则服务验证券归属、BID/OFR、价格边界、量和会话状态，再生成固定话术。验收：越界、低置信、多券歧义、实价及附加条件均不能被自由文本绕过。外部 Dify 工作流配置未读取，不声称已经发生真实越界报价。

5. **R05 · HIGH · 人工接管存在竞态，交回机器会重新处理旧消息。**

   位置：[imlink/dify_inquiry.py:293-310,331-349,389-398](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:293)，[imlink/inquiry_rules.py:261-283](/home/anlin/work/xunjia-agent/imlink/inquiry_rules.py:261)。只在调用模型前查暂停；返回后直接发送，缓存补发也不查闸门。消息倒序处理，新消息先清除 `await_inbound`，旧消息随后获准。探针确认：调用中暂停仍发送；交回后混合新旧消息调用两次模型，预期一次。整改：保留接管版本和不可回退的消息水位，投递前再次核验；缓存任务共用检查。验收覆盖调用中暂停、缓存后暂停、交回后混合消息和重启。

6. **R06 · HIGH · 任务和缓存没有完整账号身份，切换通道可从错误账号回复。**

   位置：[imlink/dify_inquiry.py:33-42,252-279](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:33)，[imlink/monitor_feed.py:511-545](/home/anlin/work/xunjia-agent/imlink/monitor_feed.py:511)，[imlink/commands.py:116-119](/home/anlin/work/xunjia-agent/imlink/commands.py:116)。任务仅携带 peer/text/msg_id，真正发送时重新读取 `store.active()`；消息缓存和幂等键也未完整限定账号。探针确认：A 入站、Dify 期间切 B，命令使用 B。整改：所有入站、会话、标签、任务、幂等键携带工作台、账号、会话类型、peer 和 message ID，任务冻结来源身份。验收：切换账号不迁移任务；相同消息 ID、相同 peer 的不同账号互不影响。

7. **R07 · HIGH · 回执仅凭同文匹配，且券状态覆盖了不同账号和收件人的结果。**

   位置：[imlink/dify_inquiry.py:429-465](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:429)，[imlink/rfq_status.py:69-79](/home/anlin/work/xunjia-agent/imlink/rfq_status.py:69)，[imlink/routes.py:328-334](/home/anlin/work/xunjia-agent/imlink/routes.py:328)。`confirm_bonds` 不验证命令 ID、账号、peer、方向和时间窗；券状态只按 code 保存。合成的另一账号/对手历史同文回执把当前 failed 命令对应券升为“询价中”。一券多人的部分失败也会被后处理结果覆盖。整改：回执以命令 ID 关联；无 ID 的兼容路径必须多条件匹配；保留逐会话状态，券状态只做明确汇总。验收：同文重发、跨账号、昨日回执和部分成功不能互相确认。

8. **R08 · HIGH · ACK 超时被永久当失败，重试与补发无法可靠收敛。**

   位置：[imlink/oss_send.py:199-228](/home/anlin/work/xunjia-agent/imlink/oss_send.py:199)，[imlink/commands.py:116-119,141-175](/home/anlin/work/xunjia-agent/imlink/commands.py:116)，[imlink/routes.py:197-219,268,317-319](/home/anlin/work/xunjia-agent/imlink/routes.py:197)，[imlink/dify_inquiry.py:270-278](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:270)。等待超时写失败；后续成功 ACK 因已有 ACK/非 submitted 被丢弃。查询仅读本地记录，不持续对账。RFQ重试每次新 UUID，即使请求显式给相同幂等键仍下发两次；普通 send 不给键又默认 link+uin，第二条不同正文会返回首条命令；自动重试会被失败历史同文记录拦住。探针确认这些幂等问题与迟到成功仍failed。整改：区分已提交、结果未知、明确失败、送达；稳定业务ID+收件人键、后台 ACK 对账、逐目标重试；同key不同内容显式冲突。验收：丢响应、迟到 ACK、重启、部分成功后重试不重复发送；同联系人两次不同业务发送不被吞掉。

9. **R09 · HIGH · 业务 JSON 并发会丢更新，文件损坏时安全闸默认放行。**

   位置：[imlink/routes.py:395-402](/home/anlin/work/xunjia-agent/imlink/routes.py:395)，[imlink/inquiry_rules.py:105-108,125-133,194-204,265-268](/home/anlin/work/xunjia-agent/imlink/inquiry_rules.py:105)，[imlink/dify_inquiry.py:172-225](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:172)，[imlink/commands.py:74-89](/home/anlin/work/xunjia-agent/imlink/commands.py:74)。每请求新建 FloorBook/SessionGate，各自实例锁不能保护同一文件；LabelBook 又被多线程改写。并发探针两次不同券写入只留一条。损坏的暂停 JSON 被视为空状态，探针显示允许回复；损坏命令日志也被当空，丢幂等历史。整改：事务存储统一 inbox/session/outbox；损坏或不可读时停止外发并告警，保留证据和恢复路径。验收：并发、损坏、写满及各提交边界崩溃测试不丢状态、不放开停止闸。

**功能闭环与前后端契约**

10. **R10 · HIGH · 询价状态读取和删除接口缺失。**

    位置：[web/src/api.ts:399-400](/home/anlin/work/xunjia-agent/web/src/api.ts:399)，[imlink/routes.py:300](/home/anlin/work/xunjia-agent/imlink/routes.py:300) 附近的 RFQ 路由，`tests/test_rfq_status.py:153-179`。前端调用 GET `/api/im/rfq/status` 和 DELETE `/api/im/rfq/status/{code}`，当前后端未注册；现有测试实测 GET 为 404。整改：按现有状态模型补完整查询/删除契约，删除语义要区别“隐藏记录”和“停止会话”。验收：前端刷新可恢复状态、指定删除有效，其他账号和券不受影响。

11. **R11 · HIGH · 绝对收益底价只保存和显示，没有进入谈价输入。**

    位置：[imlink/routes.py:405-421](/home/anlin/work/xunjia-agent/imlink/routes.py:405)，[imlink/inquiry_rules.py:150-165](/home/anlin/work/xunjia-agent/imlink/inquiry_rules.py:150)，[imlink/dify_inquiry.py:229-236,354-369](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:229)，[web/src/pages/Quote.vue:625-634](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:625)。yield 模式写 `yields`，Dify 只读 `floors.get()` 的 BP 值；`get_yield()` 没有业务调用者。探针保存 2.10 后读取谈价 floor 为 null；若存在旧 BP 则继续使用旧值。整改：以带类型、方向、估值快照和版本的统一底价对象贯穿规则及显示，切换模式清除冲突。验收：BP→绝对值→BP 切换后规则输入与界面一致，缺底价拒绝自动谈价。

12. **R12 · HIGH · 全选未包含完整筛选条件，会带入隐藏债券，也会漏掉未加载债券。**

    位置：[web/src/pages/Quote.vue:514-519,568-579](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:514)，[mock/mock_server.py:848-861](/home/anlin/work/xunjia-agent/mock/mock_server.py:848)。选择谓词只传 groups/except，漏关键字、价格、状态、渠道；开始询价从 `bonds` 而不是过滤后的 rows 取券，且只取已加载窗口。探针仅显示 B1 时 RFQ 仍含 B1+B2。全选排除还用 windcode，而后端按 bond_uni_code 比较，真实 QB 两种 ID 不同。整改：列表、计数、全选和批量操作共享完整 FilterSpec 和统一 ID；发送前由服务端解析并提供可复核清单。验收：过滤后全选、单家经纪商、排除一券、跨页及超过窗口容量时数量和实际对象一致。

13. **R13 · HIGH · 监控选中对象监听形成自动请求循环。**

    位置：[web/src/pages/Monitor.vue:58,128-152](/home/anlin/work/xunjia-agent/web/src/pages/Monitor.vue:58)。reloadDesk 替换 cps 数组，使 selCp 成为新对象，再触发监听并调用 reloadDesk。独立 Vue 探针禁用 mounted 和定时器后，summary/counterparties 仍连续各调用 7 次，chat 6 次，之后主动悬停探针。结合 R03 还会放大后台副作用。整改：监听稳定的 cp ID，刷新列表与选中会话查询分离，加 single-flight 和失效请求隔离。验收：无操作无定时器时请求有界；正常轮询不叠加，切号/切会话不串响应。

14. **R14 · HIGH · 真实债券详情和链路追踪展示了构造数据。**

    位置：[web/src/api.ts:202-239,416-419](/home/anlin/work/xunjia-agent/web/src/api.ts:202)，[web/src/lib/mock.ts:275](/home/anlin/work/xunjia-agent/web/src/lib/mock.ts:275)。即便后端成功返回 trades/hist/intra，前端仍调用 buildBondDetail 生成图表和成交；traceOf 无条件 buildTrace，忽略实际 trace。界面没有与真实数据同等显著的演示区分，不能充当分析或审计依据。整改：真实模式仅映射真实返回，缺数据明确显示不可用；演示数据用独立开关与明显标识。验收：给后端返回唯一标记值，UI/导出/追踪必须保持，缺值时不得造数。

15. **R15 · HIGH · 写接口失败被转成成功，界面可能谎报已停止或已生效。**

    位置：[web/src/api.ts:49-55,305-314,322,374-375](/home/anlin/work/xunjia-agent/web/src/api.ts:49)，[web/src/pages/Quote.vue:602-608](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:602)。withMock 在异常或 ok=false 时返回 ok=true，多个控制/配置接口使用它；停止逻辑无条件覆盖状态并提示成功。整改：生产写接口禁止 mock fallback，保留错误码、原因和待确认状态；以服务端响应更新 UI。验收：断网、403、500、业务拒绝都不出现成功提示，不改变实际显示状态为已完成。

16. **R16 · HIGH · 策略参数契约不一致，后端失败校验还会污染已保存配置。**

    位置：[web/src/api.ts:308-314](/home/anlin/work/xunjia-agent/web/src/api.ts:308)，`web/src/components/StrategyModal.vue`，[mock/mock_server.py:943-974](/home/anlin/work/xunjia-agent/mock/mock_server.py:943)。前端传 startDev/boundary/step/strict 等，后端期待 start_offset_bp/boundary_bp/step_bp/strict_match 和 templates/system，在线保存也可能无变化却报成功。后端先改共享 t，再校验，返回失败不回滚。整改：建立双向明确 DTO，候选配置完整校验后原子替换并返回版本；真实引擎引用同一配置源。验收：保存后重读一致；非法请求前后配置完全相同；在途会话是否沿用旧版本须按规格验证。

17. **R17 · HIGH · 当前前端无法通过正式构建。**

    位置：[web/src/pages/Link.vue:9,170-173,441-479](/home/anlin/work/xunjia-agent/web/src/pages/Link.vue:9)，[web/package.json:8](/home/anlin/work/xunjia-agent/web/package.json:8)。Mode 仅含 oss/mail，但仍比较 local/lan/public，出现 7 条 TS2367；模板引用未定义 accountCard，另有 1 条 TS2339。整改：先明确需保留的模式（014 仍要求保留旧模式），再统一类型、表单和模板，补回/移除失效引用。验收：`npm run build` 完整通过，并实际覆盖所有保留模式。

18. **R18 · HIGH · OSS 跨网接收在本仓库没有闭环，仍依赖外部项目本地数据库。**

    位置：[imlink/dify_inquiry.py:28,522-529](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:28)，[imlink/monitor_feed.py:488-521](/home/anlin/work/xunjia-agent/imlink/monitor_feed.py:488)。sync_live 固定读本地 QTRADE_AGENT_DB，默认路径在另一项目的 real-chain 目录。本仓库有 OSS 目录、通讯录、命令/ACK，没有装配聊天消息对象消费器；缺库/读错返回空且 API 成功。分机部署可能发得出、收不到，并把故障显示成无消息。整改：显式实现入站适配器、持久游标、账号去重和故障状态；本地读库限于联调。验收：不挂载外网数据库，仅用假 OSS 跑通双向链；故障保留最后成功屏幕并提示。外部是否另有同步服务未核实，此项是本仓库交付缺口。

19. **R19 · MEDIUM · 状态覆盖缓存与一次性回执查询导致页面不能收敛。**

    位置：[web/src/pages/Quote.vue:40,514-519,585-598](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:40)，[web/src/components/RfqAssignModal.vue:235](/home/anlin/work/xunjia-agent/web/src/components/RfqAssignModal.vue:235) 后回执处理，[web/src/pages/Monitor.vue:170-213](/home/anlin/work/xunjia-agent/web/src/pages/Monitor.vue:170)。stOverride 永久优先于服务端；待回执后没有统一持续对账。Vue 探针服务端已报价，屏幕仍待回执。底价标签也仅在当前页面内存保存，刷新后可能表现为未设底价。整改：以版本化服务端状态为准，仅保留有期限的乐观态；统一轮询/推送回执并恢复持久底价。验收覆盖晚回执、切页、刷新、失败恢复及其他端更新。

20. **R20 · MEDIUM · 邮件通道有可保存入口，但测试和收发没有邮件实现。**

    位置：[web/src/pages/Link.vue:9,156-165](/home/anlin/work/xunjia-agent/web/src/pages/Link.vue:9)，[imlink/routes.py:103-106](/home/anlin/work/xunjia-agent/imlink/routes.py:103)，[imlink/store.py:104-107](/home/anlin/work/xunjia-agent/imlink/store.py:104)，[imlink/client.py:152-163](/home/anlin/work/xunjia-agent/imlink/client.py:152)，[relay/mailbus.py:39-52](/home/anlin/work/xunjia-agent/relay/mailbus.py:39)。mail 模式仍调用 HTTP probe，把 host:993 当 API 基址；旧 MailBus 是本地文件直投，未消费新表单的 IMAP 参数。整改：未交付前明确禁用；需交付则补独立邮件适配器及相同会话/回执模型。验收用假 IMAP/SMTP 完成测试、发送、读取、去重及故障恢复。

21. **R21 · MEDIUM · 合并报价的 1500 字保护只有函数和测试，没有接入发送。**

    位置：[web/src/lib/rfqAssign.ts:3-4,99](/home/anlin/work/xunjia-agent/web/src/lib/rfqAssign.ts:3)，[web/src/components/RfqAssignModal.vue:203-226](/home/anlin/work/xunjia-agent/web/src/components/RfqAssignModal.vue:203)，`imlink/rfq_assign.py`，[imlink/commands.py:95-99](/home/anlin/work/xunjia-agent/imlink/commands.py:95)。mergedOverLimit 无生产调用者，前端确认及后端均未实施同一收件人合并长度上限。整改：服务端执行最终合并长度检查，前端调用同口径预检，明确超长拒绝或人工确认分段。验收：单券不过限但合并后超限、Unicode、编辑后超限，均在写命令前阻断；必须测入口而非只测辅助函数。

22. **R22 · MEDIUM · 空绝对收益被转换为 0，缺少数值域校验。**

    位置：[web/src/pages/Quote.vue:628-633](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:628)，[imlink/routes.py:409-420](/home/anlin/work/xunjia-agent/imlink/routes.py:409)。Number('')=0，Number.isFinite 不能识别空输入；后端 float 也没有完整有限值和业务范围检查。探针空输入触发 setFloor(...,0,'yield')。整改：先校验非空，再验证 finite、精度、允许范围及方向；不要把合法负收益与缺值混为一谈。验收：空白、NaN/Infinity、非法字符串、越界值均不落盘，合法边界按明确规则保留。

23. **R23 · MEDIUM · 导出聊天记录按钮是空实现。**

    位置：[web/src/pages/Monitor.vue:256-270,446-464](/home/anlin/work/xunjia-agent/web/src/pages/Monitor.vue:256)，[mock/mock_server.py:1111](/home/anlin/work/xunjia-agent/mock/mock_server.py:1111) 附近。doExport 无条件提示没有可导出明细并关闭，即便页面已有记录；后端导出仅返回 JSON 摘要，日期参数未形成真实记录筛选。整改：实现当前账号、对手、时间范围的真实导出及字段脱敏，或明确将未完成能力禁用。验收：有数据可下载、内容与筛选一致，空结果才提示无数据。

24. **R24 · MEDIUM · Markdown 直接进入 v-html，问答输入可反射为 HTML。**

    位置：[web/src/pages/Chat.vue:30,90](/home/anlin/work/xunjia-agent/web/src/pages/Chat.vue:30)，[mock/mock_server.py:1231-1234](/home/anlin/work/xunjia-agent/mock/mock_server.py:1231)。后端未匹配问题会原样拼回，marked.parse 输出直接送 v-html。已核实可控输入到 HTML 的链路，未执行真实浏览器攻击；当前至少存在用户自输入反射，不声称已证明跨用户存储型 XSS。整改：禁原始 HTML 或对最终 HTML 严格清洗，限制 URL scheme，补 CSP。验收：事件属性、危险链接及原始 HTML 不执行，普通 Markdown 正常显示。

**行情准确性与恢复**

25. **R25 · HIGH · 撤价没有完整传播，旧价格持续留在列表。**

    位置：[mock/qb_source.py:378-382](/home/anlin/work/xunjia-agent/mock/qb_source.py:378)，[mock/mock_server.py:1288-1294](/home/anlin/work/xunjia-agent/mock/mock_server.py:1288)，[web/src/pages/Quote.vue:185-194](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:185)。全撤价时直接 continue，不清 quote_latest；单侧撤价的 None 被后端省略，前端又用 !=null 跳过，不能表达清空。探针全撤价产生 0 条新事件，库中仍保留 bid=2.0/ofr=1.9。整改：定义缺字段=不变、null=清空、tombstone=撤销；存储、WS、REST 和 UI 一致处理。验收：双侧撤价、单侧撤价、重连补快照及详情均不得展示已撤报价为有效。

26. **R26 · HIGH · 净价缺少换算收益率时，被直接当收益率。**

    位置：[collector/qb_apollo.py:80-90](/home/anlin/work/xunjia-agent/collector/qb_apollo.py:80)。17/31 缺失时直接回退 14/28，没有检查 quote_type；源码自身说明 14/28 可能是净价。合成 protobuf 的 quote_type=1、原始净价99.85 被解码为 bid_yield=99.85，后续进入最优比较。整改：按报价类型解释字段，缺收益率时保留净价并标缺换算，禁止跨口径比较。验收覆盖收益率报价、净价报价、缺换算字段、0/撤价及未知枚举。

27. **R27 · HIGH · 估值静态快照不更新，跨日报价簿未隔离。**

    位置：[mock/qb_source.py:156-164,175-212,299-320,375-377](/home/anlin/work/xunjia-agent/mock/qb_source.py:156)。static_done=True 后不再更新估值、久期和期限；同一个进程跨日保留昨日各 broker 行，新日数据只替换有新消息的 broker，最优比较仍包含旧日。探针估值输入由1.8变2.5，落库仍1.8；昨日 broker 的1.5仍胜过今日 broker 的2.0。整改：区分真正静态主数据与按估值日变化的字段，日切按交易日重建/失效报价簿；历史末价可以显示，但须标旧日且不参与当前最优。验收：持续跨日与重启跨日一致，底价计算使用正确估值日。

28. **R28 · HIGH · 写库失败前游标和缓存已经前进，重试会永久漏行情。**

    位置：[mock/qb_source.py:163-164,352,375-395](/home/anlin/work/xunjia-agent/mock/qb_source.py:163)，[mock/mock_server.py:1344-1347](/home/anlin/work/xunjia-agent/mock/mock_server.py:1344)。last_id、DIRTY、static_done、last_best 均先更新，之后才写 SQLite；失败仅记日志进入下一轮。探针首次写库失败后，同价后续数据被去重，quote_latest 仍0行。静态事务也没有异常回滚保护。整改：持久化成功后再提交消费水位和内存缓存；失败重放同批，事务异常必须回滚。验收：在静态写、流水写、提交前后注入故障，恢复后无永久丢失且不重复错误版本。

29. **R29 · MEDIUM · 同价新报价时间不推进，增量展示还漏盘面和清算字段。**

    位置：[mock/qb_source.py:384-386](/home/anlin/work/xunjia-agent/mock/qb_source.py:384)，[web/src/pages/Quote.vue:178-197](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:178)。去重 key 不含 issue_time，同价同量的新确认不产生流水；UI applyDelta 又漏 fm/cl 和报价时间。探针一分钟后的同价更新产生0事件；前端实盘/+0收到意向价/+1仍保留原样。整改：区分内容变化与新鲜度更新，完整实现 QuoteDelta 字段映射，并明确订阅组变化后的移除/加入规则。验收：只改时间、盘面、清算及从+0切+1时，时间、筛选和展示均收敛。

30. **R30 · MEDIUM · 2000 行窗口裁剪与翻页游标冲突，后续行无法浏览。**

    位置：[web/src/pages/Quote.vue:81-89,134,411-419](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:81)。追加后从头 slice(0,MAX_ROWS)，刚拉取的第2001行立即被丢弃，但下一页游标仍前进。Vue 探针已复现。整改：实现可前后翻页的窗口和锚点，或使用真正的服务端分页；不得在展示前丢弃正在加载的目标页。验收：超过2000行后仍能访问目标券，向前向后无重复/跳行，选择状态保持。

31. **R31 · MEDIUM · 行情测试、常驻采集和运行模式使用不同配置路径与判据。**

    位置：[imlink/quote_source.py:21-25,53-55](/home/anlin/work/xunjia-agent/imlink/quote_source.py:21)，[imlink/quote_probe.py:41-42,63-65](/home/anlin/work/xunjia-agent/imlink/quote_probe.py:41)，[collector/qb_gateway.py:21,27-35](/home/anlin/work/xunjia-agent/collector/qb_gateway.py:21)，[mock/mock_server.py:39-70](/home/anlin/work/xunjia-agent/mock/mock_server.py:39)。测试支持 QB_CONFIG_DIR 和单独 SID 合并，采集器固定默认目录且不合并；WebSocket 握手后立即错误关闭也可能被测试判成功。QB_MODE 在模块导入时决定，运行中保存凭据不切换已有源。整改：统一凭据加载器和连接状态机，显示真实采集进程健康；明确配置变更生效方式。验收：自定义目录、独立 SID、鉴权后立即断开、保存新凭据等情况下测试与实际采集结果一致。

**性能、交付与冗余**

32. **R32 · MEDIUM · 每次监控读取全历史并反复重建状态，数据增长后成本失控。**

    位置：[imlink/monitor_feed.py:509-519](/home/anlin/work/xunjia-agent/imlink/monitor_feed.py:509)，[mock/mock_server.py:978-1015](/home/anlin/work/xunjia-agent/mock/mock_server.py:978)，[imlink/dify_inquiry.py:325,391-395,513-553](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:325)，[imlink/commands.py:74-89](/home/anlin/work/xunjia-agent/imlink/commands.py:74)。summary/counterparties/chat 各读全量采集记录和 JSON；标签、命令历史整文件读改写。R13 请求循环会显著放大。整改：入站增量消费和物化视图；聊天分页；命令/标签索引及保留策略；查询不运行处理器。验收：按预期日消息量与历史保留周期做合成负载测试，查询开销不随全历史线性重算，内存与延迟有阈值。未做真实性能容量压测，不给出未经验证的吞吐数字。

33. **R33 · MEDIUM · 交付不可复现，测试与文档已偏离运行行为。**

    位置：[mock/start.sh:5](/home/anlin/work/xunjia-agent/mock/start.sh:5)，`mock/README.md`，[mock/mock_server.py:1-5,98-109](/home/anlin/work/xunjia-agent/mock/mock_server.py:1)，[imlink/dify_inquiry.py:1-5](/home/anlin/work/xunjia-agent/imlink/dify_inquiry.py:1)，[collector/store.py:10-16](/home/anlin/work/xunjia-agent/collector/store.py:10)，[mock/qb_source.py:39-45](/home/anlin/work/xunjia-agent/mock/qb_source.py:39)，`.gitignore`。启动硬编码旧 macOS 路径；未见后端依赖清单/锁文件；文档仍称离线、Dify不外发，但启动装配真实 uploader。默认数据库口令写入源码，实际是否复用未核实。此次 tests/ 全部未跟踪，部分运行游标、会话和邮件文件反而已跟踪。10项失败又暴露测试/上传装配/契约不一致，不能简单全部算业务bug或删测试变绿。整改：可移植入口、依赖锁定、明确 mock/live 能力矩阵；清理源码默认凭据；将测试纳入交付并由独立验收方裁定冲突；运行数据和合成 fixtures 分离。验收：新目录干净安装可运行，CI完成构建和测试，仓库不带真实运行状态/凭据。

34. **R34 · LOW · 多套状态和弃用实现并存，测试绿不能证明实际调用链受保护。**

    位置：[web/src/lib/rowStore.ts:42](/home/anlin/work/xunjia-agent/web/src/lib/rowStore.ts:42)、[web/src/pages/Quote.vue:178-247](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:178)，[web/src/lib/rfqAssign.ts:99](/home/anlin/work/xunjia-agent/web/src/lib/rfqAssign.ts:99)，[mock/mock_server.py:1080](/home/anlin/work/xunjia-agent/mock/mock_server.py:1080)，[imlink/inquiry_rules.py:97](/home/anlin/work/xunjia-agent/imlink/inquiry_rules.py:97)，`relay/side_b/engine.py`。RowStore 在 web/src 无调用者，实际页面另写合并规则；mergedOverLimit 未接入；_detail_rows 和 downgrade_unreceipted_inquiring 无业务调用者。旧 relay 引擎有独立入口，**不能直接认定整目录死代码**，但当前真实链不使用它。整改：先画实际调用图并确定保留入口；合并重复状态/规则；有独立用途的 PoC 明确隔离，无调用且无保留目的的代码删除。验收：生产入口到规则/长度/状态检查有集成测试；删除后构建及行为回归通过，不以删掉失败测试代替修复。

**独立前后端复核补充**

35. **R35 · HIGH · 选择单一经纪商后行情停止更新。**

    位置：[web/src/pages/Quote.vue:210-211,250-277,346-368,386-387](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:210)。非aggregate模式的onBatch立即返回；headSync依赖该回调，单经纪商又没有独立轮询。初次REST后价格被冻结，界面仍处于实时行情工作台。整改：提供单经纪商订阅或带失鲜提示的定时REST刷新，不能套用跨经纪商最优增量。验收：只停留该页签时，该经纪商新报价在规定时限内出现，其他经纪商变化不会污染。

36. **R36 · MEDIUM · 渠道、筛选和分页没有统一请求版本，旧响应可覆盖新范围。**

    位置：[web/src/pages/Quote.vue:81-89,317-321,358-365,465-478](/home/anlin/work/xunjia-agent/web/src/pages/Quote.vue:81)。首屏不校验scope；筛选只比较filterSeq；翻页和渠道只比较channel/broker，未比较筛选代次。快切渠道/筛选后，慢请求可能回填旧价格集合。整改：所有读取共享包含渠道、经纪商、筛选及代次的请求身份，拒绝旧响应，loading同样按代次维护。验收：反转首屏/切渠道/翻页/筛选响应次序，最终仅保留当前范围。

37. **R37 · MEDIUM · 债券聚焦混用简称和代码，消息与右侧券卡不能正确联动。**

    位置：[web/src/pages/Monitor.vue:54,77,105,350,362-368](/home/anlin/work/xunjia-agent/web/src/pages/Monitor.vue:54)，[web/src/api.ts:157-161](/home/anlin/work/xunjia-agent/web/src/api.ts:157)。右卡及初始化用name，消息contexts按code比较；点消息又把activeBond改code，右卡按name判断。整改：状态、路由、锚点统一code，简称只展示。验收：从路由、券卡、消息锚进入同一券时高亮一致；同名不同券不混淆。

38. **R38 · MEDIUM · 读取故障被包装成成功空结果，清掉已有聊天且不提示原因。**

    位置：[web/src/api.ts:318-335](/home/anlin/work/xunjia-agent/web/src/api.ts:318)，[web/src/pages/Monitor.vue:128-145](/home/anlin/work/xunjia-agent/web/src/pages/Monitor.vue:128)，[web/src/components/ImLinkBar.vue:16](/home/anlin/work/xunjia-agent/web/src/components/ImLinkBar.vue:16)。monitorChat异常返回ok=true/data=[]，其他查询也用成功空值；reloadDesk因此清缓存，imFeedError却只被清空。独立探针已确认网络异常产生成功空数组。目录状态主要在挂载时读取，开页后离线也可能不及时显示（后端发送闸仍能拒绝，不能据此声称可绕过后端）。整改：区分失败与空结果，保留最后成功内容、更新时间及错误；定期更新通道健康。验收：有数据→失败→恢复时内容不丢、错误可见、状态及时恢复。

39. **R39 · MEDIUM · 人工发送等待期间切对手，成功消息被写到新对手并清空新输入。**

    位置：[web/src/pages/Monitor.vue:163-167,176,188-194](/home/anlin/work/xunjia-agent/web/src/pages/Monitor.vue:163)。请求使用发起时peer，pushHuman使用完成时selCp/activeBond。实际Vue探针给A发的消息落入extraMsgs[B]并挂B券；当前输入也被清空。本项是本地显示归属错误，与R06的真实账号错误独立。整改：捕获不可变账号、peer、券、cmd_id和编辑版本，回执只更新原任务；通过ID合并临时行与后端回流。验收：延迟回执期间换对手/账号并输入新文，消息仍属原对手、新输入保留、回流不重复。

40. **R40 · MEDIUM · 通道测试可把离线账号判为通过，前端又清掉诊断步骤。**

    位置：[imlink/client.py:199-231](/home/anlin/work/xunjia-agent/imlink/client.py:199)，`imlink/store.py` 的save_test，[web/src/pages/Link.vue:130-131,237-241](/home/anlin/work/xunjia-agent/web/src/pages/Link.vue:130)。probe只展示state，没有要求running；离线账号旧sessions接口可读就通过。探针login_required、contacts404、sessions200得到passed=true。前端收到steps后刷新fill又清空steps/foundAccounts，探针失败原因变为空。整改：按状态和稳定self_uid验证在线，失败不踢其他健康绑定；表单刷新与测试结果分开管理。验收：offline/login_required/空UID拒绝，running通过；测试原因和账号选项在刷新后仍可见。

**功能完成度边界**

| 能力 | 当前结论 | 验收所需补充 |
|---|---|---|
| 真实行情列表 | 已接通，但撤价、口径、估值与恢复有缺陷 | R25–R31 的异常和跨日用例 |
| 多券多收件人开始询价 | 已有下发；选择范围、状态和重试不可靠 | R06–R12、R19、R21 的集成验收 |
| 停止、暂停、人工接管 | UI/接口存在，真实链控制不完整 | R02、R05 的在途与重启验收 |
| 自动谈价与拿实价转人工 | 有旧规则和模型回复，真实链未统一到规则 | R04；明确状态转移、方向、边界和人工确认 |
| L1–L5 归属、多券拆分、撤回、跨日首句确认 | 设计有要求，不能以当前自由文本外发证明已实现 | 建立逐项规格到执行代码的映射及黄金样本；不从旧PoC测试推断新链能力 |
| OSS 跨网双向聊天 | 下发和目录已有，入站依赖本地其他项目数据库 | R18；无共享磁盘的双向收发验收 |
| 邮件模式 | 当前入口无完整传输实现 | R20 |
| 真实曲线、成交与审计追踪 | UI仍可能生成合成内容 | R14 |
| 记录导出 | 按钮占位 | R23 |
| 保存筛选方案 | `Quote.vue:777`按钮无事件；08中是待建能力 | 明确未支持或实现持久化，不以按钮存在当完成 |
| 全量关键字与本地条件筛选 | 仅过滤已加载行，见`Quote.vue:377,514` | 明示范围或移至服务端；搜索后页券应可达 |
| 问答历史及溯源 | 固定CONVS/点击重问，尚非持久历史；文案有溯源承诺 | 作为规划能力标注，不宣称已有；不要求本期重做整套问答 |
| 人工确认成交 | 按钮明确disabled | 视作未开放能力；未发现可据此绕过确认的现有入口 |
| 国产数据库、OT/授信及部分主数据 | 文档含后续方案和待确认口径 | 作为生产化依赖登记，不能直接断言当期漏做 |
| 智能问答重做、Pinia、页面架构重拆 | 最新014明确不在本期改造范围 | 不以这些规划扩大整改范围；现有HTML渲染漏洞仍需修 |

最新 `docs/014-前端交互说明.md` 仍是待审交互稿，旧规格列有未决口径；测试又引用仓库外的2026-10-12交接。本次未获得该交接全文。对已经有可复现错误的项目直接整改；对交付范围有争议的能力先由产品/验收方确认，不用旧快照覆盖当前实现，也不把所有规划能力一概判缺陷。

**整改顺序和责任划分**

| 批次 | 目标与覆盖 | 交付及独立验收门 |
|---|---|---|
| A 先约束真实外发 | R01–R06、R09、R15 | 统一授权/停止/接管检查；GET零外发；在途结果和切号不绕过。通过前不应把此版本用于无人值守自动外发。 |
| B 统一业务状态与回执 | R07–R12、R16、R18–R22 | 持久会话与命令outbox、入站inbox、回执关联、幂等和对账；补接口与底价类型；双向链及崩溃恢复通过。 |
| C 修行情真值 | R14、R25–R31、R35–R36 | 统一报价口径、撤价协议、日切和估值版本；写失败可重放；真实图表不造数，所有已支持渠道持续更新。 |
| D 修页面闭环和交付 | R13、R17、R23–R24、R32–R33、R37–R40 | build通过、完整测试通过、筛选/分页/回执/导出场景通过，mock/live入口清晰可复现。R17可与A并行修复以恢复验收入口。 |
| E 最后清理 | R34及前述替代后的旧路径 | 先锁行为、再删除无用代码和重复状态；不预先大重构，不新增无必要的依赖或微服务。 |

建议保留现有 Vue + Python 服务边界，在现有后端内部收敛三个职责：业务会话/规则、传输适配器、查询视图。通过事务数据库保存账号范围内的 session、inbox、outbox、receipt、policy_version 和审计事件。用户动作是授权命令，模型结果是待验证输入，传输 ACK 是独立事件；三者不能相互冒充。当前不建议为修复这些问题先拆微服务或同时迁移整套数据库。

实现者负责修复和开发者测试；独立验收者维护入口级场景和故障注入用例；文档在行为稳定后更新；终审复核真实调用链与证据。被验收门阻断的实现者不自行改松验收门。所有外部真账号验收另按授权执行；本报告不替代价格/成交操作确认。

**必须覆盖的回归场景**

1. 未登录、错误角色、越权账号、非允许来源进入配置/发送接口。
2. 全局停止、单券停止、人工接管，分别发生于入站前、模型中、排队后、投递前和重启后。
3. 两账号同 peer/同 message ID，模型期间切号；旧消息导入与跨日首句。
4. 同文不同收件人、同文重发、部分批次失败、ACK晚到、上传结果未知及服务重启。
5. BP/绝对收益切换、BID/OFR、越界/非有限数字、实价/模糊/多券及非价格条件。
6. 全撤价、单侧撤价、净价缺换算、估值更新、跨日、同价新报价和数据库失败后重放。
7. 关键字/价格/状态/经纪商筛选后的全选和排除、超过2000行、切页恢复状态。
8. 真实详情不生成模拟值、审计追踪可回到原始事件、导出范围准确、Markdown不执行HTML。

解除阻断的条件是以上适用场景得到独立验证、构建和测试有新证据、真实发送/跨网/生产数据库的未验证边界明确，而不是仅把当前10个失败测试改绿。本次仅完成评审与整改方案，**尚未实施修复**。
