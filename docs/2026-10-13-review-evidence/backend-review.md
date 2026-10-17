# 安琳：Python 后端独立代码审查

## Code Review Summary

**Files Reviewed:** 44（关键路径阅读；另对全部 61 个 Python 文件做语法诊断）  
**Total Issues:** 12  
**Recommendation:** REQUEST CHANGES

### By Severity

- CRITICAL: 1（必须修复）
- HIGH: 8（必须先关闭风险）
- MEDIUM: 3（建议修复）
- LOW: 0

本报告针对 2026-10-13 当前工作树，包含未提交修改和相关未跟踪源码。只做审查和内存复现，没有修改源码、数据或测试，没有启动服务 lifespan、ADB、真实采集器、OSS 或账号发送。宏观架构由另一审查通道负责；行情撤价、跨日和写失败的专项证据交主线汇总，不在本报告重复计数。

规格依据：`docs/00-README.md`、`docs/05-workflow-statemachine.md`、`docs/09-实时行情管线契约.md`、`docs/10-实时数据流对接说明.md`、`docs/12-im-collector-对接说明.md`、`docs/014-前端交互说明.md` 与 `mock/README.md`。014 自称待审阅，因此其新增要求仅作背景；下面阻断项主要基于可执行代码与明确发送安全契约。已排除文档明确延期的 callback、未接入 QT/做市商、PoC 真机定位表等，不把这些自动记为缺陷。

### Issues

#### C1 [CRITICAL] 真实发送和通道配置接口没有调用方鉴权，且允许任意 Origin

- 位置：`mock/mock_server.py:122`、`mock/mock_server.py:124`、`mock/mock_server.py:102`；`imlink/routes.py:21`、`imlink/routes.py:258`。
- 触发：部署启动流程无条件安装真实 OSS uploader；能访问 HTTP 后端的调用方，无须任何认证即可访问 `/api/im/send`、`/api/im/rfq/send`、绑定修改等接口。CORS 同时开放所有源、方法和请求头。旧 README 的“离线 mock”已不能代表运行行为。
- 影响：攻击者可借已配置的真实账号发询价、改变通道绑定、读取通讯录和聊天记录。实际公网/局域网可达范围未探测；“应用接口无鉴权”已经确认，是否另有反代隔离需部署方核实。
- 已验证：在 ASGI 内存传输中对 `/api/im/send` 发无认证请求，带 `Origin: https://untrusted.example`，返回 HTTP 200，发送函数替身调用 1 次，响应 `Access-Control-Allow-Origin: *`。没有网络或真实发送。
- 修复：为后端读写接口落到明确调用方身份与权限；至少将发送、凭据和绑定修改纳入同一认证依赖，限制受信任 Origin；默认启动必须明确选择离线或真实发送能力。不能仅靠隐藏按钮或外部采集端鉴权，因为采集凭据已由本后端代持。
- 验收：匿名/无权调用发送、配置、聊天读取必须拒绝且下游调用数为 0；可信身份正常通路通过；恶意 Origin 的预检和跨域请求不被许可。

#### H1 [HIGH] 模型回复原文直接外发，绕过报价、边界与转人工规则

- 位置：`imlink/dify_inquiry.py:389`、`imlink/dify_inquiry.py:392`、`imlink/dify_inquiry.py:279`；关联 `relay/side_b/phrase.py:59`。
- 触发：监控读取进入 `sync_live`，`run_pending` 从 Dify 输出提取任意 `reply_text`，随后调用 `dispatch_dify_reply` → `_write_oss` → uploader。此路径没有进入 `relay.side_b.engine.decide`，只过成交词/内部字段词表。
- 影响：起始偏离、探价边界、步幅、追问量次数、拿实价即转人工均不能约束模型生成的价格和话术。对方消息或模型错误可让机器人作出越界报价。与 docs/05“报价/让步/比大小一律规则引擎算”直接冲突。
- 已验证：模型替身返回 `bid 102584288.IB 25宜春发展MTN003 5000W 估值-999能出么`，底价输入为 5 BP，原文仍到达发送替身；真实 `guard()` 对此返回允许。
- 修复：把模型输出限定为意图与结构化抽取，外发文本由规则判断后的允许动作和模板产生；在发送边界验证券锚、方向、价差与会话状态。原始模型回复只能作为待人工审阅建议，不能直接进入 uploader。
- 验收：越界价、多券归属不明、实价、反问、非价格条件和低置信输出均不得直接发送模型文本；需观察规则动作、转人工状态与下游调用数。

