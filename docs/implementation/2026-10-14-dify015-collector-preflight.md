# 2026-10-14 Dify 0.15.3 与采集端 v2 预接线结论

## 现役 Dify 无法直接加载完整 Chatflow

- 已核实 docker-api-1 正在运行 Dify 0.15.3；应用 DSL 是 0.1.5，当前模板 DSL 是 0.7.0。版本校验返回 pending。
- 0.15.3 的 NodeType 注册表中不存在 agent；模板有 31 节点、32 连线，其中两个 agent 节点均不支持。这是运行时功能缺失，并非简单 YAML 版本号不匹配。
- 模板保留 langgenius/agent 插件版本标识、模型 Provider、工具 Provider、内部 HTTP 地址和 SERVICE_AUTH 待配置字段。即使跳过版本提示，实际运行仍会失败。
- 安全决策：没有导入、覆盖、发布现役 Dify 应用；没有重启现役 Dify 0.15.3，也没有修改现役数据库。

## 采集端实际状态与旧新协议差异

- QTrade Agent 管理接口只读查询得到 inquiry_configured=false、catalog_configured=false；旧 xunjia_inbox 虽在源代码注册，但当时运行态未装配。
- 旧采集端源码 C:/Users/anlin/Desktop/work/src/qtrade_agent/ossbox/inbox.py 使用旧 AES 密封、旧命令字段与 ok/code ACK；新 CollectorBridge 使用 v2 账号/对手身份、HMAC、control_version、取消墓碑与原生 ID 结果。不得同时开启旧新两套消费者。
- 企点客户端、Windows WinAgent 服务、QTrade 控制台及旧 Dify 均保持原状。

## 已交付的受控预接线代码

- relay/qtrade_native_port.py：默认 allow_native_send=False；必须提供即时检查旧轮询器关闭的 callable。绑定账号必须唯一，状态为 running/degraded；适配 QTrade Bus 幂等键与原生 readback。
- 真正认定 sent 时，必须 QTrade Bus 返回 DELIVERED 或可靠的 IDEMPOTENT_REPLAY，且有 ext_msg_id + confirmed_by；只收到提交响应时返回 unknown，不自动重发。
- native_reconcile 只读查询 QTrade 持久幂等账本，且验证 op、参数哈希、原命令 ID 关联，不再次调用 bus.submit。
- normalize_native_message 要求正确账号/会话/消息原生 ID 和秒级时间，历史回放默认历史模式，不能触发自动回复。
- 模块为未来采集宿主注入的执行 port，不是独立 daemon，不会新起轮询器、Docker、WSL、企点或 QTrade。
- 测试 tests/acceptance/test_qtrade_native_port_20261014.py：10 项隔离回归通过；与 QTrade 项目真实 Command/CommandOrigin 数据结构做了跨仓非发送验证；Python 全量回归 688 passed、0 failed。

## 部署前仍须完成

1. Dify 版本与功能裁决：推荐独立新建 1.17.1 Chatflow 环境并保留现役 0.15.3；如果坚持 0.15.3 必须重设计 Agent 节点与模型工具路径，不能保证原功能不降级。
2. Dify Agent、模型 Provider/工具 Provider 及内部 HTTP 端点的正式配置、服务凭据注入和端到端验收。
3. 在 QTrade host 内实现互斥启停 v1/v2 消费者的原子切换、注册 v2 CollectorBridge scheduler，以及原生事件 publish_native 回调。当前 adapter 未安装到正在运行的 host。
4. 配置各账号唯一 owner/account/workbench 绑定、v2 seal/sign key、持久接收账本与 OSS/邮件共享事件源；不得把 v1 密封或回执误认成 v2。
5. 使用假 Bus 做跨进程断网、回执迟到、人工屏障、账号切换和重启测试后，才申请真实企点测试号的灰度启用。
6. 生产 STS 必须具备合法证书主机名、预期 bucket 和正式 OSS 权限，现有 MQ HTTP 测试例外不能视为生产 TLS 闭环。

## 本轮结论

当前完成的是源代码与模拟原生执行的预接线。旧新协议双消费已显式避免；没有实际完成“Dify 0.15.3 导入新 Chatflow”或“QTrade 采集端线上切换”。
下一步必须先确认：保留原功能选择独立 Dify 1.17.1，还是坚持 Dify 0.15.3 重新设计受支持节点。

机器证据：/tmp/xunjia-dify015-collector-20261014/result.json
测试日志：/tmp/xunjia-dify015-collector-20261014/full-python.log
当前主要变更仍未 commit/push。
