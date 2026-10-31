# 监控消息持久化溯源修复交接

安琳，本次仅修改监控消息溯源读取及弹窗，不修改 `Chat.vue` 欢迎语，不操作现场数据库或真实发送。

## 根因与修复

- 原消息由 `Store.ingest` 写入 `xj2_message`；分析快照与结果、决策、命令回执和审计分别已写入 `xj2_analysis`、`xj2_decision`、`xj2_command`、`xj2_audit`。
- `/api/monitor/chat` 未携带 `trace`，旧 `TraceModal.vue` 仅调用同步 `traceOf`，因此固定进入“服务端尚未提供此消息的审计证据”占位分支。
- 新增 `GET /api/negotiation/messages/{message_id}/trace`，按持久化消息 ID 与线程原始 owner 校验，只读上述数据库事实；没有网络调用、采集端在线检查、重放或读触发写入。
- 分析关联同时要求 revision 范围与持久化输入快照中确实存在此 message_id；回复/决策要求本消息所属子会话与已提交分析水位对应；命令使用决策 command_id 或出站消息的原生标识关联。
- 接口允许原 owner 在工作区归档、binding 切换后继续读取历史；旧账号后来归属的新 owner 无法读取原 owner 的历史，未知与越权 ID 均返回 404。
- 弹窗打开时请求数据库证据，明确区分读取中、读取失败、已持久化；切换消息或关闭时取消请求，并阻止旧响应覆盖新消息。
- 仅展示确实持久化的阶段；排队/失败/待回执保留原始状态，未记录耗时或时间不推算。`evidence` 同时提供结构化原始关联记录。

## 文件

- 新增 `server/negotiation/trace.py`；`ApplicationStore.message_trace` 委托只读投影。
- `server/negotiation/http.py` 注册接口。
- `web/src/api.ts` 新增 `getMessageTrace`；`web/src/components/TraceModal.vue` 接入请求。
- 新增后端开发者测试 `tests/developer/test_persisted_message_trace_20261030.py` 和前端 `web/tests/developer/messageTrace.test.ts`，后者纳入开发者运行器。

## 验证边界

- 5 项新增后端开发者测试使用临时 SQLite，覆盖原文真实落盘、分析/决策/原生回执关联、失败状态、历史排除造成的快照空洞、后续消息与其他券排除、归档/binding 切换、跨 owner 拒绝。
- 重启测试另启独立 Python 进程并禁用 socket，只从同一 SQLite 文件读回相同结果；读取前后数据库 dump 一致。HTTP 用离线调用即抛错的采集端替身，证明只读接口未调用采集端。
- 后端定向回归 127 passed；前端开发者回归 100 passed；`npm run build` 中类型检查与 Vite 生产构建通过；Python 编译与 scoped `git diff --check` 通过。
- 这些是隔离代码验证，不是现场数据库、真实模型或真实 IM 验收。未核实截图消息 `b83eb553c9844756b22885a7d073a107` 的现场内容；该消息若存在于该 owner 的 `xj2_message`，新接口会返回其已落库证据，实际缺失的后续阶段不会补造。
- 不新增表或迁移，不把归档媒体 SQLite 与业务 `xj2_*` 混为同一证据源。未 commit，由主控集成打包及独立验收。
