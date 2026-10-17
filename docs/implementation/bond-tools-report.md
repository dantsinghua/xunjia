# 安琳的四类债券只读工具实施交付

日期：2026-10-13。实施目录：`/home/anlin/.codex/worktrees/b372/xunjia-agent`。分支：`codex/xunjia-chat-remediation-20261013`。

本批完成四个只读工具和现场数据接口，未连接真实数据库、Dify、外部接口或账号，也未提交代码。HTTP鉴权及挂载由主审在 `server.app` 完成；本模块不拥有发送、成交或修改账户状态的能力。

## 文件与入口

- `server/bond_tools.py`：`BondTools(provider=None).invoke(name, arguments) -> dict`。工具名为 `bond_info`、`similar_bonds`、`issuer_info`、`bond_calc`。每次调用 `arguments.schema_version` 必须为字符串 `"1"`。
- `server/reference_data.py`：只读 `ReferenceDataProvider.snapshot() -> ReferenceSnapshot`；快照提供 `lookup_bonds/search_bonds`、`lookup_issuers/search_issuers`、`load_bond_terms`。默认未配置，不创建数据库连接或后台任务。
- `tests/developer/test_bond_tools_contract.py`：37个开发者测试，合成数据和独立算术预期，无生产网络。

统一结果包含 `status/request_id/snapshot_id/generated_at/as_of/data/missing_fields/warnings/sources/usage`。`usage` 固定为 `analysis_only`。未配置数据返回 `status=unavailable` 和 `warnings[].code=NOT_CONFIGURED`；供应商错误/畸形快照返回 `SOURCE_UNAVAILABLE`，不返回私有异常文本。

## 现场只读数据接口

```python
ReferenceSnapshot(
    snapshot_id="source-version",
    as_of="2026-10-13",
    sources=[{"id": "source", "origin": "编制方", "transport": "授权供应商",
              "dataset_version": "version", "as_of": "2026-10-13"}],
    bonds=[{
        "bond_id": "stable-bond-id", "code": "000001.IB", "name": "合成券",
        "market": "IB", "aliases": [],
        "master": {"issuer_id": fact, "maturity_date": fact},
        "valuation": {"provider": fact, "basis": fact, "yield_pct": fact,
                      "valuation_date": fact},
        "quotes": [],
    }],
    issuers=[{"issuer_id": "stable-issuer-id", "full_name": fact,
              "short_name": fact, "aliases": fact}],
    terms={"stable-bond-id": {"terms_snapshot_id": "terms-v1",
                             "quality": "verified", "source_ref": "source",
                             # 下述计算口径、现金流、结算与应计字段
                             }},
)
```

`fact` 为 `{"value": value, "quality": "verified", "source_ref": "source", "as_of": "2026-10-13", "missing_reason": None}`。缺值使用 null；金额、收益率及价格用十进制字符串，整数期限使用整数。余额需独立 `balance_unit/balance_date`。行业为 `scheme/level/code`；评级为 `agency/scale/value/rating_date`。部署方只读视图、列映射及连接参数由服务端配置，模型不能指定 SQL、URL、表名或用户身份。

每次调用深拷贝同一快照后检索。查券和发行人先检索完整授权字典再分页，歧义使用完整结果集合判断；唯一模糊候选不等于已解析。分页游标绑定快照、原查询及分页大小。精确代码保留前导零及市场后缀。身份歧义不自动合并市场。

所有输出使用字段白名单，包含嵌套字段及元数据。内部意向、授信、原始供应商对象不返回。发行余额、发行人、评级或估值日缺失时不推断、不补今天、不补模拟值。

报价按侧保留来源、原始类型/数值、收益率、基准、量/单位、经纪商、结算、源时间、采集时间、最后确认时间、时间范围及有效状态。最优报价仅采纳已验收来源、明确有效且fresh、明确结算/收益率口径、带时区来源/确认时间的收益率。多个不可比口径返回分组，不合成一个最优价。报价心跳或采集时间不用于刷新报价年龄；部署方负责以已验收的有限阈值及交易日历生成 `freshness`。

相似券按明确到期/行权日及指定 `as_of_date`（缺省用快照日期）计算期限。同/跨发行人按稳定法人ID；行业和评级要求同体系。缺硬筛字段分别计入 `unknown_counts`，排除计入 `excluded_counts`，不静默放宽。余额仅单位/生效日一致才比较；估值仅同编制方、基准及估值日比较；每侧报价仅同结算及收益率基准且合格有效才比较。差值为候选减锚券；收益率另给 `difference_bp`。

## 计算输入合同

所有操作使用 `{"schema_version":"1", "operation": ..., "inputs": {...}}`。输入拒绝未知键、布尔数值、浮点数值、NaN、Infinity和无效日期。

