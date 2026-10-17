# 安琳的跨模块验收风险预检

本文件由独立测试方维护，只记录已执行的行为复现与验收缺口，不代替正式代码质量或安全终审。产品修改由主审完成；下列问题均有独立回归测试，测试方不改产品代码。

当前最终状态：原目录 `/home/anlin/work/xunjia-agent` 已完成独立统一验收，`/tmp/xunjia-verification-5mksioj3/result.json` 为 passed=true/source_drift=[]，256 前后清单一致，252 冻结代码子集不变，额外仅 4 个保留的旧性能 JSON。本轮 702 Python、52 Node 测试及构建全部通过；11 份最终证据原样校验后持久复制到 `test-logs/final-original/`。此前已知红测均在该次全门重新执行通过。下面的失败及修复过程保留为历史，不代表当前仍红；真实账号/MQ/OceanBase 的范围留口保持。

## 当前新增发现

| 编号 | 风险与触发 | 观察到的结果 | 最小修复方向 | 证据 |
|---|---|---|---|---|
| QA-P01 · HIGH | `server/chat_runtime.py::_check`：源文“估值+2.20bp”，模型伪报 `kind=yield,value=2.20` | 产生新的 queued 报价；`\bbp\b` 漏掉数字紧贴的 BP 后缀 | 报价口径识别须覆盖数字后缀；模型声明与原文单位不一致时拒绝 | `runtime-risk-precheck.log`，`test_bp_suffix_attached_to_number_cannot_be_relabelled_as_absolute_yield` |
| QA-P02 · HIGH | 同函数：授权为 BUY/1000万，公开正文改为“我方卖出1000万，收益率2.48%” | 正文仍获签证且 queued，方向与结构化动作相反 | 最终正文的方向需与动作/会话授权核对 | 同日志，`test_public_direction_and_volume_units_must_match_the_structured_authorization` 第一个参数 |
| QA-P03 · HIGH | 同函数：1000万授权，正文改为“我方买入1000亿” | 数字白名单只看到合法的 1000，错误放行单位扩大后的数量 | 校验数量和单位的组合，不只对数字做集合匹配 | 同日志，同测试第二个参数 |
| QA-P04 · HIGH | `server/business.py::receipt`：sent → cancelled → 重放最初 sent 证据 | 中间正确变 unknown，重放旧证据却又变 sent | 持久保留互相矛盾证据，重复 ACK 不得消除冲突；真正对账应有明确解决语义 | `receipt-conflict-precheck.log`，`test_replayed_success_does_not_erase_a_conflict_between_execution_receipts` |
| QA-P05 · MEDIUM | `server/market.py::aggregate_sessions` / 状态 API：sent 清 pending 指针后收到冲突回执 | command 为 unknown，界面状态 API 却显示询价中 | 聚合需要关联导致当前价格事实的原命令，不能只看尚未清除的 pending 指针 | 同日志，`test_conflicting_receipts_remain_visible_as_unknown_in_the_rfq_status` |
| QA-P06 · MEDIUM | `server/business.py::manual_send`：已登录调用 `/api/monitor/send`，`cp=[]` | HTTP 500；非字符串 peer 进入数据库参数绑定 | 进入事务/查询前验证 peer_id 类型、空白及长度；用 400/422 表达非法输入 | `http-input-shape-precheck.log`，`test_non_string_manual_peer_is_a_client_error_without_state_change` |

发现时 P01–P03 为 3 failed / 1 passed / 37 deselected（显式拒绝继续用例通过），P04–P05 为 2 failed / 41 deselected。均已交主审修复，独立重验后在本文件追加状态，不用实现者自行跑绿取代独立验收。

P06 定向为 1 failed / 1 passed / 36 deselected；同批嵌套 `peer_ids` 预览输入已正确返回 4xx，未列为缺陷。

后续独立重验：P01–P05 对应门及公开市场后缀、每券授权时间、人工对账后旧社交不恢复等，已纳入 `runtime-diagnostics-retest.log` 的 Chat/HTTP/双通道 **105 passed**。P06 对应 `review-fixes-current.log` 定向 **4 passed / 59 deselected**。独立方未修改产品实现。后续新增命令 INSERT 占位数回归曾造成 **20 failed / 33 passed**，原日志 `runtime-diagnostics-independent.log` 保留；修复后才取得上述 105 绿，两个轮次不混同。

