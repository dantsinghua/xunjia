# 独立验收记录

安琳，**原目录集成后的最终统一验收已通过**：全部质量门绿色、零失败、零源码漂移。此结论限定于下述冻结文件和合成验收边界；真实 MQ 凭据/工作账号、目标 OceanBase 租户仍未接入。开发者自测、历史结果和隔离 Dify 运行另列，不拼接成当前结果。

## 原目录最终统一验收（当前交付依据）

实际运行目录：`/home/anlin/work/xunjia-agent`。主审精确集成并重新 `npm ci` 后，独立执行原样 runner：

```bash
/tmp/xunjia-clean-install-20261013/bin/python tests/run_verification.py --mysql --load --browsers-path /tmp/xunjia-playwright-20261013
```

**最终 passed=true，source_drift=[]。** 256 项 manifest 前后完全相同，其中 252 项冻结子集与 `af3b276f22512a73bbba2c324e4f22d52807641722301ea253cd8bfb073fc02f` 原清单逐项匹配；仅额外保留以下 4 个此前已有 JSON，未排除、改写或删除：`web/tests/out/qb-live-e2e.raw.json`、`qb-live-e2e.result.json`、`qb-live-perf.json`、`qb-replay100-perf.json`。

本轮唯一临时证据目录 `/tmp/xunjia-verification-5mksioj3`，外层日志 `/tmp/xunjia-original-final-runner-20261013.log`。按主审要求，原样复制并逐文件 SHA256 对比通过的 11 份结果、前后清单和八门日志持久存于 [test-logs/final-original/](test-logs/final-original/)：

- [最终结果 result.json](test-logs/final-original/result.json)
- [运行前清单](test-logs/final-original/source-before.json) 与 [运行后清单](test-logs/final-original/source-after.json)

| 本次验证门 | 实际结果 | 持久日志 |
|---|---|---|
| 全量默认 pytest | 661 passed，0 failed | [pytest.log](test-logs/final-original/pytest.log) |
| 新隔离 MySQL 事务／并发 | 6 passed，0 failed | [mysql-compat.log](test-logs/final-original/mysql-compat.log) |
| 既有前端 Node 单元 | 8 passed，0 failed，0 skipped | [web-unit.log](test-logs/final-original/web-unit.log) |
| 前端开发者行为 | 44 passed，0 failed，0 skipped | [web-developer.log](test-logs/final-original/web-developer.log) |
| vue-tsc＋Vite 构建 | exit 0 | [web-build.log](test-logs/final-original/web-build.log) |
| 正式同域服务实际构建产物 | 1 passed，0 failed | [production-web.log](test-logs/final-original/production-web.log) |
| 独立真实浏览器 | 32 passed，0 failed | [web-browser.log](test-logs/final-original/web-browser.log) |
| SQLite＋MySQL R32 负载 | 2 passed，0 failed | [load-acceptance.log](test-logs/final-original/load-acceptance.log) |

本轮 Python 合计 **702 项**，Node 合计 **52 项**，全部为原根同一次 runner 的结果；累计阶段耗时 **197.79 秒**。`npm ci` 的既有 postinstall 允许策略未调整，实际 vue-tsc/Vite 及生产 assets 门证明本轮安装后的工具链可用。Python 的 Starlette/TestClient 弃用警告如实保留，未过滤警告或 skip/xfail 变绿。

原根本轮负载仍为各环境 5 身份×100000 合成历史、10 当前会话/人，5 人共 300 次只读请求与 100000 行导出同时执行；有界页读与每批最多 500 行断言通过：

| 环境 | 只读 P95 | 最慢读请求 | 导出耗时 | 独立证据 |
|---|---:|---:|---:|---|
| SQLite 临时库 | 0.063928 秒 | 0.100884 秒 | 5.298 秒 | `/tmp/xunjia-load-acceptance-wx271ngt/result.json` |
| 新隔离 MySQL 8 | 0.489910 秒 | 0.757072 秒 | 6.667 秒 | `/tmp/xunjia-load-acceptance-r5x3o0p8/result.json` |

只读 P95 均达到本机测试环境 ≤1 秒目标，不外推为目标 OceanBase 的生产容量承诺。本轮新 MySQL 容器已核验 `removed_container=true`：并发门 `d9d0dec42a3843dadc522a07a1f11fdd8550df8442313902d1e38026e0ce11d8`（127.0.0.1:32780，证据 `/tmp/xunjia-mysql-acceptance-wgo53h6t`），负载门 `76958be401f95d6fb01d7a46effd78b86e2ef0dec8909cff59240f25a31cdede`（127.0.0.1:32781，证据 `/tmp/xunjia-mysql-acceptance-cf8l2cvl`）。数据和日志保留，没有连接或清理现役数据库。

本轮独立方没有修改产品、测试、runner、锁文件或 DSL，没有管理原目录 runtime DB/mail/state。最终交付范围仍保留真实 MQ/工作账号/现场采集器及 OceanBase 留口；同冻结源码的 Dify 新实例独立只读证据见下节，不拿早期候选结果替代。

## 工作树正式冻结终验（集成前独立证据）

- 冻结时刻：`2026-10-13T08:34:56.383816Z`；252 文件 manifest SHA256 `af3b276f22512a73bbba2c324e4f22d52807641722301ea253cd8bfb073fc02f`，原清单 `/tmp/xunjia-final-freeze-20261013-yb6ljazq/source.json`。
- 干净环境 `/tmp/xunjia-clean-install-20261013/bin/python`，按锁定开发依赖安装；独立 `pip check` 输出 `No broken requirements found.`。
- 精确命令：`/tmp/xunjia-clean-install-20261013/bin/python tests/run_verification.py --mysql --load --browsers-path /tmp/xunjia-playwright-20261013`，cwd 为下方唯一工作树。
- 唯一最终目录：`/tmp/xunjia-verification-y9jpt1cs`；`result.json` 为 **passed=true，source_drift=[]**。独立另比 `source-before.json`、`source-after.json` 与主审冻结清单，252 项完全相同。受冻结的产品/测试/runner/配置/锁文件均未修改。
- 外层日志 `test-logs/final-frozen-runner.log`。runner 各阶段累计 192.79 秒；下面计数均来自这一次执行，不挪用历史定向结果。

