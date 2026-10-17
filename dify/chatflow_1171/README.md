# 安琳的 Dify 1.17.1 Chatflow 交付包

> 2026-10-17：本目录新增的 `v22-analysis.yml` / `v22-reply.yml` 是新核心当前入口，见 [V2.2 导入与合同](../negotiation_v22/README.md)。下文描述 schema=1 历史包及当时验证状态，不适用于当前 V2.2 A/B 接线；不要再向新核心导入 `chatflow.template.yml`。

本包是基于固定上游源码核验的导入模板和渲染器，**尚未实际导入 Dify、安装插件、调用模型或发布应用**。现有0.15.3实例、Docker和原应用保持不动。`chatflow.template.yml` 包含未配置占位符，供评审；不得把它当成已配置且通过运行验收的应用。

## 文件

- `chatflow.template.yml`：`kind=app`、`mode=advanced-chat`、DSL `0.7.0`，31节点。主Agent及一次历史补查使用同一策略和提示词；公开LLM没有工具和原生历史。
- `render.py`：按明确部署配置生成Chatflow和公共工具OpenAPI；正式渲染拒绝占位符、latest、缺失版本/包哈希/镜像digest。
- `deployment.example.json`：模型供应商/型号、公共工具Provider ID、精确插件标识与Dify镜像digest的配置入口；不含任何真实凭证。
- `public-tools.openapi.yml`：四工具导入合同，唯一请求字段为 arguments_json；公开工具无需认证凭据或会话票据。
- `arguments-schemas.json`：四工具内部JSON参数schema；必填条件及最终业务校验由 `server.bond_tools` 执行。
- `agent-turn.schema.json`：最终机器提案顶层合同；详细观察、动作、权限和正文审核由服务端继续验证。
- `prompts/`：私有Agent、公开措辞、单次格式修复的版本化提示词。
- `node_code.py`：嵌入Dify Code节点的纯函数。`artifacts.py`生成schema和OpenAPI。
- `source-lock.json`：Dify/策略源码版本、graphon wheel SHA256、逐文件来源哈希及核验范围。

## 已锁定与尚待锁定

已核固定源码：Dify `1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`；官方Agent Strategies `0.0.50`，commit `3b4e77c0be57dfcdde48183243fef411664e8755`；graphon `0.7.0`，wheel SHA256 `6f7608aaaa65607c4935735137ed4b7029b1e6ca80202cbe829af3370cf4404e`。Dify该版本的 `CURRENT_APP_DSL_VERSION` 为 `0.7.0`。

部署镜像的真实registry digest、策略插件实际包hash、模型供应商插件包及模型型号尚未提供。不能从git commit推算插件zip或镜像digest。正式配置必须填写这些值；本轮没有伪造任何hash，也没有下载或安装模型插件。

## 准备导入

在隔离Dify测试环境先安装经过核验且与配置一致的策略/模型插件。应用API Key由服务端保存。配置模型为支持tool calling的模型，采用内网获准的模型出口。

若需要 Dify Tool 插件，可导入四类公开数据 Tool 的 OpenAPI 文件；不需要 Token、Dify 管理员权限或会话票据。接入已经发布的 Chatflow 只需应用 API Key，导入 Tool 是非必需步骤。

复制并填写 `deployment.example.json`，然后执行：

```bash
.venv/bin/python dify/chatflow_1171/render.py \
  --config /secure/deployment.json \
  --output /tmp/xunjia-chatflow.yml \
  --openapi-output /tmp/xunjia-public-tools.openapi.yml
```

路径仅为示例，渲染器不读取默认私有配置、不访问凭据、不调用Dify。在Dify工作区导入渲染后的Chatflow，在应用环境变量 `SERVICE_AUTH` 中设置固定HTTP私有服务凭证，不把其值写回仓库。导入后必须核对工具Provider、插件精确版本、模型能力和所有节点校验状态；本地结构检查不代替Dify实际导入/发布检查。

评审模板可重复生成：

```bash
.venv/bin/python dify/chatflow_1171/artifacts.py
.venv/bin/python dify/chatflow_1171/render.py --review-template \
  --output dify/chatflow_1171/chatflow.template.yml
```

## 后端HTTP合同

私有请求只来自固定HTTP节点，URL由部署配置决定。Authorization为 `Bearer {{#env.SERVICE_AUTH#}}`，可携带后端注入的 `X-Service-Capability`；具体身份来自不透明 `turn_ref` 的服务端映射，永不从模型自报字段决定。

