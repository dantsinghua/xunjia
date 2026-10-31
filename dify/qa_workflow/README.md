# 安琳的智能问答独立工作流

本目录新增一个 Dify Workflow，不修改分析 A 或回复 B。链路为：Start → LLM 意图识别 → 严格 JSON 校验 → HTTP 只读行情查询 → 校验查询回执 → End。

## 导入与配置

1. 在 Dify 新建/导入 `workflow.yml`，选择现场已经配置的模型及 provider，再发布为新的 Workflow 应用。DSL 固定为南银现场要求的 `version: 0.6.0`，HTTP 正文和超时字段也与南银 0.6.0 导出格式对齐。模型占位符须替换为现场已配置模型；不得随参考模板升级 DSL 版本。
2. 在“本人账号与收发通道”页的“智能问答工作流”卡片填写 Dify API 地址、新应用 App API Key，以及 **Dify 容器能够访问的询价应用地址**。例如应用对外带 `/xunjia` 前缀时，回调根地址也须包含该前缀。不要填 Dify 容器自己的 localhost，也不要填写不存在的现场地址。
3. 保存并启用。密钥只写入安装目录 `config/qa-workflow.json`（0600），页面不回显；密钥留空表示保留已有密钥。首次配置前须有 `.deployment.json`、`XUNJIA_PREFIX` 或规范的 `XUNJIA_STATE_DIR=<prefix>/state`。
4. 分别问“你好”“查券 26广越14”“26广越14 最新报价”“我想询价 26广越14”。最后一种只提供候选选择及本页策略弹窗，必须自行明确债券、策略、收件人并确认。不要用真实账户发送作为只读联通检查。

也可以预先替换模型元数据生成 DSL：

```sh
python dify/qa_workflow/build.py --provider 已配置的provider标识 --model 已配置的模型名称
```

发布、App Key、Dify 到应用回调可达性仍须在现场确认。本仓库生成 DSL 或通过隔离测试不代表现场已导入、发布或调用真实模型。

## 接口及事实边界

- 应用：`POST /api/qa`，原 `{question}` 合同保留。未启用时继续旧只读功能，并返回明确的未启用提示。
- 配置：`GET/PUT /api/qa/workflow`，均要求应用登录，保存使用 `expected_revision` 防止覆盖旧配置。
- 回调：`POST /api/agent/tools/qa_market/query`。路径尾部 `/query` 避免被已有 A/B 的 `/api/agent/tools/{name}` 通配路由截获。虽然不使用浏览器登录令牌，但必须提供应用生成的短期 HMAC capability。
- 回调正文只有 `action`、`queries`；每次最多10项，单项最多20个返回候选。凭证绑定本人、随机 nonce 和180秒有效期，总查询预算10，不接受客户端 owner、SQL、发送动作或任意工具 URL。
- 工作流的 API 调用为 `/v1/workflows/run`，`response_mode=blocking`。输入为 `question/tool_url/capability`，End 仅返回 `receipt_id`。应用只接受自己本次生成并缓存的查询回执，不采用模型生成的价格或自由答案。
- 时间口径为 Asia/Shanghai 的当天。QB 从原始经纪商明细 `updated_at` 读取，QT 从本人可见明细 `issueTime` 读取；不使用合并快照的最新时间为旧单边报价背书。无有效当日原始明细的候选不返回价格。
- `latest` 在每个方向选择最新有效报价，`best` 在同清算口径按 Bid 最低收益率、Ofr 最高收益率选择，分别显示来源及时间。清算口径沿用对应行情行；未明确时采用最新有效明细的清算口径，无法确认则不比较。真实明细仍可能只有单边、缺量或缺价，不补零，也不代表成交或可成交承诺。
- 工具只查当前行情候选；尚未采集、仅参考资料中存在的债券不会凭空出现在工作流候选中。候选过多须补完整代码。QT 单券明细超过500条则明确失败，不从不完整集合计算最优。
- HTTP 工具用独立白名单剔除本人底价、目标量、策略、其他账号信息。QT 报价遵从当前用户的可见范围，不进入公共广播。
- 同名、多义债券由用户明确选择；模型不自动选择并发送询价。模型和回调工具均没有发送接口。

工作流 API 合同参考 Dify 官方 [1.17.1 Workflow API 控制器](https://github.com/langgenius/dify/blob/1.17.1/api/controllers/service_api/app/workflow.py)，API 版本不能替代现场 DSL 版本要求。HTTP 正文先在 Code 节点显式 `json.dumps` 成字符串，再使用南银 0.6.0 的 `body.type: json` / 字符串 `body.data` 形式发送；超时使用 `max_connect_timeout/max_read_timeout/max_write_timeout`。DSL 的现场导入、模型配置和 HTTP 回调可达性仍须独立验证。
