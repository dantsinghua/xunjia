# 第一批交付修复：离线完整更新、共享配置、原子版本切换
## 本版边界
只修复交付部署与就绪诊断，不改成交/询价状态机、Dify 应用、公开 Tool 鉴权规则、Nginx 或 njcb-all。
程序不会执行客户数据库 DDL，也不会自动创建服务或数据库用户。默认停用自动发送。
公开 Tool 继续免 Token；私有会话接口的已有鉴权不属于本轮变更。
## 目录结构
    PREFIX/                              默认 /home/xunjia
      config/                            真实配置、transport.json、证书；更新/回滚不覆盖
        xunjia.env                       600 或 400 权限
      state/                             原有业务状态；更新/回滚不清空
      current -> releases/<release-id>   单一原子切换点
      app -> current/app                 保持原现场路径可用
      venv -> current/venv
      releases/<release-id>/app/
      releases/<release-id>/venv/
      backups/<id>/                      首次迁移旧目录时的原件
      deployments/<id>.json              不含密钥的持久化操作记录
      .xunjia-deploy.lock                 安装、回滚、恢复共用进程锁
首次升级旧布局会完整合并旧 app/config/ 到 PREFIX/config/，包括引用的 JSON 和证书。
相同文件名内容或权限冲突会在停服前拒绝，不会任意选择一份覆盖。
旧路径 PREFIX/app/config 通过软链接继续可用。配置中引用配置目录外的文件保持原引用，不搬动外部文件。
新旧版本均使用同一个共享配置目录；回滚程序版本不会倒退现场配置。
## 环境与配置
实际应用运行仍要求 CPython 3.12、glibc >=2.28、x86_64 或 aarch64。
部署引导可使用 Python 3.9+；ARM64 包内有已批准的 CPython3.12 运行时才可解压使用。
x86_64 缺少 Python3.12 时会明确拒绝安装，不访问公网安装解释器。
包内 config/xunjia.env 是可编辑的初始化文件，不参加不可变内容校验；可以先填写真实值再运行预检。
config/xunjia.env.example、程序、依赖和建表 SQL 属于不可变内容，修改后仍会被完整性校验拒绝。
打包程序只从经过空值检查的 xunjia.env.example 生成初始化文件，完全不读取本地已填好的 xunjia.env。
字段格式为逐行 KEY='literal'，支持 UTF-8、注释和 export；不会执行 shell 命令、变量替换或命令替换。
XUNJIA_WEB_DIST / XUNJIA_STATE_DIR 为空时，根据实际 PREFIX 派生，无须写死 /home/xunjia。
Dify 只使用客户已有 Chatflow URL 与 App API Key；不需要管理员权限。
本版仍不支持任意 Workflow 输入输出协议，不把部署验收宣称为真实 Dify/OSS/IM 联调成功。
## 预检、安装
在解包根目录执行（默认只检查，不会更新）：
    bash deploy/00-preflight.sh
    bash deploy/01-install.sh --dry-run
    bash deploy/01-install.sh --apply
自定义前缀时：
    XUNJIA_PREFIX=/实际专用目录 bash deploy/01-install.sh --apply
真实服务名只能是 xunjia-agent.service 或 xunjia-agent-*.service。
首次安装或原服务停止时，新版保持停止，输出 installed_stopped，不冒充已经业务就绪。
已有运行服务：先验证配置和旧版本，离线准备新 venv，然后只停止专属服务，切换 current，重启并核验。
程序比较实际配置端口、build_id、release_id、boot_id 和 systemd 主进程 PID，拒绝错误端口或旧进程的 HTTP 200。
离线安装失败发生在切换前时，不影响旧服务；切换后失败会还原旧 current 并重新核验。
未完成记录会阻止下一次部署，必须先恢复。不会在中断后盲目继续切换。
部署不会替运维改写已有 systemd 单元。单元必须通过 PREFIX/app/deploy/start.sh 或已审核的稳定入口启动。
新的示例单元仅供审查，不能覆盖 njcb-all；自定义前缀用 render_service.py 生成对应示例。
## 严格验收：三种不同含义
    bash deploy/04-verify.sh --package-only   只查压缩包文件；不表示服务启动成功
    bash deploy/04-verify.sh --liveness      只查正确版本的进程是否存活
    bash deploy/04-verify.sh                 业务就绪；不通过返回非零
