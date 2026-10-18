# Xunjia V2.2：A 事实解析、B 谈判回复

安琳，当前入口为两个 `advanced-chat` 应用；后端只需要 Dify API 地址及 A/B 两个应用 API key。客户可在现有工作区手工导入，不需要提供 Dify 管理员凭据。公开债券工具无需 Token。

两阶段都使用 `Start → 业务投影 → LLM → Code合同检查 → Answer`，没有 Agent 插件依赖、内部 HTTP 回调或独立 Dify 会话记忆。投影省数据库hash、重复thread ID等机械字段，并前置当前成员已有报价及引用索引，保留全部实际原文、证据、水位、双方报价和授权条件；输出仍用原始完整快照校验。`context_json` 是唯一应用输入，每次用新的 Dify conversation 执行完整版本快照。A 写事实前须经生产证据校验；B 的 `SEND` 仍只是应用候选，最终价格、量、版本及发送许可由程序核验。

当前槽位支持同轮现金报价的 `slots.settlement`：选择本人当前入站中的 DATE/SETTLEMENT 候选，和净价或全价一起提交；ISO、中文和斜杠日期确定性归一，T+1 必须使用实际发布的结算日历。仅补结算另用 `meaning={intent:"SETTLEMENT",reason:...}`、`slots={settlement:候选ID}`，不重抄价格、数量或确认状态。程序只对同日、真实SENT问句绑定的当前现金报价版本补条件；提交时重查回执、版本、来源和时序，原价量及confirmed_day不变，FINAL降UNKNOWN。无日历或无本人结算证据时保留现金并继续澄清，不猜换算。数字和全角T结算词使用保持字符位置的一对一映射；否定、问句、评级等日期不作为结算确认。B完整价格序号范围不因提示减负缩减；缺步长的旧快照无可选序号。

A 对单成员、单条入站的“完整直接收益率报价＋两个显式收益率的假设计算”使用较短的普通提示：分析前段报盘，计算咨询留给 B，不混入缺报价引用的请求模板。`direct_priced_calculation` 只选择提示，不产生事实；所有原文和候选照常提供，原输出校验不变。缺计算操作数、另一报价引用不明、多券、多条、否定/转述/问句报价等均回通用联合核对；已知 `reference_gaps` 优先。B 普通发送不再重复生成所选价或内部边界的数值理由，保留 `trader_view={}`，实际价格以所选完整网格及最终校验结果为准，原始输出仍完整审计。

完整的“能/可以/能否＋价＋方向＋量”询问会在 A 联合核对前获得问句提示；事实门仍读取真实券范围原文，拒绝把它提交为 QUOTE，不按模型裁剪后的片段忽略问句，也不把陈述后附带的问题一律当询价。公开 `yield_gap` 的两个操作数都必须来自当前对手可见原文、带实际回执的我方已发价、已验证的该对手报价或可信公共工具收益率字段。计算成功、`hypothetical` 或自报 `source_ref` 本身不能证明可公开；私有边界、未发开盘计划和其他对手汇总不提供这种来源。公开值偶合内部边界仍按公开来源允许。工具本身保持免 Token，既有算术精度和完整报价域均不变。

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

当前默认策略为 `latest_quote_confirmation_v1`：只使用已给的20条同券同对手双向上下文；未找到被指代报价时，A输出 `confirmation_requests`，该成员不能同时写观察事实。B用系统最新本人记录询问确认，已撤记录不复活，过日记录明确标为上次记录。全员均存在明确超窗引用时，A切换为单独的待确认输出提示；有其他有效成员时继续逐成员分析。运行时不上翻历史，不增加历史检索模型轮。

旧 `explicit_anchors_v0` 仅供版本化历史回归：A/B可返回 `history_requests=[{member_id,message_ids}]`，程序核对owner/thread/member与固定水位，回填 `history_results`；每请求1–30个唯一ID、每member每阶段最多30个额外锚，与公开工具共享6次预算。该旧流程不再是默认产品行为，历史案例与结果仍保留。

## 输出合同

A 返回 `schema_version="2.2"`、每条原文的 `scopes` 和 `observations`；仅存在券归属歧义时输出 `unresolved` 映射，没有歧义则省略。显式错误类型仍严格拒绝，不把数组偷偷转成对象。当前模型逐条填写 `member_id/source_ids/side/slots/meaning`：`slots`只选择程序已抽取、批量分类的候选ID及必要类型，`meaning`一次表达intent/validity/negotiation_space/reason；Code从原文组装价量和证据，不让模型重抄或计算数字。当前候选先展示，既有可见的历史经济数值候选及全部原文保留并标识来源；多券仍须明确成员，不能猜测缺失ID。旧fields、中文basis和canonical合同作为兼容路径保留，不修正旧错误值。

