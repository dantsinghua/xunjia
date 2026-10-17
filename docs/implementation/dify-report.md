# 安琳的 Dify Chat 客户端与 Chatflow 包交付

日期：2026-10-13。实施目录：`/home/anlin/.codex/worktrees/b372/xunjia-agent`。本报告记录代码和静态/合成验证批次；后续真实隔离导入单列 `dify-import-report.md`，不混用结果。

## 已完成

- `server/dify_client.py`：`DifyChatClient(...).chat(...) -> ChatResult`，只使用 `/v1/chat-messages`；显式调用者user/conversation/inputs，无Workflow回退、自动重试、会话猜测或真实发送。阻塞支持官方message完成响应；SSE完整缓冲、双成功终态、ID一致性、失败/暂停/截断/限额/超时处理。内部日志不拼接answer。AgentTurn拒绝未知顶层字段、重复JSON键、NaN/Infinity/指数溢出等；结果仍须后端授权核验。`stop`只返回请求ACK。
- `dify/chatflow_1171/`：31节点、32边的经典Agent Chatflow模板，单次格式修复、一次历史补取、私有决策与公开措辞隔离、两次固定HTTP核验、拒绝分支空正文、标准JSON Answer。四工具OpenAPI、业务JSON schema、AgentTurn schema、版本化提示词、来源锁文件、部署配置示例及渲染CLI。
- `tests/developer/test_dify_client_protocol.py`：18项客户端开发者测试。
- `tests/developer/test_dify_client_package.py`：19项包测试，含生成Code实际执行、修复/补历史分支组合、拒绝分支和CLI渲染到临时目录。

完整接口和部署说明见 [`dify/chatflow_1171/README.md`](../../dify/chatflow_1171/README.md)。未改app/business/repository/domain或独立验收测试，未提交代码。

## 上游结构核验及修正

读取Dify `1.17.1`固定commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`、官方策略 `0.0.50`固定commit `3b4e77c0be57dfcdde48183243fef411664e8755`，下载并按Dify锁文件核对graphon `0.7.0` wheel SHA256后仅提取源码，无安装运行依赖。

确认并实施：

1. 当前DSL版本为 `0.7.0`，旧fixture的0.3.1不能冒称当前版本。
2. 经典Agent的model-selector需要 `provider/model/model_type/mode`；普通LLM节点使用 `provider/name/mode/completion_params`。测试先捕获错误字段再修复。
3. 1.17.1 OpenAPI工具解析器没有object映射，嵌套业务对象会落为字符串参数。因此HTTP适配采用 `arguments_json`，内部参数仍由四工具严格验证。
4. 固定HTTP独占context/history/validate；可选工具列表只有公共四工具，工具schema无Authorization或任意身份header。
5. `maximum_iterations`不等于工具调用次数。按主审新增合同增加独立 `tool_budget_ref`，只授予公共只读预算；两个Agent绑定同票据，后端持久累计六次。绑定仍可能被模型覆盖，后端验票是实际权限门。
6. 聚合节点只能引用进入所选分支后才存在的值，不能拿分支之前始终存在的旧proposal绕过修复或历史补查。开发者红测捕获后修正。

31个节点均做本地图结构检查及Code语法编译。7个If/Else/LLM/Answer节点另通过锁版graphon实际Pydantic模型验证；为了避免初始化无关运行依赖，仅加载schema子模块。其他节点以固定源码实体/运行器逐字段核验。**这不等于Dify实例真实导入/发布或全部运行时校验。** 来源逐文件哈希在 `source-lock.json`；临时取证目录 `/tmp/xunjia-dify-1171-evidence`。

## 验证记录

- `.venv/bin/python -m pytest tests/developer/test_dify_client_protocol.py tests/developer/test_dify_client_package.py tests/acceptance/test_dify_chat_stream.py -q`：最新退出0，54 passed、0 failed、0 skipped。独立SSE文件由验收方维护，本实现者未改。
- `.venv/bin/python -m compileall -q server/dify_client.py dify/chatflow_1171`：退出0。
- `git diff --check -- server/dify_client.py dify/chatflow_1171 tests/developer/test_dify_client_protocol.py tests/developer/test_dify_client_package.py`：退出0；新文件尚未跟踪，此命令不代替编译和运行测试。
- 渲染CLI：评审模板生成退出0，31 nodes、32 edges、2 agent节点（同一逻辑Agent的一次补查）、2只返回机器JSON的answer节点。正式渲染缺精确部署pins会拒绝；临时合成pins的CLI测试退出0，图与OpenAPI使用同一固定服务端点，环境secret仍为空。
- 候选全量 `/tmp/dify-client-full-pytest.log`：退出0，448 passed、1 warning，11.35秒。
- **最新全量** `/tmp/dify-client-full-pytest-final.log`：退出2，collection error 1项：`tests/developer/test_transports_io.py`导入尚未落地的 `server.transports` 失败。该文件由主审并行新增，本实现者未修改。已回报主审，不把此前448绿声称成当前冻结验收。
- warning为Starlette的httpx TestClient弃用提示，没有真实模型/API执行。

开发者初始红测包括模块缺失；随后业务红测明确捕获指数溢出未拒绝、聚合引用选到旧proposal、Agent模型选择器字段错误，再修正为绿。缺模块错误未当作完整业务证明。

```text
f1ded3560ea71a5ec307bc65338bb27549e02588b699618e4762ffeaf35c68bf  server/dify_client.py
520235915cab694c7c3caac91d1e887800ba7fb8f591a2da9d17a10a8caf30cf  dify/chatflow_1171/chatflow.template.yml
04c0523d1ca752b97c7655d28f8ba2e4dff0b5f8df04a58f4f56aedabfe93a60  dify/chatflow_1171/public-tools.openapi.yml
```

## 本批证据边界与下一批

本批没有实际导入Dify、调用真实模型、改变现有0.15.3实例或发送业务消息。模板中的部署镜像digest、实际插件artifact hash、模型provider及型号仍待隔离环境取得/选择，渲染器拒绝以占位符假装可发布。

网络I/O超时和读取前后总预算检查已实现；阻塞中的单次网络读仍受单次I/O timeout控制，未宣称硬实时抢占。自由文本事实/权限/保密/不可成交语义由后端和独立对抗验收继续把关，客户端结构合法不构成外发授权。

主审已新授权下一批创建专用project `xunjia-dify-verify-20261013`、专用临时路径/新卷/loopback端口，使用合成LLM/Provider做实际导入与链路验证。先核资源和官方镜像，不复用任何既有数据库/卷、不重启现有容器；实际结果另报，不回写成本批已完成事实。
