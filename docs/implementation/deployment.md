# 安琳的询价工作台部署说明

这是本次整改的部署入口，替代历史文档中的mock、匿名API和单机JSON状态。2026-10-13 已完成原目录同源码验收；真实内网接入边界见[交付结果](final-delivery.md)和[独立验收报告](acceptance-report.md)。

## 运行环境

使用Python 3.12，生产安装根目录完整锁定的`requirements.lock`；开发验收安装`requirements-dev.lock`（已包含生产依赖）。`requirements.txt`和`requirements-dev.txt`保留直接依赖声明，复现本轮验证应使用锁文件。本轮锁定环境为CPython 3.12/Linux x86_64、Node 24.16.0；其他平台需要另行验证。前端按`web/package-lock.json`执行`npm ci`、`npm run build`。不要把原工作目录中的运行数据或开发凭据复制进部署包。

业务数据库为独立OceanBase MySQL兼容租户/库，使用`mysql://`或`oceanbase://`连接URL。仅显式创建`xj_*`业务表，不修改主数据表或存款项目表。事务使用READ COMMITTED；已在新建隔离MySQL8.0.46上验并发幂等，OceanBase租户仍需现场验收。SQLite只用于显式离线测试和本机传输账本。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip check
# 从部署环境注入XUNJIA_DATABASE_URL，不把口令写进命令历史。
.venv/bin/python -m server.manage migrate
.venv/bin/python -m server.manage bind --owner PORTAL_USER --account QIDIAN_ACCOUNT --workbench WORKBENCH --transport oss
.venv/bin/python -m server.manage check
.venv/bin/uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8120 --no-access-log
```

门户SSO复用存款项目签名口径；设置`XUNJIA_SSO_SECRET`与独立不少于32字节的`XUNJIA_AUTH_SECRET`。五名交易员分别通过管理CLI绑定本人账号，前端不能更改归属或领取他人账号。反向代理提供HTTPS及同域`/api`、`/ws`，开发代理默认8120；跨域WS仅允许`XUNJIA_ALLOWED_ORIGINS`显式列出的来源。避免记录带SSO签名的入口URL。

`XUNJIA_RUN_BACKGROUND=1`才启动入站消费、Chat和发送循环。没有配置真实transport或Dify，不会补成合成服务。启用前核对数据库、通道、Dify固定HTTP回调及密钥；仅启动HTTP不能算完整业务链验收。

本次新增MQ认证地址与源码核对详见[STS MQ更新](sts-mq-update.md)。

## 双通道配置

设置`XUNJIA_TRANSPORT_CONFIG`为仅部署用户可读的0600 JSON文件路径，`XUNJIA_STATE_DIR`为独立持久目录。每个profile必须与业务库已绑定的owner/account/workbench一致，最多5个；每人OSS和邮件共用自己的传输账本。机密字段填写环境变量名称，配置文件不含真实密钥。

```json
[
  {
    "owner_user_id": "PORTAL_USER",
    "account_uid": "QIDIAN_ACCOUNT",
    "workbench_id": "WORKBENCH",
    "seal_key_env": "XJ_ACCOUNT_AES_BASE64",
    "sign_key_env": "XJ_ACCOUNT_HMAC_BASE64",
    "oss": {
      "endpoint": "https://YOUR_OSS_ENDPOINT",
      "sts_url": "https://nytoken.yingmi.com/api/sts",
      "auth_key_env": "XJ_STS_BUSINESS_SECRET",
      "sm4_key_env": "XJ_STS_SM4_RAW_16_BYTES",
      "expected_bucket": "YOUR_BUCKET"
    },
    "mail": {
      "smtp_host": "YOUR_SMTP_HOST",
      "smtp_port": 465,
      "smtp_tls": "ssl",
      "smtp_user": "YOUR_SMTP_USER",
      "smtp_password_env": "XJ_SMTP_PASSWORD",
      "imap_host": "YOUR_IMAP_HOST",
      "imap_port": 993,
      "imap_user": "YOUR_IMAP_USER",
      "imap_password_env": "XJ_IMAP_PASSWORD",
      "outbound_from": "inquiry@example.invalid",
      "outbound_to": ["collector@example.invalid"],
      "inbound_from": ["collector@example.invalid"],
      "inbound_to": ["inquiry@example.invalid"]
    }
  }
]
```

STS按用户HTML执行SM4-CBC认证，只缓存短期AK/SK/token/bucket完整元组；SM4环境变量是原始UTF-8 16字节，不猜hex/Base64。OSS正文的AES-GCM布局仍为nonce12+ciphertext+tag16。双方升级到v2身份/命令摘要信封后才进入新链路，旧无身份回执不能冒充成功。详见[STS设计](../2026-10-13-sts-integration-design.md)和transports报告。

两路入站使用统一源event_id/source_seq并在落业务事务后ack；主用只影响发送，禁止自动故障切换。上传/SMTP受理是pending；只有实际出站证据才是sent。unknown保留原command_id对账，禁止换ID重发。切换主用通道须停止机器人、取消排队指令并完成全部在途对账。

采集端必须使用ReceiverLedger并让OSS和邮件共用执行账本。先持久pending再执行；取消写cmd墓碑及peer控制版本屏障。RPA出站无法和数据库构成分布式原子事务，进程在发送后崩溃时保留unknown，靠native_id或真实查询核对，不承诺靠重试实现数学上的exactly-once。真实出站echo携带经native_id关联的source_command_id，不能仅按文本去重。

## Dify与公共数据

配置`XUNJIA_DIFY_URL`、`XUNJIA_DIFY_APP_KEY`、`XUNJIA_AGENT_SERVICE_TOKEN`、`XUNJIA_AGENT_TOOL_TOKEN`。两个服务token必须独立、至少32字符。固定HTTP上下文/历史/验证节点用私有service token+短期turn_ref；四工具用公共tool token+独立tool_budget_ref，不能互换。交付包中的Agent使用只读工具，公开措辞看不到底价和内部分析。

正式DSL和模型/插件配置由[dify/chatflow_1171](../../dify/chatflow_1171/README.md)渲染。升级前备份和按锁版迁移；本次隔离实例不替换现有0.15.3实例。缺真实模型和内网回调不能宣称已经上线。

公共债券/发行人数据通过`ReferenceDataProvider.snapshot()`接入已核验的只读视图。默认NOT_CONFIGURED；资料缺失、口径不一致、过期估值、行权或净价缺完整条款均明确返回缺口/转人工。自动BP换算只使用有来源、verified、maturity口径且不超过3日的中债估值；节假日数据无法证明新鲜时转人工。现场映射应提供snapshot_id、as_of、sources、债券身份、发行人及逐字段质量，不允许模型拼SQL或选择任意表。

已有QB公共行情只有在`XUNJIA_QUOTE_SOURCE=qb`且显式提供`QB_MYSQL_HOST/USER/PASSWORD/DB`与`XUNJIA_QUOTE_CACHE`后启用；独立collector由部署进程管理，本服务不偷偷拉起。旧行情cache可迁到新持久目录，经校验后通过环境变量指定，本次不会自动搬运运行数据库。

## 验收及停止

```bash
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/python tests/run_verification.py --browsers-path /YOUR/ISOLATED/PLAYWRIGHT --mysql --load
```

运行前冻结全部代码写者；该入口保存源码前后哈希和pytest、前端unit/developer/build/browser、正式构建产物服务、独立MySQL及两数据库负载结果。需要预装隔离Playwright浏览器；MySQL门仅创建专用测试容器，不连接业务数据库。任何红测或源码漂移都判失败。不将历次通过数相加，也不把合成端到端测试当作真实账户验收。

停止按钮先持久撤销自动授权和取消排队，仍有pending/unknown则显示停止中并继续对账。人工接管和恢复有新的控制版本/消息水位；历史、授权前迟到消息、停止期间消息不会补答。跨日价格策略需要重新确认。人工发送仍不能执行成交或披露内部信息。