当前V2 Engine显式启用 `include_known_outbound_scopes`：只有实际SENT、非MANUAL、同thread/native/text、命令自身body哈希一致且已有匹配成员关联的出站消息，才进入 `known_outbound_scopes`。原生事件可先于发送回执入库，事件哈希与命令body哈希描述不同对象，均保持原样。模型可省略可信键，由Code补齐；缺入站或未知出站关联、显式改写可信映射仍拒绝。该映射只表示固定原文的券归属，不授予发送/结算/跨日确认权限；晚来的UNKNOWN不能阻断合法新报价事实，经济确认仍核当前回执。旧直接Store调用默认维持原合同。候选另带保守的明确券码分句索引，完整原文/历史/候选不裁剪；索引不是主体或实报认定，未标券和跨句引用仍由联合语义处理。

引用“按你某价，我出/收某量”须关联实际所指价格，不能用无关旧对手价替代；程序只对可确定的显式矛盾拒绝，不替模型换价。跨日slot不自行确认日期：在事实提交事务内重新核对同成员当前SENT问句及其后的当日入站，符合条件才派生确认日；回执变UNKNOWN时仍保存合法新价量，保持未确认、不计满足量。模型显式非法确认日期仍由原严格校验拒绝，旧Store调用默认不启用自动推导。

归属纠正使用 `corrections=[{message_id,member_ids,evidence}]`；程序会补取固定快照旧锚、撤销错券贡献。重新提取正确券的事实仍需独立观察及原锚+本轮澄清证据，不能把旧错券报价自动搬过去。

程序只对可定位的价格原文证据拒绝提供一次 `validation_feedback.mode="EVIDENCE_ONLY"`。模型可返回窄 `evidence_repairs=[{index,add_evidence:{price:[IDs]},add_segments:[{message_id,text}]}]`；仅可信反馈许可的目标可追加实际引用/片段，至少有一项新增，不删旧引用、不补猜证据、不加工具/history。Code复制可信original_response的其余字段，非目标失败哨兵也原样保留。旧完整修复响应仍走原严格通路，不能抽取其中合法部分隐藏越权改动。重建后的完整canonical继续受后端原冻结字段、原文证据、阶段总预算与租约门约束。

B当前普通发送输出 `reply_plan`，模型选合法价位、资料答案ID、问题和语气，程序生成 `action=SEND` 与 `proposal`，不输出自由经济正文。REPLY用 `quote_index` 整数选择 `quote_grid` 的0..N完整格点，代码精确换算价位；大区间保持常量大小的等价索引范围，不删中间点。索引与旧 `quote_value` 不可混写；旧数值接口继续严格校验。索引计划同样绑定member/side/basis及原价域，旧计划不能在新上下文重解释。资料数字由实际Tool来源验证，经济句由事实和计划渲染。旧裸SEND/proposal仅在历史兼容策略或原修复合同中接受。WAIT/HANDOFF仍返回对应action且proposal为null。价量、步长、共享量、跨日及整量人工分配继续由服务端约束。

对原 SEND/REPLY 候选仅因收益率正文不匹配或公开方向不一致而拒绝的情况，程序可给一次 `validation_feedback.mode="TEXT_ONLY"`。B可仅返回 `text_repair:{text}`，由Code复制可信原响应的非text字段；动作、kind、结构化价格、内部说明等冻结，不增加工具/历史请求。所有patch都须携带schema_version、匹配当前stage和唯一反馈尝试；未知键或混入完整响应均拒绝。修复沿用原阶段剩余预算，输出仍过原价格、方向、整量和提交门；越界价格、超步长、最终价不利让价等经济错误不能借此重做决策。首次拒绝、重建候选及结果保存在同一reply job持久审计；重建canonical不是模型逐字返回，QA另保留实际LLM patch与workflow/轮次关联。

程序提供 `quote_constraints`，给出已发价或授权opening、step、边界和最终价保价交集后的0.01收益率网格闭区间。只有模型已根据业务状态选择REPLY时才据此选价；AVAILABLE不等于发送许可，EMPTY/UNAVAILABLE也不预选WAIT/HANDOFF，仍须处理归属、跨日、净价结算等原决策优先级。模型不能用区间字段绕过原后端验证，也不会由程序代填报价。

B输入前置的 `current_counterparty_quote` 原样复制本成员相关一侧（BUY看ofr、SELL看bid）。对手现金价缺结算日或可验证换算结果时，本询价流程先由B生成CLARIFY；我方可报价范围的AVAILABLE不能当作对手现金已换算。此呈现不会改写事实或新增核心的无条件现金禁报价门。

已知 0.15.3 provider 会把 `reasoning_content` 编码为固定 `<details ...>` 包装；Code 节点仅剥离这个经源码核验的固定包装，再严格解析一个 JSON。任意前后杂文、重复键、NaN、不闭合包装仍拒绝，失败不会伪装成空事实成功。

## 测量

运行 [measure_dify_v22.py](../../scripts/measure_dify_v22.py)；凭据文件、真实响应和运行数据库放仓库外。脚本以合成对手原文调用真实 Dify，实际执行 `Engine.analyze_thread → 事实事务 → Engine.decide_member → 候选事务`；没有 IM sender。普通报价不能全部 WAIT/HANDOFF 通过。成功要求 A 事实全部接受、独立预期值正确、B 通过生产校验并提交。

