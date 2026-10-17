# 安琳的 V2.2 现役切换进度（2026-10-17）

状态：实际实施与受控验收中；尚未完成最终同版全量、真实模型测量及 GitHub 发布。

## 已接线

- `server.app:create_app` 固定装配 `ApplicationStore / Inquiry / Engine / Inbox / Dispatcher`，现役业务只写15张 `xj2_*` 表。旧实现仅保留历史离线回归，不由生产开关选择。
- A解析与B决策分别调用两只普通 Dify Chatflow App Key；原文先落库，事实独立事务，B拒绝不回滚A；生产字段/原文片段核对与正文价格/数量/授权校验已接入。
- 分券分对手双边事实、字段证据、共享目标整量覆盖、`>=`目标交人工、停止发送仍分析、UNKNOWN只对账、三阶段旧候选屏障已联通。
- 归属纠正有投影版本核对与审计，旧错券贡献撤销，不凭纠正捏造新券价格；模型corrections解决历史UNRESOLVED。撤回按仍有效字段证据重放。
- 非法解析关闭相应经济发送但可以澄清；错误观察不使同消息另一券事实丢失。消息/决策租约、发送回执、原生回显与前端revision异步更新使用同一持久状态。
- 每个本地新日期要求逐对手确认，绝对限价沿用；BP必须有最新发布日历和可信中债快照。缺日历仍会跨日失效，提交/dispatch门也挡午夜旧报价。
- 净价保留原始口径；结算日须有对手文本证据，真实资料snapshot+公开bond_calc才可生成可追溯yield，模型自填computed_yield拒绝。
- 公开4个资料Tool免Token；HTTPReferenceProvider按显式URL/字段映射取资料，未配置明确返回缺口。未接通任何假定客户库表。
- 前端统一V2 API、双边价量/来源、原文先显、ID+projection_revision、认证SSE与断线补拉；一券暂停不影响其他券。
- 采集端ReceiverLedger新增最终permission检查；QTrade宿主/Bus/UI的最后原生动作前复核由独立批次完成，详见采集交接。
- V2 OSS通讯录使用独立 `wb/{workbench}/out/xunjia/{account}/contacts.bin`，避免覆盖仍服务其他业务的旧共享 `qidian/.../contacts.json`。
- 部署SQL、健康检查、干净env模板、包内新核心验收对齐；后台分析与真实外发开关分离，默认真实外发关闭。

## 八门对应验证入口

| 门 | 持续维护的验收 |
|---|---|
| 非空解析与真实语义 | `tests/cutover/test_acceptance_contract.py`、真实Dify测量（尚未终验） |
| 多券归属与纠正 | `test_acceptance_contract.py`、`test_corrections_and_delivery.py` |
| 双边价量、字段修订、并发共享量 | `tests/negotiation/`、`test_acceptance_contract.py` |
| 最终性、问句/意向/转述、版本 | `test_acceptance_contract.py`、`test_daily_and_semantic_positive.py` |
| 停止、pending/unknown仍分析、租约恢复 | `tests/negotiation/`、`test_corrections_and_delivery.py` |
| 新入站与旧候选、原生执行许可 | `test_corrections_and_delivery.py`、`test_v22_native_permission.py`、QTrade独立22门 |
| 事实/决策分事务与可重生 | `test_acceptance_contract.py`、`test_v22_recovery.py` |
| 前端异步、控制、终态与重连 | `web/tests/acceptance/test_v22_projection.py`及全浏览器门 |

## 当前已取得的证据与边界

本轮改前全tests基线805 passed；中间整合全tests861 passed，均有正常结束标记。其后继续改动，所以两者均不代表最终冻结版本。前端本批69 Node、35浏览器、类型/构建通过；采集独立22门与3条真实组件合成联合链通过，安装出的collector wheel已替换源码路径再跑联合验证。

真实模型初测已经发现并保留数字等值harness误报、撤价assertion混用、净价澄清复述价格被正文门拒绝及阶段超时。正在修正prompt并计划同版42例终轮，未把模型HTTP200或普通场景成功冒充全部语义通过。聚合窗口仍未按单例/失败时间拍默认。

OceanBase既有QA容器停止，启动许可仍待答；未执行客户数据库DDL或真实对手IM发送。真实STS/OSS及客户资料源未标通过。

GitHub原历史有两个>100MiB对象、通讯录和历史凭据；已完整bundle备份并审计，当前live探针密码已移除，原历史未改写/未推送。最终发布树与迁移决定仍开放。

运行记录：用户指定的外部运行目录 `HANDOFF-ACK.md / STATUS.md / RESULT.md`，不进入Git。源码和测试目前仍可能受外部客户端写入，终验将检查前后哈希，不能拼接不同版本绿灯。

### 后续检查点

- 新一轮全tests899 passed/1 warning，45.26秒（仍为终验前快照）；随后增加真实引用方向反例并完成63项定向回归。
- 真实初测36例中27例完整成功，未满足30例门，失败全部保留，不输出生产聚合profile；已定位prompt/超时/程序引用方向问题，下一轮42例使用完整冻结根。
- 当前唯一测量及交付A/B包是 `dify/negotiation_v22/`；同时出现的 `dify/chatflow_v22/` 为未评审外部候选，原文件保留、不纳入主发布包，不能与实测产物混用。
- QTrade首次完整tests 3296通过/1失败（init_db新目录0750受进程umask裁为0700），实现方修复仅新建leaf目录权限且不调整umask；独立门与最终全量仍在收尾。
