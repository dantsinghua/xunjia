> 2026-10-13实施更新：mock服务已删除。以下历史设计中的mock路径和旧流程不再是启动入口；当前运行、权限与接口以[部署说明](implementation/deployment.md)、[Chat设计](2026-10-13-dify-chat-agent-design.md)及server实现为准。

# 前端 ↔ 后端接口契约与 Mock 数据规格

依据设计稿 `现券询价机器人.dc.html`(Claude Design 画布,`{{ }}` 占位 + 底部 `renderVals()` 定义全部数据形状)。三屏:**意向询价 / 机器人监控 / 智能问答**。设计语言:南银深红 `#8A2823`,玻璃拟态,Noto 字体。

> ⚠️ 前端策略弹窗的参数(对手让步空间/最小可接受量比例/最大价差)是**旧口径**,v1.0 已删除。Mock 与真实后端按 v1.0 三参:`起始偏离 / 探价边界 / 步幅`。前端这块要改。

真实前端建议用 Vue3 + ant-design-vue 重写(与 bondtrade demo 一致工具链),把 `.dc.html` 的视觉与数据结构落成组件;Mock 后端一律 `{ok, data}` 形状。

---

## 屏 1 · 意向询价(`isQuote`)

### 1.1 行情主表 `GET /api/bonds`
每行字段(对齐 `renderVals().rows` 与 13 个子列):

| 前端键 | 含义 | 后端字段 |
|---|---|---|
| `name` / `code` | 简称 / 市场代码 | `sec_name` / `windcode` |
| `term` | 剩余期限 | `residual_maturity` |
| `val` | 中债推荐估值(前端 `估值/净价` 形如 `1.8491/100.32`) | `val_cnbd_rec` |
| `dur` | 修正久期(净价↔收益率换算) | `mod_duration` |
| `coupon` | 票面 | `coupon` |
| `ibv`/`ib` | 内部意向 bid 量 / bid 底价 | instruction.investable + session |
| `io`/`iov` | 内部意向 ofr 底价 / ofr 量 | 同上 |
| `obs`/`obv`/`ob` | 最优 bid 方 / bid 量 / 最优 bid | market_quote |
| `oo`/`oov`/`oos` | 最优 ofr / ofr 量 / 最优 ofr 方 | market_quote |
| `status` | `待询价`/`询价中`/`转人工` | session.state 聚合 |
| `tags` | `公募`/`绿色债`/`科创债`… | bond 标签 |
| `isNew` | 新到行情高亮 | |
| 详情弹窗补充 | `issuer`/`issuerRating`/`implied`/`industry`/`prov`/`nature`/`gLimit`(含担保额度)/`ngLimit`(非担保额度)/`zz`(中证—v1.0 取消,mock 可留空) | bond + instruction |

筛选维度(`filterGroups`):方案、报价类型(实盘/可部分执行/意向价)、清算(+0/+1/+N)、标签、类型(国债/政金/中票/短融/PPN/公司债/企业债/ABS/次级债)、期限(<1Y…10Y+)。价格区间行:Bid/Ofr/Bid-中债/中债-Ofr/买量/卖量。渠道页签:最优/QT意向/经纪商/做市商。

价格显示三态(`intentMode`):收益率 / 净价 / 估值±BP —— 前端用 `dur` 本地换算,后端给收益率即可。

### 1.2 债券详情弹窗 `GET /api/bond/{windcode}`
- 内部意向指令:买/卖各 投资经理、报bid/ofr、量。
- 当前市场最优:按渠道(最优/做市/X-Bond/QT/经纪商/交易所)切换的 best bid/ofr + 量 + 报价方。
- 最近成交 `trades[]`:时间、渠道、方向(GVN/TKN/TRD)、成交价、量。
- 历史成交价格图 `hist`(YTM 折线:平均成交 + 中债估值,区间 7D/14D/1M/3M/6M)。
- 盘中 `intra`(bid/ofr/估值三线)。
- 右侧对手卡 `modalCps[]`:在聊该券的对手,点击跳监控页定位。

