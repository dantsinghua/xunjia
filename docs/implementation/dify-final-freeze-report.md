# 安琳的最终冻结版 Dify → root 整链复验

日期：2026-10-13。冻结工作树：`/home/anlin/.codex/worktrees/b372/xunjia-agent`。本报告替代旧root进程的候选整链结果，用于本次冻结版本的实现方复验；独立QA仍单独出具结论。

**结果：真实新Dify到重新加载冻结源码的 `server.app` 整链通过。252项源码前后无漂移，23个实际加载模块与冻结清单一致，六次工具预算与凭证隔离负例通过。** 本轮只修改 `/tmp` 验证产物和本报告，没有修改源码、DSL模板或测试。

## 冻结身份与实际加载证据

原清单：`/tmp/xunjia-final-freeze-20261013-yb6ljazq/source.json`，252项，SHA256：

`af3b276f22512a73bbba2c324e4f22d52807641722301ea253cd8bfb073fc02f`

本次证据根：`/tmp/xunjia-dify-verify-20261013/final-freeze-af3b276f`。启动前及复验后逐项计算工作树文件hash，均匹配清单；不是仅比较清单文件本身。

专用临时启动器 `launch.py` 拒绝源码hash不符，并对 `server.*` 从已校验的源文件字节直接编译执行，不读取旧pyc。`loaded-modules.json`记录进程PID、启动时间、模块路径及实际编译字节SHA256。23个模块全部匹配；关键模块如下：

| 模块 | SHA256 |
|---|---|
| `server.app` | `84c48e156389ef9ad39ad39c357c21adf6e12d32ee6fa12099b4f4cfa68e1e4c` |
| `server.schema` | `e8ac3030b00fed2160bf4f9808f9f5ef269fae7f84edf72ba7fd3a7ec7e2fd42` |
| `server.chat_runtime` | `a7a58b9e489004fb878974e86c44c42e699ed82b8e1e64b5839a95a2afffa40e` |
| `server.agent_routes` | `5807f1ef58fb9fdaa3a134b0a645c32b20dbc3cf931ca40f155146c827915615` |
| `server.business` | `99531c392546b13668d58af5978ede12c8b96981daaf2e07b4f4af4c11b64a89` |
| `server.runtime` | `d3ac98f55d414e8bbd90ccfed80e62551583c93696d82240c787cbd05854d681` |
| `server.public_text` | `ad3a395b9b543f31989a0b4a972190f23ec1a755ea9d3d71a5bb91c123d76a22` |
| `server.dify_client` | `f1ded3560ea71a5ec307bc65338bb27549e02588b699618e4762ffeaf35c68bf` |

原验证harness先按冻结hash核对，再仅在临时副本中替换输出根并加入模块/SSE记录器；FakeTransport、合成数据和实际产品调用不改。变更说明和前后harness hash见 `harness-instrumentation.json`。首次启动器误引用了不存在的 `server/schema.sql` 清单键，实际schema为 `server/schema.py`；只修正 `/tmp` 取证脚本后重建本次测试root服务，产品合同无需解冻。

## 旧新容器、数据库与稳定访问点

精确project仍为 `xunjia-dify-verify-20261013`，仅替换它的 `root_backend` 服务；Dify应用、模型fixture、数据库、插件和其他服务均未重启。

| 项目 | 旧root进程 | 本次冻结root进程 |
|---|---|---|
| 完整容器ID | `d815d9a3553c819225131fea905fb7b51099b7435ed2b303f71263c8d9b5d363` | `794149b9996aeb072552b90410cf0e57ed3dc2d33d8bb2199f21096359486255` |
| StartedAt（UTC） | `2026-10-13T06:28:22.010744925Z` | `2026-10-13T08:40:30.536169236Z` |
| 容器名 | `xunjia-dify-verify-20261013-root_backend-1` | 同名新容器 |
| loopback管理口 | 32774 | `127.0.0.1:32779` |

新SQLite：

`/tmp/xunjia-dify-verify-20261013/final-freeze-af3b276f/root-instance-1791880831192235177.sqlite`

