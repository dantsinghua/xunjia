# 行情真值批次交付

安琳，本批在 `/home/anlin/.codex/worktrees/b372/xunjia-agent` 完成行情真值实现与定向验证。分支 `codex/xunjia-chat-remediation-20261013`；基础 HEAD 为 `26acaa0ab99ca19dc12e3ef33d874e1d8f6fb0e4`，包含执行合同规定保留的未提交基线。未提交、未连接真实 MySQL/行情、未改独立验收或旧测试。

## 实现

- `collector/qb_apollo.py`：按类型解析，收益率类型 3 可用原始收益率；净价类型 1 仅接受换算收益率并保留净价；0、未知、缺类型不产生收益率。合法零和负收益率可进入比较。
- `collector/store.py`、`collector/schema_qb_live.sql`：保留双侧报价类型与明确来源的估值日字段；保存不修改调用方原始字典；共享连接由 RLock 串行化并显式 begin/commit/rollback。
- `collector/migrate_quote_truth.sql`：既有 MySQL 表三列的显式一次性迁移。先核对 `SHOW COLUMNS`，仅执行缺列对应语句；新建表已包含字段。启动检查缺列并报操作提示，不静默 ALTER。历史缺类型记录保留 NULL、不猜测口径、不进入收益率比较；历史估值日不回填为今天。
- `collector/qb_gateway.py`：采集与 `imlink.quote_source.load_for_probe` 共用动态配置目录；完整 cookie 名匹配 SID，支持独立 SID；认证失败和 CLI 输出不回显 token 内容。
- `mock/qb_source.py`：全撤价产生明确 null 快照，单侧撤价保留另一侧；内部意向不随市场撤价清空；新鲜度含时间；静态估值/期限/久期每轮刷新，估值日未知为 null；按源日期清除旧日簿并撤销没有新行的旧券；拒绝低版本恢复已撤价格；详情、分页和消费串行共用连接。
- `mock/db.py` 与消费器：支持嵌套保存点，静态、流水、最新值、持久消费 checkpoint 同事务提交。失败恢复缓存和待处理标记；COMMIT 成功但应答失败时，下轮按持久水位恢复，不重复流水。checkpoint 保留报价簿、日界和去重状态，进程重建也不重复消费。写入过程中出现的新意向标记留待下一轮。
- 回放循环在下一轮起点清簿，避免上一轮末值压过重新开始的较低序号。

## 验证证据

基线先实跑独立 12 例：10 失败、2 通过；开发者首批 5 例均失败，随后逐项修复。新增撤价意向、回放第二轮、显式迁移、意向并发标记等用例也先观察到红测。

最终命令：

```text
.venv/bin/python -m pytest tests/acceptance/test_quote_truth.py tests/developer/test_market_safety.py tests/test_quote_channel.py -q
32 passed，退出码 0

.venv/bin/python -m compileall -q collector/qb_apollo.py collector/store.py collector/qb_gateway.py mock/qb_source.py mock/db.py
退出码 0

git diff --check -- collector/qb_apollo.py collector/store.py collector/qb_gateway.py collector/schema_qb_live.sql mock/qb_source.py mock/db.py
退出码 0
```

原始通过输出保存在 `docs/implementation/market-tests.log`。其中独立验收 14 例、开发者 13 例、旧渠道 5 例；测试使用临时 SQLite 与假的源读取/连接。故障覆盖静态第二行失败、流水完成但外层提交前失败、提交成功后响应丢失、持久水位重建；连接验证包含 5 线程写事务及 5 线程详情读取。没有运行全量 pytest，按委派约束由主审与独立 QA 统一执行；本报告不将定向通过视为整体验收完成。

## 主审整合边界与未验证项

1. `mock/mock_server.py:_to_delta`：字段存在时传递 null，缺字段才省略；全撤价目前保留券行并清空有效价格、量、报价方。
2. `mock/mock_server.py:_qb_loop`：动态估值/期限写库后需刷新静态组缓存；清算/分组切换把 delta 路由到旧组与新组并集，旧组订阅端据新字段移出。REST 静态真值已更新，前端静态缓存及估值展示由主审联动验收。
3. `mock/mock_server.py:_qb_mode`、`imlink/quote_probe.py`：主审统一调用 `load_creds` / `merge_sid`，修复握手后立即异常关闭的成功误判，落实配置显式应用生命周期。本批只完成共用加载器。
4. MySQL 迁移与真实源连接没有执行；新旧 schema 只做定义检查与假连接缺列拒绝测试，不能称真实迁移已通过。迁移部署顺序需先显式补列，再启动新读写端。
5. `cnbd_date` 当前原始协议没有已核验字段号，因此未知保持 null；只有源明确给出日期才保存，未臆造协议映射。
6. 本批运行期间没有修改 `mock/mock_server.py`，没有调整独立验收断言。当前停止点为行情实现及本批定向验证完成，等待主审整合与独立全量验收。