### 1.3 底部操作条(勾选后浮现)
- `开始询价`/`停止询价`(勾选的券)→ `POST /api/rfq/start` `{windcodes, ...}` / `/api/rfq/stop`。
- 底价统一调整:`估值 ± N BP` `应用到已选` → `POST /api/instruction/floor` `{windcodes, bp}`。

### 1.4 询价策略弹窗 `GET/POST /api/strategy`
**按 v1.0 改成:** 起始偏离(BP)、探价边界(BP)、步幅(BP)、严格匹配报量开关;系统级 S1 取整档位、S2 跨日阈值。保存即生效,在谈不受影响。

---

## 屏 2 · 机器人监控(`isMonitor`)

### 2.1 机器人开关与统计 `GET /api/monitor/summary`
- 机器人状态:`正常`/`启动中`/`停止`,`POST /api/bot/toggle`。
- `stats[]`:活跃对话、今日询价、已获报价(有效率)、平均回复、转人工(红线闸拦截次数)。

### 2.2 对手 × 券卡片 `GET /api/monitor/counterparties`
`cps[]` 每个对手:`name`、`bonds[]`(每券:`name`、`quote` 状态、`stage`/`step`(待询价→询价中→转人工 三步进度)、`spread`(价差)、`updated`)。**同一对手多券共用一个 thread,前端标注 ⚠ 同一线程**(对应 v1.0 第 7 章 thread/session)。

### 2.3 聊天流 `GET /api/monitor/chat?cp={id}`
`chatMsgs[]`:`role`(bot/peer/human/sys)、`text`、`tag`(意图标签)、`time`、`bond`(**券锚**,用于按券高亮与归属)。点消息→链路追踪 `trace`(该条从收到→归属→规则判定→动作的全链路,对应留痕)。
- 人工介入:同屏直接打字 `POST /api/monitor/send` `{cp, bond, text}`(经话术合规闸)。
- 一键接管 / 终止:`POST /api/session/{id}/takeover` / `/abandon`。

### 2.4 询价明细 / 导出 `GET /api/monitor/detail` `GET /api/monitor/export`
明细列:市场代码、剩余期限、我方交易方向、中债估值、对方最终报价、对方报量、我方底价、我方报量、距底价价差、我方交易员、投资经理。导出按时间范围。

---

## 屏 3 · 智能问答(`isChat`)

`POST /api/qa` `{question}` → `{answer, chart?}`。前端有快捷 chips(多为**出图**类):
- "今天 23国开10 各渠道最优 bid 画柱状图"、"机器人今日与哪些对手在聊,价差分布"、"某券近一月成交趋势折线图"、"按机构类型统计今日外部报价数量"、"内部 bid 底价高于市场最优 ofr 的债券"。
→ 问答需支持**文字 + 图表**(柱/折/散点)。复用 bondtrade demo 的 markdown 问答,扩展图表返回。

---

## Mock 数据规格(离线可跑,供截图与联调)

Mock 后端(FastAPI,仿 bondtrade demo)内存造数,一律 `{ok, data}`:
- **8~12 只债券**:覆盖国债/政金/中票/短融/PPN/ABS/次级债、公募私募、含担保/不含担保、含权/普通;字段齐全(估值、久期、评级、可投量、最优报价)。
- **4~6 个对手**(含 1 个经纪人 `华创证券刘素`),每个对手挂 1~3 只券的 session(演示同对手多券同线程)。
- **谈价会话**覆盖各状态:待询价 / 询价中(第 N 轮)/ 转人工(拿到实价)/ 挂起 / 跨日待确认。
- **聊天流**:真实话术格式 `bid 102400895.IB 24苏科技城MTN001 3000W 估值+10能出么` → 对方还价 → 机器人让步 → 拿到实价转人工;带券锚、意图标签、归属级别、链路追踪。
- **成交/历史/盘中**曲线用种子随机生成(可复现)。
- **问答**:规则匹配返回文字 + 一个 mock 图表数据。

数据字段口径以 `01-data-model.md` 与 `01b-bond-master-fields.md`(财汇)为准。