#### H2 [HIGH] 机器人停止、会话暂停不能阻止已生成回复的发送

- 位置：`mock/mock_server.py:1070`；`imlink/dify_inquiry.py:293`、`imlink/dify_inquiry.py:309`、`imlink/dify_inquiry.py:393`、`imlink/dify_inquiry.py:546`。
- 触发：`/api/bot/toggle` 只修改展示用 `BOT.status`，实际 Dify/发送路径没有读取它。`run_pending` 仅在调用模型前检查 SessionGate，模型返回后不重新检查；`flush_replies` 对缓存回复直接发送，完全绕过 SessionGate。
- 影响：点击停止或人工接管后机器人仍可发消息，与操作员同时报价。暂停期间读取监控页仍会触发缓存补发。
- 已验证：把 `BOT.status` 设为“停止”后仍观察到发送调用；缓存补发复现中 `dispatch_calls=1`、`pause_gate_calls=0`。
- 修复：在每次真正提交指令前统一重验全局停止和会话暂停状态；暂停使正在等待模型结果、缓存重试和队列任务全部停止发送；恢复只允许恢复时点之后、满足会话条件的新入站。
- 验收：分别在模型调用前、模型返回前、等待上传/重试期间停止或暂停，停止时点后的外发计数应为 0；恢复不能补发暂停前缓存。

#### H3 [HIGH] 异步回复未绑定原账号，切换当前通道会从另一个账号发出

- 位置：`imlink/dify_inquiry.py:263`、`imlink/dify_inquiry.py:279`、`imlink/dify_inquiry.py:393`；关联 `imlink/monitor_feed.py:511`、`imlink/monitor_feed.py:534`。
- 触发：监控先为账号 A 读取消息并调用模型。Dify 等待期间用户切换当前通道为 B；后台线程发送时重新执行 `store.active()`，将 A 的回复封装为 B 的指令。传给 sender 的仅有 peer/text/msg_id，没有原 workbench/account/session。
- 影响：错误账号对原对手外发，甚至把不同机构/席位的询价上下文交叉披露。标签主键 `message_key` 也只用 ext_msg_id，暂停以裸 peer 记账，不包含账号；跨账号 ID 相同还会共享识别/补发状态。
- 已验证：内存复现输入 `account-A` 入站；在模型替身返回前切换为 `account-B`；发送替身实际接收到 `account-B`。
- 修复：在接收消息时固化 workbench、account、session、原生消息 ID，并把这些身份一路传至发送与状态键；发送时验证绑定仍满足授权，切换当前 UI 通道不能改变已有任务来源账号。
- 验收：使用两个账号、相同 peer 和相同原生消息 ID；在模型等待期间切换当前账号，A 的任务只能按明确策略取消或从 A 发出，不能用 B；两号标签和暂停状态互不影响。

#### H4 [HIGH] 首次打开监控会处理全部历史入站，包括未发起询价和跨日消息

- 位置：`imlink/monitor_feed.py:509`；`imlink/dify_inquiry.py:331`、`imlink/dify_inquiry.py:349`、`imlink/dify_inquiry.py:354`；`mock/mock_server.py:991`。
- 触发：`read_collector` 全量读取账号所有消息，不限制日期、会话种类或已授权询价。首次没有 labels 时，`run_pending` 逐批选取尚未识别的入站；没有前序外发、债券代码或底价仍调用模型并发送回复。
- 影响：打开监控、清理/损坏标签文件或接入旧采集库，会触发历史聊天补回复，可能向非询价联系人发送消息；跨日首条人工确认的契约失效。读取接口还隐含触发真实外发副作用。
- 已验证：`time=1`、正文“你好”、无前序外发/无券/无底价的入站，产生 `model_calls=1`、`sent_calls=1`。
- 修复：为自动回复建立明确的启用时点/消费游标和已发起询价范围；只处理匹配活动业务会话的新增消息；历史回看、群聊及跨日待确认消息保持只读/人工处理。不能用“最多两条”当边界，它只会分多次重放全部历史。
- 验收：装入昨日消息、群消息、陌生人寒暄以及未发起询价的消息后反复读取监控，外发为 0；新活动会话按顺序处理一次；跨日未经确认不发送。

