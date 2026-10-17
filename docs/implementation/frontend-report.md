# 前端实施报告（分批证据）

安琳，当前前端实现以文末“第二批补记”为准；以下第一批记录保留为历史验证快照，不能合并成同一次全量验收。

## 第一批历史快照

安琳，本批在 `/home/anlin/.codex/worktrees/b372/xunjia-agent` 实施，保留四页布局，无提交、推送或真实外发。原始脏工作树基线以 `docs/implementation/baseline.json` 为准；不能用相对旧 HEAD 的总 diff 当成本 agent 的全部改动。

验证日期：2026-10-13。最终前端源码（`web/src/**/*.ts,*.vue`）与两份 package 文件按路径排序、以路径/NUL/内容/NUL拼接的 SHA256：`1acfb4024b5f703b07506ff9ee14d2b2c976a544edc8e3c5444b090c7743f6c3`。

## 本批实现

- `web/src/pages/Link.vue`：修复原有 8 个 TypeScript 构建错误；表单刷新不抹掉通道测试诊断及候选账号（R17/R40）。
- `web/src/api.ts`、`web/src/lib/http.ts`：所有业务 HTTP 使用独立 `xj_auth_token`，发送 Bearer 与 X-Auth-Token；HTTP/业务失败保持失败，不回退成功示例；策略 templates/system 与页面三参数双向转换，轮次 ceil 派生（R15/R38）。
- `web/src/App.vue`：从门户读取 userName/nickName/ts/sign，调用 SSO，清 URL 认证字段，以 `/api/auth/me` 验证本人后挂载页面；身份失效卸载页面/行情客户端，清通道私有状态；显示真实 display_name 与未绑定提示。
- `web/src/lib/quoteClient.ts,quoteCore.ts,quoteTypes.ts`：SharedWorker 按用户命名；INIT冻结 userScope/token，未初始化及跨用户端口不能访问缓存或订阅；静态请求带认证头；WS首帧 auth、收到 `{op:'auth',ok:true}` 才订阅及标 OPEN。token 不进入 URL、stat 或日志。
- `web/src/pages/Quote.vue`：服务端每页50行，保留前后游标及跨页选择，取消2000行头裁剪；完整 FilterSpec 共用于列表与全选，筛选/账号变更清选择；`/api/bonds/resolve` 解析完整选择供弹窗/底价操作，不能把当前页当全集。所有读取校验范围及请求代次，单经纪商每3秒REST刷新（R12/R30/R35/R36）。
- `Quote.vue` 与 `api.ts`：本人询价状态/底价从状态及 floor GET收敛，删除长期用户操作覆盖态；公共增量不接受私有底价/状态，null清价、fm/cl/报价时间完整更新；绝对收益空白拒绝、负值保留（R19/R29）。
- `web/src/pages/Monitor.vue`：只监听 peer ID，防止对象刷新请求环；失败保留最后成功内容并提示；聚焦全部使用券code；人工发送捕获账号/对手/券/编辑版本，稳定request_id重试，按cmd_id轮询未决回执，切人后不写错对话/清新输入；删除extraMsgs第二份聊天历史（R13/R37/R38/R39）。
- `web/src/components/BondModal.vue`、API归一：缺成交/曲线/审计证据不造数；历史曲线与盘中曲线独立渲染。未提供used时显示“让价空间使用情况未知”，不从会话阶段推40%/70%等百分比（R14）。
- `web/src/components/RfqAssignModal.vue`、`rfqAssign.ts`：实际confirm入口按每收件人合并的编辑原文校验1500个Unicode字符；同次重试沿用request_id。
- `web/src/components/ImLinkBar.vue,OrgTree.vue`、`imLink.ts`：5秒健康刷新，账号变化隔离通讯录；组织架构空态/读取通知帮助器已接真实组件，保留原先独立测试行为。
- `web/src/pages/Chat.vue`：新增 DOMPurify 清洗 Markdown HTML，危险链接/事件属性不能进入执行；移除假历史列表，错误明确上屏。保存筛选方案未接入时禁用并说明；导出尚未接入时不再谎称“没有明细”。

## 验证证据

全部命令在上述唯一实施目录运行，未连接真实STS、OSS、Dify、企点或行情。

| 命令 | 结果 |
|---|---|
| `npm --prefix web test` | 退出0；旧TS测试8通过；新增开发者测试22通过；0失败/跳过 |
| `npm --prefix web run build` | 退出0；vue-tsc无错误；Vite 3231模块构建成功（8.68s） |
| `.venv/bin/python -m pytest -q web/tests/acceptance/test_browser_entrypoints.py tests/test_org_tree_spec.py` | 退出0；19通过（浏览器14+组织树5），0失败/跳过；13.50s |
| `npm --prefix web audit --json` | 退出0；0漏洞 |
| `git diff --check -- web` | 退出0 |

日志：`/tmp/xj-web-tests-final.log`、`/tmp/xj-web-build-final.log`、`/tmp/xj-web-browser-final.log`。开发者测试先复现API假成功/模拟数据、请求环、空yield、delta缺失、分页裁剪、旧响应污染、超长发送、诊断清除、worker认证与未初始化端口等红测，再实现修复。独立验收新增“真实历史曲线不因盘中缺值消失”和“缺used不造百分比”曾红，修产品后原验收用例转绿，未改验收门。

