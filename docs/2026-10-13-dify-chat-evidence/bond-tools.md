# 安琳的 xunjia 债券聊天工具：字段证据、接口边界与验收口径

日期：2026-10-13。模式：只读需求分析及接口设计；未修改项目、未连接数据库/账号、未读取真实数据文件、未新增抓取或安装依赖。源码存在他人未提交修改，本稿引用本轮读取的工作树。仅此 `/tmp` 证据文件由本任务写入。

结论：以 **4 个业务工具** 支持 Dify 聊天 Agent 的查券、相似券比较、发行人查询和确定性计算。Agent 可以组合工具回答、形成比较结论和解释价格差距；工具返回有来源的数据和计算结果，不能给聊天回答附加真实外发权。现有代码只满足部分行情查询，完整主数据和可靠计算前提仍有缺口。

## 1. 最新裁决与既有边界

- 本轮最新决定覆盖旧“拿实价即停”：**明确实价但 gap > 0，仍可在已确认授权内继续谈价；gap <= 0 或命中停止/接管条件，转人工。** 达边界不等于成交，不自动发送接受成交话术。
- 卖出例子已澄清：对方为 **bid 2.40%**，市场 bid 为 **2.45%**，我方最高可接受收益率 **2.10%**；对方差距 30 BP，市场差距 35 BP。原先“对方卖”歧义已关闭。
- Dify 当前 0.15.3，允许提出升级；本任务不部署、不修改 Dify。以下是业务 schema，不声称已通过 0.15.3 的工具导入兼容验收。
- 最新决定允许决策支持 Agent 调计算器，**不自动放宽旧 D07 的净价谈价转人工边界**。确定性换算可供人工分析；自动净价外发/以换算值代替授权边界，仍需要独立的产品变更和验收。
- 不新增投资推荐、最佳投资券打分、资金/仓位配置、自动成交、收费数据绕过或网页抓取。相似表示明确字段条件下的可比，不代表信用/流动性/投资价值等同。

旧边界证据：`docs/2026-10-13-remediation-design.md:19,128-130`；旧 WF3 的 NL→查询→答案路径：`docs/06-dify-workflow.md:34-40`。本节最新裁决来自主 agent 转达的安琳当次明确答复，优先于旧文档。

## 2. 字段的实际来源与缺口

以下“可见”只表示源码字段链存在，不代表真实样本、供应商语义、使用许可已验收。

| 用户字段 | 当前可见来源 | 能承诺什么 / 仍缺什么 |
|---|---|---|
| 代码、简称、统一券ID | `qb_bond_dict.bond_code/bond_short_name/bond_uni_code`；`mock/qb_source.py:111-118,302-308` | 可复用字典身份；ID 用字符串，代码保留市场后缀与前导零。还没有完整跨市场主键映射 |
| 剩余期限 | `qb_live_best_quote.tenor/remain_days/maturity_date`；`collector/schema_qb_live.sql:13-15` | 可展示源字段及时间；`tenor` 是文本，不能当精确现金流参数。到期/行权期限口径须分开；当前 API 未把 maturity_date 全链传出 |
| 当前余额 | 未见已接入的明确余额字段 | `issue_amount` 不能冒充当前余额；解析器连“发行规模(亿)”也有问号：`collector/qb_apollo.py:79`。余额需财汇/授权主数据的余额值、单位、生效日 |
| 票面利率、票面类型 | `coupon_rate` 在行情行可见；`collector/qb_apollo.py:78` 仍标待核 | 票息值不是计息方式。固定/浮动/零息/贴现、付息频率、起息日、付息计划、赎回额、日计数、含权条款均未可靠接入 |
| 中债估值 | 源解析 `cnbd_yield_ex/mat/net_price/duration`；`collector/qb_apollo.py:96-99` | 字段注释的行权/到期含问号；无法证明哪列为业务要求的“中债推荐估值”。禁止把 mat 名称当已核实语义 |
| 估值日/发布时间 | 没有真实发布日进入当前可用契约 | `mock/qb_source.py:319` 使用本机 today；并且 `375-377` 仅首次写静态，不能作为来源估值日/每日刷新证据 |
| Bid/Ofr 收益率、量、经纪商 | 行情行两侧字段、`qb_broker`；最优算法在 `mock/qb_source.py:175-232` | 收益率口径 best bid=min，best ofr=max 可复用。但必须先修复/隔离类型、有效性、清算口径与 freshness 缺口，不能把整个现有聚合行直接交给 Agent |
| 原始报价类型/净价 | `collector/qb_apollo.py:80-92` 有 type、净价、换算收益率 | 当前换算收益率缺失会回退原始值；净价可能被当收益率。`collector/store.py:23-27` 的入库列还未保留 quote_type。工具层必须保留类型和原始值，缺换算就不比较收益率 |
| 经纪商与真实交易对手 | `broker_id` 及名称 | “报价商”目前只能承诺经纪商来源。经纪商不是已识别交易对手，更不是可发送消息的联系人；缺映射时名称为 null，不推断收件人 |
| 发行人、债项/主体评级、行业、地区、性质 | 实时 `static_row` 全是 None；`mock/qb_source.py:311-314` | 不能把 mock 种子、名称推断或前端示例用于真实回答。`guess_type` 只可标记为推断，不能作为权威类型硬筛 |
| 发行人简称候选 | `qb_bond_dict.com_short_name`；DDL:82 | 当前 `_refresh_dict` 只读取券代码/简称，未接发行人简称。即使取出也不能替代发行人ID、全称、USCC、地区和行业 |