#### H5 [HIGH] 超时或目录暂不可用被写成不可逆失败，迟到成功回执无法纠正

- 位置：`imlink/oss_send.py:225`、`imlink/commands.py:148`、`imlink/commands.py:170`；关联 `imlink/routes.py:197`。
- 触发：上传已成功，但回执读取超时，`deliver_uploaded` 写入 failed；或者 GET receipt 时目录暂时陈旧/不可读，`pull_receipt` 直接生成 failed ACK。之后 `apply_downlink` 遇到已有 ack 或非 submitted 状态立即返回，拒绝迟到成功回执。读取 receipt 接口本身没有重新拉取真实 OSS 回执。
- 影响：实际可能已发出的询价永久显示失败，人工重试生成新指令后可能重复发。目录健康状态被误当成单条命令的最终执行结果。
- 已验证：同一指令先 `apply_downlink(ok=False)`，随后 `apply_downlink(ok=True)`，最终状态仍为 `failed`。
- 修复：区分已确认拒绝和发送结果未知；超时/目录缺失保持可对账的未知状态，按原 cmd_id 拉取并处理迟到回执。只有确定未发送或明确失败时允许新业务重试，不能靠新的随机命令绕开不确定结果。
- 验收：上传成功后延迟 ACK、临时 403/网络错误、目录短暂过期后恢复，最终状态能由真实 ACK 正确收敛；过程中不得重复外发。

#### H6 [HIGH] 读库回执仅按正文匹配，昨日或其他对手消息会误确认当前询价

- 位置：`imlink/dify_inquiry.py:437`、`imlink/dify_inquiry.py:457`、`imlink/dify_inquiry.py:482`。
- 触发：`_receipts_of` 丢掉账号/peer/session，只保留 text/ext_msg_id/ts；`confirm_bonds` 将所有回执按规范化正文建字典，不校验时间或接收人。当前指令只要正文与历史某条外发相同就升为“询价中”，甚至覆盖“发送失败”。
- 影响：同一模板发不同对手、同一对手重复询价、跨日复用报价文本都会假报送达。状态按券聚合还会掩盖同券某个收件人的失败。
- 已验证：当前 account-A/peer-new 的失败指令，配上前一天 account-old/peer-old 的同正文回执，返回状态“询价中”。
- 修复：优先通过 cmd_id/采集端 trace_id 对应具体外发消息；若外部协议只能读库匹配，必须同时约束账号、会话、接收人、发送时间窗口和唯一候选，歧义保持未知；先保存每指令/每收件人状态再聚合。
- 验收：同正文不同账号/peer/日期不能相互确认；失败指令不能被旧消息提升；同券两收件人一成功一失败应保留各自结果。

#### H7 [HIGH] 暂停和幂等状态损坏时静默返回空，恢复为允许发送

- 位置：`imlink/inquiry_rules.py:202`、`imlink/inquiry_rules.py:266`；`imlink/commands.py:77`；同类问题见 `imlink/dify_inquiry.py:179`。
- 触发：暂停文件 JSON 损坏或读取出错时，SessionGate `_read` 返回 `{}`；`accept_inbound` 缺记录默认 paused=False。CommandLog、LabelBook 也将读取失败当作没有历史。
- 影响：状态损坏会解除人工接管、忘记幂等记录或重新处理全部历史。此处属于掩盖故障的默认放行，不能作为兼容兜底批准。
- 已验证：暂停文件读取替身返回 `{broken` 时，真实 `SessionGate.accept_inbound` 返回 `True`。
- 修复：不存在文件与读取/解析失败分开处理；安全状态无法确认时阻止发送，保留故障文件、明确报警并等待恢复。读取失败不能用空状态覆盖原文件；恢复后按持久化身份对账。
- 验收：损坏 JSON、权限错误、部分写入时不产生外发，界面/日志能识别故障；修复文件后原暂停和幂等状态仍有效。

#### H8 [HIGH] 询价批量重试每次生成新幂等键，重复发送无法去重

