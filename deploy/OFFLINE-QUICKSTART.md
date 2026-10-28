# 麒麟 V10 ARM64 离线全新部署

本包自带独立 CPython 3.12 和锁定的生产依赖、已构建前端、OceanBase驱动、SQL及静态ARM解包工具。目标机无需 Python、tar、Node、pip、编译器、Docker 或外网软件仓库。使用麒麟自身的 Bash、coreutils、glibc 2.28及systemd。应用与前端同进程提供服务，不额外安装nginx。

**`07-fresh-install.sh --apply` 是全新重置，不是保留数据升级。它清空 `xunjia_business` 的旧表／视图并建立新版结构，替换专用询价目录内的旧配置和运行状态。成功后删除旧询价安装目录。南银 `deposit` 库、`njcb-all.service` 和南银安装目录不在操作范围。失败时停止并保留执行证据，不自动回旧代码或重放不确定DDL。**

## 1. 解包及填写新配置

在部署机本地核对随包 `.run.sha256`，再用Bash自解包。此步骤无需系统tar，只提取文件，不执行安装或数据库操作；目标目录必须不存在。

```bash
sha256sum -c Xunjia-Agent-KylinV10-ARM64.run.sha256
bash Xunjia-Agent-KylinV10-ARM64.run --extract ./xunjia-unpacked
cd xunjia-unpacked/xunjia-agent-batch1-*
```

实际文件名以本次交付说明为准。不要解到 `/home/aipt/njcb` 或覆盖原询价目录。部署前请保留包的校验文件；不要在包内改程序、依赖和示例模板。

```bash
chmod 600 config/xunjia.env
vi config/xunjia.env
```

必须填写：

| 配置 | 内容 |
|---|---|
| `XUNJIA_DATABASE_URL` | `oceanbase://用户名:密码@158.188.2.206:3306/xunjia_business`；用户名／密码中的 `@`、`#`、`:` 等须URL编码。与南银共用服务器／租户，数据库固定为询价专用库。 |
| `XUNJIA_SSO_SECRET` | 门户约定的SSO密钥；与Dify密钥不同。 |
| `XUNJIA_AUTH_SECRET` | 新询价实例的至少32字节随机会话密钥。 |
| `XUNJIA_DIFY_URL` | 现场Dify API；文档记录为 `http://158.188.1.161/v1`，安装前核实。 |
| `XUNJIA_DIFY_ANALYSIS_APP_KEY` / `XUNJIA_DIFY_REPLY_APP_KEY` | 导入并发布本包对应Dify版本的A／B Chatflow后取得的两个普通App Key。南银存款解析应用的Key不能代用。 |
| `XUNJIA_AGGREGATION_PROFILE` | 默认前缀下填 `/home/xunjia/config/aggregation-profile.measured.json`。包中有该文件，现场换模型后应重新测量。 |
| `XUNJIA_INSTITUTION_CONFIG` | 默认前缀下填 `/home/xunjia/config/institution-connection.json`。 |

文件通道准备：

```bash
cp config/institution-connection.example.json config/institution-connection.json
cp config/institution-auth.example.yaml config/institution-auth.yaml
mkdir -p config/tls
chmod 700 config/tls
# Put the newly supplied client.crt and client.key into config/tls.
chmod 600 config/institution-connection.json config/institution-auth.yaml config/tls/client.key
```

填写 `institution-auth.yaml` 的机构key和16字节sm4_key；它们不是固定accesstoken。连接JSON仍使用 `https://its-relay.yingmi.cn`，保留服务端证书和域名验证、mTLS客户端证书。账号队列path由应用内置，现场不填写旧MQ地址或旧nyoss／nytoken地址。

若南银与询价同时消费同机构目录，两个项目的连接JSON应配置同一专用 `directory_state_dir`（位于两者独立业务state之外）。这项共享目录不可纳入询价全新重置清理范围；现场由两个项目运维确认一致，不由本脚本改南银配置。

## 2. 一键全新安装

默认前缀 `/home/xunjia`、独立端口 `18770`、服务 `xunjia-agent.service`。新模板监听 `0.0.0.0`，按现场网络要求调整。安装脚本不会修改防火墙或南银nginx。

