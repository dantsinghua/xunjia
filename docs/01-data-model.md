# 数据模型 —— 现券询价谈价机器人(v1.0 架构)

依据《现券询价谈价机器人需求说明书 v1.0》与前端设计稿 `现券询价机器人.dc.html`。债券基础字段目录见 `01b-bond-master-fields.md`(由财汇探查补全)。

## 0. 分层总览

```
传输层  thread(聊天线程)      = 一个对手方(QT/QQ/企点 的一个会话窗口)
业务层  session(谈价会话)     = 意向号 × 对手方 × 债券代码
        instruction(内部意向)  = 内网筛券后生成的买/卖意向指令
        bond(债券主数据)       = 财汇/中债 基础数据
        market_quote(外部报价) = 各渠道最优 bid/ofr
        strategy_template      = 3~5 套模板(起始/边界/步幅)
        round(谈价轮次)+snapshot(会话快照)+audit(留痕)
```

一个 thread 上可挂多个 session(同对手多券)。消息到达 thread 必须先**路由到 session** 才能进策略引擎。这是全项目最易错处(第 7 章),数据模型据此设计。

---

## 1. bond —— 债券主数据

字段全集见 `01b-bond-master-fields.md`。此处列谈价引擎与前端最少需要的:

| 字段 | 说明 | 来源 |
|---|---|---|
| `windcode` | 市场代码 `102400895.IB` | 财汇/OT |
| `sec_name` | 简称 `24苏科技城MTN001` | 财汇 |
| `residual_maturity` | 剩余期限(年) | 财汇 |
| `mod_duration` | 修正久期(净价↔收益率换算用) | 财汇 |
| `coupon` | 票面利率 | 财汇 |
| `val_cnbd` | **中债估值(到期口径)** —— 唯一估值源 | 中债 |
| `val_cnbd_rec` | **中债推荐估值** —— 含权债直接用,不分行权/到期 | 中债 |
| `issuer` / `issuer_rating` / `implied_rating` | 发行人/主体评级/隐含评级 | 财汇 |
| `bond_type` | 国债/政金/中票/短融/PPN/公司债/企业债/ABS/次级债 | 财汇 |
| `is_guaranteed` / `guarantor` | 是否有担保人 → 决定可投量取哪个口径 | 财汇 |
| 准入类:`is_banned`/`is_default`/`is_suspended` | 禁投/违约/停牌 → 硬拦截 | 准入库 |

> **估值单一来源**(业务确认 2026-09-14):只用中债,不用中证。含权债用推荐估值,不区分行权/到期。中证估值字段取消。

---

## 2. instruction —— 内部意向指令(内网推送)

来源:内网 OT 筛券结果推送报文(第 11 章,字段口径 **P0 待确认**,需带列名导出样例)。

| 字段 | 说明 | 备注 |
|---|---|---|
| `instruction_id` | 意向号 | **session 主键的一部分**;同券同对手可有不同意向(不同产品/组合) |
| `windcode` / `sec_name` | 关联 bond | |
| `direction` | `BID`(买) / `OFR`(卖) | 决定 BP 符号方向 |
| `product` / `portfolio` / `invest_type` | 产品 / 组合 / 投资类型 | 意向绑定 |
| `guar_investable_wan` | 含担保可投量(万) | 口径 **P0 待确认**(样例与不含差 3.6 倍,取错超发) |
| `nonguar_investable_wan` | 不含担保可投量(万) | 同上 |
| `screen_trader` | 本方筛选交易员 | **决定转人工提示推给谁**,复制文本常丢 |
| `counterparty_contact` | 对手方询价联系人 | **缺了发不出去**,OT 有但复制易丢 |
| `biz_date` | 报文日期 | |

外发时**禁止外泄**:含/不含担保可投量、产品代码、组合号、本方交易员姓名、授信额度、持仓(第 10.4)。

---

## 3. session —— 谈价会话(业务核心实体)

主键:`(instruction_id, counterparty_id, windcode)`。挂在某个 `thread_id` 下。

| 字段 | 说明 |
|---|---|
| `session_id` | 主键 |
| `instruction_id` / `counterparty_id` / `windcode` | 三元业务键 |
| `thread_id` | 所属聊天线程(对手方) |
| `direction` | 继承 instruction:BID/OFR |
| `pricing_mode` | `REL`(相对估值) / `ABS`(绝对价) |
| `strategy_template_id` | 选用模板 |
| `start_offset_bp` / `boundary_bp` / `step_bp` | 起始偏离 / 探价边界 / 步幅(BP);绝对价模式改存 `start_px`/`boundary_px` |
| `investable_wan` | 本次询价量(默认 `min(对方报量,本方可投量)` 按 S1 取整) |
| `max_rounds` | 派生 `=(起始−边界)/步幅+1`,不配置 |
| `cur_round` | 当前轮次 |
| `cur_offer` | 我方当前对外报价(收益率或价格) |
| `conceded_step` | 已让到第几档(跨日续谈要用) |
| `state` | 见状态机(§6):`PENDING`/`ROUND_SENT`/`ASK_VOL`/`TO_HUMAN`/`SUSPENDED`/`CLOSED_NOPRICE`/`CLOSED` |
| `state_reason` | 转人工/结束原因(拿到实价 / 追问仍模糊 / 过券 / 加量超投 / 反问 / 归属不明 / 到边界未获实价 …) |
| `got_real_price` | 是否拿到实价(核心产出) |
| `real_price` / `real_vol` | 拿到的实价与量 |
| `asked_vol_once` | 追问量是否已用(每会话最多一次) |
| `attribution_level` | 最近一次归属判定级别 L1~L5 |
| `attribution_pending` | 是否「待确认归属」(L4/L5) |
| `opened_at` / `last_msg_at` | 时间戳(服务端时间为准) |