主数据规划证据：`docs/01b-bond-master-fields.md:3-7,23-47` 明确财汇表名/字段映射未落实、券/发行人ID join 未决，中债为单一估值来源且含权用推荐估值。不能把该文件列出的外仓表和“已有真实数据”当成本轮已验收事实。

允许复用的 ibquote 资产仅是模式：`/home/anlin/work/qtrade/ibquote/db/000_baseline.sql:84-104` 提供机构、法人、联行号、地区及别名结构；`src/njcb_deposit_agent/load_institutions.py:1-18,41-64` 是银行 CNAPS 主数据加载及派生简称。**它不是财汇全量债券发行人库**；银行分支法人归并、自动别名和单 alias 主键不能直接解决所有债券发行人重名。源文件已只读核查；未读 `data/refdata`、配置或真实机构数据。

现有可用 API：`/api/bonds`、`/api/bond-static`、`/api/bond/{windcode}`（`mock/mock_server.py:744-834`）。它们不足以提供全市场模糊搜索、发行人检索与现金流计算；`/api/qa` 在 `1215-1235` 仍为规则答复。公共工具只取公共字段白名单，不能把现有返回里的 `instructions/ib/io/ibv/iov` 混入公共行情结果。

## 3. 四个最小工具及可信 schema

所有请求：`schema_version="1"`；对象拒绝未知键；参数化查询；无 SQL/URL/表名参数。身份及数据权限来自服务端会话，不由模型传入任意 user_id。代码、ID、Decimal 都用字符串；收益率 `2.40` 明确表示 **2.40%**，BP 用独立单位，每百元价格用 `price_per_100`，面值量用 `CNY_10000`。日期 ISO 8601；时间带时区。空值不得用 0、AAA、今天或猜测值补齐。

共同结果结构（设计 schema，`data` 由具体工具限定）：

```text
ToolResult<T> = {
  schema_version: "1",
  status: "ok"|"partial"|"ambiguous"|"not_found"|"unsupported"|
          "invalid_input"|"unavailable",
  request_id: string,
  snapshot_id: string|null,
  generated_at: ISO8601WithZone,
  data: T|null,
  missing_fields: string[],
  warnings: {code:string, fields:string[]}[],
  sources: {id:string, origin:string, transport:string,
            dataset_version:string|null, as_of:string|null}[],
  usage: "analysis_only"
}
Fact<T> = {
  value: T|null,
  quality: "verified"|"source_unverified"|"derived"|"user_supplied"|"missing",
  source_ref: string|null,
  as_of: ISODateOrDateTime|null,
  missing_reason: string|null
}
```

`verified` 表示字段映射及来源语义已验收，不表示投资结论可靠；派生项另外保留输入事实引用和算法版本。`origin=中债`、`transport=授权供应商/QB` 可以并存，不把传输平台改写成估值编制方。不回传凭据、原始账户信息或任意供应商 raw_json。

### 3.1 `bond_info`

```text
request = {
  mode: "search"|"get",
  query?: string(1..80),
  bond_id?: string,
  market?: enum(已支持市场),
  include: subset["master","valuation","quotes"],
  limit?: integer(1..20, default 10),
  cursor?: opaque_string
}
```

