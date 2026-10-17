# 安琳的 Dify 1.17.1 隔离导入与实际链路验证

日期：2026-10-13。角色：实现方真实集成验证；独立QA另行复查，不以本报告代替独立终验。工作树：`/home/anlin/.codex/worktrees/b372/xunjia-agent`。

结果：固定版本的新Dify已实际导入并发布31节点Chatflow；同一conversation完成两轮context epoch更新和真实只读工具调用。第二个应用已通过实际 `server.app → ChatRunner → 新Dify → 固定HTTP/工具/最终核验 → queued命令 → FakeTransport回执`。没有连接真实模型、业务数据库、交易账号或外发通道。

## 隔离范围与当前状态

精确Compose project：`xunjia-dify-verify-20261013`。专用目录：`/tmp/xunjia-dify-verify-20261013`。Compose文件：该目录下 `compose.yml`。本次初查可用内存约19GiB、磁盘823GiB、24逻辑核；最终9个容器中8个运行、初始化权限容器已正常退出，合计内存上限约9.7GiB。只公开loopback随机端口，不公开数据库、Redis、插件或sandbox端口。

原有15个运行容器（含Dify0.15.3）的完整ID和StartedAt在 `baseline-containers.json`；结束前逐项核对，全部仍运行且StartedAt一致。证据为 `isolation-proof.json` 和 `read-only-audit.json`。没有重启、改配置或复用它们的数据库/卷。

用户态下载解决了既有Docker daemon代理超时：原 `docker pull` 指向既有代理 `172.19.176.1:7890` 超时，未修改daemon代理、frp或FlClash。采用官方Google `crane v0.20.6` 临时二进制从官方registry取归档，再 `docker load`；该工具发布归档SHA256为 `c1d593d01551f2c9a3df5ca0a0be4385a839bd9b86d4a76e18d7b17d16559127`，下载后已核对官方Release元数据。

## 固定来源与真实digest

Dify源码版本 `1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`；DSL `0.7.0`；图实体依赖graphon `0.7.0`，wheel SHA256 `6f7608aaaa65607c4935735137ed4b7029b1e6ca80202cbe829af3370cf4404e`。策略源码commit `3b4e77c0be57dfcdde48183243fef411664e8755`。详细源码文件哈希仍见 `dify/chatflow_1171/source-lock.json`。

下表是实际registry中linux/amd64平台manifest digest，不是编造值：

| 镜像 | 官方平台manifest SHA256 |
|---|---|
| `langgenius/dify-api:1.17.1` | `04f435a2e366c73f1ccefe9e7fbdaa2ac6440bdcde268d5d20f19c3ba7e7dcdb` |
| `langgenius/dify-web:1.17.1` | `c4baeb297722671a23317b17d2af669f50b36f6239d1d976832a77b2057344e5` |
| `langgenius/dify-plugin-daemon:0.6.10-local` | `0b8f1f1e766826352d868aaf8c9eeeee8e37a3a3b023044ed8ab0d145f853cdf` |
| `langgenius/dify-sandbox:0.2.15` | `942acde5b90350498f71c9f89fe1c0a578578c51521d6683783e073528b6a26a` |

web只查询并固定digest，没有启动web容器，导入和运行走REST。API/plugin/sandbox归档的config hash与registry manifest中的config digest逐项一致，导入后的RootFS层也逐项一致。Docker29的containerd存储在载入Docker归档时生成新的Docker manifest ID，因此本地ID不能冒充原OCI平台digest；两者均记录在 `images-lock.json`。

真实下载并通过新Dify安装的官方插件：