| 验证门 | 结果 | 原始日志 |
|---|---|---|
| 全量默认 pytest（含独立及开发者门） | 661 passed，0 failed | `pytest.log` |
| 隔离 MySQL 并发与事务 | 6 passed，0 failed | `mysql-compat.log` |
| 既有前端 Node 单元 | 8 passed，0 failed，0 skipped | `web-unit.log` |
| 前端开发者行为 | 44 passed，0 failed，0 skipped | `web-developer.log` |
| vue-tsc＋Vite 构建 | exit 0 | `web-build.log` |
| 正式服务同域实际构建产物 | 1 passed，0 failed | `production-web.log` |
| 独立真实浏览器 | 32 passed，0 failed | `web-browser.log` |
| R32 SQLite＋MySQL 合成负载 | 2 passed，0 failed | `load-acceptance.log` |

Python 验证合计 **702 项**，Node 测试 **52 项**；不另重复计入 pytest 内含的开发者门。部分 Python 命令各报同一条 Starlette/TestClient 弃用警告，未过滤或隐瞒；无 skip/xfail 变绿。逐门的精确命令、退出码与耗时记录在最终 `result.json`。

本轮 R32：各后端均为 5 个合成身份×100000 历史、每人 10 当前会话；5 人 300 次读请求与一路 100000 行导出并行，所有返回与账号隔离断言通过。SQLite P95 **0.054983 秒**、导出 **4.618 秒**，证据 `/tmp/xunjia-load-acceptance-oexfjdlt/result.json`；MySQL P95 **0.474632 秒**、导出 **6.648 秒**，证据 `/tmp/xunjia-load-acceptance-qlywch94/result.json`。真实游标读页最多 201 行、导出每批最多 500 行。仅为本轮本机隔离环境实测，不是 OceanBase 生产 SLA 承诺。

本轮仅新建的两个 MySQL 测试容器均已核 ID 后停止并移除，保留数据与证据：并发门 `9e1a14827bc4703293d854245477902c8750489d7bda3cf66f70f7adf39eb905`，回环端口 32776，`/tmp/xunjia-mysql-acceptance-b8a4ltus/container.json`；负载门 `3efd324c6f597ac161f17102290b4f7b6a9c5f86757e18d2f19c8b4043970b33`，回环端口 32777，`/tmp/xunjia-mysql-acceptance-eggpfk5d/container.json`。网络 guard 仅允许各 fixture 核验的新端口，既有业务数据库未接触。

同冻结 hash 的新隔离 Dify→root 实例已完成下述独立只读复核。真实 MQ/auth.key/sm4_key/临时 AK/SK、现场 OSS/邮箱/企点账号、目标 OceanBase 仍未测试；不得据本次离线全绿写“真实生产链路全部验收”。

### 新冻结版 Dify→root 独立只读复核

结果 **passed=true**，独立证据 `/tmp/xunjia-final-dify-independent-36ig0vm9/result.json`。本轮独立运行只读 Python 检查，直接检查证据、容器、Dify 原生 PostgreSQL 和 SQLite；没有调用实现者 `audit.py` 作为替代，没有重跑 `/_verify/run` 或 budget/start，没有发新的模型/业务请求。

- 新 root 容器 `794149b9996aeb072552b90410cf0e57ed3dc2d33d8bb2199f21096359486255`、StartedAt `2026-10-13T08:40:30.536169236Z`，端口仅 `127.0.0.1:32779`，project 标签正确。已阅读临时 FrozenLoader，其实际编译源码 hash 记录的 23 个 server 模块均匹配冻结 252 项清单；清单与统一门 source-after 也相同。
- 新 Dify PostgreSQL 以 `BEGIN READ ONLY` 直接查询 run `cd80e137-626d-4dd5-97bc-51fadd06fb11`，状态 succeeded，app 为 `af146254-ee04-4ad0-81fe-7c2b58f39593`。SSE 无 error，有完整双成功终态及 22 个成功节点，真实 tool_response 解码得到 30.00 BP。
- 自行从 SSE 拼出最终 AgentTurn，对 `validation_ref=None` 的候选重新做规范 JSON SHA256，精确匹配新 SQLite 持久签证的 digest，并核对 ref、turn/source IDs；done turn 真实 tool_calls=1，正文与对应 sent 命令完全相同。新库总共 2 个 sent，分别为合成首句及本轮 FakeTransport 回执；未声称是真实企点外发。屏障新 schema 和命令字段存在，本次空屏障不替代独立 R18 非零屏障对抗门。
- 预算轮原生 SQLite 确为 tool_calls=6 且零该轮命令；只读请求记录为 200×6、400，以及 4 种凭据混用均 403。此处是已执行 HTTP 记录与持久状态交叉核对，未拿旧票据重新发请求。
- 原 15 个基线容器以及本次 root 以外的 36 个受保护容器，完整 ID、StartedAt、运行状态均与基线相同。已通知主审可精确停止本测试 project 并保留数据；独立方未操作清理。
- 证据根 `/tmp/xunjia-dify-verify-20261013/final-freeze-af3b276f`，新库 `root-instance-1791880831192235177.sqlite` 以 `mode=ro` 和 `PRAGMA query_only=ON` 读取；SSE 为 0600。凭据明文、签证 ref 和私有票据未写入公开报告。

第一次临时只读取证脚本遇到工具 schema 描述中的非数字 `raw_gap_bp` 而 Decimal 解析失败；补非数字字段过滤后重新只读核对通过。没有修改任何冻结源码/测试/图，没有重跑业务。真实工具结果仍必须精确为 30.00，未把 schema 字段或外层成功标签当计算证据。

## 初期范围与版本（历史，当前终验以上节为准）

- 唯一运行目录：`/home/anlin/.codex/worktrees/b372/xunjia-agent`。
- 起始 HEAD：`26acaa0ab99ca19dc12e3ef33d874e1d8f6fb0e4`；当前源自原脏树 188 文件快照，详见 `baseline.json`，不能仅用 HEAD 代表测试内容。
- 运行环境：隔离 `.venv`，Python 3.12；测试临时业务路径由 `tests/conftest.py` 在采集产品模块前设置。
- 网络隔离：清除继承的业务凭据；禁止外部 DNS 和全部未由本测试绑定的 TCP 地址。旧 HTTP 探针仅可访问自身绑定的 loopback 临时服务。未访问真实 STS、OSS、Dify、企点或生产数据库。
- 产品代码由实现方维护；独立验收仅修改测试及本报告。

## 首批最小验收矩阵（历史计划，后续进展见增量证据）

