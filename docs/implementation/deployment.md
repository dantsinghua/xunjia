# 安琳的询价工作台部署说明

这是V2.2部署配置入口。生产固定使用 `server.negotiation`，旧业务实现只作历史回归。完整离线包的预检、安装、回滚与恢复执行[deploy/INSTALL.md](../../deploy/INSTALL.md)，当前验证范围见[切换进度](2026-10-17-v22-cutover-progress.md)。旧2026-10-13交付记录不是本版部署合同。

## 运行环境

使用Python 3.12，生产依赖完整锁定在`requirements.lock`，开发验收使用`requirements-dev.lock`。麒麟aarch64离线包携带独立运行时、wheels和已构建前端；客户现场不需要联网安装依赖或运行Node构建。目标参考环境为Kylin V10 SP3/glibc2.28，原生实机与ARM仿真验收范围以最终包报告为准。开发环境才按`web/package-lock.json`执行`npm ci`、`npm run build`。

业务数据库为独立OceanBase MySQL兼容租户/库，使用`mysql://`或`oceanbase://`连接URL。经DBA确认目标库后显式执行包内`sql/oceanbase/001_xunjia_schema_mysql.sql`创建15张`xj2_*`表；安装器不执行DDL。旧`xj_*`数据保留，不把建表命令当作旧数据迁移。当前核心已通过真实OceanBase CE4.4.2.1专用schema事务验收，客户目标租户仍须现场确认。SQLite只用于显式离线测试和本机传输账本。

```bash
# 安装后在实际PREFIX/app目录，加载经审核的现场配置后执行。
../venv/bin/python -m server.manage check
../venv/bin/python -m server.manage bind --owner PORTAL_USER --account QIDIAN_ACCOUNT --workbench WORKBENCH --transport oss
```

`server.manage`使用与现役app相同的ApplicationStore；check只读核对完整新schema，空库或只有旧表会失败。`migrate`是显式新建表命令，不能自动迁移旧业务数据，也不由服务启动调用。首次绑定默认停止发送，重复相同绑定不更改原账号归属；服务通过包内稳定入口`deploy/start.sh`启动。

门户SSO复用存款项目签名口径；设置`XUNJIA_SSO_SECRET`与独立不少于32字节的`XUNJIA_AUTH_SECRET`。五名交易员分别通过管理CLI绑定本人账号，前端不能更改归属或领取他人账号。反向代理提供HTTPS及同域`/api`、`/ws`，开发代理默认8120；跨域WS仅允许`XUNJIA_ALLOWED_ORIGINS`显式列出的来源。避免记录带SSO签名的入口URL。

标准部署默认 `XUNJIA_RUN_BACKGROUND=1`，启动入站、分析、派发和对账。确认授权后的指令按用户暂停状态、有效期、水位和去重规则派发，不再另设真实发送开关。需要暂停时使用现有用户／券组／会话暂停功能；没有配置真实transport或Dify不会补成合成服务，仅启动HTTP不等于完整链路就绪。

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

配置`XUNJIA_DIFY_URL`、`XUNJIA_DIFY_ANALYSIS_APP_KEY`和`XUNJIA_DIFY_REPLY_APP_KEY`。客户在现有工作区导入对应版本的A/B画布并提供普通App Key，不需要管理员权限或新Agent插件。V2.2不要求旧SERVICE_AUTH/AGENT_TOOL_TOKEN；四个公开只读资料Tool免Token，应用业务接口仍验证用户会话和账号归属。

正式A/B合同、0.15.3与1.17.1导入方式和测量入口见[dify/negotiation_v22](../../dify/negotiation_v22/README.md)。程序将真实上下文通过context_json传入，工具与历史请求由后端执行，不要求Dify能回调客户内网服务。A事实先独立提交，B失败保留A并按持久任务恢复。

`XUNJIA_AGGREGATION_PROFILE`必须指向成功实测生成的四字段JSON；未配置时不启用自动聚合分析。上下文默认20条、范围10–30。HTTP超时和阶段总预算须与实际测量配置一致，初始化默认90/150秒不是任意模型均已通过的保证。验证可优先复用安琳指定的南银本机shim，客户正式模型仍选择客户现有可用服务。

公共债券/发行人数据通过`ReferenceDataProvider`或显式`XUNJIA_REFERENCE_CONFIG`接入已核验只读来源，默认NOT_CONFIGURED。资料缺失、口径不一致或净价缺完整条款时明确返回缺口。BP估值使用可信发布日历确认最新发布日，跨日按新估值重算；不能用固定“三日内”替代最新发布日。绝对限价沿用，但每个新日期仍向每个对手确认。净价换算须有真实条款、原文结算日及程序bond_calc证据；模型不能自行给出可信换算结果。

已有QB公共行情只有在`XUNJIA_QUOTE_SOURCE=qb`且显式提供`QB_MYSQL_HOST/USER/PASSWORD/DB`与`XUNJIA_QUOTE_CACHE`后启用；独立collector由部署进程管理，本服务不偷偷拉起。旧行情cache可迁到新持久目录，经校验后通过环境变量指定，本次不会自动搬运运行数据库。

## 验收及停止

```bash
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/python tests/run_verification.py --browsers-path /YOUR/ISOLATED/PLAYWRIGHT --mysql --load
```

运行前冻结全部代码写者；该入口保存源码前后哈希和pytest、前端unit/developer/build/browser、正式构建产物服务、独立MySQL及两数据库负载结果。需要预装隔离Playwright浏览器；MySQL门仅创建专用测试容器，不连接业务数据库。任何红测或源码漂移都判失败。不将历次通过数相加，也不把合成端到端测试当作真实账户验收。

停止按钮先持久撤销发送授权并取消排队，继续解析新事实和对账。pending/unknown不能当作已发或换ID重发。人工接管和恢复使用新的控制版本与消息水位。当前券组最终认可且达限价的整笔数量合计`>=`目标时，可提前交人工并停止该券组发送，其他券组不受影响；不自动成交或锁量。
