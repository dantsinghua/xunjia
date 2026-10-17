# Collector v2 集成与部署边界

安琳，本文件说明当前仓库与现役采集端之间必须兑现的合同，依据当前 `relay/collector_bridge.py`、`server/receiver_ledger.py`、`server/transports.py`、`relay/protocol.py`及[传输实施报告](transports-report.md)。现役采集仓库尚未部署本桥；没有启动真实账号、重启既有服务或执行真实外发。

## 责任与入口

| 位置 | 责任 | 交付状态 |
|---|---|---|
| 内网 `server.app` | 认证、本人授权、预览/确认、Chat 审核、事务命令、后台派发/入站、停止和对账 | 本仓已实现，有候选独立门；待冻结终验 |
| `relay.protocol.Codec` | 固定 owner/account/workbench 身份；v2 信封、摘要、签名、期限 | 合成协议门通过；真实密钥部署未验 |
| `ReceiverLedger` | 执行去重、claim、取消墓碑、控制版本、native ID 精确映射 | 开发者及独立双路门；真实原生动作未接 |
| `CollectorBridge` | 消费两路命令、优先取消/查询、调用注入 native 能力、持久回执、发布原生事件 | 可注入桥已交付；echo 已有独立候选门；held 到期与人工屏障后续修复待关闭 |
| 现役采集宿主 | 注入已认证 OSS LIST/GET、TLS 邮件、同一账号写守卫、真实 native send/query、生命周期和原生消息回调 | 现场留口，不能猜私有函数名或凭空声称完成 |

Bridge 的 `run_once` 由现役调度器有界调用；真实消息落库后进入 `publish_native`。构造参数与安装示意保留在传输报告中；本文件不新增未经现场运行的启动命令。

## 不能省略的协议边界

- 绑定使用 `owner_user_id/account_uid/workbench_id`；同文、同 peer 或同原生 ID 在不同账号下不能串用。native 事件至少有真实原生 ID、peer、direction、秒级 occurred_at；历史回灌必须显式标记，不能把导入时间当发生时间。
- 对象密码布局仍是 AES-GCM 的 `nonce(12) || ciphertext || tag(16)`、空 AAD；v2 JSON 增加身份、摘要、HMAC、有效期。**旧采集器不会因密码布局相同而兼容 v2**。缺版本/身份/摘要/签名的旧 ACK 不接受。
- `native_send`/`native_reconcile` 返回明确的 status/evidence/native_id。布尔值、HTTP 200、SMTP 或 OSS 接受只证明提交，不能判 sent；native 动作结果未知只查询，不再执行原发送。
- native 回调须使用现役同一账号写守卫，实际动作前校验授权/有效期；数据库事务不能跨 RPA/网络等待。底层操作不支持原子回执时，系统保留 unknown，不承诺数学意义的 exactly-once。
- 内网业务事务提交后才 ACK 入站；采集端账本结果及待发回执持久成功后才确认输入。失败恢复沿用原 command/event ID，不创建新的价格命令碰运气。

## 两路上行与主通道

当前后续裁决是**源事件持续发到全部已配置上行通道；主通道只决定 command 及其回执路线**。这覆盖原整改设计中“源端停止旧上行、切到唯一新上行”的方案。

OSS 和邮件 adapter 共享同一受保护的 `state_path`，共同保存原生事实、event_id/source_seq；每 route 单独记录待发状态。单路失败不阻止另一条，恢复后补发原 ID/序号；内网按稳定事件身份去重。命令固定原 transport，不因 unknown 暗切备用通道，不在双路同时发送价格。

用户切主通道仍要求机器人停止、无 queued/pending/unknown、匹配 expected_version。切换不是对未知执行结果的绕过。两路 journal 的保留范围和恢复窗口须由部署方确认。

## 原生 echo 与人工消息

只有账本内精确 native ID、账号与 peer 关联，才能把原生 out 标为机器人回流并附 source_command_id；正文和时间接近不构成关联证据。可信来源明确标记的 human out 应立即上行触发接管。

当前补充实现为：同 peer native 执行在途、但 out 尚无精确关联时，先持久 deferred，`publish_native` 可返回 None；`flush_deferred` 在 `run_once` 前后处理。默认 `echo_wait_seconds=5`、`native_verification_budget=5`，等待必须大于零、不超过 5 秒且不超过核验预算。关联成功才转 bot；结束、超时或重启仍无法关联则按人工消息处理，并建立持久人工屏障。held 自动 command 尚未执行；仍有效且等待分类时查询返回 pending，不调用 native，也不确认原输入；已到期则持久 failed，命中取消墓碑、旧控制版本或未观察当前人工屏障则 cancelled，均不调用 native。真正已 claim 的 pending/unknown 不能仅凭到期推断未执行。