| 门 | 入口及风险 | 当前证据/缺口 |
|---|---|---|
| 原有 131 测试 | 旧功能行为、fixture 契约冲突 | 原始基线复现 121 绿 10 红；已按新规格裁决修 fixture，产品红另报 |
| STS 协议 | SM4-CBC、UTF-8、秒字符串、PKCS7、标准 Base64 | 独立 OpenSSL 固定向量及完整填充验收 |
| STS 生命周期 | UTC、并发刷新、原子快照、过期/预算、退避、脱敏 | 独立 27 例当前通过，含过期显式快照不得发送 |
| OSS 认证 | GET/PUT token 参与签名、有限过期重试、403 不循环 | 验证完整 HMAC canonical string；重试对象键/密文保持 |
| 生产身份 | `server.app:create_app`，匿名拒绝、5 人归属、SSO/session 篡改 | 15 例合同已落地；首次运行模块尚缺，仅属装配缺口，非业务红证据 |
| 本人域/纯查询 | 跨账号读写拒绝；所有 GET 零发送 | 等待生产路由/仓储接口后补入口场景 |
| 控制与命令 | stop/接管/恢复、模型在途、late ACK、幂等、unknown | 第二批，待实现接口稳定后按实际入口验证 |
| Chat | 买 20 卖 30 BP、firm gap>0 续谈、SSE 半包不外发 | 第二批，待受控运行器合同 |
| 额度/记忆 | 一次澄清、一次历史补取、重启不重置、memory_patch 不改权限 | 第二批，需事务接口和真实运行器证据 |
| 工具/行情 | 四工具缺数据不造数、撤价/版本/失败重放 | 待工具和行情实现合同 |
| 前端入口 | 认证、错误不成功、分页选择/响应代次、真实详情、HTML | 已与前端实现者对齐 API 和 Node/Playwright 方式，独立页面验收未开始 |
| 真实集成 | OceanBase 事务、双端 OSS/邮件、Dify 镜像/插件、企点执行证据 | 本轮无真实服务授权；尚无验收证据，不用离线测试替代 |

## 原始基线（2026-10-13）

命令：`timeout 1200 .venv/bin/python -m pytest tests/test_*.py -q --tb=short`

结果：退出码 1，**121 passed / 10 failed / 0 skipped**，2.97 秒。日志：`test-logs/baseline-20261013.log`。

10 红的实际来源：

1. `test_org_tree_spec.py::test_frontend_tree_sorts_and_renders_only_the_open_branch`：缺 `orgEmptyLabel`。
2. `test_rfq_assign_spec.py` 两例：无上行/回执却断言 `ok=True`；其中最后一例同段又断言 `ok=False`，并引用未定义 `blob`。
3. `test_rfq_oss_send.py` 五例：安装 Fake OSS，却没有安装生产装配的 `upload_live` 适配器，未走密封上传/ACK 链。
4. `test_rfq_send_gate.py::test_upload_without_receipt_is_not_ok`：实际把上传无 ACK 判成功。
5. `test_rfq_status.py::test_send_failure_and_delivery_persist_without_touching_live_files`：GET 状态入口 404。

## 旧测修订裁决与证据

主审明确批准以下修订；未删除测试、skip 或放松业务断言。

- `test_rfq_assign_spec.py::test_one_bond_two_uins_makes_two_batches`：`ok=False`，增加无上行原因断言；保留两收件人、两命令、原文、独立 ID、状态及文件不变断言。依据新设计第七节：本地提交不是执行成功。
- `test_rfq_assign_spec.py::test_send_does_not_change_link_book_or_wechat_groups`：去除矛盾 `ok=True`，保留已有 `ok=False`；从响应生成 `blob`，恢复原先不可达的微信群隐私断言。
- `test_rfq_oss_send.py::install_fake` 和缺询价密钥用例：显式安装 `upload_live`；网络仍只到已注入 Fake OSS。保留原文密封、密钥隔离、失败 ACK、写入证据等全部断言。
- `test_rfq_oss_send.py::test_put_rejection_logs_status_without_secret`：按 STS 公共构造合同用 `CredentialSet + put_transport` 注入替身，替代已删除的 `_cfg/object_url` 内部补丁；保留 403/AccessDenied 日志、统一安全异常、秘密与密文不回显的全部断言。

修 fixture 后命令：`timeout 1200 .venv/bin/python -m pytest tests/test_rfq_assign_spec.py tests/test_rfq_oss_send.py tests/test_rfq_send_gate.py tests/test_rfq_status.py -q --tb=short`

结果：退出码 1，**30 passed / 4 failed / 0 skipped**，0.75 秒。日志：`test-logs/legacy-fixture-correction.log`。

其中缺询价密钥、负 ACK 原因原被 fixture 问题遮挡；接通实际装配后返回统一“发送失败”，没有保留安全原因。此两项为新增揭露的产品缺陷，未改断言迎合实现。其余两红是上传无 ACK 被判成功及状态 GET404。

## 新验收运行

