# 2026-10-14 现券询价 Agent：受控集成验收

## 审计基线与安全边界

工作区：/home/anlin/work/xunjia-agent
本地 main HEAD：83c7e0f2b60f2be51cf156c72dff5666dd9f00da
所有业务用户、身份、估值、订单、邮件、OSS 对象和执行器均为合成或内存模拟。没有启动真实询价服务、没有修改真实交易账号，也没有提交/推送 Git。

## 实际完成的验证

| 项目 | 结果 | 证据 |
|---|---|---|
| Python 全量离线回归 | 678 passed，0 failed，2 warnings | /tmp/xunjia-controlled-integration-20261014/full-python.log |
| 单独 MySQL 8.0 并发与事务 | 6 passed，0 failed | /tmp/xunjia-controlled-integration-20261014/mysql.log |
| 双通道、人工屏障、停止/Chat 回归子集 | 125 passed，0 failed（与 Python 全量重复，不重复计数） | /tmp/xunjia-controlled-integration-20261014/transport-chat.log |
| Chromium 浏览器，独立 Vite 与合成 API | 32 passed，0 failed | /tmp/xunjia-controlled-integration-20261014/browser.log |
| Node 测试 | 52 passed，0 failed | /tmp/xunjia-controlled-integration-20261014/web-tests.log |
| vue-tsc + Vite 构建 | 成功 | /tmp/xunjia-controlled-integration-20261014/web-build.log |
| SQLite 5账号×10万条，300次并发查询及10万行xlsx | P95 0.0485 秒，导出 4.61 秒 | /tmp/xunjia-load-acceptance-gz8n6swl/result.json |
| MySQL 5账号×10万条，300次并发查询及10万行xlsx | P95 0.4335 秒，导出 5.13 秒 | /tmp/xunjia-load-acceptance-pzf93vi6/result.json |

两个数据库均达到当前隔离负载下 P95 低于 1 秒的方案目标；不能推断生产 OceanBase 容量。两次负载源代码漂移列表均为空。独立 QA MySQL 容器已核实按自身容器身份删除。

## 新增端到端路由联调

文件：tests/acceptance/test_controlled_pipeline_20261014.py；3个用例均已纳入678项全量 Python 验收。

1. SSO → BP 预览 → 服务端估值冻结确认 → 命令落库 → Dispatcher → ReceiverLedger 模拟原生执行 → 回执查询 → 同ID去重 → 人工接管 → 停止 → 普通确认被拒。
2. 估值快照在预览后变化、匿名用户试图确认：服务端分别返回拒绝；没有执行命令。
3. 明确绝对收益率的授权在无参考数据提供者时可以确认，但不会伪造估值快照。

## 未验收的外部环节

- 已有 Dify 0.15.3 仍正常运行。隔离 1.17.1 项目容器停机，原 /tmp Compose 配置与证书挂载和应用票据缺失，未擅自重启；本次未证明真实 Dify 1.17.1 图与这次改动整链正常。
- MQ HTTP 无凭据 GET 返回 401。HTTPS 主机名 nginx.mq.dq.yingmi-inc.com 与服务证书 *.yingmi-inc.com 不匹配；真实开发凭据没有成功调用 STS，真实 OSS GET/PUT 未执行。不得关闭 TLS 主机名验证冒充正式验收。
- 真正的 Windows 采集宿主 v2 接线、企点 native 出站、人工屏障、OSS/邮箱双路现场中断和对账，以及生产 OceanBase 均未执行。
- 缺量交易消息当前 fail-closed；持久化的一次问量及 Dify 模板联动、生产中债估值提供者、单独社交授权仍未交付。

## 结论与后续放行门

结论：批准继续受控集成；不批准真实账号无人值守自动谈价。
待办：①独立重建 Dify 1.17.1 合成模型测试环境并重验本地源码；②接入受控采集端 v2、native ID 和双路断网补发；③取得匹配 TLS 主机名的 STS 服务与正式桶权限，完成真实只读访问授权及受控 OSS 对账；④完善一次问量和可靠估值源。

机器可复核 JSON：/tmp/xunjia-controlled-integration-20261014/result.json
SHA256 证据保存在 JSON 内；现有源码更改未提交或推送。