| 路径 | 请求 | 响应 |
|---|---|---|
| `POST /internal/agent/context` | `{turn_ref}` | `{ok:true,context:{turn_id,conversation_epoch,source_message_id,decision_context}}` |
| `POST /internal/agent/history` | `{turn_ref,request}` | `{ok:true,messages:[...]}`，最多50条，服务端持久预算至多一次 |
| `POST /internal/agent/validate` | `{turn_ref,proposal}` | `{ok,needs_public_reply,public_context,validation_ref}` |
| POST /api/agent/tools/{name} | arguments_json 或业务 JSON | 公开只读 ToolResult；不需要 Token 与会话票据 |

公开查询接口不依赖 tool_budget_ref、用户会话或其他 Token；旧版请求中的 tool_budget_ref 会被忽略。会话内部接口 /internal/agent/* 是单独的私有能力，只在现有 Dify Chatflow 确实需要回调时配置其服务认证。

首个validate只审核动作和事实，提案 `public_message/validation_ref` 为null；`public_context`只允许 `allowed_actions/public_facts/peer_message/relationship_context`。公开LLM仅接收这个对象，不读取私有Agent.text、底价、剩余空间、turn_ref、service_capability或私有历史。生成正文后调用第二个validate，服务端核验实际文本并绑定签发validation_ref。拒绝或不需公开回复分支返回 `public_message=null,validation_ref=null`；终审拒绝直接使本轮失败，不能回落到未审核正文。

## Chat客户端

```python
from server.dify_client import DifyChatClient

client = DifyChatClient(base_url, app_key)
result = client.chat(
    query=peer_text,
    user=trusted_dify_user,
    conversation_id=stored_dify_conversation_id,  # 首轮可为None
    inputs={"turn_ref": turn_ref, "context_epoch": str(epoch),
            "schema_version": "1", "service_capability": service_capability},
    response_mode="streaming",
)
# result.proposal仍是候选；调用者复核身份/版本/validation_ref，再走业务事务。
```

`base_url`接受实例根或末尾 `/v1`，只调用 `/v1/chat-messages`，不回落Workflow，不自动重试。`ChatResult`包含完整proposal及conversation/message/task/run标识。已有conversation返回不同ID即拒绝。stream只拼 `message.answer` 与明确 `message_replace`；内部日志不传给对手，不返回半成品。必须收到 `message_end`和`workflow_finished(status=succeeded)`且正常结束；两者顺序可不同。blocking仅接受官方 `event=message` 完成响应，其简化envelope不保证携带run_id，因此该值可为null。

未知顶层字段、重复JSON键、NaN/Infinity/指数溢出、截断、失败/暂停、漂移ID、超限响应均拒绝。异常只公开固定错误码，不带原始私有响应和Key。默认每次I/O超时90秒，总预算180秒在每次读取前后检查；阻塞中的单次网络读仍受I/O超时控制，总预算不是跨所有底层系统调用的硬实时抢占保证。`stop(task_id,user)`只返回Dify请求ACK，不声称业务命令已撤销或真正停止。

## 本地验证与隔离验收清单

本轮完成：客户端开发者18项、包开发者19项、独立SSE验收17项（共54项）；生成的Code节点在本地执行，覆盖正常、格式修复、历史补取、两者组合、首审拒绝和最终文本拒绝，并以临时合成配置实际运行渲染CLI。7个If/Else/LLM/Answer节点另用该版本实际graphon Pydantic模型验证；其他节点对照源码结构检查。没有把本地分支执行器当作Dify引擎。

隔离环境仍须执行：实际导入/发布；同conversation两轮完整inputs更新；插件tool calling和预算覆盖攻击；两轮独立user隔离；真实SSE事件顺序和stop后迟到结果；拒绝分支无正文；工具失败/缺数据；公开措辞无私有泄露；历史额度跨重试/重启不重置；模型质量及语义成交/泄密对抗样本。每次记录Dify镜像digest、插件artifact hash、模型和导入包hash。通过前禁止把模板宣称为已上线或真实模型零违规。

源码依据：[Dify DSL版本](https://github.com/langgenius/dify/blob/8387590ace4a094de812b7847fc6a4c3a27cd52b/api/constants/dsl_version.py)、[Agent参数/模型选择器](https://github.com/langgenius/dify/blob/8387590ace4a094de812b7847fc6a4c3a27cd52b/api/core/workflow/nodes/agent/runtime_support.py)、[Chat响应转换](https://github.com/langgenius/dify/blob/8387590ace4a094de812b7847fc6a4c3a27cd52b/api/core/app/apps/advanced_chat/generate_response_converter.py)、[OpenAPI解析器](https://github.com/langgenius/dify/blob/8387590ace4a094de812b7847fc6a4c3a27cd52b/api/core/tools/utils/parser.py)、[官方Function Calling参数](https://github.com/langgenius/dify-official-plugins/blob/3b4e77c0be57dfcdde48183243fef411664e8755/agent-strategies/cot_agent/strategies/function_calling.yaml)。