开发者组件测试编译真实SFC的setup、执行Vue响应性与实际方法，未挂载DOM，因此出现预期的Vue生命周期“无active component instance”告警；不以这些测试冒充浏览器挂载证明。真实浏览器由独立验收维护，用专用Vite和全量合成API拦截；本批也重跑了原样的独立用例。Python仍有既有Starlette/httpx弃用警告。

依赖仅新增已授权的 DOMPurify 3.4.17（及其可选类型包），并按主审授权将既有 `source-map-js` 1.2.1锁至1.2.2。GitHub已审阅公告列明1.2.2为修复版本：[GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q)。未升级其他依赖。

## 接口与下一批依赖

1. 全量筛选合同为 `filter={keyword,chips,prices,channel,broker,groups}`；全选带 `selection.filter/except`。GET `/api/bonds` 保留 before/nextBefore/beforeUni/nextUni；POST `/api/bonds/resolve {selection}` 返回原始行；GET `/api/im/inquiry/floor` 返回 `{items:[{code,kind,value,revision}]}`。客户端已接合同；真实生产入口整合与完整FilterSpec数据库验证归主审/独立验收。现有游标不是不可变结果集快照，行情变化下严格无跳行遍历仍需服务端结果ID快照。
2. **自动询价新预览/确认仍待下一批**：主审已给 `/api/im/rfq/preview` 及 `/api/im/rfq/send {batch_id,expected_version,request_id}`，需新增显式本人BUY/SELL、量上限、开价/边界/步幅确认。按主审要求本批先收尾认证和修复，当前RfqAssignModal仍是旧items发送合同，不能宣称新批次授权链完成；生产后端应拒绝未预览旧发送。
3. 真正xlsx导出、完整Chat事实板/工具/SSE等下一批未交付；当前导出明确提示尚未接入，不造下载成功。
4. 未验证门户真实签名、生产身份绑定、真实行情WS、真实OSS/邮件发送、生产数据库及整套后端全量回归；这些不能由本批合成前端绿测替代。全局goal及“0回归0红”最终验收由主审继续执行。

本批停止条件：指定前端修复与新增行情认证已落代码；开发者测试、独立入口测试与构建有新证据；后续契约集成和真实环境边界已明确记录。没有修改独立验收文件、后端或原测试门。


## 第二批补记：预览授权、导出、交易板与静态增量

实施与验证日期：2026-10-13，仍为相同隔离工作区；主审明确本批完成后停止写入、等待下一责任分配。没有修改后端或独立验收文件，没有真实发送。第一批所记“旧items发送/缺真实导出”已由本批下述实现替代（行情XLSX已接；监控聊天记录导出仍未接）。

当前源码/package指纹（与第一批相同计算方法）：`187181e4a41785629145947fca56491932dca6c9138e54af2681be068a63dd61`。

### 交付内容

- `RfqAssignModal.vue`、`api.ts`：用真实 `/api/im/rfq/preview` → `/api/im/rfq/send` 两步合同替换旧items直发。本方BUY/SELL与量初始为空、必须本人填写；起始收益率/边界/步幅明确展示。绝对收益边界仅从本人已保存底价预填，步幅仅从已保存策略预填；显式点击后才用已有估值与策略补充空字段，不覆盖用户编辑、不拿未知估值当0。
- 原行情原文保留编辑/复制功能并明确标注“仅复制或人工发送，不进入自动首句”；预览请求不携带该自由文本。单独显示服务端逐收件人最终首句与规范化授权参数，核对后才确认批次ID、expected_version和稳定request_id。
- 参数/收件人/账号变化即时使预览失效；账号变化清收件人。迟到预览不能复活旧授权。确认响应丢失后重试沿用原批次/版本/request_id；确认开始后冻结本批编辑，保留刷新确认结果入口。queued显示“已入队，尚未发送”；pending/unknown不当发送成功。关闭不会取消已授权发送，界面明确说明。
- 按主审最新裁决，零外发预览仅要求本人binding。`App.vue`、`imLink.ts`从auth/me或本人links读取绑定，通道未测试/离线不阻止预览，但健康展示仍保持真实状态，实际投递闸仍在后端。
- `Quote.vue`导出按钮使用完整FilterSpec请求 `GET /api/bonds/export`，下载服务端原始XLSX字节，不拼CSV；JSON错误、空文件不触发伪下载。筛选/账号在请求期间变化时不把旧范围当新范围下载。
- `Monitor.vue`本券交易板显示服务端平铺字段：own_side、peer_quote、own_limit、gap_bp、market_bid、market_ofr、quote_at、freshness、phase；零值保留、空值“—”，不计算或猜底价/价差/方向。生产sent回执归一为已发送；只有dir=out/outgoing而无明确角色时显示“本方”，不猜人工/机器人。
- 正式行情来源允许public/innodealing/not_configured；未配置展示服务端emptyReason，mock仍拒绝。rg确认无引用后删除未鉴权的旧quoteStream入口。筛选状态在末尾追加待核对、停止中，保持旧索引。
- `QuoteDelta.s?:BondStatic`接入公共静态白名单：worker更新已缓存券，按flow拒绝旧静态，迟到REST快照不能覆盖新delta；页面真实applyDelta同步券名、券种、期限、估值等静态字段，筛选随之移除旧组成员。未合入任何公共推送中的私人底价/状态。

