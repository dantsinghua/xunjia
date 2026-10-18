# 现券询价工作台

面向企业内网的五人询价工作台：一人一个企点账号，业务数据本人可见，公共行情共享。机器人在交易员确认批次、方向、量和策略后询价谈价；达到目标或触发停止条件转人工，不执行成交。

正式后端为 `server.app:create_app`，固定使用 `server/negotiation` V2.2核心及15张 `xj2_*` 表；前端为 `web/`。原文先显示，A解析事实与B生成回复分阶段执行。旧 mock 服务及造数种子已删除。测试使用注入的合成数据和隔离服务，生产代码不补造行情、余额、授信或issuer资料。

- [当前部署与配置](docs/implementation/deployment.md)
- [麒麟aarch64离线包安装、更新及回滚](deploy/INSTALL.md)
- [V2.2 A/B Chatflow与真实测量](dify/negotiation_v22/README.md)
- [V2.2当前实施与验收进度](docs/implementation/2026-10-17-v22-cutover-progress.md)
- [已确认的整改设计](docs/2026-10-13-remediation-design.md)
- [Chat Agent设计](docs/2026-10-13-dify-chat-agent-design.md)
- [实施与验证交接](docs/implementation/HANDOFF.md)
- [2026-10-13历史交付结果与边界](docs/implementation/final-delivery.md)
- [2026-10-13历史独立验收报告](docs/implementation/acceptance-report.md)

2026-10-18 当前交付为[非Dify阶段检查点](docs/implementation/2026-10-18-non-dify-checkpoint.md)。Dify规则抽取与流程优化已按最新修订获准实施，另批验收，真实模型验收及最终ARM部署尚未完成。公开资料Tool免Token，客户仅提供现有Dify的普通A/B App API Key；默认关闭真实外发。客户资料、邮箱/OSS及真实采集端按现场配置接入，本地合成测试不代替真实业务验收。