1. `timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_auth_entrypoint.py -q --tb=short`：退出码 1，1 failed / 14 errors；均源于 `server` 模块尚未交付，只用于暴露合同装配缺口，不计为已有业务缺陷。日志 `test-logs/auth-contract-red.log`。
2. `timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_sts_protocol.py -q --tb=short`：退出码 0，26 passed，0.08 秒。日志 `test-logs/sts-independent-first.log`。
3. 增加显式过期凭据快照用例后：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_sts_protocol.py tests/test_rfq_oss_send.py::test_put_rejection_logs_status_without_secret -q --tb=short`：退出码 0，28 passed，0.30 秒。日志 `test-logs/sts-independent-snapshot-red.log`（该文件名沿用计划命名，实际输出全绿；实现方在运行前已修复快照校验）。

固定中文向量独立来源：本机 OpenSSL `enc -sm4-cbc`，仅使用测试 KEY `0123456789abcdeffedcba9876543210` 和 IV `000102030405060708090a0b0c0d0e0f`；JSON `{key:业务口令, ts:1800000000字符串}`。测试使用冻结结果，不在运行时调用产品加密函数生成期望值。

## 停止条件

所有适用设计场景都需要实际入口断言、定向与同一版本全量测试、前端构建和受控端到端新鲜证据；未接入的真实数据库/采集端/Dify能力必须明确登记。当前只完成首批协议与基线工作，不满足整体交付停止条件。

## 第二批新增证据

- 生产认证扩展到 24 例：增加 ±60 秒 SSO 合法边界、token 截止时刻失效、跨应用/协议版本/错误 exp 类型拒绝、签名合法但 JSON 非对象的清洁拒绝、query 冒充账号无效。
- 首次扩展跑出 `[]`、`null` session payload 返回 500 两红，实现方补类型验证后，独立 `auth + STS` 组合 **51 passed / 0 failed / 0 skipped**，0.96 秒。命令：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_auth_entrypoint.py tests/acceptance/test_sts_protocol.py -q --tb=short`；日志 `test-logs/auth-sts-current.log`。
- 行情验收 `tests/acceptance/test_quote_truth.py` 首次 12 例 **2 passed / 10 failed**，0.23 秒；真实 protobuf 解码和 `QBSource.poll -> SQLite`，仅替换外部行情来源。命令：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_quote_truth.py -q --tb=short`；日志 `test-logs/quote-truth-first-red.log`。覆盖净价误当收益率、0/未知类型、全撤、同价刷新、估值更新、跨日旧价、静态/行情写入失败重放；两绿为单侧撤价和旧版本不能恢复撤价。后增加来源估值日缺失不得填 today、合法负收益率两例，等待实现批次重跑。
- 组织树 runner 因真实模块新增 bundler 风格的扩展名省略 import，原 Node strip-types 直接导入失败；主审批准用 esbuild 打包真实模块后调用原 runner，所有行为断言保留。打包后暴露原有 `orgEmptyLabel` 缺失，已反馈前端实现方。
- `pytest.ini` 使用 `testpaths=tests`，包含全部既有、developer 和 acceptance 测试。已核 `relay/side_a/echo_loop_test.py` 是需指定真实 peer、ADB 操作和真外发的手工硬件 main 脚本，不包含 pytest 测试函数；不将它纳入常规离线套件，不算跳过回归测试。
- 独立浏览器套件已落 `web/tests/acceptance/`：由 fixture 独立启动 Vite 端口，拦截全部业务 API，阻止其他地址访问；采用主审安装的临时 Chromium `/tmp/xunjia-playwright-20261013`。第一轮遇前端并行变更的登录存储键，已按最终合同 `xj_auth_token` 修 fixture；初轮终止不记通过/失败数，正在定位浏览器运行栈并完成首次有效运行。

## 当前候选全门运行（仍非最终验收）

命令：`.venv/bin/python tests/run_verification.py --browsers-path /tmp/xunjia-playwright-20261013`。

证据目录：`/tmp/xunjia-verification-uos6x1pn`，含精确命令/工作目录/退出码/耗时、分门日志、前后源码 SHA256 清单及 `result.json`。

| 验证门 | 本次结果 |
|---|---|
| 默认全量 pytest | 252 passed / 0 failed / 0 skipped，3.99 秒 |
| 既有前端 Node 测试 | 8 passed / 0 failed / 0 skipped |
| 前端开发者测试 | 22 passed / 0 failed / 0 skipped |
| `npm run build` | vue-tsc 与 Vite 均通过，命令退出码 0 |
| 独立真实浏览器 | 14 passed / 0 failed / 0 skipped，10.79 秒 |
| 同版本检查 | **未通过**：运行期间新增 `tests/developer/test_bond_tools_contract.py`，`source_drift` 非空，统一结果 `passed=false` |

不得把上述各门绿计数合成最终“0 回归”。新增四工具、Chat、生产业务 HTTP 和远端执行链仍在实现中。

本轮明确修订/补充：

- 主审批准将 `test_rfq_assign.py` 和 `test_rfq_send_gate.py` 中要求前端事件回调本地写“询价中/发送失败”的旧字符串断言，替换为真实 Quote setup 行为探针 `web/tests/acceptance/quote_status_probe.mjs`。实现 `onRfqSent/Pending/Failed` 后检查实际可显示行：服务端待回执则保持待回执，failed 回调不会覆盖服务器确认的在谈状态，重读失败保留最后成功状态。保留原 modal/禁止直发等其他断言。依据新设计删除前端覆盖态、服务器权威状态。
- 业务服务独立 17 例通过：预览零命令、5 并发同确认唯一命令、幂等内容冲突、账号/回执隔离、重复停止/迟到 ACK 不恢复、重复 ACK 不推进、重启停止和稳定 ID、买 20/卖 30 BP 及失效报价。`test-logs/batch-lifecycle-current.log`。首跑曾有验收 fixture 访问不存在的 `Database.url` 属性，已改用自身临时数据库路径，明确属于测试设置错误，不算产品红。
- 行情独立扩展 14 例全部通过，`test-logs/quote-truth-current.log`，0.17 秒。
- 浏览器额外发现两项真实问题并保持红测推动修复：历史曲线被日内空数据错误隐藏；缺少 `used` 字段时按在谈阶段虚构 40% 用量。现 14 例通过；分别留存 `browser-entrypoints-expanded.log`、`browser-invented-usage-red.log` 及候选总门绿色日志。
- 浏览器初轮卡住根因由 faulthandler 定位为 Playwright 同步 WebSocket 回调中调用 `close` 死锁，改为不转发的惰性 mock WebSocket，未加重试或放大超时。仅终止对应本任务 pytest/driver/Vite 精确 PID，没有处理其他服务。

## 生产接口、四工具、Chat协议与派发器增量

- 生产 HTTP 独立 24 例：实际合法身份预览/确认成功、旧裸 items 不可绕授权、异人批次/回执拒绝、重复确认幂等、四个 GET 确实存在且零业务变化/零发送、非法版本/数值/超长 Unicode 合并拒绝、重复停止只影响本人、无绑定拒绝。与四工具 24 例组合 **48 passed**，日志 `http-tools-expanded.log`。
- 四工具覆盖 NOT_CONFIGURED、全字典分页歧义、前导零、缺余额/估值日不补值、同发行人硬筛、公开白名单、独立手算零息/反算、净全价与结算日、当期收益率不是 YTM、未支持类型不降级。故障注入发现 `sources=[None]` 会抛 AttributeError，实现方补供应商快照结构验证后 24 例全绿。日志 `tools-source-boundary.log` 保留原红，后续 `rfq-contract-tools-current.log` 为工具 24 + RFQ 旧门 2 的 26 绿。
- 工具最初两处测试设置修正有明确合同依据：相似查询填写非空比较字段；假设当期收益率明确 `hypothetical=true/coupon_basis=fixed_annual`。另加缺 coupon_basis 返回 partial 的正向缺失门，未放松计算输出断言。
- `tests/acceptance/test_dify_chat_stream.py` 独立 **17 passed**，日志 `dify-independent-first.log`：7 字节分块、完整提案、两个终态任意先后但缺一不可、终态后 error、partial JSON、message_replace、每轮新 inputs、可信 user/conversation、私有 thought 和错误脱敏、非法 AgentTurn 与预算拒绝。此处仅证实 transport 完整性，未代替 ChatRunner 业务约束。
- `tests/acceptance/test_dispatcher_safety.py` 独立 **13 passed**，日志 `dispatcher-independent-current.log`：5 并发认领一次、accepted 仅 pending、丢响应 unknown/重启只对账、迟到确认、queued 停止零发送、pending 取消屏障、四字段回执身份校验、授权到期、网络 I/O 中 stop 不被事务锁挡住、晚 ACK 只补历史、BaseException 模拟提交后进程崩溃保持 pending 并在重启后不重发。
- 新 RFQ 模态窗实际浏览器首批 **7 passed**，日志 `browser-rfq-first.log`：显式方向/量、服务器最终原文、确认仅带 batch/version/request、queued 不冒充已发、改参数/收件人使预览失效、503 重试不换请求 ID。按主审新增裁决补本人已绑定但目录 stale 仍允许零发送预览；该用例已在后续定向执行通过。
- RFQ 旧源码门中 `status==='delivered'` 和 emit 先后断言随正式六状态合同迁移，改为真实 `RfqAssignModal.setup` 执行：queued 只发 pending 事件、sent 才发 sent、failed 只发 failed、无 preview 零请求、confirm 不再带裸 items。主审批准同一迁移范围；原其他门保持。
- 真实生产 DTO → 浏览器发现 NEGOTIATING 被显示为待询价（后端 state/前端 stage 不匹配），已由主审补兼容字段；独立 `test_production_dto.py` 保留为跨层回归门。
- XLSX 与交易板独立 **4 passed**，`browser-export-board-current.log`：真实 openpyxl 合成文件下载字节不变、完整 FilterSpec、JSON 错误不下载、九个事实字段和数值零保留、缺失显示破折号。第一次事件监听 fixture 把 Python 内建 `list.append` 直接给 Playwright 触发适配错误，改显式 lambda 后重跑；不是产品错误。
- 行情 HTTP/WS 初批 **8 passed / 1 failed**，`market-http-ws-first.log`：2051 行分页无漏重、list/resolve/export 同筛选、排除用市场代码、公共数据不读私有意向、XLSX 公式文本、WS 匿名/Origin/过期通过；`delta.s` 嵌套静态数据未过滤内部字段的红测已交主审。随后增补两个静态 HTTP 端点的同类白名单门，等待对应修复与重验。

下一验收缺口：ChatRunner 合法提案正例与越权/停止竞态、持久澄清/历史额度、记忆修订和公开回复签证；完整双通道/远端执行端及真实 OceanBase/Dify 环境仍无本轮验收。所有增量结束后等待源码冻结通知，再运行同一源码哈希的统一全门验证。

## 后续 Chat 与真实 MySQL 兼容验收

Chat 正反例通过 context→validate 获得真实本轮签证，避免“所有模型结果都拒绝”的伪绿。曾独立复现并交实现修复：源文方向与模型声明冲突、对方超确认量要求、交割附加条件被忽略、社交清空一次澄清额度、迟到的授权前旧消息、跨日未重确认继续报价，以及 history_request+WAIT 在 transport 与 runner 间语义不一致。证据分别为 `chat-runtime-expanded.log`、`chat-runtime-crossday.log`、`chat-history-branch.log`。此前 32 项（含真实 Dify SSE 客户端→真实 ChatRunner 的完整/截断对照）已绿；跨日/history 最新修复仍需独立定向重验。

主审后续明确授权独立创建 MySQL 测试容器。命令：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/mysql_contract.py -q -s --tb=short`。