HTTP 接口：
- /api/health：兼容历史 200 状态，必须读取 data.ready，不能用它作为严格探活。
- /api/health/live：进程存活，不访问数据库。
- /api/health/ready：检查 9 张表的全部预期字段、类型、主键/唯一约束、15 个二级索引及登录配置；不通过返回 503。
- /api/health/automation：本轮未接入真实跨端能力验证，明确返回未验证，不把 configured 当成已验证。
数据库诊断只返回安全错误码、缺失的表/列/索引名，不输出密码或底层异常原文。
业务就绪并不证明真实 Dify、STS/OSS、企点收发通过；它们仍属于后续联调批次。
## 回滚、中断恢复
    bash deploy/02-rollback.sh --apply
    bash deploy/03-recover.sh --apply
回滚前检查旧代码完整性及旧 venv 可用性；缺失旧版本时在停服前拒绝。
旧版重启失败时还原回滚前版本，只有完整验证成功才记录 COMMITTED。
恢复脚本读取持久化记录，处理突然中断/被杀进程后的未完成切换；失败保持 RECOVERY_REQUIRED 并返回非零。
配置、状态目录和数据库不回滚，不清理客户历史数据。
不要删除 .xunjia-deploy.lock 或 deployments 文件来绕过锁/恢复校验。
## 交付包专用测试
    bash deploy/05-test-package.sh
只使用包内 Python wheels 和 tests/release 及显式列出的协议验收用例，不需要 Node/esbuild 或 web/src。
开发环境完整 tests/ 另行验收。包专用测试不等于整个源码开发套件，两者结果分开记录。
测试使用本轮临时目录，结束后删除，不修改现役配置/数据库/服务。
## OceanBase 建表
保留 sql/oceanbase/001_xunjia_schema_mysql.sql（9 张表、15 个普通索引）。
只供 DBA 审核后在明确选定的 xunjia_* 数据库内执行；安装、回滚、恢复均不自动运行它。
CREATE TABLE IF NOT EXISTS 不是旧表迁移，数据库升级另行审查。
## V2.2 当前接口与采集端配套

- 当前生产入口固定为 `server.app:create_app`，必须用本包 `sql/oceanbase/001_xunjia_schema_mysql.sql` 的15张 `xj2_*` 表；旧9表SQL在archive中仅供历史对照，不是升级方案。安装器不执行客户DDL，不覆盖现有现场配置或数据。
- 客户Dify导入 `dify/negotiation_v22/` 对应版本A/B画布，仅填 `XUNJIA_DIFY_URL`、`XUNJIA_DIFY_ANALYSIS_APP_KEY`、`XUNJIA_DIFY_REPLY_APP_KEY`。旧单APP_KEY不能作为新两阶段合同。公开Tool免Token，客户不需要提供管理权限。
- `XUNJIA_AGGREGATION_PROFILE` 指向实测报告生成的参数；未测量时不启用自动分析窗口。`XUNJIA_BOND_CONTEXT_MESSAGES` 默认20、范围10..30。`XUNJIA_REFERENCE_CONFIG` 为可选公开资料/发布日历adapter配置，缺少真实源必须显示未配置。
- `XUNJIA_RUN_BACKGROUND=1` 可与 `XUNJIA_ENABLE_LIVE_TRANSPORT=0` 配合，仅接收、分析、对账；新包默认两者均0，不自动向真实对手发送。
- 构建包先执行 `scripts/build_collector_runtime.py --output <fresh-dir> --wheelhouse <local-build-wheels>`；主包构建增加 `--collector-wheel <fresh-dir>/xunjia_collector_runtime-2.2.0-py3-none-any.whl`。打包会核wheel代码与本版源码逐文件一致。
- QTrade采集端需先部署本轮 `qtrade_agent/ossbox/xunjia_v22.py`、Bus/UI guard与app互斥接线，再在其**专用Python环境**离线安装包内 `collector-runtime/*.whl`。依赖cryptography与QTrade现有依赖一致；安装前检查 `pip show xunjia-collector-runtime`，安装后检查 `pip check` 和模块路径。不能在其他服务的共用Python环境中覆盖同名server/relay包。该wheel不含Web服务、现场密钥或运行数据。
- QTrade以 `XUNJIA_COLLECTOR_VERSION=2.2` 和 `XUNJIA_COLLECTOR_CONFIG_FILE` 显式启用新宿主。私有JSON仅引用seal/sign key文件，默认 `allow_native_send=false`；旧XunjiaInbox必须互斥关闭，其他业务仍使用的旧CatalogPublisher保留。缺依赖不回退v1、不虚报V2能力。
- V2通讯录路径为 `wb/{workbench}/out/xunjia/{account}/contacts.bin`；旧 `qidian/{account}/contacts.json` 属于共享历史协议，不能覆盖。
- wheel真实安装及合成跨仓联合通过不等于完整QTrade安装EXE、真实OSS或IM已验收。上线前仍需在指定测试对手范围核真实native ACK；OSS accepted始终不算已发。