- 位置：`imlink/routes.py:317`、`imlink/routes.py:318`、`imlink/routes.py:325`；相关另一端行为 `imlink/routes.py:268`、`imlink/commands.py:117`。
- 触发：同一次 `/api/im/rfq/send` 在响应丢失、用户重试或批次部分成功后重提，服务端给每个收件人重新生成 UUID，不接受稳定的请求幂等标识。之前成功的收件人也被重新发送。相反，普通 `/api/im/send` 若未传 key，又默认固定 `link+uin`，第二次不同正文会被当作第一次而静默丢掉。
- 影响：业务重试重复询价；API 的缺省普通发送则可能错误报成功但未发新正文。客户端自行生成时间戳不能解决“同一请求重试复用同一键”的契约。
- 已验证：相同 body（显式给同一个 idempotency_key）调用 rfq_send 两次，下游调用 2 次、实际生成 key 不同、两次都返回 ok=True；CommandLog 相同 key 不同正文直接返回首条指令。
- 修复：以调用方提供且在重试时保留的批次业务 ID 加收件人生成稳定键；持久化各收件人结果并只重试可重试项；key 必填或由服务端创建可恢复的请求资源；同 key 的账号/收件人/正文不同应显式冲突，不能回旧成功。
- 验收：响应丢失后原请求重试、重复点击、部分批次失败后的恢复，只对每个业务键外发一次；同 key 不同内容返回冲突；同联系人两次不同业务发送都能正常完成。

#### M1 [MEDIUM] Dify 标签和会话/底价文件并发读改写没有共享锁

- 位置：`imlink/dify_inquiry.py:191`、`imlink/dify_inquiry.py:202`、`imlink/dify_inquiry.py:397`；`imlink/routes.py:395`、`imlink/routes.py:400`；`imlink/inquiry_rules.py:108`、`imlink/inquiry_rules.py:196`。
- 触发：每条模型回复启动独立线程，LabelBook `put`/`mark_dispatch` 无锁执行读改写，所有线程共用 `.json.tmp`；FloorBook/SessionGate 虽有实例锁，但每个 API 请求和后台 prepare_live 都创建新实例，同一路径之间互不互斥。
- 影响：A 读旧状态、B 写暂停/新标签、A 再写回，会覆盖新状态；共用临时文件也可导致 FileNotFoundError/读取空窗口。发送闸门和识别结果可能丢失。
- 证据等级：并发入口和非原子读改写已确认；未对真实落盘文件压测，也未声称出现过实际线上损坏。
- 修复：按规范化路径共享读改写锁，或采用支持事务的已有持久化存储；所有更新在同一临界区完成，使用各次写入独立临时文件；后台 dispatch 结果也必须更新现存记录而不覆盖其它更新。
- 验收：并发暂停/恢复、两个线程写标签、同券不同底价操作有确定顺序，不丢字段、不误解除暂停，不抛临时文件冲突。

#### M2 [MEDIUM] 掉线账号仍能通过通道测试并被设为当前

- 位置：`imlink/client.py:199`、`imlink/client.py:209`、`imlink/client.py:231`；关联 `imlink/store.py:save_test`。
- 触发：账号接口明确返回 `state=login_required`，client.probe 仅将它拼进显示文字而不判失败；旧 contacts 接口 404 后读取仍存在的私聊会话，最终 passed=True。`save_test` 还可据此把同 UID 的其它通道标为被踢。
- 影响：不可发消息的通道显示测试通过、被允许激活；本可用的另一通道可能被错误踢下线记账。
- 已验证：模拟账号 `login_required`、contacts=404、sessions=200 空列表，得到 `passed=true`、`source=sessions`。
- 修复：在获取账号后按契约验证 `running` 和非空稳定 self_uid；未登录不能使用旧会话目录证明在线，不更新其它通道互斥状态；接口业务体 `ok=false` 同样必须显式失败。
- 验收：running 正常通过；login_required/offline/空 self_uid 失败，且不会修改其它健康通道的在线记录。

#### M3 [MEDIUM] MySQL 密码在源码中提供固定非空默认值

