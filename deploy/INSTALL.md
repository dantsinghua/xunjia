# Xunjia V2.2 独立离线应用包
## 本版边界
本包包含现役 V2.2 询价核心、API、构建后的前端、A/B Chatflow 画布、采集端协议 wheel 与宿主接线资料、离线 Python 依赖及包内验收用例。完整 ARM 交付还包含独立 CPython 3.12 运行时；目标机不需要 Node、编译器或公网下载依赖。
复用客户已有 OceanBase 和 Dify，仅需普通 App API Key，不捆绑或改造这两项服务。安装器不会执行客户数据库 DDL、创建数据库用户、修改 Nginx 或 njcb-all，也不会替运维自动创建服务。后台生命周期按模板开启；外发仍需选定企点账号、完成通道握手并获得业务授权。
公开 Tool 继续免 Token；私有会话接口保留鉴权。部署成功不等于真实采集端、模型和对手外发链已验收。
## 目录结构
    PREFIX/                              默认 /home/xunjia
      config/                            机构连接 JSON、私有认证 YAML、证书；更新/回滚不覆盖
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
麒麟参考环境为 V10 SP3 / Halberd / aarch64 / glibc 2.28，已有现场资料中的系统 Python 为 3.7.9。系统 Python 3.7+ 可以通过 `deploy/bootstrap_runtime.py` 引导经校验的包内 ARM64 CPython 3.12；不用替换系统 Python，也不影响依赖系统 Python 的 yum/dnf。
引导先核实际架构、glibc、运行时压缩包的双清单 SHA256、安全路径/链接及 ARM ELF，再解到本次 0700 临时目录，运行固定部署管理器或包测试器，结束清理临时目录。引导不修改 PATH 或写入安装前缀；真正 `--apply` 时由部署管理器将永久运行时放到 `PREFIX/python`，venv 不依赖稍后删除的临时路径。
已存在的 CPython 3.12 优先使用；Python 3.9+ 仍可直接运行部署管理器，由其验证和安装包内运行时。ARM64 包没有实际运行时文件或其校验不通过时明确拒绝，不会联网补装。
x86_64 缺少 Python3.12 时会明确拒绝安装，不访问公网安装解释器。
包内 config/xunjia.env 是可编辑的初始化文件，不参加不可变内容校验；可以先填写真实值再运行预检。
config/xunjia.env.example、程序、依赖和建表 SQL 属于不可变内容，修改后仍会被完整性校验拒绝。
打包程序只从经过空值检查的 xunjia.env.example 生成初始化文件，完全不读取本地已填好的 xunjia.env。
字段格式为逐行 KEY='literal'，支持 UTF-8、注释和 export；不会执行 shell 命令、变量替换或命令替换。
标准安装入口将空的 XUNJIA_WEB_DIST / XUNJIA_STATE_DIR 按实际 PREFIX 派生。现场应明确记录 XUNJIA_STATE_DIR 和 XUNJIA_INSTITUTION_CONFIG；直接启动应用时必须显式提供状态目录，机构连接格式见 `config/institution-connection.example.json`。
Dify 只使用客户已有 Chatflow URL 与 App API Key；不需要管理员权限。
本版仍不支持任意 Workflow 输入输出协议，不把部署验收宣称为真实 Dify、文件通道或原生 IM 联调成功。
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
- /api/health/ready：检查 V2.2 的 15 张 `xj2_*` 表全部预期字段、类型、主键/唯一约束、13 个二级索引、登录配置及发布身份；不通过返回 503。
- /api/health/automation：本轮未接入真实跨端能力验证，明确返回未验证，不把 configured 当成已验证。
数据库诊断只返回安全错误码、缺失的表/列/索引名，不输出密码或底层异常原文。
业务就绪并不证明真实 Dify、正式文件通道、企点收发通过；它们仍须按对应版本单独验收。
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
仅有系统 Python 3.7.9 的 ARM64 目标上，此入口同样自动引导经校验的包内 3.12，无需手工解压或设置 PATH；已有 `PREFIX/python`、`PREFIX/venv` 或显式 `XUNJIA_QA_PYTHON` 时先核版本再使用。
只使用包内 Python wheels、tests/release、tests/cutover、tests/negotiation、tests/benchmarks 及显式列出的协议验收用例，不需要 Node/esbuild 或 web/src。创建新临时 venv，离线安装、pip check、先收集再执行全部随包用例；失败返回非零并保留日志。旧包没有 benchmarks 目录时沿用其原有测试范围。
开发环境完整 tests/ 另行验收。包专用测试不等于整个源码开发套件，两者结果分开记录。
测试使用本轮临时目录，结束后删除，不修改现役配置/数据库/服务。