| 插件 | Marketplace唯一标识 | 下载的完整difypkg SHA256 |
|---|---|---|
| Agent Strategies 0.0.50 | `langgenius/agent:0.0.50@bf1040682cc9e1e5caf9b4670531639c440bf9448025cd5ee0915fdcf7a921f3` | `a79adee0c4aaa76a6ab247f44f963e35aec66a54c39809ead02f4566663f7e54` |
| OpenAI-compatible 0.0.68 | `langgenius/openai_api_compatible:0.0.68@ba61c5eccb814be7364117c0bec3710a51e09b20f31e37243e747a00f9377969` | `3e134cc200b86edb905fc9b43cd5525741005f3004bacfa49c63601f7b271705` |

唯一标识末尾hash与签名完整归档hash属于不同口径，分别记录，没有相互替换。新插件daemon的 `FORCE_VERIFYING_SIGNATURE=true`。安装任务 `01a133e7-3ffa-7d04-b624-0bec62c89d6d` 返回两插件均installed/success。插件包、上传验证结果和安装状态在专用目录的 `packages/`、`*-upload.json`、`plugin-task-current.json`。

模型仅注册 `xunjia-fixture`，Provider为 `langgenius/openai_api_compatible/openai_api_compatible`，endpoint固定本项目内 `http://fixture:8000/v1`。使用虚拟Key，无真实模型凭据。

## 真实导入与两轮SSE

应用 `cbaadab2-5231-4de1-a762-6d5c1e19e040`，名称 `Xunjia Isolated Verification`。导入结果 `completed`、`app_mode=advanced-chat`、current/imported DSL均 `0.7.0`，warnings为空，发布成功。

最终成功两轮使用同一conversation `a7a265e1-fa33-4296-815c-e02c61ccae62`：

| 轮次 | context/AgentTurn epoch | 本地Dify原生workflow run | 原生状态 |
|---|---:|---|---|
| 1 | 1 | `b9814a5e-8dc9-4d3b-ae3c-3abea005a5ef` | succeeded |
| 2 | 2 | `f7ae63fd-7e5d-4db4-924f-38539221d0ce` | succeeded |

每轮实际顺序：固定context HTTP → 经典Agent产生工具调用 → 公共 `bond_calc` HTTP返回SELL 2.10/2.40的30BP → Agent完整提案 → 动作核验 → 只见公开上下文的措辞LLM → 最终正文核验 → JSON Answer → `DifyChatClient`收到message_end和成功workflow_finished后解析通过。

模型替身要求收到真实包含 `raw_gap_bp` 和 `analysis_only` 的工具结果才产生下一提案，工具错误不能被假成功吞掉。公开节点替身检测私有字段/能力引用，发现则报错。最终正文为合成固定句“请问这只券目前可做多少量？”。这些证明协议与流程，不证明真实模型的识券、策略或语义安全水平。

证据：`chat-turn-1.json`、`chat-turn-2.json`、`complete-tool-chain.sse`、`complete-second-turn.sse`、`fixture-events.jsonl`。只读复核还直接查询本次新PostgreSQL的 `workflow_runs`，确认上述run和root集成run均为succeeded；没有仅依赖实现方汇总JSON。

## 实际root应用、签证、命令及预算

第二应用 `af146254-ee04-4ad0-81fe-7c2b58f39593`，名称 `Xunjia Root Integration`，同样实际导入无警告并发布成功。固定HTTP及公共Provider指向 `https://root_backend:8443`；只读工具不是fixture模拟返回，而是当前工作树 `server.app` 挂载的真实四工具/预算路由。

root后端使用当前 `create_app`、`ChatRunner`、仓储和Dispatcher；新SQLite文件：`/tmp/xunjia-dify-verify-20261013/root-instance-1791872902867910983.sqlite`。仅合成owner/account/peer/债券、合成ReferenceSnapshot，background关闭。FakeTransport只返回本地identity匹配的sent回执，不连接OSS/邮件/企点。

真实Dify run：`c5c41159-b8c5-4dff-8e5f-61a283fa267b`，原生状态succeeded。实际后端结果：