```bash
.venv/bin/python scripts/measure_dify_v22.py \
  --credentials /secure/dify-private-apps.json \
  --output /secure/measurement --workers 3 --timeout 300 \
  --stage-budget-seconds 540
```

凭据文件结构为 `source.base_url` 以及 `apps.analysis.key/apps.reply.key`；可附 source 中 provider/model/source 元数据。入口先冻结源码并从冻结根运行，前后检查源码哈希；应用的已发布workflow ID及graph哈希一并留证。保留36个默认20条上下文代表例、6个仅有2条实际历史的新会话高频路径，增加1个超20条历史补取和4个实际公开工具案例，共47例，失败也全部列入报告。工具数据明确来自合成公开资料快照，不能作为客户资料源接通证明。此前一个真实工作流207.062秒才完成，175秒客户端已超时；下一完整轮据此采用HTTP240秒、整阶段540秒、租约570秒。产品默认仍为HTTP90秒、阶段150秒；部署若使用该测量证据需显式配置相同预算，HTTP硬上限300秒且受剩余阶段预算限制。

随后240秒轮的历史场景B在同一真实workflow耗277.133秒成功，客户端先超时；下一轮据此采用HTTP300秒，stage540/lease570保持，不再无限加长。QA夹具以合成交易日中午为时钟基准、按真实monotonic秒数推进，避免跨午夜重放的日期假失败。模型耗时与报告UTC仍是真实时间；恢复夹具持久化同一时钟基准，重开进程不跳过生产退避。

`overall/paths`展示所有最终成功案例的完整端到端实耗，包括有界修复成本。聚合校准另取清洁样本：最终成功、明确没有初次A/B拒绝或任何修复、每次HTTP成功、每个工具结果ok，并且A/B逐次证据与各自repair审计完整。B审计从持久reply job记录读取，正文重生另标text_repair，首次失败与最终成功分列。修复/失败等待不能抬高窗口；输入自称eligible不能绕过程序推导。只有至少30个清洁样本且有清洁普通路径，并且原全部case最终通过，才导出可加载的 `aggregation-profile.json`。`T_reference=min(清洁普通高频最快路径均值,清洁总体均值)`，严格 `0≤W≤C<T_reference/2`。当前算法给出有界候选，未将没有抵达轨迹数据的结果称为 J 优化最优解。

[measure_dify_recovery.py](../../scripts/measure_dify_recovery.py)另行收集真实HTTP超时故障注入及恢复证据：A、B分别保留同一SQLite、真实生产退避、不同进程重开、逐HTTP尝试和事实/候选版本。首试1秒超时属于显式注入，不能称为自然超时；恢复结果须独立验收，全部排除标定样本。脚本不会启动IM发送器。

[measure_dify_settlement.py](../../scripts/measure_dify_settlement.py)单独验收现金报价→B问结算→本地合成SENT→只补日期→B正常候选的真实A/B App API往返。保留每次HTTP及专用SQLite，不构造IM sender，不称作原生发送验收，也不纳聚合标定。拒绝Python优化模式和复用已有证据目录；上游错误保留原类型。每批结果独立，旧失败不补签。

真实测量须使用适用于该用途的模型服务来源。当前本地0.15.3内置兼容provider会过滤未声明的思考参数，仅在DSL写入enable_thinking或thinking_budget不会生效；其max_tokens也不能约束Qwen3.8的思考长度。相关边界见[阿里云参数说明](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)与[Dify接入说明](https://help.aliyun.com/zh/model-studio/dify)。Token Plan来源已停止追加调用，历史运行资料保留，不据旧失败轮发布生产profile。

2026-10-18按安琳指定的新来源，仅两只专用QA应用选择现有 `dify-test-proxy` 模型，经本任务独立启动的既有南银shim入口调用上游普通Workflow API。共享provider和南银源码未变。该桥单worker且同步等待上游，正式测量使用1并发；SSE只是完整结果一次输出，仍计完整A+B实耗。桥返回的token usage为占位，物理模型不从旧DSL猜测，记录代理别名与实际上游workflow。客户后端仍只需普通A/B App API keys，不需要Dify管理员权限。

后续专用QA已使用独立映射到qwen-max的shim别名；实际模型、上游Workflow和两应用发布图以各批source记录为准，不沿用早期代理名称推断身份。当前测量采用HTTP90秒、阶段150秒、租约180秒；历史长超时轮保留，不代表当前默认参数或当前成功保证。

## 历史包

`chatflow.template.yml`、`chatflow-0153-safe.yml` 和旧 `render.py` 是 schema=1 历史回归资料，不能接当前 `server.negotiation`。旧转换器仅通过显式 `--legacy-readonly` 生成；默认生成命令已转为 V2.2 A/B。