终审第三轮转来的 held 过期问题已独立复现：`test_expired_held_command_reaches_failed_without_ever_calling_native`，`collector-held-expiry-red.log`。第一条 native 回调内回声暂存，第二条授权仅落 held 且从未调用 native；第一条精确映射后回声已发布，时钟超过第二条授权有效期，reconcile 却仍返回 pending。该路径不能靠重投过期原命令恢复，应根据从未执行的账本事实安全终结。修复职责为实现方，独立门保留失败要求；截至本次追加仍待复验。

R17/R18 后续修复已独立复验：`manual-fence-final.log` **27 passed**，关闭 held 到期问题。新增跨两路签封/重放、native 在途更高版本不能绕人工屏障、根 ingest→显式恢复→Agent/人工命令→接收端唯一执行、历史初次授权、同版 ID 冲突拒绝/旧版不倒退的门。正式终审结论仍由终审方独立维护；此处只记录行为证据。主审尚未给最终冻结时刻，未把本轮定向通过声明为最终 0 回归。

## 隔离 Dify 的独立证据

- 明确执行 `tests/acceptance/dify_runtime_contract.py` 的两轮门，**1 passed，4.43 秒**，日志 `dify-isolated-independent.log`。自身生成新 user，不改实现方已有会话文件。同一个真实 Dify conversation 中 turn/source 后缀 1→2、epoch 1→2；每轮真实工具日志包含 `raw_gap_bp=30.00`，并有本轮 context/tool/validate_action/validate_text。证据 `/tmp/xunjia-dify-independent-7f7rl010/result.json` 及两份私有 SSE；API/fixture 的 container ID 与 StartedAt 前后相同。
- 前两次第一轮已成功执行工具和返回完整提案，但独立提取器未识别插件 `tool response: JSON.JSON` 格式。仅修 QA 观测解析，先用已保存 SSE 离线确认 30.00，再有界追加一次完整两轮。没有放宽任何运行成功、工具输出、epoch 或身份断言；不把提取器错误计作 Dify 产品失败。
- 明确执行同文件 `-k existing_root`，**1 passed / 1 deselected，0.27 秒**，`dify-root-readonly.log`。通过 SQLite `mode=ro` 读取本轮 root 集成实例：done turn 确有工具调用和最终签证，匹配已 sent 命令及完整正文；6 次工具预算 turn 没有任何出站命令。证据 `/tmp/xunjia-dify-root-readonly-nks379fh/result.json`。
- 同一只读门逐 ID 对比原 15 个运行容器 StartedAt 且仍 running，未发现本轮改变旧环境。没有调用单次驱动 `/_verify/run` 或 budget/start，没有重启任何 Dify 服务。

正式冻结后的更新：新 root ID `794149b9996aeb072552b90410cf0e57ed3dc2d33d8bb2199f21096359486255` 已独立只读复核通过，证据 `/tmp/xunjia-final-dify-independent-36ig0vm9/result.json`。252 源文件/23 实际加载模块均与冻结清单匹配；原生 Dify run succeeded，22 成功节点、30 BP 真实工具响应；自行拼最终 AgentTurn 并重算摘要，匹配 SQLite 最终签证和 Fake sent 命令。预算持久 6 次且无出站，HTTP 权限拒绝记录交叉核对；15 原基线和 36 其他容器未改变。详情在 acceptance-report 当前终验节；此新证据替代较早 root 进程的版本适用性。

## 已核对的相关保护

- 真实 private capability 与 public tool 凭证互不可替代；伪 turn_ref 拒绝，公共计算不暴露私有上下文。
- 输入拒绝继续、跨日未重确认、授权前迟到消息、超量/改变交割条件、人工接管/停止/撤回在途签证，均有独立行为门。
- 消息、命令和 turn 并发唯一性已在独立 MySQL 8.0.46 实际验证；不能据 SQLite 绿推断 MySQL 行锁正确。
- 公开行情静态 HTTP 和 WebSocket 的顶层/嵌套私有字段泄漏已复现后修复，并独立重验。
- 出站 unknown 不自动重发/换路；人工发送明确接管但不能绕过未决任务、成交或内部底价禁令。

## 仍需单独保留的边界

- 原则上，自然语言所有表达不能靠有限样本或禁词正则证明零语义风险；本轮结论仅限已构造的确定性案例与已固定的输入输出合同。
- Dify 独立真实运行仅针对新隔离 1.17.1 项目、合成模型及合成资料；既有 0.15.3、真实工作账号、真实价格/成交均未触碰。
- OceanBase 目标租户和现役采集端部署状态没有现场证据，独立 MySQL/适配器/接收端账本测试不能替代它们。
- 源码尚未冻结。最终结论须取同一次统一验证与零 source_drift 的结果，不能拼接本文件中的分批通过数。