echo 暂存已有独立 20 项候选通过；随后终审 F17/F18 发现 held 到期无终态及更高旧授权版本绕过人工屏障，已追加上述终态与下述因果合同，**后续修复的 QA/终审仍在闭合**。manual_send 不受临时 echo hold，但仍受版本、有效期和人工/取消屏障约束。现场宿主必须保留源事件与重试能力，不能把 None 当成事件丢弃，也不能再补一次同文发送。

人工屏障不再只依赖 control_version 大小。接收账本为每次人工事件持久保存稳定 `manual_fence_id` 与 peer 单调 `manual_fence_version`，放在 event 签封顶层；它们不进入原生 payload/content_hash，但受 AES-GCM/HMAC 保护。两路重试、TTL 续签与历史重放保留该事件原屏障对，不重新生成。

根 Inbox 验证并把已观察屏障存为 conversation 的 `receiver_fence_id/receiver_fence_version`：同版异 ID 拒绝、旧版本不覆盖新版本。未观察过的新屏障即使随历史重放到达也会暂停；历史正文仍只展示，不自动回复。根业务以 human_lock 区分真实接管与未授权历史对话；明确恢复/新授权后，命令携当前 receiver 屏障对。接收端必须同时核对精确屏障对、control_version 和有效期，数字更大的旧授权或旧人工草稿不能绕过。迟到 echo、ACK、重复事件不自动恢复会话，unknown 只能对账，不能重发。

## 部署前必须停用旧消费者

旧 xunjia poller 与新桥使用相同下行/ACK 路径：

| 用途 | 对象位置 |
|---|---|
| 命令与控制 | `wb/{wb}/in/xunjia/` |
| 原命令回执 | `wb/{wb}/ack/{command_id}.bin` |
| v2 事件/manifest/catalog | `wb/{wb}/out/xunjia/{account_uid}/` |
| 本人通讯录/组织 | `wb/{wb}/qidian/{account_uid}/contacts.json`，内容为 v2 密封对象 |

**必须精确停用旧 xunjia poller，再让新桥读取同一前缀**。否则旧消费者可能重复执行或用旧 ACK 覆盖 v2 结果。该要求不授权重启整个 QTrade、企点、容器、WSL 或其他采集任务；应先定位现役 poller 注册点和独占配置，保留其他服务状态。目前没有操作外部仓库，该动作仍未执行。

切入前需要核定：固定账号绑定与两端密钥；受保护的独占账本/传输目录及恢复方式；真实 native ID/查询能力；取消先到和动作中停止；双路重复/单路断网恢复；同 ID 改正文拒绝；回执丢失后的原 ID 对账。没有这些事实，不应宣称远端停止或双向收发已上线。

## 历史恢复与认证

`replay_events(first_seq,last_seq)` 是管理员有界恢复入口，单次最多 200 条，只重签既有 journal，保留 event_id/source_seq/occurred_at/正文，顶层附 `transport_replay=true`。返回 queued 仅表示排队；内网必须只读展示，不唤醒自动回复；以连续落库且 ACK 的源水位证明恢复完成。它不能续期价格命令的原授权。

STS 按[MQ 更新](sts-mq-update.md)执行：默认 HTTPS 保留；唯一允许的 MQ HTTP 完整 URL 为 `http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts`，不增删路径、不带 query/fragment、不跟随凭据重定向。SM4-CBC/PKCS7、随机 16 字节 IV 前置、标准 Base64，以及字符串 Unix 秒协议不变；认证只读核验提交 `ac26e9ed72f816e2a19b9e1d01c9572bd8d214a4`。

当前没有实际认证 key/SM4 key，也没有真实 MQ 请求。用户示例 token、临时 AK/SK/security_token 均不得写入源码、文档、fixture 或日志。真实邮件认证、OSS 权限与 LIST 接线、native 账号、Windows 文件锁和密钥轮换均属现场未验范围。

## 验证与停止条件

[传输报告](transports-report.md)记录开发者 Codec/双路 adapter/账本/Bridge 假端到端；[独立验收报告](acceptance-report.md)记录真实生产类配合假外部 IO 的双路、停止、恢复和只读重放用例。它们不是现场网络或原生账号验收。Dify 的真实隔离运行也使用合成模型与 FakeTransport，不能补足采集端现场证据。

本文件的停止点是合同和未接线项可供现役采集宿主审阅；实际部署、最终同版独立验收与原目录同步由主审继续负责，不在本文中冒报完成。