- **6 passed / 0 failed / 0 skipped**，16.28 秒；日志 `test-logs/mysql-compat-first.log`。
- 服务实测版本 **MySQL 8.0.46**；镜像 digest `sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b`。
- 容器名 `xunjia-mysql-verify-20261013`；容器 ID `47e939502034580b59b6c4df0d7f8f513f514b441902063e8aea455eba8c06e5`；回环随机端口 `127.0.0.1:32768`。
- 证据及数据 `/tmp/xunjia-mysql-acceptance-az2phz_a`。`container.json` 记录已停止并移除该精确新容器；保留独立 bind 数据目录及每例独立 schema 清单。未管理、连接或变更既有数据库容器。
- Python 网络禁令全程保留，测试级 fixture 仅登记经 Docker inspect 核验、本次新建的回环端口；用例结束移除放行。
- 使用产品数据库连接实测 `READ-COMMITTED`；显式 schema、异常 rollback、5 路绑定唯一、5 路同确认只生成一个命令、5 个独立 dispatcher 只发送一次、重复入站/并发 turn 只调用 Agent 一次均通过。没有在测试里替产品执行 SET 隔离级别来掩盖默认设置。
- `mysql_contract.py` 是显式数据库集成入口，不符合默认 `test_*.py` 收集名；不存在 skip/xfail。冻结终验时可用 `tests/run_verification.py --mysql --browsers-path ...` 将本门纳入同一次源码哈希验证。
- 此证据验证 MySQL 8 协议/事务兼容与并发，**未验证目标 OceanBase 版本、租户及生产权限**。

最新用户裁决删除 mock 服务后，独立维护的 `test_quote_truth.py` 与旧 `test_quote_channel.py` 导入已改为 `server.market_cache/qb_source/quote_channel`，其余断言不变。定向 **19 passed**，0.28 秒，日志 `market-migration-imports.log`；测试不再要求恢复已删除的 mock 启动入口。

## 上述红测关闭后的独立复验