- search 必须 query，禁止 bond_id；get 必须 bond_id，禁止 query。search 返回 `candidates[]`、`match_kind`、命中字段/片段、是否已唯一解析、`total/has_more/cursor`；服务端全量字典检索后分页，不能只搜当前行情页。
- 唯一解析使用服务器返回的稳定 bond_id；多个市场代码挂在同一发行券时需明示 listing/market，不能自行合并具有不同交易条件的报价。
- get 返回 `identity/master/valuation/quotes/best`。master 包含期限口径、到期日、余额及日期、票息类型/率、issuer_id、券种、行业、两种评级。缺项由 Fact/null 表达，不因为无行情就宣称该券不存在。
- valuation：`provider/basis(recommended|maturity|exercise|unknown)/yield_pct/clean_price/dirty_price/duration/valuation_date/published_at`。仅返回实际有来源的内容。
- QuoteSide 至少含 `side=bid|ofr`、`quote_type=yield|clean_price|dirty_price|spread|unknown`、原始价、可验证的 `yield_pct`、`yield_basis`、`volume`、`broker_id/name`、`firmness`、`settlement_date/settlement_text`、`source_updated_at`、`captured_at`、`last_confirmed_active_at`、`timestamp_scope`、`status/freshness`、`quote_id/version`。买卖侧分开；未知字段保持未知。

### 3.2 `similar_bonds`

```text
request = {
  anchor_bond_id: string,
  as_of_date?: ISODate,
  tenor_basis: "maturity"|"exercise",
  remaining_days: {min:integer>=0, max:integer>=min},
  issuer_scope: "same"|"different"|"any",
  industry?: {scheme:string, level:string, codes:string[]},
  bond_types?: string[],
  rating?: {scope:"bond"|"issuer", agency:string,
            scale:string, allowed_values:string[]},
  compare_fields: bounded_enum_array,
  sort?: ["tenor_distance","code"]|["code"],
  limit?: integer(1..20, default 10),
  cursor?: opaque_string
}
```

返回 `anchor/criteria_applied/candidates/excluded_counts/unknown_counts/relaxation_options`。候选每项带硬筛结果、期限差、可比/不可比的字段理由和所用事实快照；没有结果时不暗自改期限或跨发行人。跨发行人指法人 issuer_id 不同；同集团不是同发行人。

### 3.3 `issuer_info`

```text
request = {
  mode:"search"|"get", query?:string(1..80), issuer_id?:string,
  limit?:integer(1..20, default 10), cursor?:opaque_string
}
```

与查券相同的条件必填及候选确认规则。get 返回稳定 issuer_id、USCC、全称、简称、别名/曾用名与生效区间、注册地区及行政区划代码、企业性质、行业 scheme/level/code、主体评级机构/日期。注册地不等于经营地区，母公司评级不等于子公司评级，债项评级不等于主体评级。无发行人主数据时返回 unavailable/partial；不从券名自动截出公司全称。

### 3.4 `bond_calc`

```text
request = {
  operation: "yield_gap"|"clean_dirty"|"price_from_yield"|
             "yield_from_price"|"current_yield",
  inputs: 对应 operation 的封闭对象（见第6节）
}
```

服务端确定性计算，不让 LLM 心算。结果至少含 `method/version/inputs_used/result/units/assumptions/missing_inputs/usage`；真实债券使用 `bond_id+terms_snapshot_id` 取完整现金流，假设计算必须显式 `hypothetical=true` 且标记 user_supplied。模型不能填一个“看起来合理”的付息频率、估值日或条款版本。

四工具是业务粒度；Dify 0.15.3 的具体 OpenAPI 导入能力需由集成方验证。服务器仍须独立执行所有必填、枚举、条件分支与数值边界校验，不能把工具调用被模型生成视为合法输入。一次查询返回上限、翻页次数与超时均由服务端有限配置，超时返回 unavailable，不回退模拟数据。

## 4. 模糊搜索、歧义及行情 freshness

