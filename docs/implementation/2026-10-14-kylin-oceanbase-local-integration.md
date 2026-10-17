# 2026-10-14 本地 Dify 0.15.3 / OceanBase / 麒麟离线包联调报告

## 实机结果
- Dify 0.15.3 已在 Docker 中运行。Windows 浏览器从 http://127.0.0.1:8090/ 得到 HTTP 200，且从 WSL Bridge IP http://172.19.181.143:8090/ 得到 HTTP 200。容器宿主监听 0.0.0.0:8090。
- Dify 新导入的独立应用：现券询价受控Chat Agent（Dify0.15只读兼容）；应用 ID 为 8855ccb7-ce8c-40d0-b5cb-bca6e30c1fc2。官方 AppDslService 导入 completed，草稿 31 节点，已注册 NodeType 均支持；未发布，原有 5 个应用未改动。
- 原 1.17.1 Chatflow 有两处 agent 节点，在 Dify 0.15.3 无对应节点。兼容草稿以 LLM 节点替换，严格限制 WAIT / REQUEST_HANDOFF / SOCIAL_REPLY，禁用动态工具与自动还价，不能冒称完整自动谈价系统。
- OceanBase CE 4.4.2.1 通过独立 Docker 容器 xunjia-oceanbase-qa-20261014 启动，使用 MySQL 兼容租户 test，WSL 回环监听 127.0.0.1:2883，专用业务库 xunjia_qa，已创建 9 张 xj_* 表。
- 在真实 OceanBase 上执行了 5 路并发确认、唯一命令、停止控制测试，全部通过；未使用真实询价对手或执行器。配置临时保存在 WSL /tmp/xunjia-oceanbase-qa-20261014/ 的受限文件；不要复制、上传或打入 tar。
- OceanBase 测试 API 从 WSL 0.0.0.0:18770 启动。Windows 从 http://172.19.181.143:18770/api/health 得到 HTTP 200，Dify API 容器主动请求同 URL 也得到 HTTP 200，且 database_ready=true。由于还未配置完整的 SSO/服务令牌，overall ready=false、background_enabled=false、automation_configured=false，所有真实外发关闭。
- QTrade Agent 鉴权只读接口显示 2 个 running 账号，其中 1 个企点 qidian；WinAgent 服务 Running；旧 inquiry_configured=false，未装配旧的 XunjiaInbox。未注入当前隔离测试应用的 v2 CollectorBridge，不执行真实企点外发。
- OpenVPN Data Channel Offload Up，Windows 到 172.16.18.43:80 的 TCP 可达，源地址 10.98.24.224，路由经过 OpenVPN。MQ STS 不携令牌 GET 返回 HTTP 401，证明地址及认证门存在，不等于凭据验证通过；HTTPS 主机名 nginx.mq.dq.yingmi-inc.com 与服务证书 *.yingmi-inc.com 不匹配。未通过安全检查传递用户敏感密钥执行真正 STS 登录，没有取得临时 AK/SK/security_token，也未执行 OSS GET/PUT。

## 离线交付
独立 tar 已生成 Windows 桌面：
C:\Users\anlin\Desktop\Xunjia-Kylin-Offline-20261014.tar.gz

- 文件大小：123537190 字节，约 118 MiB。
- SHA-256：3cf296dcca03a6fb491807fd9507889c6c6dc853a539212d6f74dacccaba9d3a
- 包含 CPython 3.12 双架构 x86_64、aarch64 各 33 个运行及 QA wheel，加 3 个通用 pip/setuptools/wheel 的 wheel；Dify 0.15.3 导入 DSL、1.17.1 原模板、服务端、前端静态资源、OceanBase 脚本、systemd 模板、合成测试、可复核 SHA-256SUMS。
- 独立目录通过 pip --no-index 完整安装（x86_64）、pip check 和 SM4/项目导入检查；aarch64 以 pip --no-index / --platform 验证全部 33 个锁定 wheel 能解析（未在物理 ARM 上执行）。
- 包内 OceanBase 只读预检针对本机真实 CE 实例通过，包含 9 张 xj_* 表。压缩文件 gzip 完整性及 SHA-256 经 WSL / Windows 双重核实。
- 目标前置：经过信创批准的 CPython 3.12 + venv、glibc >= 2.28、麒麟 x86_64 或 aarch64。未包含 Python 解释器自身、系统 RPM、Dify 或 OceanBase 服务端、Chromium 可执行文件。需要在目标麒麟上再做兼容性验证。

## 未完成及拒绝放行事项
- Dify 0.15.3 应用目前仅为只读/人工转接兼容草稿：HTTP URL 仍是部署占位地址，SERVICE_AUTH 需在 Dify 中用 Secret 填写，应用 API Key、实际模型以及正式 Node 执行未跑通；未发布。
- 本地 xunjia OceanBase 测试 API 尚未准备好真实 SSO/内部服务签证，不能把 db_ready=true 当成整体 ready。
- 现役采集端 v2 只是独立适配层通过合成 QA，尚未实际替换或联动运行中的 QTrade。需先确认旧新消费者互斥、命令墓碑、原生回执/ID、权限和真实测试对手。
- STS 实网令牌校验、正式 Bucket、OSS 权限及双通道收发未验；不得绕过 TLS 校验。
- 麒麟目标机具体版本、glibc、架构和 Python3.12 安装来源仍需运维确认，尤其 aarch64 尚未实机验。

结论：本轮完成本机 Dify 与 OceanBase API 的网络、DSL 草稿、数据库引擎与并发基础联通，生成可离线安装的双架构应用包。无真实消息外发。尚不满足自动谈价生产上线条件。
