# xunjia.env 字段清单

真正需要在内网首次部署时填写：
- XUNJIA_DATABASE_URL：已预设 OceanBase 的地址、租户编码用户名和 xunjia_business 库名；仅替换 REPLACE_DATABASE_PASSWORD。URL 密码包含 @/#/% 等字符时需 percent-encode；切勿公开密码。
- XUNJIA_DIFY_ANALYSIS_APP_KEY、XUNJIA_DIFY_REPLY_APP_KEY：分别填写已发布 A 解析和 B 回复应用的普通 API Key。
- XUNJIA_SSO_SECRET、XUNJIA_AUTH_SECRET：项目自身登录（SSO 和会话签名）配置；若无门户对接，请先明确集成方案。

本次不需要：
- XUNJIA_AGENT_TOOL_TOKEN：公开市场数据 Tool 免认证。
- OSS_TOKEN：OSS 独立使用 STS 下发的阿里云临时 AK/SK/SecurityToken，本项目 Dify 调用不需要 OSS Token。
- Dify 管理员账号、插件安装权限、创建新的模型 Provider。

可选：
- XUNJIA_AGENT_SERVICE_TOKEN：只有现有 Chatflow 回调内部会话接口才需设置。
- XUNJIA_TRANSPORT_CONFIG：真实通道连线验收后填写文件绝对路径；它引用环境中的密钥名称，不在 JSON 写明文。
- XUNJIA_QUOTE_SOURCE：可保持空，不从客户其他存款业务数据库猜行情数据源。
- XUNJIA_ALLOWED_ORIGINS：同源前端正常不需要额外指定，跨域使用时由部署者填写。

永远不要让升级包覆盖现场已经填过的 xunjia.env；安装器在检测到现场原文件时会保留。

## V2.2 双阶段配置

新默认模板使用 ANALYSIS_APP_KEY / REPLY_APP_KEY 两个应用Key；测量配置与资料adapter分别由 XUNJIA_AGGREGATION_PROFILE / XUNJIA_REFERENCE_CONFIG 指定。真实配置 xunjia.env 仍为私有可变文件，升级不覆盖；缺新字段需显式补齐，旧单Key不自动变成新核心配置。后台分析可开启而外发保持关闭。

## 已测聚合配置

`aggregation-profile.measured.json` 是可由应用直接读取的四字段配置，`aggregation-measurement.json` 记录来源及原字节摘要。本轮 qwen-max 经本机 Dify shim 的完整47例全部成功，46对清洁实耗配合事前固定的合成到达线，选得静默窗口1463ms、累计聚合上限4389ms；参考完整A+B耗时11706.480ms，满足严格半窗约束。

选用时将 `XUNJIA_AGGREGATION_PROFILE` 填为四字段文件的绝对路径。首次安装会随配置目录带入这两个文件；已有站点继续保留现场配置，升级后可从解包目录另存新的测量配置并显式选择。模板的后台和真实发送开关保持关闭。

该结果对应记录中的模型、链路与固定合成时序；模型或运行环境变化后应在对应链路重新测量。累计上限约束聚合就绪条件，忙时排队另外统计。完整指标和范围见 `docs/implementation/2026-10-18-dify-acceptance.md`。