ARM 验证必须记录实际 Python 架构、glibc、依赖导入与包内测试结果。QEMU 用户态在 openEuler/glibc 2.28 环境执行只能证明该 ARM ABI 环境的兼容性，不能改称麒麟原生机器验收；真正麒麟环境和客户具体补丁版本的结果另列。

本版运行时及锁定依赖已在官方 `cr.kylinos.cn/kylin/kylin-server-platform:v10sp3` 的 ARM64 用户态中实际离线安装、pip check、导入与 SM4 运算通过，系统为 V10 SP3 / Lance / glibc 2.28 / Python 3.7.9。`.productinfo` 标识为 `aarch64-Build23.02/20230324`，镜像元数据构建时间为 `2026-10-10T17:22:56.43012458Z`（北京时间 2026-10-11）；与上文历史现场 Halberd 画像分开记录。验证镜像 ARM manifest 为 `sha256:8b0496126f596b44f92da0f117a71e1af290e62037ea577f8b0b388c54d41b30`。该验证由 QEMU 用户态在 WSL/x86_64 宿主内核上执行，不是客户物理 ARM 机器；完整最终包的实际测试结果以对应 build_id 的交付证据为准，不能借用旧包结果。
## OceanBase 建表
使用 sql/oceanbase/001_xunjia_schema_mysql.sql（V2.2 的15张 `xj2_*` 表及声明索引）。旧9表SQL仅作历史对照。
只供 DBA 审核后在明确选定的 xunjia_* 数据库内执行；安装、回滚、恢复均不自动运行它。
CREATE TABLE IF NOT EXISTS 不是旧表迁移，数据库升级另行审查。
## V2.2 当前接口与采集端配套

- 当前生产入口固定为 `server.app:create_app`，必须用本包 `sql/oceanbase/001_xunjia_schema_mysql.sql` 的15张 `xj2_*` 表；旧9表SQL在archive中仅供历史对照，不是升级方案。安装器不执行客户DDL，不覆盖现有现场配置或数据。
- 客户Dify导入 `dify/negotiation_v22/` 对应版本A/B画布，仅填 `XUNJIA_DIFY_URL`、`XUNJIA_DIFY_ANALYSIS_APP_KEY`、`XUNJIA_DIFY_REPLY_APP_KEY`。旧单APP_KEY不能作为新两阶段合同。公开Tool免Token，客户不需要提供管理权限。
- `XUNJIA_AGGREGATION_PROFILE` 指向实测报告生成的参数；未测量时不启用自动分析窗口。`XUNJIA_BOND_CONTEXT_MESSAGES` 默认20、范围10..30。`XUNJIA_REFERENCE_CONFIG` 为可选公开资料/发布日历adapter配置，缺少真实源必须显示未配置。
- `XUNJIA_DIFY_TIMEOUT_SECONDS` 为每次HTTP调用超时（模板90秒、范围1–300秒），`XUNJIA_DIFY_STAGE_BUDGET_SECONDS` 为包含工具/历史补取的阶段总预算（模板150秒、最大540秒）。实际HTTP超时还受当前剩余阶段预算限制；租约为阶段预算加30秒。部署时须显式沿用所选模型报告的HTTP/阶段预算，不能把默认值冒充已验证配置。
- `config/aggregation-profile.measured.json` 提供本轮已测 qwen-max/shim 链路的四字段聚合配置：1463ms静默窗、4389ms累计上限；溯源及字节摘要位于 `config/aggregation-measurement.json`。应用的 `XUNJIA_AGGREGATION_PROFILE` 指向前者。首次安装带入配置目录；升级保留现场文件，运维可另存新测量配置并显式选择。该配置来自46对真实清洁实耗与固定合成到达实验；其他模型/运行环境按对应链路重新测量，累计上限不包含忙时排队。后台任务使用统一生命周期配置。
- 标准部署默认 `XUNJIA_RUN_BACKGROUND=1`，启动入站、分析、派发和对账；有效授权指令通过用户暂停状态、有效期、水位和去重检查后派发，不另设真实发送开关。需要暂停时使用工作台现有暂停功能。
- 构建共享包执行 `python3 scripts/build_collector_runtime.py --output <fresh-dir> --wheelhouse <local-build-wheels>`；主包使用 `deploy/build_offline.py` 的 `--collector-wheel <fresh-dir>/xunjia_collector_runtime-2.2.11-py3-none-any.whl`。输出目录必须为本次新目录，构建依赖来自指定本地 wheelhouse；打包会核 wheel 与本版15个共享源码逐文件一致。
- QTrade必须部署匹配的完整冻结应用，在其**专用Python环境**离线安装精确版本 `xunjia_collector_runtime-2.2.11-py3-none-any.whl`；安装前检查 `pip show xunjia-collector-runtime`，安装后检查 `pip check` 和模块路径。不能在其他服务的共用Python环境中覆盖同名 server/relay 包。该 wheel 不含Web服务、现场密钥或运行数据。
- 当前宿主由采集端自有 `CollectorReportingSettings` 自动装配唯一 `InstitutionCollectorHost`，有效授权指令经原生守卫交给绑定账号；不得以旧环境开关启用另一套 factory。参考闭包及精确文件 hash 见 `deploy/collector-qtrade-v22/README.md`、`deploy/collector-qtrade-v22/manifest.json`，其中 `deploy/collector-qtrade-v22/source/src/qtrade_agent/app.py`、`deploy/collector-qtrade-v22/source/src/qtrade_agent/ossbox/collector_settings.py`、`deploy/collector-qtrade-v22/source/src/qtrade_agent/ossbox/institution.py` 展示现役接线；该子集不能覆盖安装为完整应用。
- 机构目录/组织及按工作台、channel、UID派生的账号队列 path 均内置；采集端和询价端不填写远端文件路径或两方向 URL。询价UI只选择企点，微信目录/消息交由南银采集消费。
- wheel离线安装及合成跨仓通过不等于完整QTrade安装EXE、真实文件通道或IM已验收。上线前仍需在指定测试对手范围核真实native ACK；传输 accepted/pending 始终不算已发。

