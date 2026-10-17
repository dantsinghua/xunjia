# R12 生产行情分页整改

安琳，本批实现只修改 `server/market.py`、`server/market_cache.py`、`server/quote_runtime.py`，新增开发者回归 `tests/developer/test_market_pagination_sql.py`。未修改独立 QA、app、业务仓储或其他 agent 文件。

## 已完成

- `MarketService` 从绑定的正式 QB provider 发现 `query` 能力；应用现有构造调用不变。正式路径执行完整 FilterSpec 的参数化 SQL WHERE、同一事务快照的 COUNT 和 keyset LIMIT。返回 50 行时数据库至多物化 51 行，不再读取、投影和排序整库行情。
- 新建市场代码索引 `idx_static_windcode` 与游标索引 `idx_latest_page(flow_id DESC,bond_uni_code DESC)`。二元游标比较使用 SQL 行值比较，可通过索引从游标位置查起，没有 OFFSET。
- 完整覆盖关键词、chips、六类价格范围、通道、经纪商和组筛选。Unicode 小写、有限数、期限文本、标签通过小型 SQLite 标量函数保持已有展示语义；SQL NULL 不满足有效价格区间。没有引入新依赖、额外业务数据库或快照服务。
- 精确 resolve 限 500 个输入代码，通过 `s.windcode IN (?,...)` 查询；空代码列表不查询行情。全选和导出共用完整筛选，明确 10 万行上限；排除市场代码在计数和取行之前处理，超限明确报错。
- 本人状态仍从 `InquiryService.sessions/status_commands` 聚合，本人底价仍取 owner preferences；没有退回全历史 commands。行情 SELECT 不读取缓存的 `ib/io/status` 等私有列。状态筛选将本人代码集合参数化传入 SQL。
- 单 broker 使用 QBSource 已有 RLock 取得当前书簿的公共快照，完整筛选后计数/分页。全书簿明确限制 2 万券，超过限制拒绝查询并报告容量错误，不退化为“全市场最优”或忽略筛选。兼容注入 iterable provider 的路径保留，未增加 pytest 专用分支。
- 修正一个边缘缺陷：未知 `bond-clearing_*` 后缀原先可能与缺失清算值匹配，现统一返回不匹配。未知维度、缺失期限与非法数值均有回归覆盖。

## 验证证据

初始新增回归结果为 **2 失败、7 通过**，失败证明旧路径 COUNT 未下推价格筛选、精确选券未使用代码 IN。

最终定向命令：

```bash
.venv/bin/python -m pytest tests/developer/test_market_pagination_sql.py tests/developer/test_market_service.py tests/developer/test_market_safety.py tests/acceptance/test_market_http_ws.py tests/acceptance/test_quote_truth.py tests/test_quote_channel.py tests/test_quote_last.py tests/test_quote_probe.py tests/test_quote_source.py -q
```

结果：**66 passed，7.66 秒**。其中新增开发者回归 14 项，原有独立市场 HTTP/WS 验收 11 项原样通过。现有 FastAPI/TestClient 的 StarletteDeprecationWarning 1 条仍存在，与本改动无关。

可复核性能和正确性证据：

- 2,051 券、50 行页，遍历全部页面无漏重；把全部 flow_id 改为相同值后，字符串 bondUniCode 次排序仍无漏重。
- SQLite trace 断言：价格条件同时进入 COUNT 与结果 SELECT，取行 SELECT 只执行一次且带 `LIMIT 51`。
- `EXPLAIN QUERY PLAN` 断言：带游标查询使用 `SEARCH l USING INDEX idx_latest_page`，不生成排序临时 B-tree；精确选券使用 `idx_static_windcode`。
- 全部 chips 选项、价格上下界、Unicode 关键词、多维组 OR/AND、无值/非法值与注入 iterable 参考路径逐项等价；未知清算组修正两条路径同步。
- 账号 A 的 unknown 状态与底价不进入账号 B；公共缓存伪造 `已成交/ib` 无效。
- 103 券 broker 书簿先筛选出 51 券，再分 25/26 两页无漏重；容量超限明确拒绝。
- `git diff --check` 与四个 Python 文件的 `py_compile` 通过。

## 限制与交接

- COUNT 仍须按筛选扫描或使用适用索引；关键词、标签和文本期限等复杂条件不宣称常数时间。改动消除了每页 Python 全量对象构造及全量排序，没有把所有查询描述为 O(1)。
- broker 当前是有界内存快照路径，不宣称已改成 SQL 书簿。大于 2 万券须后续实现索引书簿并重新验收容量。
- 单次请求的 COUNT 与页面来自同一 SQLite 事务快照；连续翻页期间真实市场仍可更新，不提供跨 HTTP 请求冻结市场的语义。无漏重回归在固定数据集上成立。
- 使用离线合成行情，没有连接真实 QB、企业数据库、交易账号；没有生产容量压测或全工程终验声明。
- 主 agent 仍负责把监控板面的全市场 rows 调用改为按在谈代码 resolve，以及总体 R12 历史状态增长整改、最终独立验收和全量测试。

本批完成后停止写入，等待独立复核。