旧SQLite `.../root-instance-1791872902867910983.sqlite` 保留原位，不覆盖、不删除。新数据库由冻结的 `server.schema` 初始化，已核对 `xj_conversation.receiver_fence_id/receiver_fence_version` 和本轮命令body中的对应字段。

原15个业务容器（含Dify0.15.3）全部仍运行，完整ID和StartedAt与初始基线一致。本次替换root之外的36个已有容器，状态与StartedAt也无变化。证据：`untouched-containers-before.json`、`root-before.json`、`root-after.json`、`final-proof.json`。

## 真实冻结版运行结果

继续使用已实际导入的Dify1.17.1应用 `af146254-ee04-4ad0-81fe-7c2b58f39593`，其图与当前冻结模板相同，只绑定测试Provider/模型和固定服务配置。模型仅访问既有合成fixture，不调用真实模型。

本轮原生Dify workflow run：

`cd80e137-626d-4dd5-97bc-51fadd06fb11`

直接查询本次新Dify PostgreSQL `workflow_runs` 得到：

```text
cd80e137-626d-4dd5-97bc-51fadd06fb11|succeeded|af146254-ee04-4ad0-81fe-7c2b58f39593
```

SSE证据：`dify-chat-1791880910031150167.sse`，含22个成功节点和完整成功终态。路径覆盖：context HTTP → 当前产品四工具中的真实 `bond_calc`（30.00BP）→ 私有Agent提案 → 当前后端动作核验 → 公开措辞 → 当前后端最终签证 → JSON Answer。工具成功按真实tool_response内容核对，不依赖Agent日志的外层success标签。

root原生SQLite及驱动结果同时确认：

- `ChatRunner.process`返回 `queued`，本轮入队恰1条。
- 命令ID：`cmd_5d5d73f0df399f4fffd3cd161aee8d13`。
- 本轮源消息：`msg_520fdb1397ff42b39bb5b4e0822144dd`；turn最终 `done`。
- 本轮持久 `tool_calls=1`，最终签证存在并绑定提案摘要。
- FakeTransport后sent总数2，包含初始化合成开价和本轮公开正文；没有真实外发。

证据：`root-chain-result.json`、`driver-response.json`、新SQLite、SSE和 `final-proof.json`。验证harness背景任务关闭，所有账号、券、行情与传输均为合成。

## 当前产品预算与凭证隔离复验

在新SQLite另起合成预算轮，向当前真实HTTP工具入口连续调用：

`200, 200, 200, 200, 200, 200, 400`

数据库最终 `tool_calls=6`，第七次未增加。以下四个负例均返回403：

1. 公共预算票据冒私有turn_ref访问context。
2. 私有service token冒公共tool token调用工具。
3. 私有turn_ref冒公共预算票据调用工具。
4. 公共tool token访问私有context。

证据为 `budget-result.json` 与原生SQLite。预算负例没有创建发送命令；捕获时turn为running，按当前产品300秒有效期到期，后台关闭。不要通过改数据库或续期旧票据重复案例。

## 独立只读复核与私有配置

QA已收到精确新ID、StartedAt、数据库、SSE、结果和哈希路径。可运行：

```bash
python3 /tmp/xunjia-dify-verify-20261013/final-freeze-af3b276f/audit.py
```

该脚本只读核对252项源码、23个实际加载模块、成功SSE和30BP工具结果、新Dify原生run、root签证/命令/预算以及容器隔离。已真实退出0，结果保存在 `final-proof.json`。

私有合成配置在证据根下 `credentials.json`、`root-app-key.json`、`budget-ticket.json`，权限600，未在聊天或报告披露明文。SSE同为600，已将该精确文件owner改为anlin供QA读取，未改变其他权限。入口 `/_verify/run` 与 `/_verify/budget/start` 为一次性驱动，独立只读复核不应再次调用。

## 证据摘要哈希