```bash
# Read-only local plan; does not reset the database or start/stop services.
bash deploy/07-fresh-install.sh --check

# Destructive fresh deployment explicitly requested for xunjia_business.
sudo bash deploy/07-fresh-install.sh --apply
```

自定义目录：先将env中 `/home/xunjia/config/` 的绝对路径改为实际新目录，再用 `--prefix /home/实际询价目录`。目标目录必须是可证明归属询价的专用目录。旧服务的入口、目录或端口归属不明确时，预检会报具体错误；不要指向南银前缀或随意删除守卫文件。

每次显式 `--apply` 都表示再次全新重置。上次执行中断或DDL结果UNKNOWN时，下一次会拒绝。先按包外持久审计目录中的结果核对数据库；不要删除审计哨兵或换路径绕过它。

## 3. 验证

```bash
bash deploy/06-check-runtime.sh
bash deploy/04-verify.sh
systemctl status xunjia-agent.service --no-pager
```

运行时检查不访问数据库、模型或IM。业务检查验证实际版本、进程、数据库表结构及配置；健康200仍不代表真实Dify或IM已验收。最小包不附开发测试、Playwright或x86轮子；发布验证报告随分发包另附，现场无需安装测试依赖。

浏览器访问 `http://应用机地址:18770`，使用正常门户SSO登录，选择当前企点账号。Innodealing在页面重新提供当前有效Cookie/SID和设备标识后启动采集。

## 字典的两条路径

新模板已启用 `XUNJIA_QUOTE_SOURCE=qb` 和 `QB_DICTIONARY_SOURCE=innodealing`，空的 `QB_MYSQL_*` 仅从这份新env的业务DSN派生，行情与询价使用同一个 `xunjia_business`。全新部署创建16张询价表、4张行情表及1个视图。

没有外部字典时，`QB_DICTIONARY_CONFIG` 保持空。实际行情先落库，缺失的真实券ID由后台最多100个一批向Innodealing查询编码、简称并缓存。不会使用数字ID虚构债券代码；认证或源接口失败时保留待补项并在后续重试。新库尚未收到行情时页面没有报价是正常空状态。

现场字典提供后，可复制 `bond-dictionary.mysql.example.json` 或 `bond-dictionary.file.example.json` 为新的 `bond-dictionary.json`，填写只读库／视图及列映射或本地JSON文件路径，在env中设置 `QB_DICTIONARY_CONFIG=/home/xunjia/config/bond-dictionary.json`。MySQL/OceanBase源采用参数化只读查询，密码放独立0600文件；JSON为真实行对象数组，字段为 `bond_uni_code`（整数）、`bond_code`、`bond_short_name`，可选 `com_short_name`。JSON、配置及密码文件权限均为0600。字典ID必须与Innodealing的 `bondUniCode` 相同，不能拿其他系统内部编号混用。

外部字典优先；缺项再由Innodealing补齐。修改env后正常重启询价服务；只改已配置路径下的数据文件时停止并重新启动采集即可。没有该字典时，无需创建空文件或填假记录。

## 网络边界

离线安装期间不会下载软件。运行时仍需访问现场OceanBase、Dify、门户SSO、行情与正式文件通道。

现场已确认 **172.26.64.36 的nginx stream TCP透传**：50443转正式its-relay的443，12443转rest.innodealing.com的443，13443转bosh2.innodealing.com的443，11443转web.innodealing.com的443。模板 `XUNJIA_EGRESS_GATEWAY=172.26.64.36` 只改变TCP落点，Host、SNI、服务端证书验证和mTLS保持原域名。数据库与内网Dify不走该转发。不设置全局HTTP_PROXY，不修改hosts，不关闭TLS验证。字典按需batch同样走12443，不依赖额外OSS端口。

`deploy/nginx-stream-egress.conf.example` 是网关侧参考，50443的 `proxy_pass` 必须完整写成 `its-relay.yingmi.cn:443`。本安装脚本不会修改或重启网关nginx；30080旧HTTP入口、60443旧nytoken入口均不被新版应用使用。

本包不分发本机密钥、Cookie、真实业务数据或已填写的旧配置。现场主机、数据库和Dify地址来自用户指定的南银生产环境文档；该文档记录时间为2026年8月，安装前应核对现场现状。