- `ChatRunner.process`返回queued；最终签证存在且绑定候选摘要。
- 入队前后证据中，本轮queued命令恰1条，ID `cmd_a9984e6a988e30aaa4166f801a6a9721`。
- 真实工具预算计数为1，turn最终done。
- FakeTransport处理后sent总数为2：一条初始化合成开价、一条本轮Agent正文；不存在真实发送。
- 验证文件 `root-chain-result.json`，并由只读SQLite核对xj_turn与xj_command。

第二个合成源消息仅用于预算负例：HTTP前六次公共工具请求均200，第七次400；SQLite持久 `tool_calls=6`。此外：工具预算票据尝试读取私有context为403；私有服务token冒用公共工具token为403；私有turn_ref冒工具预算票据为403。证据 `root-budget-result.json`，票据本身只在权限600的 `root-budget-ticket.json` 中。

预算负例turn未产生命令，测试捕获时状态running；后台关闭，该票据按真实后端300秒到期逻辑失效。不要为复测把过期票据手工续期或直接改数据库。本次单次驱动端点 `/_verify/run` 和 `/_verify/budget/start` 不适合在同一案例上重复调用；独立复核优先读本次原生库和SSE，另起案例需由主审安排。

## 发现并修复的验证环境问题

1. 既有daemon代理超时：用已验官方工具下载归档导入，不改变既有代理。
2. 新实例初始化密码API最多30字符：将仅本次INIT_PASSWORD设为生成密码的24字符片段，owner密码独立保留。只重建本次API/worker，没有动基线15容器。
3. sandbox禁网与Dify默认Code运行请求不一致，首次明确返回-400：本次sandbox恢复官方默认 `ENABLE_NETWORK=true`。图内Code仍只有纯JSON处理，没有外部网络逻辑。
4. 首次工具fixture事件记录函数参数同名冲突造成工具HTTP断开。虽然Dify Agent的CALL日志标success，内部tool_response实际为错误；本次检查发现后修复，并加强Fake模型“没有golden工具响应就拒绝提案”。最终两轮有实际工具HTTP和30BP证据，未将早期表面成功算入通过。

这些环境修复只改 `dify/verify_1171/` 或本次专用Compose；未为适配改root产品代码、独立QA门或旧Dify环境。

## 新容器精确身份与端口

完整记录见专用目录 `container-inventory.json`；以下为本批停止写入时的完整容器ID。所有名称前缀均为 `xunjia-dify-verify-20261013-`。

| service | 完整容器ID | host监听 |
|---|---|---|
| api | `48470bad5033f7ab98659072213dbabff8c4ce4112879fed93ecee5e9e106a17` | `127.0.0.1:32771` → 5001 |
| worker | `8d395593c23cc885e73740dab686877c6ea043c520e519319576805cb3c9660b` | 无 |
| plugin_daemon | `1ca83ce492a037314383ddda01f7c7f6e1010843ed6af6df375dd803f0f44282` | 无 |
| sandbox | `6a79c76664f217e9700175555aec6da1a478847c68bb49506c906b7b7b06079d` | 无 |
| db_postgres | `1edb5b8895636752d4070b81517fc83a078d6e517ea9e024a4cd4d30f9792515` | 无 |
| redis | `6e66f9779b58ba537d56e4589dc594b484e397f31794c6b1dea55b63bf65fa7e` | 无 |
| fixture | `efcc92ab6cf264f799d5550f97bd1874941921e84f524501e8f0d8d34b26e467` | `127.0.0.1:32773` → 8000 |
| root_backend | `d815d9a3553c819225131fea905fb7b51099b7435ed2b303f71263c8d9b5d363` | `127.0.0.1:32774` → 8000 |
| init_permissions | `a5637a27455e474d61143688be757487fce6fe1b10d5bd7e893db1353fc149f0` | 已退出，无 |

新named volume均由该project创建：`app_storage/postgres_data/redis_data/plugin_storage/sandbox_dependencies`，真实全名带project前缀；没有旧数据库挂载。fixture和root_backend只挂当前工作树只读以及本次 `/tmp` 专用根。TLS为仅本次生成的CA/证书，未修改宿主机全局信任。

