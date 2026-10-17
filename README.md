# 现券询价工作台

面向企业内网的五人询价工作台：一人一个企点账号，业务数据本人可见，公共行情共享。机器人在交易员确认批次、方向、量和策略后询价谈价；达到目标或触发停止条件转人工，不执行成交。

正式后端为 `server.app:create_app`，前端为 `web/`。旧 mock 服务及造数种子已删除。测试使用注入的合成数据和隔离服务，生产代码不补造行情、余额、授信或issuer资料。

- [当前部署与配置](docs/implementation/deployment.md)
- [已确认的整改设计](docs/2026-10-13-remediation-design.md)
- [Chat Agent设计](docs/2026-10-13-dify-chat-agent-design.md)
- [Dify 1.17.1交付包](dify/chatflow_1171/README.md)
- [实施与验证交接](docs/implementation/HANDOFF.md)
- [本轮交付结果与边界](docs/implementation/final-delivery.md)
- [独立验收报告](docs/implementation/acceptance-report.md)

2026-10-13 改造已精确集成到当前原目录；同源码完整验证通过：Python 702 项、Node 52 项、类型检查及构建全部通过，源码前后无漂移。Dify 1.17.1 新隔离实例的真实图与后端整链另经独立复核。未提交或部署真实业务；内网OceanBase、主数据视图、真实邮箱及采集端账号按现场条件接入，测试证据不代替真实业务验收。