> **不设单轮超时、不设并发上限**(人工逐个触发天然受控)。长期无回复进 `SUSPENDED`,靠跨日机制或人工收口。

---

## 4. thread —— 聊天线程(传输层)

| 字段 | 说明 |
|---|---|
| `thread_id` | 主键 |
| `counterparty_id` | 对手方 |
| `channel` | `QT` / `QQ` / `QIDIAN`(前期验证 QQ/企点,生产 QT) |
| `peer_handle` | 通道内标识(企点 uin / QQ 号 / QT 联系人),RPA 定位会话用 |
| `is_broker` | 报价方是否经纪人(如样例 `华创证券刘素`)——话术与规则不同 |
| `open_session_ids` | 该线程当前未闭合的 session 列表(L3 归属用) |

---

## 5. message —— 消息(收发原文,留痕核心)

| 字段 | 说明 |
|---|---|
| `msg_id` | 主键;通道原始 id(去重指纹) |
| `thread_id` | 所属线程 |
| `session_id` | 归属到的会话(可空=归属不明/噪声) |
| `dir` | `in`(对方) / `out`(我方机器人) / `human`(人工介入) / `sys` |
| `text` | **完整正文**(600 字截断需先拼接再归属) |
| `intent` | 模型分类结果(§9):实价/虚价/拒绝/还价/非价格条件/反问/挂起/噪声 |
| `extracted` | 结构化抽取 `{基准:估值, 偏离:+5BP, 量:3000}`(比大小交规则引擎) |
| `attribution_level` / `attribution_basis` | 归属级别与依据(**必须留痕**,事后可查挂错哪级) |
| `blocked` | 是否被话术合规闸拦截(要约词/内部字段外泄) |
| `ts` | 服务端时间戳 |
| `revoked` | 撤回标记(按撤回前状态回退) |

---

## 6. market_quote —— 外部报价(各渠道最优)

前端「外部报价」6 列 + 债券详情弹窗按渠道切换。

| 字段 | 说明 |
|---|---|
| `windcode` | 关联券 |
| `channel` | 最优 / QT意向 / 做市商 / X-Bond / 经纪商 / 交易所 |
| `best_bid` / `best_bid_vol` / `best_bid_side` | 最优买价/量/报价方 |
| `best_ofr` / `best_ofr_vol` / `best_ofr_side` | 最优卖价/量/报价方 |
| `firmness` | 实盘 / 可部分执行 / 意向价 |
| `clearing` | 清算速度 +0/+1/+N |
| `updated_at` | 更新时间 |

---

## 7. strategy_template —— 策略模板(运营维护 3~5 套)

**⚠️ 前端设计稿里的策略参数(对手让步空间/最小可接受量/最大价差)是旧口径,v1.0 已删除。以此为准:**

| 字段 | 说明 |
|---|---|
| `template_id` / `name` | 模板名 |
| `start_offset_bp` | 起始偏离(对本方最有利的一端) |
| `boundary_bp` | 探价边界(最大让步,非成交底线) |
| `step_bp` | 步幅 |
| （派生)`max_rounds` | `=(起始−边界)/步幅+1` |

系统级:`S1 询价量取整档位`(默认 1000 万)、`S2 跨日估值变动阈值`(默认 3BP)。
校验:边界须劣于/等于起始;`0<步幅≤(起始−边界)`;绝对价与当日同类券成交中位数比对防手误。

---

## 8. round + snapshot —— 轮次与会话快照(跨日续谈)

每轮谈价后写快照(第 8.4),否则跨日无法比对:

| 字段 | 说明 |
|---|---|
| `session_id` / `round_no` | |
| `snap_val_cnbd` | 当时中债估值 |
| `snap_peer_bid` / `snap_peer_ofr` / `snap_peer_vol` | 当时对方报价与量 |
| `snap_conceded_step` / `snap_offer` | 已让到第几档 / 当时报价 |
| `ts` | 服务端时间 |

跨日「说话前先看一眼」判定(第 8.3):估值变动≤S2 且对方报价在→续谈;>S2→按当日估值重置、让步清零;对方报价撤销→挂起;可投量归零→关闭。**跨日第一条消息必须人工确认后才发。**

---

## 9. handoff_task —— 转人工任务(复用存款项目工单复核台范式)

| 字段 | 说明 |
|---|---|
| `task_id` / `session_id` | |
| `reason` | 拿到实价 / 追问仍模糊 / 非价格条件(过券过桥) / 加量超可投量 / 反问 / 归属不明 / 对方发其他券报价 |
| `priority` | 复用存款项目 P0/P1/P3 时限 |
| `real_price` / `real_vol` | 若因实价 |
| `notify_trader` | 推给哪个交易员(=instruction.screen_trader) |
| `notify_channel` | 走外网 QT 私聊(下行内网延时 1~2h,提示只能外网侧) |
| `resolved` / `operator` / `resolved_at` | |

---

## 10. audit —— 全量留痕(合规,只增不改版本链)

每轮发出原文、对方回复原文、意图分类、**归属判定依据与置信级别**、规则引擎判定、转人工时点与交易员应答。口径照搬存款项目审计表与版本链。归属依据必须可回溯(事后查实价挂错哪级)。

---

## 11. 待确认(P0,阻塞建模/编码)

1. OT 报文字段口径(需带列名导出样例)—— 影响 §2 instruction。
2. 含/不含担保可投量计算口径 —— 影响 §2、询价量。
3. 卖出(OFR)模板参数与话术是否与买入镜像。
4. 多券并发 vs 串行 —— **推荐串行**,L3 即可覆盖归属。
5. 交易员 → QT/QQ 号映射表来源(疑似缺失)—— 影响 thread.peer_handle。
