# 债券基础数据字段目录与来源(财汇探查结果)

## 0. 两个关键结论

1. **权威清单 = 《数据需求清单_供甲方核对_20260914.xlsx》**,141 项 / 5 sheet:债券基础 47(B01–B47)、发行人 38(I01–I38)、估值行情 22(V01–V22)、内部 34(N01–N34)。债券/发行人字段来源基本都是**财汇**,估值类**中债/财汇**,额度类**内部 Oracle**。
2. **⚠️ 本机没有财汇原生表结构**。xlsx 说明页写明:datadict.finchina.com 未能访问,"对应表名/字段名"三列**全空,待甲方回填**。这是建模最大缺口。

## 1. 建 mock 直接用本机真实 DDL(无需等财汇)

| 用途 | 真实表 | 位置 | 说明 |
|---|---|---|---|
| **券基础数据(主)** | `qb_bond_ref` | `bondtrade/db/schema.sql` | QB 羿珩 OnshoreBondFilterV3 真实生产 schema,63 列,PK `bond_uni_code`;**已有 500 条真实数据** `bondtrade/data/onshore_bond_filter_v3.json`;导入器 `db/import_qb_bonds.py` 有完整 camelCase→snake 映射 |
| 券基础(Wind 风格备选) | `bond_description` | 同上 | s_info_windcode / s_info_name / b_info_bond_type / b_info_couponrate / 到期日 / residual_maturity / embedded_option / 评级 / 发行量 |
| **中债估值** | `bond_cnbd_valuation` | 同上 | yield_cnbd / net_cnbd / dirty_cnbd / modidura_cnbd / sprd_cnbd_bp(PK windcode+trade_dt) |
| **QT 报价** | `bond_broker_quote` | 同上 | direction(BID/OFR) / yield_quote / volume_wan / guarantee_vol_wan / firmness / broker / settle_speed / counterparty |
| 成交 | `bond_market_trade` | 同上 | yield_trade / volume_wan / trade_mark(TKN/GVN/TRD) |
| 收益率曲线 / 资金面 | `bond_yield_curve` / `money_market_daily` | 同上 | |
| **对手方/机构库** | `dim_institution` / `dim_inst_alias` / `dim_trader_nickname` | `ibquote/db/000_baseline.sql` | 人行联行号 `pbc_code`、别名(FULL/SHORT/COLLOQUIAL)、群昵称→交易员(带生效区间) |
| **授信额度** | `NEW_QUOTA_CREDIT_OBJ/_DETAIL/_CREDIT` | `ibquote/src/.../sync/oracle_quota.py` | 机构(法人)级授信;`remained_amount/1e8=亿`;**红线:取数失败保留原值不清空** |

> 真实对接财汇时,只需把财汇字段映射到上面这套列名,mock 阶段先用真名占位。

## 2. 字段全集(节选 P0,全量见 xlsx 与探查结果)

**债券基础 P0**:代码(`s_info_windcode`/`bond_uni_code`)、简称、债券类型、发行方式、是否永续/次级/城投、起息/到期日、剩余期限、**是否含权/权利类型/行权日程/行权后剩余期限**(B16–B19,直接决定谈价底线算对算错)、票面/计息方式/付息频率、发行量/当前余额/面额最小单位、**是否有担保/担保人**(决定取含/不含担保可投量)、债项评级/评级机构日期/评级变动历史、隐含评级、上市流通/违约展期/停牌(硬拦截)、发行人代码/名称。

**估值行情 P0**:`yield_cnbd`(到期,谈价底线锚)、`yield_cnbd_exer`(行权)、净价全价、`modidura_cnbd`、估值发布时点(口径)、Bid/Ofr(**收益率 or 净价待确认**)、Bid/Ofr 量、Bid-中债/中债-Ofr 偏离(筛选口径)、报买/卖方、报价类型、清算速度、报价更新时间。

**发行人 P0**:统一社会信用代码、全称、**曾用名**(存款项目吃过一字之差的亏)、简称别名、企业性质、行业、主体评级(当前+**变动历史**)、控股股东/实控人/**所属集团**(集中度按集团合并)、是否城投/平台层级/行政区划、**违约记录/展期非标逾期**。

## 3. 来源划分

- **财汇**:B01–B47、I01–I38 全部,+ V11 信用利差、V12 历史估值序列。核对方式=对 datadict.finchina.com 逐项回"有/没有/部分有/需另购模块"。
- **中债**(从财汇转取或直连,待定):V01–V07、V10 曲线、B38 隐含评级。**业务已定:估值单一来源=中债,含权用推荐估值不分行权/到期。**
- **QT/OT**(已有):V15–V22 报价行情。
- **内部 Oracle**:授信额度(存款已接,复用)。
- **内部南银**:产品/组合/持仓/禁投/投资范围/名单/指令/对手联系人/合规。

## 4. 缺口(P0,mock 用占位,真实对接前必补)

1. **财汇实际表名/字段名全未知**(最大缺口)。
2. **主键对齐**:发行人用 统一社会信用代码 还是 财汇 comUniCode → 决定券库/发行人库怎么 join。三套 ID:财汇发行人代码 ↔ 机构库人行联行号 ↔ QT 机构名。
3. **含/不含担保可投量计算口径**(样例差 3.6 倍,取错超发)——要算法不要结果数。
4. **含权要素**(权利类型/行权日程/行权后剩余期限)——财汇是否有条款明细模块、是否另购。
5. **OT 推送报文字段口径**(`2.35`久期?`-/1.8491`中债/中证?`AA/-`评级?)——需带列名导出样例。
6. **交易员→QT/QQ 号映射**(疑似缺失,发得出去的前提)——需新建,dim_trader_nickname 可作骨架但缺号列。
7. 估值发布时点 / 盘中是否重算(部分已定:跨日 >S2=3BP 重置)。
8. 话术白名单/黑名单表——需新建。