1. 候选生成顺序：完整市场代码/可靠统一ID精确匹配 → 全名/登记简称/已核别名精确匹配 → 前缀/包含匹配 → 有界错字候选。保留数字、期次、市场与品种区别；不把 24/25、01/02、SCP/MTN 合并。模糊相似度是排序依据，不是已确认概率。
2. 唯一精确代码或唯一已核简称可以只读解析；同名、短代码跨市场、错字匹配即使只剩一个候选，也展示候选请求选券，之后使用稳定 ID。不得因为 top1 高于某任意分数就替用户确认。已确认实体可用于后续“它/这只”；新上下文冲突时重开候选，不能从全局聊天或其他人的会话借实体。
3. 所有搜索返回命中范围和覆盖信息。无行情但主库有券 = master 有数据、quotes 空；尚未接入发行人库 = unavailable，不等同无该主体。候选被截断时 has_more=true，不能把本页唯一误判成全库唯一。
4. 每侧报价保留来源时间及采集时间。provider 只有整行时间时写 `timestamp_scope=broker_row`，不伪造 side 独立更新；仅当供应商协议证明该整行确认两侧当前有效，才可用它确认两侧。心跳/本地轮询成功不能刷新 quote age。
5. 输出 `age_seconds`、`freshness=fresh|aged|stale|unknown`、`market_session=open|closed|unknown` 与 freshness policy 版本。服务端必须配置有限的报价年龄阈值和交易日历；阈值未确认时标 unknown，不能承诺实时。对话模型不能自行把阈值放宽成“今天都算实时”。
6. 盘后显示“截至某时最后报价”；跨交易日不混作当前最优。断源/时间缺失/未来时间/未核时区分别告警；收到晚包按来源版本处理，旧版本不得把新撤价复活。撤回/过期一侧从当前最优剔除，可保留历史事实。
7. best 仅聚合同券、同收益率口径、同结算条件、同来源有效性策略的报价；负收益率只要类型和有效性合法即可，不能照搬当前 `y<=0` 过滤。缺收益率的净价盘仅显示净价，不进入收益率排序。无量可显示价格，但不宣称该量可成交；跨经纪商同价量只标“报价量简单相加，未去重”，不能保证独立可成交量。
8. 估值采用真实 valuation_date/published_at 的独立刷新政策，不能套用报价秒级 TTL。推荐/到期/行权口径未核则比较字段 unavailable。服务端返回一次结果的 snapshot_id、各来源时点；多源非原子快照需明示，不声称同一瞬间。

## 5. 相似券：先硬筛，后透明排序

- 硬筛：用户明确期限区间、期限基准、同/跨发行人、行业体系及层级、券种、评级范围；有效日期一致。锚券从结果排除。用户未说期限区间时先展示可用默认方案供确认，不能偷偷假设“上下1年”。
- 建议首版严格可比集合限同币种、同市场/结算可比范围、同票息结构及含权类别。不同清偿顺位、担保、永续/含权、违约/展期状态要做显式分组或标注；不把相似查询等同“可投名单”或擅自作授信/准入结论。
- 硬筛字段未知不能视作匹配。用户选“同发行人”但 anchor issuer_id 缺失时明确 missing_fields；券的行业缺失不能落入一个“其他”类别凑结果。未知导致排除的数量独立显示，防止空结果被解释成全市场不存在。
- 软排序首版只用可复算的字典序：`abs(candidate.remaining_days-anchor.remaining_days)` 升序，其后稳定代码排序。用户可改为指定字段排序；明示排序不代表优劣。
- 同/跨发行人、行业、券种都已是硬筛时，不再给这些项重复“加分”。不采用 LLM 生成的权重或无验证依据的 0~100 综合相似分，不把收益率高低作为优先推荐。
- 报价/估值差值只对同价格类型、同收益率基准、同结算和可比来源日期计算。跨日期估值仅并列显示并告警；净价和收益率不做直接相减；“中债推荐估值”不能在未披露推荐基准差异时被当统一到期收益率。
- 放宽条件由工具返回显式选项（如行业取消/期限扩宽），保留原结果和当前条件，不自动静默放宽。

## 6. 计算器的可兑现边界与 primary 依据