- `timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_chat_runner_safety.py -q --tb=short`：**36 passed**，2.76 秒，`chat-runtime-current.log`。包含跨日/授权前旧消息与 WAIT+history 接缝修复。随后增加私有/公共能力 HTTP 边界一例，单独 **1 passed / 36 deselected**，`agent-capability-http.log`：私有服务凭证可读取合法 turn_ref；公共工具凭证不能读私有上下文；私有凭证不能冒充公共工具；伪 turn_ref 拒绝；公共计算结果不携带私有 turn_ref/账号。
- `timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_application_lifecycle.py -q --tb=short`：**1 passed**，1.43 秒，`application-lifecycle.log`。真实 create_app lifespan 在没有任何 HTTP GET 时派发授权队列；shutdown 后新授权不被残留 worker 派发。测试初稿复用已有在谈券/对手被正确拒绝，已改为新对手以验证 shutdown 本身，未改变业务断言。
- 浏览器第二批三个实际入口文件（RFQ 预览授权、XLSX/交易板、生产 DTO）独立 **13 passed**，32.92 秒，`browser-phase2-current.log`。其中 stale 目录仍可进行本人零发送预览，生产 NEGOTIATING→询价中 DTO 已收敛。
- 行情静态白名单修复后，`test_chat_runner_safety.py` 首批 11 + `test_market_http_ws.py` 11 组合 **22 passed**，3.07 秒，`chat-runtime-first.log`；静态 HTTP 与 WS 均不再透出测试私有字段。

目前继续按冻结前定向验收推进，不将不同时刻的通过数合并成总验收。最终统一命令为：`.venv/bin/python tests/run_verification.py --mysql --browsers-path /tmp/xunjia-playwright-20261013`；它会记录同次 MySQL、pytest、前端单元/开发者、构建、浏览器及前后源码哈希。覆盖 Python/Vue/TS/SQL/启动脚本/锁文件和 Dify 运行 prompt；`source_drift` 非空时即使各门绿也拒绝最终通过。

## 双通道、执行端账本、人工消息和切换

新独立用例 `test_dual_transport.py` 10 项、`test_manual_switch_http.py` 初批 10 项组合 **20 passed**，1.76 秒。精确命令：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_dual_transport.py tests/acceptance/test_manual_switch_http.py -q --tb=short`；日志 `dual-manual-switch-current.log`。

- 双通道用真实 `Codec/OSSTransport/MailTransport/ReceiverLedger/Inbox`，只替换 OSS HTTP、SMTP、IMAP 和 native 执行器。覆盖加密入口重复投递/重启只执行一次、取消先到及同 peer 旧控制版本屏障、执行认领后进程崩溃不重发、同 ID 改正文和密文篡改拒绝、OSS 接受后响应丢失不新建业务对象、unknown 不暗切邮件、SMTP 接受只表示 accepted 且正文仅密封附件、坏邮件隔离/UIDVALIDITY 改变不重做已 ACK 事件、两路同源事件共享序号、业务提交失败不 ACK 且恢复后可重放。
- 人工消息走实际生产 HTTP：明确人工发送和接管在同事务提交、取消已有 queued 自动回复、机器人停止时显式人工消息仍能执行；成交措辞/内部底价不可绕过；同请求幂等且异正文冲突；跨 owner 不可发送他人对话；unknown 即使停止后也不能通过人工或换通道绕过；通道切换要求本人 ID、停止/清理未决和正确 expected_version；普通交易员不能 POST 改账号绑定。
- 随后新增 monitor 真实 XLSX 导出：显式 `+08:00` 时间范围内选出本人的唯一消息，不导出另一用户私聊。单独 **1 passed / 10 deselected**，0.57 秒，`chat-export-timezone.log`。因此该文件目前 11 个用例，此新增例尚未与所有门作同版本冻结合跑。

当前待完成的是源码冻结后的统一验收，以及隔离 Dify 1.17.1 的真实导入/节点运行（若环境提供）。目标 OceanBase 租户和现役采集端/真实邮箱/OSS/企点工作账号仍未连接，离线适配器及独立 MySQL 的通过不能替代这些现场边界。

## 后续独立补验与当前阻断（2026-10-13）

以下为后续定向证据，更新前文对应的历史待办；源码仍未冻结，不能据此宣称全量零回归。

- MQ STS：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_sts_protocol.py tests/acceptance/test_sts_mq_contract.py -q --tb=short`，**43 passed，0.18 秒**，`test-logs/sts-mq-independent.log`。仅精确 MQ HTTP URL 允许，其余相似主机、路径、query/fragment 变体拒绝；独立解密合成 SM4 token、检查字符串时间戳；生成 curl 配置为 0600、零网络调用、不打印 token、不覆盖已有文件、不回退长期 AK。只读上游源码 HEAD 为 `ac26e9ed72f816e2a19b9e1d01c9572bd8d214a4`。**没有读取或使用真实 auth.key/sm4_key、用户示例 token/临时 AK/SK，也没有调用真实 MQ。源码一致与合成请求通过不代表真实 MQ 已通过。**
- 隔离 Dify 1.17.1：独立 `dify_runtime_contract.py` 两轮实跑 **1 passed，4.43 秒**；同一 conversation 的 epoch、turn/source 更新，每轮真实经过合成工具并获得 30 BP，证据 `/tmp/xunjia-dify-independent-7f7rl010/result.json`。另 `-k existing_root` 只读真实实例 SQLite 的工具预算、签证及 sent 命令，**1 passed，0.27 秒**，证据 `/tmp/xunjia-dify-root-readonly-nks379fh/result.json`。详细运行与提取器修订已写 `review-notes.md`；合成模型不代表真实模型或工作账号验收，进程加载旧源码的历史运行不代替最终冻结版本。
- 人工对账浏览器：`PLAYWRIGHT_BROWSERS_PATH=/tmp/xunjia-playwright-20261013 timeout 1200 .venv/bin/python -m pytest web/tests/acceptance/test_reconciliation.py -q --tb=short`，**2 passed，5.55 秒**，`test-logs/reconciliation-browser.log`。实际 Vue 弹窗要求结果、凭证和勾选确认；请求包含当前结果 hash，成功不发送新命令或开启机器人；业务拒绝保留 unknown 且不显示成功。
- 双通道恢复与管理员重放：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_dual_transport.py -q --tb=short`，当时 **13 passed，1.18 秒**，`test-logs/dual-replay-current.log`。包含过期事件重新签封保留原 ID/seq/发生时间，以及管理员 replay 仅展示、fresh 入站正常可运行的正反例。此后该文件再新增下面两项诊断门，历史 13 不是当前完整计数。
- 运行诊断新增 5 门：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_business_http.py tests/acceptance/test_dual_transport.py -k 'runtime_diagnostics or public_health or inbox_failure or one_slow_inbox' -q --tb=short`，**5 passed / 48 deselected，0.71 秒**，`test-logs/runtime-diagnostics-focused.log`。真实 HTTP 验证本人/他人诊断隔离与匿名拒绝、异常原文不进响应或日志、DB 故障健康标记降级；真实 Inbox 验证单路故障不污染另一通道、恢复保留历史失败计数，以及慢通道等待时另一通道可完成业务校验和 ACK。并行门用事件同步而非固定 sleep。
- **当前新红**：上述两个文件完整定向合跑为 **33 passed / 20 failed，11.49 秒**，`test-logs/runtime-diagnostics-independent.log`。20 项失败共因 `server/business.py:149` 的命令 INSERT 抛 `sqlite3.OperationalError: 14 values for 15 columns`，发生于合法 confirm；已向主审上报，独立方未修改产品代码或放松测试。运行诊断本身 5 门通过不能消除这个回归。等待修复后重跑受影响门，再由主审通知冻结全量。


