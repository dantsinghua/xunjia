# xunjia.env 字段清单

真正需要在内网首次部署时填写：
- XUNJIA_DATABASE_URL：已预设 OceanBase 的地址、租户编码用户名和 xunjia_business 库名；仅替换 REPLACE_DATABASE_PASSWORD。URL 密码包含 @/#/% 等字符时需 percent-encode；切勿公开密码。
- XUNJIA_DIFY_ANALYSIS_APP_KEY、XUNJIA_DIFY_REPLY_APP_KEY：分别填写已发布 A 解析和 B 回复应用的普通 API Key。
- XUNJIA_SSO_SECRET、XUNJIA_AUTH_SECRET：项目自身登录（SSO 和会话签名）配置；若无门户对接，请先明确集成方案。

本次不需要：
- XUNJIA_AGENT_TOOL_TOKEN：公开市场数据 Tool 免认证。
- Dify 管理员账号、插件安装权限、创建新的模型 Provider。

可选：
- V2.2 不读取 XUNJIA_AGENT_SERVICE_TOKEN；解析/回复只使用两份普通 App Key，旧内部回调已退出。
- XUNJIA_STATE_DIR：询价服务自己的持久状态根目录。现场配置应明确填写实际路径；标准安装入口会将空值派生为实际 `PREFIX/state`，直接启动应用时须显式提供。
- XUNJIA_INSTITUTION_CONFIG：唯一现役机构连接 JSON 的路径（0600），结构见 `config/institution-connection.example.json`。现场应明确指定；`auth_file` 指向包含平铺 `key`、`sm4_key` 的私有 YAML（0600），`sm4_key` 的 UTF-8 编码必须恰为16字节。凭据文件、客户端证书及私钥由部署方提供，不随交付包分发。
- XUNJIA_QUOTE_SOURCE：可保持空，不从客户其他存款业务数据库猜行情数据源。
- XUNJIA_ALLOWED_ORIGINS：同源前端正常不需要额外指定，跨域使用时由部署者填写。

永远不要让升级包覆盖现场已经填过的 xunjia.env；安装器在检测到现场原文件时会保留。

## 唯一现役文件通道

正式 `base_url` 为 `https://its-relay.yingmi.cn`，不附加旧服务前缀；客户端内置 `/api/files/pending`、`upload`、`download`，以机构 `key` 与 `sm4_key` 动态生成 HTTP 头 `accesstoken`。必须设置 `client_cert_file`、`client_key_file`；`ca_file` 仅在需要指定 CA 时填写，服务器证书链与主机名校验始终保留。连接 JSON 中的凭据、证书路径相对该 JSON 解析。

私有 `auth_file` 使用以下平铺结构，示例占位值需全部替换：

```yaml
key: REPLACE_INSTITUTION_KEY
sm4_key: REPLACE_WITH_16B
```

签封密钥由机构凭据内部派生，目录、组织和账号队列 path 由系统内置。删除旧 `XUNJIA_TRANSPORT_CONFIG`、`XUNJIA_MQ_CONNECTION_CONFIG`（启动会拒绝非空值），以及独立 `XUNJIA_SEAL_KEY`、`XUNJIA_SIGN_KEY` 配置；不要再部署两方向 URL 档案。删除 `XUNJIA_MQ_ALLOW_HTTP`，连接 JSON 的 `allow_http` 字段会被拒绝，不能用旧开关启用明文 HTTP。

采集端由自有 `CollectorReportingSettings` 自动装配唯一 `InstitutionCollectorHost`，使用匹配的 `xunjia_collector_runtime-2.2.11-py3-none-any.whl` 和完整冻结采集应用；参考闭包见 `deploy/collector-qtrade-v22/README.md` 及 `deploy/collector-qtrade-v22/manifest.json`，不以旧启用开关另起宿主，也不将参考子集覆盖安装为完整程序。

询价工作台只选择企点账号；微信由同一采集宿主发布，交由南银消费。若询价与南银在同一主机运行，两端机构连接 JSON 的 `directory_state_dir` 必须指向同一可读写的绝对目录：共享 `<directory_state_dir>/<institution_id>/directory.db`，并通过同一 `.consume` 短锁串行取目录/组织文件，避免破坏性下载争抢。两端业务状态和账号收发账本仍使用各自独立目录。

保存配置、连接测试和目录刷新不授予外发权限；更换机构配置或账号会归档旧执行工作区并要求重新选择，原消息、价量、回执、UNKNOWN 和审计保留，旧任务不迁移、不重发。文件接口 accepted 或 pending 不能当成原生已发回执。

## V2.2 双阶段配置

新默认模板使用 ANALYSIS_APP_KEY / REPLY_APP_KEY 两个应用Key；测量配置与资料adapter分别由 XUNJIA_AGGREGATION_PROFILE / XUNJIA_REFERENCE_CONFIG 指定。真实配置 xunjia.env 仍为私有可变文件，升级不覆盖；缺新字段需显式补齐，旧单Key不自动变成新核心配置。后台分析可开启而外发保持关闭。

## 已测聚合配置

`aggregation-profile.measured.json` 是可由应用直接读取的四字段配置，`aggregation-measurement.json` 记录来源及原字节摘要。本轮 qwen-max 经本机 Dify shim 的完整47例全部成功，46对清洁实耗配合事前固定的合成到达线，选得静默窗口1463ms、累计聚合上限4389ms；参考完整A+B耗时11706.480ms，满足严格半窗约束。

选用时将 `XUNJIA_AGGREGATION_PROFILE` 填为四字段文件的绝对路径。首次安装会随配置目录带入这两个文件；已有站点继续保留现场配置，升级后可从解包目录另存新的测量配置并显式选择。模板的后台和真实发送开关保持关闭。

该结果对应记录中的模型、链路与固定合成时序；模型或运行环境变化后应在对应链路重新测量。累计上限约束聚合就绪条件，忙时排队另外统计。完整指标和范围见 `docs/implementation/2026-10-18-dify-acceptance.md`。

## 2026-10-19 复审修复
- 同一交易员/企点账号最多50个不同活跃对手。同一人多券只计一人；暂停只停发送，仍占分析名额；其全部OPEN券组结束交人工或归档后释放。预览和确认都核对，确认以数据库事务为准，不能用并发绕过。可查询 `/api/negotiation/capacity`。
- `XUNJIA_VALUATION_REFRESH_SECONDS` 默认2秒（允许1..30）：在一次成功读取恢复资料后立即应用内部BP估值和gap，不再只等下一日。网络调用自身的超时另外约束，2秒不是网络恢复SLA。
- 新Inno行中只采用明确 `cnbd_yield_mat/cnbd_date`，不将bid/ofr行情当作中债估值或本人对手报价。成功入站即触发同一重算；行情最优价不变的估值更新也会触发。
- 资料表通过现有可映射ReferenceProvider接入；业务库写入暂时失败则下一轮重试。仍需已配置的最新发布交易日日历，缺失不猜日期。同日两个来源不同值明确报数据冲突，不擅自指定孰真。
- 重算只改我方CHINABOND_BP边界和策略版本，不改绝对限价，不自动替对手确认当日价格，不解除暂停或人工状态。失效候选取消/待对账；重复相同估值不产生新的策略版本。
- r20实测聚合文件仍是历史证据，不能把旧47例成绩记成当前修订通过；当前DSL上线前应重新跑合成业务的实际模型验收。