## 安装后统一现场检查
见 `deploy/SITE-ACCEPTANCE.md` 与 `deploy/site_acceptance.py`。默认仅只读检查；`--probe-dify` 明确调用普通App API测试合成报价，不发送IM。完整放行条件未满足时返回2，不以HTTP200替代同版链路验收。
## 页面配置采集端与企点号

在现场 `xunjia.env` 明确设置实际 `XUNJIA_STATE_DIR` 和 `XUNJIA_INSTITUTION_CONFIG` 路径。
按 `config/institution-connection.example.json` 创建私有连接 JSON（0600），正式地址为
`https://its-relay.yingmi.cn`，不加旧服务前缀；配置 `client_cert_file`、`client_key_file` 路径，
需要指定 CA 时再填 `ca_file`，不得关闭服务器证书链或主机名校验。
`auth_file` 引用部署方另行提供的私有 YAML（0600），平铺 `key`、`sm4_key`，后者UTF-8编码恰为16字节；
认证和证书路径相对连接 JSON 解析，凭据、证书与私钥不随离线包交付。
客户端用机构凭据动态生成 HTTP 头 `accesstoken`，并内置 `/api/files/pending`、`upload`、`download`。
签封密钥由机构凭据内部派生，不需要独立 seal/sign key。

登录“采集通道”页面，保存机构连接和认证配置，刷新目录后选择工作台及其企点号。
首次安装不预设账号或工作台；保存、pending连接测试和目录刷新均不授权外发，连接成功也不代替原生 ACK。
`applied` 只表示当前磁盘版本已由运行线程加载，不能当成真实账号业务验收。
更换机构配置、账号或检测到原生换号会停发并归档旧执行投影，要求重新选择；历史消息、价量、回执、UNKNOWN及审计保留，不迁移或重发旧任务。

同机部署询价和南银时，两端连接 JSON 的 `directory_state_dir` 使用同一可读写绝对目录，
共享 `<directory_state_dir>/<institution_id>/directory.db` 和同一 `.consume` 短锁来取机构目录/组织队列，
避免两套私有缓存竞争破坏性下载。两端业务状态、账号执行及收发账本继续各自隔离；询价只消费企点，微信由南银消费。

删除旧 `XUNJIA_TRANSPORT_CONFIG`、`XUNJIA_MQ_CONNECTION_CONFIG`（非空值启动会拒绝）、
`XUNJIA_SEAL_KEY`、`XUNJIA_SIGN_KEY`、`XUNJIA_MQ_ALLOW_HTTP`，以及连接中的 `allow_http` 字段。
旧采集启用开关和手工文件路径均已退役，不用于启用现役宿主或明文HTTP；正式 runtime 仍只读取共享配置文件，不继承外部 shell 的临时 `XUNJIA_*` 值。