### 本批单独验证

| 命令 | 本次实际结果 |
|---|---|
| `node web/tests/developer/run.mjs web/tests/developer/quoteAuth.test.ts web/tests/developer/tradeExport.test.ts web/tests/developer/components.test.ts web/tests/developer/rfqPreview.test.ts` | 退出0；33通过，0失败/跳过；0.75秒 |
| `npm --prefix web run build` | 退出0；vue-tsc通过；Vite 3231模块，11.19秒 |
| `.venv/bin/python -m pytest -q web/tests/acceptance/test_rfq_authorization.py web/tests/acceptance/test_export_tradeboard.py web/tests/acceptance/test_production_dto.py` | 退出0；13通过，0失败/跳过；29.79秒 |
| `git diff --check -- web` | 退出0 |

日志为 `/tmp/xj-batch2-final-targeted.log`、`/tmp/xj-web-batch2-build.log`、`/tmp/xj-batch2-browser-final.log`。先红后修的新增覆盖包括两步预览、资格、稳定确认ID、静态变更及迟到静态快照、来源识别、sent回执、导出/事实板归一。受合同变更直接影响的**开发者**旧items测试改为验证“旧items不能绕过预览”；独立测试未改。

独立浏览器实际验证：预览零发送、明确方向/量、逐收件人服务端原文、参数变更失效、丢响应稳定重试、离线binding仍可预览；下载字节与真实openpyxl合成XLSX完全一致且可解析，JSON失败无下载；交易板字段与缺值；真实生产API产生的NEGOTIATING DTO正确显示“询价中”。浏览器外部API使用合成拦截，生产DTO用临时SQLite真实ASGI入口。未接真实账号或生产外部系统。

按主审要求，**本批未运行全量回归**。setup级开发测试仍有已说明的Vue生命周期告警，Python有既有Starlette/httpx弃用警告；浏览器入口证据与这些setup级测试分开记录。

### 交接边界与停止点

本批前端代码已按锁定合同交付，定向开发测试、独立浏览器验收和构建通过；按主审指令现在停止代码写入。主审继续真实生产路由/来源装配、全量回归及发布前验收：public来源、XLSX导出、逐券权威状态汇总、公共静态s推送须与真实入口整合，不能把合成页面拦截当成已连真实服务。真实门户签名、通道健康、OSS/邮件发送、Dify与生产数据库仍未由本前端批次验证。监控“导出记录”仍明确提示未接入，与本批已交付的**行情筛选XLSX**不是同一能力。

## 最后收口批：个人策略草稿与只读问答（2026-10-13）

仅修改 Chat.vue、StrategyModal.vue、api.ts、必要types和开发者测试，新增server/qa_routes.py；采集协议/传输/账本/Bridge保持上一冻结版本，未改根app/business或独立QA。

- 新账号 `configured=false, templates=[]` 正常显示未保存草稿，读取不写入，默认模板ID显式为personal；只有保存动作提交DTO并将返回模板标为configured。S1/S2旧控件移除，DTO为现有合同保留system字段；策略只供新批次预览参考，量逐批人工填写、跨日重新确认。派生数字标为参考步数，不宣称强制轮次能力。
- `qa_router(service,market,tools)` 提供 POST /api/qa，body仅question。支持“查券 简称/代码”“报价 完整代码或唯一全称”“我的在谈概要”；代码/简称调用现有MarketService和BondTools，本人概要调用本人sessions/conversations；未知请求明确不支持，未接入行情/资料明确说明。拒绝额外owner/query参数和SQL文本，不执行外发或持久问答历史。
- 图表只包含已知有限Bid/Ofr值；缺失侧不画0。前端对问答类别图也过滤非数值类别。Chat快捷问题与说明仅宣传这三种能力，保留Markdown净化。
- 删除无人调用的按券DELETE方法，新增api.archiveSession(id)→POST /api/sessions/{id}/archive；exportMonitor增加可选peer参数。Monitor按钮与服务端挂载/peer过滤由主审负责，本批未越界接线。

验证：`npm run test:developer` 44 passed（/tmp/xj-ui-close-tests.log）；`npm run build`退出0（/tmp/xj-ui-close-build.log）；`.venv/bin/python -m pytest -q tests/developer/test_qa_routes.py` 4 passed（/tmp/xj-qa-final.log），使用真实Repository/InquiryService/MarketService/BondTools并注入合成只读源，覆盖本人/他人会话隔离、SQL与跨人参数拒绝、缺值图表、未配置和参考候选。compileall与git diff --check通过。仅有现有Starlette TestClient关于httpx的弃用警告；没有升级依赖。未执行真实网络/账号，独立QA另行验收。