## 独立复查入口与凭据边界

新脚本都在 `dify/verify_1171/`：`prepare.py`、`console_client.py`、`fixture_server.py`、`run_chat.py`、`add_root_backend.py`、`root_app_server.py`、`audit_existing.py`。这些仅用于本次专用隔离环境，不是生产服务入口。

可随时运行的只读复核：

```bash
.venv/bin/python dify/verify_1171/audit_existing.py
```

该命令核对15基线容器、9本次容器身份、两插件包hash、两轮SSE中的真实工具结果和双成功终态、同conversation epoch更新、原生Dify PostgreSQL成功run，以及root SQLite签证/命令/预算。已真实退出0，输出保存为 `read-only-audit.json`。

重放合成Dify两轮的参考命令（会产生新的测试会话，并覆盖对应本地结果文件；独立QA可用自己的客户端和独立证据文件）：

```bash
PYTHONPATH=. .venv/bin/python dify/verify_1171/run_chat.py --turn 1 --label independent-turn-one
PYTHONPATH=. .venv/bin/python dify/verify_1171/run_chat.py --turn 2 --label independent-turn-two
```

已有成对结果请先保留，或直接按独立验收脚本调用。原smoke应用虚拟turn输入为 `verify-turn-ref-1/2`，对应epoch字符串1/2；第二轮传第一轮真实conversation_id。Fake工具票据有六次计数限制，不进行无限重试。

凭据只存在专用私有文件，不写入本报告：`credentials.json`（新实例生成凭据）、`cookies.txt`（新控制台会话）、`app-key.json`（smoke应用）、`root-app-key.json`（root集成应用）、`compose.yml`和两份含环境secret的渲染DSL。上述权限为600。请勿复制到仓库、聊天或公共报告。QA已获得这些精确位置与调用合同，没有发送token明文。

## 精确停止但保留数据

按主审要求当前保留环境，供独立QA；本实现者不提前关闭。QA完成后主审可先核对本报告project与容器ID，再执行：

```bash
docker compose -p xunjia-dify-verify-20261013 \
  -f /tmp/xunjia-dify-verify-20261013/compose.yml ps
docker compose -p xunjia-dify-verify-20261013 \
  -f /tmp/xunjia-dify-verify-20261013/compose.yml stop
```

这只停止本次项目，保留容器、卷、SQLite、日志和归档；不使用 `down -v`、prune或模糊进程停止命令。既有0.15.3 Compose文件不参与该命令。

## 证据哈希与终止边界

```text
f9080ee94501a62393ea7f22dadd6330b88ada2c951cd639b2a310d9e0dafb76  chat-turn-1.json
7047363f83f259d8c9da73f16a54507875887fce9d5621ecdf123fe000d36f77  chat-turn-2.json
9ca9c4217d11ba86eb1e19dc84a79b3a4667edfd1b1c19117eb474128a6b0c8a  root-chain-result.json
07b6083fb3fbc71294abe406cf2943bc84c3581c752a5918eadd992a06966eca  root-budget-result.json
7a2cb3e5a28c2e99c6c2c2c89cbb96c219df6a51dafc80c8d31c4e6b005a11f9  chatflow.rendered.yml
0bfb928639a3c71771252b4192d51e06917ee1cf790518ee7d589a72b4e2d0f2  root-chatflow.rendered.yml
```

验证脚本 `python -m compileall -q dify/verify_1171` 退出0；上述真实导入/发布/3个原生成功run、真实root签证入队及预算负例均已取得证据。本批不声称真实模型质量、真实OceanBase/财汇、真实账号双通道、全仓最终冻结或生产迁移已经完成。独立QA与主审最终同版本验收仍由其各自职责完成。本实现者到此停止新增工作、保留环境并交回。