上述 INSERT 回归修复后的独立重验：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_business_http.py tests/acceptance/test_dual_transport.py tests/acceptance/test_chat_runner_safety.py -q --tb=short`，**105 passed，14.47 秒**，`test-logs/runtime-diagnostics-retest.log`。原 20 红日志保持不动。此轮同时新增第 6 项诊断门：真实 ChatRunner 的模型异常关联 `CHAT_GENERATION_FAILED` 与持久 turn ID，另一账号不可见；原异常文本不进入 API 结果、诊断、turn 数据和日志，且不产生任何新出站命令。该合跑也重新覆盖每券授权时间、错误市场后缀、人工裁决后旧社交消息不隐式恢复等已补行为门。现存 1 条 Starlette/TestClient 弃用警告，未通过屏蔽警告掩盖。

R12 正式行情 SQL 路径独立补 3 门后，`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_market_http_ws.py -q --tb=short`：**14 passed，6.21 秒**，`test-logs/market-sql-independent.log`。使用真实 `QBMarket → market_cache SQLite → MarketService → create_app HTTP`，不再只用 iterable provider；137 券同 flow_id、7 行页面遍历无漏重，完整组合 FilterSpec（关键词、标签/清算/期限/实盘、六类价量边界和省份/类型组）与 selectAll 排除及 XLSX 集合一致；状态筛选只取本人 queued 会话，缓存伪造私有意向/已成交与另一个 owner 均不能污染结果。验证固定数据集的行为正确性，**没有进行性能压测，也不宣称跨请求市场更新下的快照语义**。监控历史复合游标尚等稳定接口合同，未写无接口的伪红。

诊断门另补持久 `xj_audit` 验证：关联当前 turn 的唯一 `agent_failed` 审计保留错误阶段码，同时不含合成异常秘密。`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_chat_runner_safety.py -k model_failure_records -q --tb=short`：**1 passed / 51 deselected**，`test-logs/chat-diagnostic-audit.log`；该断言比前述 105 项合跑更新，最终统一验证仍待冻结。

监控聊天复合游标接口就绪后，独立 HTTP 9 门通过：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_business_http.py -k chat_cursor -q --tb=short`，**9 passed / 38 deselected，1.81 秒**，`test-logs/chat-cursor-independent.log`。105 条同时间戳消息精确分页为 50/50/5，首屏后新增消息不进入当前历史遍历，返回最新可见；他人游标不能漏出他人正文；非法编码/形状、布尔或无限时间、空/超长 ID、与旧 before 混用均返回 4xx，全部零发送。

实际 Monitor 浏览器 `test_chat_pagination.py`：`PLAYWRIGHT_BROWSERS_PATH=/tmp/xunjia-playwright-20261013 timeout 1200 .venv/bin/python -m pytest web/tests/acceptance/test_chat_pagination.py -q --tb=short`，**1 passed，2.95 秒**，`test-logs/chat-pagination-browser-current.log`。翻到历史页后轮询仍发原 cursor，页面保持单页 50 条；末页显示 5 条且禁用更早按钮，返回最新恢复 50 条，全程无 POST。初稿失败因推进虚拟时钟后立即读取 Python 请求列表，异步请求尚未完成；改为等待实际聊天 HTTP response 再断言，未加 sleep/重试或放松游标断言，原日志 `chat-pagination-browser.log` 保留。

采集端原生回声竞态新增 5 门后，`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_dual_transport.py -q --tb=short`：**20 passed，1.89 秒**，`test-logs/collector-echo-current.log`。使用真实 `CollectorBridge/ReceiverLedger/OSSTransport` 与合成 native 回调/内存对象库：native 回调内抢先出现的 out 先暂存，后续自动命令 held 且查询不调 native；精确原生 ID 回执才关联并释放后续授权；相同正文但不同 ID 保持人工并阻止旧控制版本；5 秒超时和新账本实例重启分别失败关闭；迟到映射不改已发布事实；可信 `role=human` 即时发布且阻止旧自动命令。未启动现场采集器、真实邮箱/OSS 或企点账号。调用预算、计时和线程竞争都由显式合成输入控制，没有扩大 native 运行授权。

## 归档、导出、只读问答和负载收口

- 归档/导出/CSP/同域真实构建：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_manual_switch_http.py tests/acceptance/test_business_http.py tests/acceptance/web_assets_contract.py -k 'archive or export or security_headers or web_dist' -q --tb=short`，**17 passed / 58 deselected，11.98 秒**，`test-logs/archive-export-csp-current.log`。归档只接受本人已停且无未决命令的指定 session，幂等、不影响同对手另一券、不删除消息；pending/unknown 阻止归档。导出要求有效起止时间，真实 1001 行读取为 500/500/1/0，账号和 peer 隔离，公式样正文仍为字符串。额外以源规模注入证明第 100001 行、20000001 字符均报 400，不返回截断的“成功文件”。
- CSP 原红 `test-logs/archive-csp-first.log` 为 **2 failed / 5 passed**：匿名认证早退响应没有安全头；修复后纳入上述 17 绿。真实 `web/dist` 同域服务只在显式配置后启用，所有注册页面与实际 index 字节一致，实际构建 assets 可读且不可路径穿越；API 认证仍然有效。此门位于不默认收集的 `web_assets_contract.py`，统一 runner 在 `npm run build` 后显式执行，不让普通 pytest 依赖预先存在的构建产物。
- 只读问答真实入口：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_market_http_ws.py -k readonly -q --tb=short`，**7 passed / 14 deselected，1.60 秒**，`test-logs/qa-readonly-independent.log`。真实公共行情/本人会话数据；缺 Bid 不补数，Ofr 为零仍保留，双缺不画图；参考资料不可用明确说明；SQL、身份字段覆盖和坏类型拒绝；另一个 owner 的在谈券与对手不泄露，查询或不支持的成交指令都不产生新命令。
- 首次策略保存浏览器：`PLAYWRIGHT_BROWSERS_PATH=/tmp/xunjia-playwright-20261013 timeout 1200 .venv/bin/python -m pytest web/tests/acceptance/test_strategy_first_save.py -q --tb=short`，**2 passed，6.99 秒**，`test-logs/strategy-first-save-browser.log`。新账号显示未保存草稿，打开窗口零 POST；只有点击保存才以 personal 模板提交，成功重开显示已保存，失败保留草稿；不发送 RFQ。
- 对应生产 HTTP 再验：`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_business_http.py -k first_personal_strategy -q --tb=short`，**1 passed / 50 deselected，0.77 秒**，`test-logs/strategy-first-save-http.log`。本人首次保存后可重读，另一账号仍为未配置，业务命令与外发保持为空。