官方定义区分当期收益率与到期收益率：前者只是年息相对价格，后者须使未来现金流现值与价格相符。不能把票息/价格、票息与剩余期限的简式宣称成精确到期收益率。见[证监会天津监管局《债券初印象（下）》](https://www.csrc.gov.cn/tianjin/c105375/c95d3b22085d64eabba839336531bd261/content.shtml)，2021-08-09。

上游实现的实际输入证明了完整前提：QuantLib `FixedRateBond` 要求面额、付息日程、票息和计息日基准，并包含支付日调整、赎回价、发行日及除息安排；`BondFunctions` 计算基于现金流、结算日、日计数、复利方式和频率，净价从全价扣应计利息。见 [FixedRateBond 上游源码](https://raw.githubusercontent.com/lballabio/QuantLib/master/ql/instruments/bonds/fixedratebond.hpp) 和 [BondFunctions 上游源码](https://raw.githubusercontent.com/lballabio/QuantLib/master/ql/pricingengines/bond/bondfunctions.cpp)。这支持输入合同，不代表已经选择安装 QuantLib，也不证明它的任一默认值等同本市场口径。

CFETS 官方[《债券估值手册》V1.4，2024-01-09](https://www.chinamoney.org.cn/dqs/cm-s-notice-query/fileDownLoad.do?contentId=2784791&mode=save&priority=0)对固定、浮动、含权、违约分别建模，佐证不能拿固定利率简式覆盖所有券。本轮已打开原始 PDF 核对版本与章节，部分后续逐页请求超时；不声称已逐式复核该手册全部模型。估值来源仍按项目决定使用中债，CFETS 文档仅作方法边界参考。

### 输入与结果

| operation | 必需输入 | 可输出的结果与限制 |
|---|---|---|
| yield_gap | our_side、我方边界收益率、对方方向及收益率、同口径确认；来源/授权引用 | 有符号 gap BP、方向冲突/不可比原因；不调用发消息函数 |
| clean_dirty | 同一结算日的 clean 或 dirty（二选一）及可信 accrued_interest_per_100；可由完整条款计算 AI | `dirty=clean+AI`，反向减法。缺 AI 且无条款则 missing_inputs，不按票息×剩余期限补 |
| price_from_yield | settlement_date、完整有效条款/现金流快照、yield_pct、yield_basis、命名并版本化的计算口径 | clean/dirty/accrued，以及所用现金流。须通过本市场黄金样本后才称“按指定口径计算” |
| yield_from_price | 上述同类前提，加明确 price_type 和正价格 | 迭代收益率及求解状态；无合法根或不收敛返回计算失败，不回退票息 |
| current_yield | annual_coupon_per_100 与正 clean_price_per_100，固定/已明确年息口径 | 年息÷净价×100%；结果名称固定“当期收益率”，不可替代 YTM/行权收益率 |

完整条款至少包括：币种、债券类型、起息/到期日期，全部付息/兑付时间与金额或能无歧义生成这些现金流的票息结构、频率、首末不规则期间、还本安排/赎回价；应计利息日计数与收益率日计数、复利方式/频率、结算日、结算日现金流归属、支付日历/调整、除息安排、百元面值归一规则。实际到期/支付日不同需保留两者。应计利息为用户提供时标假设，并校验结算日一致。

“简单”应约束支持范围和输入体验：首版支持已验证的普通固定利率到期还本、零息券；含权/浮息/永续/分期还本/违约展期返回 unsupported，或待未来独立模型验收。不得将推荐估值所隐含的行权情景直接当真实到期现金流。税费/交易费用若未入模型要在结果明确未计入，不能显示为交易净收益。

假设教学例：两年后仅兑付100、恰好两个年复利期间、收益率5%，全价 `100/(1.05)^2=90.7029478458`；没有应计利息时净价相同。这只验证指定假设的算法，不是“任意剩余2年债券”的精确价。净价99.2、同结算日应计利息1.3，全价100.5。

求解验收建议：同口径 golden fixture 的百元价格绝对误差 ≤ 0.0001，收益率误差 ≤ 0.001 BP；price→yield→price 在同一 tolerance 内。该精度是拟议验收门，不是对当前实现的宣称。显示四舍五入与规则比较分开；合法负收益支持，数值域不合法、NaN、Infinity、到期后结算、缺现金流、零价格应明确拒绝。

## 7. Bid/Ofr 与 gap：无歧义函数合同及验收例

同券同结算同收益率口径下，bid 是**报价方买入**，ofr 是**报价方卖出**；用户卖出比较 peer bid，用户买入比较 peer ofr。best bid 为最低收益率，best ofr 为最高收益率。见现有 `mock/qb_source.py:213-215`；不能用前端“买/卖”标签反推本方方向。

定义（所有 y 都是百分数数值，例如 2.40）：

```text
gap_bp(BUY, our_min_yield, peer_OFR) = (our_min_yield - peer_OFR) * 100
gap_bp(SELL, our_max_yield, peer_BID) = (peer_BID - our_max_yield) * 100
```

正数表示对手还差多少 BP 才到我方边界；零或负数表示收益率价格条件已达边界，但仍不授权成交。函数不取 abs，不用四舍五入后的显示值判断。方向不互补、价格类型/收益率基准/结算条件不一致时返回 `incomparable`，不能默算。

| case | 输入 | 期望 |
|---|---|---|
| 已确认卖出例 | SELL，our_max 2.10，peer BID 2.40，market BID 2.45 | peer gap +30 BP；market gap +35 BP；peer 改善5 BP但仍未达边界 |
| 卖出已达边界 | SELL，2.10，peer BID 2.10 / 2.05 | 0 / -5 BP，转人工 |
| 买入尚有差距 | BUY，our_min 2.10，peer OFR 1.95 | +15 BP；若有有效授权且未触停，继续谈价 |
| 买入已达边界 | BUY，2.10，peer OFR 2.10 / 2.15 | 0 / -5 BP，转人工 |
| 最优收益率 | bid [2.45,2.40]，ofr [2.35,2.38]，条件相同 | bid 2.40；ofr 2.38；不把价格大小逻辑套错 |
| 同向话术 | SELL，peer 明确“我卖2.40” | 方向冲突，不能作为 peer bid 计算/继续自动 |
| 合法负收益 | BUY，our_min -0.05，peer OFR -0.10 | +5 BP；不能因负收益当无价 |
| 临界值 | SELL，2.10，peer BID 2.100001 | gap +0.0001 BP；即使显示0，内部仍为正 |
| 缺值/净价 | peer yield null 或 quote_type=clean_price、raw=99.85 | missing/incomparable；99.85 不能作收益率 |

状态验收必须结合授权和条件：实价且 gap>0 只是允许继续评估，不覆盖停止、接管、数量上限、附加条件、归属歧义、报价过期、未决发送和权限检查；均满足才在已确认边界内继续。gap<=0 转人工，不生成接受成交命令。若步幅让下一轮越界，夹到已确认边界或触停，禁止超越；具体状态动作属于主设计规则层。

本轮使用 Python3 Decimal 只验证了卖出30/35 BP、买入15 BP以及假设零息价/净全价算术。没有运行项目测试、Dify调用或真实市场对账，不能据此宣称四工具已可用。

## 8. 必须补齐的验收与未决项

1. 映射验收：授权主数据的 issuer_id/bond_id 关联、余额与单位、行业体系、评级机构/日期、票息结构、真实估值日及“推荐估值”字段逐项有映射和样本证据。未接入项 API 返回 null/missing，不填演示值。
2. 查券验收：同名两券、代码前导零、跨市场短代码、后缀缺失、错一字、相邻期次、无行情券、候选分页都不能静默选错；确认结果绑定稳定ID。
3. Freshness 验收：报价不变但新确认、撤价、旧版本晚到、跨日、断源、bid新/ofr旧、无时间、净价缺换算、负收益分别有测试；最优只用合格样本。
4. 相似券验收：同/跨发行人不是名称包含匹配；行业同体系；硬筛 unknown 单列；排序可复算；无结果不改条件；金额/评级/期限来自同一已声明时间口径。
5. 计算验收：付息当天、闰年、首末不规则期、结算日变化、日计数变化、负收益、无根/不收敛、净全价关系和往返 golden fixture。尚未支持的券必须明确拒绝，不能偷偷降为近似。
6. 权限验收：四工具无外发副作用，不能返回他人意向/会话/账号内容；假设计算不能被自动规则当真实行情；Agent答案中的所有数字可追溯到工具事实或计算输入。
7. 当前尚需产品/数据负责人决定的值：实际供应商表字段及授权覆盖、freshness 有限阈值与交易日历、行业/评级体系、计算 convention profile 和黄金样本来源、未提供期限范围时的交互策略。可先完成这些业务契约；真实数据上线和精确换算承诺止于上述证据未齐之处。

优先结论：先把已存在的行情类型、来源日期和单侧有效性变成可信事实，再开放 Agent 的工具组合；发行人/余额/票面条款要按数据可得性显示缺口。聊天支持分析的能力可以推进，不需要为等待完整财汇数据而伪造全量字段，也不以模型回答代替真实交易授权。