- `yield_gap`：必填 `our_side=BUY|SELL`、`peer_side=bid|ofr`、`limit_yield_pct`、`peer_yield_pct`、`same_basis`。可附 `quote_type`、双方 `yield_basis/peer_yield_basis`、双方 `settlement_date/peer_settlement_date`、`source_ref/authorization_ref`。方向或口径不一致返回 `INCOMPARABLE`；不取绝对值，不用显示舍入值判断目标。
- `clean_dirty`：`clean_price_per_100` 与 `dirty_price_per_100` 二选一；必填 `settlement_date/accrued_interest_per_100/accrued_interest_settlement_date`。应计日期必须一致。
- `current_yield`：必填 `annual_coupon_per_100`、正 `clean_price_per_100`、`coupon_basis=fixed_annual`。结果名称固定为“当期收益率”。
- `price_from_yield`：共同现金流字段加 `yield_pct`。
- `yield_from_price`：共同现金流字段加 `price_type=clean|dirty` 和正 `price_per_100`。净价输入另需可信应计。

共同现金流字段：

```json
{
  "hypothetical": true,
  "bond_type": "zero_coupon",
  "settlement_date": "2025-01-01",
  "cashflows": [{"payment_date": "2027-01-01", "amount_per_100": "100"}],
  "convention": "ACT/365F-compounded-v1",
  "compounding_frequency": 1,
  "yield_basis": "maturity",
  "settlement_cashflow": "exclude",
  "accrued_interest_per_100": "0",
  "accrued_interest_settlement_date": "2025-01-01"
}
```

假设分析须明确 `hypothetical=true`，不能同时声称真实 `bond_id/terms_snapshot_id`。真实计算必须提供两者并由已验收来源加载条款；调用方不能补造快照中缺失的现金流、年息或应计，也不能覆盖已验收条款。缺输入返回 `partial` 和 `missing_inputs`。全价可由完整现金流单独得出；没有应计时净价保持null并标部分可用。

本期仅支持明确 `ACT/365F-compounded-v1`、周期复利及显式普通固定/零息现金流。现金流金额按每百元面值，结算日当天包含/排除必须明确；结算日前现金流排除，无未来现金流拒绝。合法负收益率允许。浮息、含权、永续等返回unsupported；不生成付息计划、不假定支付日历/除息/不规则票息。算法不含税费/交易费用，结果声明该假设。二分求解明确处理无合法根、无法括根、不收敛，不回退票息。

## 验证证据及边界

- 开发者红测：最初模块缺失形成23个明确缺实现错误；实现后23项通过。后续真实业务红测捕获了真实条款可被caller补造、嵌套私有元数据透出、发行人名称Fact投影、余额单位不一致仍可比、合格报价未计算差值、gap口径字段类型/日期未验证等问题，修复后37项通过。没有把缺模块错误声称为业务回归证据。
- `.venv/bin/python -m pytest tests/developer/test_bond_tools_contract.py tests/acceptance/test_bond_tools.py -q`：退出0，61 passed，0 skipped。独立验收方拥有后一个文件，本实现者未修改。
- 独立验收发现 `sources=[None]` 异常快照抛AttributeError；已修复为unavailable，并由同门24项复跑通过。
- `.venv/bin/python -m compileall -q server/bond_tools.py server/reference_data.py`：退出0。
- `git diff --check -- server/bond_tools.py server/reference_data.py tests/developer/test_bond_tools_contract.py`：退出0；上述为新增未跟踪文件，此命令不替代Python编译检查。
- 最终本批全量命令 `timeout 1800 .venv/bin/python -m pytest -q > /tmp/bond-tools-full-pytest-final2.log 2>&1`：退出1，336 passed、1 failed、0 skipped、1 warning。失败为 `tests/test_rfq_send_gate.py::test_page_does_not_mark_inquiring_on_submitted`，旧静态前端检查期望confirm源码块含 `status === 'delivered'`；已报主审和独立验收协调，未修改该门。warning为Starlette对httpx TestClient的弃用提示。本批不声称全仓0红。

本批源码哈希：

```text
3e1ed61e38d6bfff2d61e6fb1ecd36840dc2312a9409439fcaf9f8c95c671aa0  server/bond_tools.py
efe14b803982897610346db7d9557536576a13bae3551884606f77263bc017ac  server/reference_data.py
5c33dbf0b13888e6ad43fe1840c84481f97bd09f4e5a4950d0aec3be982a8f83  tests/developer/test_bond_tools_contract.py
```

停止条件：只读模块及合成数据合同已完成开发者测试和指定独立验收回归。真实OceanBase/财汇字段映射、生产freshness策略、市场计算黄金样本、Dify工具导入及生产鉴权挂载不属于本批已验证事实；现场数据接口保留符合本轮授权。全仓失败和后续整合归主审持续处理。