```text
7f94eabbfb0d07437ceb49ef60973d178ba2ac54a60e202c8c3fd11c8148551f  loaded-modules.json
6e9c502c3264e6f48f496f529db4a14b8a08301ec1d6dbdf77cc5440e97ca1bc  driver-response.json
705afb8f2bc49368713b6e26254a7e0847691c6d8a564e5580d2edd296131cae  budget-result.json
a36dadfecf708a792c32d7f38e7fb841788c0a8dcfe21a5834b9f18b5ebf5804  dify-chat-1791880910031150167.sse
2c92704537c86b2436a84b98fe52dd1441742a1a1ac600c23ac3d4b84243db46  final-proof.json
```

## 收尾边界

新实例保持运行且不再调整，等待QA只读复核后由主审精确停止。Compose仍是 `/tmp/xunjia-dify-verify-20261013/compose.yml`；按该project执行 `docker compose ... stop` 只停止本次项目，保留新旧数据库和所有证据。不要调用旧root ID或旧管理端口，也不要删除卷。

本次证明的是指定冻结源码的真实Dify协议/工具/授权签证/入队/Fake回执和预算隔离。没有把实际采集端非零fence对抗场景、真实模型语义质量、真实OceanBase/财汇或真实交易通道冒称为本轮验收。全仓统一门和独立终验由QA及主审各自完成。


## 已完成本次测试环境收尾（2026-10-13）

安琳，独立QA已通过冻结版Dify整链验收，证据为 `/tmp/xunjia-final-dify-independent-36ig0vm9/result.json`（`passed=true`、252项源码无漂移、23模块hash匹配、原生Dify run succeeded、签证摘要一致、预算与隔离检查通过）。按主审后续明确授权，本节更新前文“保留运行待QA”的状态：**本次测试项目现已全部停止，数据与证据保留。**

停止前逐个核对完整容器ID、project/service标签和StartedAt，9个对象均与记录匹配，无未知或缺失对象。按完整ID分三组执行 `docker stop --time 30 <已核验的完整ID>`：先root_backend/fixture/worker/api，再plugin_daemon/sandbox，最后db_postgres/redis。三组命令均退出0；只停止原先运行的8个任务容器，init_permissions保持原已退出状态。最终通过 `docker inspect` 及 `docker ps -aq --no-trunc` 确认9个容器对象全部仍在且均为exited。fixture与sandbox在30秒宽限后由Docker结束，容器退出码137；其他本次运行容器退出码0。没有执行rm、down或卷清理。

计数口径复核：此前“36其他容器”快照包含本项目的8个非root任务容器；本次已获授权停止它们。扣除任务对象后的28个非任务容器，完整ID、运行状态和StartedAt全部保持原样；原15个业务容器（含Dify0.15.3）也逐项无变化。未停止任何身份异常或非任务对象。

保留内容已核实：9个容器、5个named volume（app_storage/plugin_storage/postgres_data/redis_data/sandbox_dependencies）、全部临时目录与证据。新旧root SQLite均以 `mode=ro` 执行 `PRAGMA integrity_check` 返回ok，各自两条sent合成命令保持；8份关键验证证据SHA256前后完全一致。原工作树252项文件再次匹配冻结清单 `af3b276f22512a73bbba2c324e4f22d52807641722301ea253cd8bfb073fc02f`，`source_drift=[]`。

收尾记录仅保存于 `/tmp/xunjia-dify-verify-20261013/`：

- `shutdown-preflight.json`：目标完整ID、起始状态与非任务对象集合。
- `shutdown-execution.json`：三组精确stop命令结果；开始 `2026-10-13T08:59:40.849686+00:00`，完成 `2026-10-13T09:00:43.041510+00:00`。
- `shutdown-sqlite-proof.json`：新旧SQLite只读完整性及命令计数。
- `shutdown-proof.json`：最终对象、卷、隔离及源码冻结验证，时间 `2026-10-13T09:03:53.267742+00:00`；SHA256 `ad4c80f37cca2a36512f3b4f559e45852bad81d74447de98cb665e0430709f14`。

停止后的在线HTTP/SSE和原在线audit入口不再作为可运行探针；已有原生数据库、SSE与独立QA证据均保留。本次仓库写入仅为本节追加，没有改源码、DSL、测试或隔离工作树文档，也没有覆盖其他工作者的改动。
