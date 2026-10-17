# Xunjia V2.2：A 事实解析、B 谈判回复

安琳，当前入口为两个 `advanced-chat` 应用；后端只需要 Dify API 地址及 A/B 两个应用 API key。客户可在现有工作区手工导入，不需要提供 Dify 管理员凭据。公开债券工具无需 Token。

两阶段都使用 `Start → 业务投影 → LLM → Code合同检查 → Answer`，没有 Agent 插件依赖、内部 HTTP 回调或独立 Dify 会话记忆。投影仅省数据库hash、重复thread ID等机械字段，保留全部实际原文、证据、水位、双方报价和授权条件；输出仍用原始完整快照校验。`context_json` 是唯一应用输入，每次用新的 Dify conversation 执行完整版本快照。A 写事实前须经生产证据校验；B 的 `SEND` 仍只是应用候选，最终价格、量、版本及发送许可由程序核验。

## 生成及导入

选择工作区**已经配置**的 provider 和 model；无需改其他应用或新增供应商。

```bash
.venv/bin/python scripts/build_dify0153_dsl.py \
  --version 0.15.3 --provider openai_api_compatible --model MODEL_NAME
.venv/bin/python scripts/build_dify0153_dsl.py \
  --version 1.17.1 --provider EXISTING_PROVIDER --model MODEL_NAME
```

分别导入对应目录的 `v22-analysis.yml`、`v22-reply.yml`，检查模型选择后发布，并取得各应用的 API key。仓库内生成件含明确模型占位符；它们不是客户已配置的应用。1.17.1 需选该工作区现有模型插件的 provider 标识；本包不安装或升级插件。

应用配置：

```text
XUNJIA_DIFY_URL=<实例根或/v1>
XUNJIA_DIFY_ANALYSIS_APP_KEY=<A应用key>
XUNJIA_DIFY_REPLY_APP_KEY=<B应用key>
XUNJIA_AGGREGATION_PROFILE=<成功测量后生成的JSON文件>
XUNJIA_BOND_CONTEXT_MESSAGES=20
```

`XUNJIA_AGENT_TOOL_TOKEN`、Dify 管理员 key 和内部 `SERVICE_AUTH` 均不是此 V2.2 流程的必需配置。`tool_requests` 仅允许 `bond_info`、`similar_bonds`、`issuer_info`、`bond_calc`，由后端真实调用并回传 `tool_results`；每阶段总数有上限，字段仍由真实工具校验。公开 HTTP Tool 合同继续使用 [public-tools.openapi.yml](../chatflow_1171/public-tools.openapi.yml)。

## 输出合同

A 返回 `schema_version="2.2"`、每条原文的 `scopes`、`unresolved`、非空有效报价 `observations`。每个观察包含 `member_id/side/changes/evidence/segments/interpretation`。数值必须可在相应券、相应对手的原文片段中证明；字段证据必须含当前入站，指代同时引用历史锚。FINAL 必须给出上下文语义理由，不能用一个关键词替代判断。

归属纠正使用 `corrections=[{message_id,member_ids,evidence}]`；程序会补取固定快照旧锚、撤销错券贡献。重新提取正确券的事实仍需独立观察及原锚+本轮澄清证据，不能把旧错券报价自动搬过去。

B 返回 `action=SEND|WAIT|HANDOFF`。SEND 的 `proposal.kind` 为 `REPLY/RECONFIRM/ASK_VOLUME/CLARIFY/SOCIAL`。REPLY 必须有结构化 `our_price` 且正文显式收益率一致；其他动作不能夹带价格。HANDOFF/WAIT 的 `proposal` 必须为 null。价量、步长、共享量、跨日及整量人工分配仍受服务端约束。

已知 0.15.3 provider 会把 `reasoning_content` 编码为固定 `<details ...>` 包装；Code 节点仅剥离这个经源码核验的固定包装，再严格解析一个 JSON。任意前后杂文、重复键、NaN、不闭合包装仍拒绝，失败不会伪装成空事实成功。

## 测量

运行 [measure_dify_v22.py](../../scripts/measure_dify_v22.py)；凭据文件、真实响应和运行数据库放仓库外。脚本以合成对手原文调用真实 Dify，实际执行 `Engine.analyze_thread → 事实事务 → Engine.decide_member → 候选事务`；没有 IM sender。普通报价不能全部 WAIT/HANDOFF 通过。成功要求 A 事实全部接受、独立预期值正确、B 通过生产校验并提交。

```bash
.venv/bin/python scripts/measure_dify_v22.py \
  --credentials /secure/dify-private-apps.json \
  --output /secure/measurement --workers 1 --timeout 90
```

凭据文件结构为 `source.base_url` 以及 `apps.analysis.key/apps.reply.key`；可附 source 中 provider/model/source 元数据。入口先冻结源码并从冻结根运行，前后检查源码哈希；应用的已发布workflow ID及graph哈希一并留证。保留36个默认20条上下文代表例，另加6个仅有2条实际历史的新会话高频路径，失败也全部列入报告。只有至少30例语义及事务成功才给出聚合候选，全部case通过才导出可加载的 `aggregation-profile.json`；失败耗时不进入成功均值。`T_reference=min(普通高频最快路径均值,总体均值)`，严格 `0≤W≤C<T_reference/2`。当前算法给出有界候选，未将没有抵达轨迹数据的结果称为 J 优化最优解。

## 历史包

`chatflow.template.yml`、`chatflow-0153-safe.yml` 和旧 `render.py` 是 schema=1 历史回归资料，不能接当前 `server.negotiation`。旧转换器仅通过显式 `--legacy-readonly` 生成；默认生成命令已转为 V2.2 A/B。