- 位置：`collector/store.py:14`、`mock/qb_source.py:40`。
- 事实：两个模块对 `QB_MYSQL_PASSWORD` 都设置了相同固定明文默认值。本报告不复述该值，没有连接数据库验证其现行有效性。
- 风险：漏配环境变量时静默使用共享密码；若此默认值仍用于真实环境，拿到仓库即可尝试连接，凭据也无法按环境独立轮换。
- 修复：删除非空凭据默认值，缺失时显式拒绝启动真实 MySQL 接入，改由环境/既有私密配置提供；核查实际使用过该默认值的环境并轮换。不要把密码迁到另一个跟踪文件。
- 验收：源码和构建产物不包含该默认秘密；未配置时失败明确且不泄露配置；受控假凭据的连接参数测试通过。实际凭据轮换属于运维操作，审查没有执行。

### 根因守卫与规格覆盖

发送规则、停机/人工接管、身份隔离、跨日门禁、幂等和回执真实性均出现实质未闭环，不能按“只是 mock”豁免，因为默认 lifespan 已接上真实 uploader。H5/H7 是必须修复的故障隐藏/默认放行；补日志或增加同类兜底不足以关闭问题。

文档明确限定的 contacts→sessions 404 兼容路径本身不作为违规；M2 是兼容路径上遗漏账号状态校验。relay PoC 的本机邮件直投、未填真实 profile 不单独计 bug。

### 冗余和可维护性清单（不另计缺陷）

- `mock/mock_server.py:1080` 的 `_detail_rows` 已被真实监控路径替代，源码搜索无生产调用；`imlink/monitor_feed.py:127` 的 `merge_monitor_messages`、`:628` 的 `annotate_bond_context`、`imlink/catalog.py:18` 的 `builtin_rows` 在生产代码中只见定义，部分仍供测试使用。先核对导出/测试用途，再收敛，不应直接批删。
- `imlink/dify_inquiry.py:4`、`:519` 声称“不发消息”，而同模块已真实 dispatch；`mock/mock_server.py:3` 和 `mock/README.md` 的“离线”说明同样过期。整改真实发送开关后同步修正这些说明，避免操作员按只读/离线认知启动。
- `mock/start.sh` 仍硬编码旧 `/Users/anlin/work/xunjia-agent`。当前 Linux 路径执行文档启动命令会找不到解释器；以脚本自身位置定位仓库并同步安装依赖清单。此为交付清理项，未在当前运行进程执行启动。

### 阅读和验证证据

关键路径阅读 44 个源码文件：

- `imlink/`：address_book、catalog、client、commands、crypto、dify_inquiry、friends、hmac_sign、inbound_context、inquiry_rules、monitor_feed、oss_catalog、oss_send、quote_last、quote_probe、quote_source、rfq_assign、rfq_status、routes、store、uplink。
- `mock/`：db、mock_server、qb_source、quote_channel。
- `collector/`：pb_raw、qb_gateway、qb_run、qb_ws、store。
- `relay/`：mailbus；side_a 的 config、run_a、selftest、downlink、state、gate、uplink、session、db_reader；side_b 的 engine、phrase、run_b、sessions。

未跟踪的 echo/监控/解码辅助脚本只做危险调用搜索及语法诊断，未完整做设备级验收；未阅读私密账户数据或真实聊天正文。本报告不将 61 个 AST 通过文件等同于 61 个文件全部深审。

实际执行：

- 全部 61 个 Python 文件 `ast.parse`：0 个语法错误；使用 `-B`，未生成 `.pyc`。
- 本审查涉及的跟踪修改 `git diff --check`：通过。
- 纯内存复现输出：未认证发送 HTTP 200 / CORS `*`；停止且切号后仍发送 B；缓存补发调用 1 次但暂停检查 0 次；历史无券消息产生模型/发送各 1 次；迟到成功后状态仍 failed；异人昨日同文回执将失败提升询价中；损坏暂停 JSON 后允许回复；RFQ 同请求两次生成不同键；掉线账号 probe passed=true。
- ASGI 复现使用内存传输、发送替身并将同步路由调度改为测试进程内直接执行，避免沙箱线程唤醒问题；未启动 lifespan 或连接任何账号。
- 没有安装新诊断依赖；本地没有 ruff 可执行文件。整套 pytest 由主线在隔离副本执行，本报告不冒认其结果。

### Recommendation

**REQUEST CHANGES**。先关闭未认证真实外发、模型直发、停止/暂停失效及跨账号发送，再修复历史重放、回执身份/迟到回执和业务幂等。验收应使用替身验证“拒绝分支下游调用为零”，并保留对正常发送与重复请求的覆盖，不能只看 HTTP 200 或页面按钮状态。