显式 R32 负载门 `tests/acceptance/load_contract.py` 不纳入日常 `test_*.py` 收集，已加入统一 runner `--load`。精确首跑命令：`timeout 1800 .venv/bin/python -m pytest tests/acceptance/load_contract.py -q -s --tb=short`，**2 passed，91.67 秒**，`test-logs/load-contract-first.log`。

每个后端独立建立 5 个合成身份，各 100000 条历史、10 个当前会话。5 线程各循环 20 次摘要/对手/聊天，共 300 次真实 HTTP 只读请求，与一路本人 100000 行导出同时执行。没有开启后台派发。历史页默认 50 行；观察真实数据库游标，`fetchall` 必须有 LIMIT 且最多 201 行，导出 `fetchmany` 每批最多 500；MySQL 使用真实 SSDictCursor。导出逐行验证恰好 100000 条且全部本人数据。

| 实测隔离环境 | 普通只读 P95 | 最慢只读 | 10 万行导出 | pytest 进程峰值 RSS | 证据 |
|---|---:|---:|---:|---:|---|
| SQLite 临时 WAL 库 | 0.051745 秒 | 0.075579 秒 | 5.748 秒 | 91332 KiB | `/tmp/xunjia-load-acceptance-xfzorxmd/result.json` |
| 新隔离 MySQL 8 容器 | 0.627508 秒 | 0.858582 秒 | 11.343 秒 | 108652 KiB | `/tmp/xunjia-load-acceptance-itnzgs_q/result.json` |

两者均达到本轮测试环境的 P95 ≤ 1 秒门，server 源文件前后哈希无漂移。RSS 是含测试及 XLSX 验证的进程累计高水位，不是生产服务独立内存测量；数据内容、机器、并发规模固定，**不能据此承诺 OceanBase 目标租户容量或生产服务延迟**。

MySQL 仅创建 `xunjia-mysql-verify-20261013`，ID `3cc015a5e9f4825c2733a5fcae28900f46eefbe4deb742d9b76feb60f35a61aa`，`127.0.0.1:32775`；已核 ID 后精确停止并移除，保留 `/tmp/xunjia-mysql-acceptance-wmm6_d6x` 数据与 container.json。网络 guard 只登记该新端口，未连接既有库。最终冻结命令需带 `--mysql --load`；全仓源码哈希门仍由统一 runner 执行，以上候选负载结果不替代最终冻结验收。

当前待关闭新增红测：`test_dual_transport.py::test_expired_held_command_reaches_failed_without_ever_calling_native`，`test-logs/collector-held-expiry-red.log` 与 `collector-held-expiry-current.log`。精确 echo 已分类但第二条 held 命令授权过期时，reconcile 仍永久 pending；该命令有从未进入 native 的持久证据，应转失败而不查询 native。已交实现方，测试方不修改产品或放松终态要求。

## R17/R18 修复后的独立复验与冻结建议

`timeout 1200 .venv/bin/python -m pytest tests/acceptance/test_manual_fence.py tests/acceptance/test_dual_transport.py -q --tb=short`：**27 passed，3.33 秒**，`test-logs/manual-fence-final.log`。上述 held 到期红已关闭，原失败日志不覆盖。

新增 6 门使用真实 CollectorBridge、ReceiverLedger、OSS/Mail 签封适配器、Inbox、InquiryService、ChatRunner 签证和 Dispatcher，只替换外部 native/对象库/SMTP/IMAP：

- 同一人工事件两路信封顶层的屏障 ID/version 相同，正文及业务 hash 不变，重复发布不递增；管理员重放保持屏障与业务事实，已 ACK 的订阅端不重复消费。
- 未观察到当前屏障、伪 ID、旧屏障即使 control_version 很高也不能执行；持有精确当前屏障且有效的明确授权可实际执行一次。native 回调仍在途时发生可信人工 event，随后版本 9999 的无新屏障命令仍取消、零 native。
- 根链路实际收到人工签封后转 HUMAN；显式 resume 后，合法 Agent 回复和明确人工发送的新命令均携持久屏障，通过接收端账本执行且没有重复 native。
- 新用户首次仅收到历史/重放事件，不因无授权初始 HUMAN 状态永久卡死；首次明确 preview/confirm 携正确屏障，经真实 Dispatcher 到账本实际 sent 一次。
- 同版本不同屏障 ID 拒绝并回滚；历史事件携更高屏障仍优先接管；恢复后旧版重复事件不回退屏障，也不撤销新的明确恢复。

该文件初稿 `manual-fence-first.log` 的 1 红是 QA 错误要求已 ACK 订阅端再返回同一重放事件。改为直接解码实际重放的 OSS/邮件密文并同时断言已 ACK 不重复消费，未修改产品或放松签名/业务 hash/控制屏障断言；修正后首轮 26 绿，追加在途高版本及真实首次派发后最终 27 绿。

独立方当前已知阻断清零，已通知主审可准备冻结。严格匹配作为本批数量授权核对约束，不将关闭该偏好说成已实现部分成交能力。仍需主审明确最终冻结时刻，然后运行 `.venv/bin/python tests/run_verification.py --mysql --load --browsers-path /tmp/xunjia-playwright-20261013`；所有源/测试/prompt/锁文件须零漂移才能形成最终统一证据。
