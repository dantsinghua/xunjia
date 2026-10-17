# 安琳的询价改造交付结果

2026-10-13，本轮已确认范围的实现已集成回 `/home/anlin/work/xunjia-agent`，从该目录完成独立全量验收，所有验证门通过，没有通过跳过用例或放宽验收门消除红测。未 commit、push，也未部署或操作真实交易账号。

## 实现与简化

- 正式入口统一为 `server.app:create_app`，门户登录兼容既有存款项目签名，五人一人一号；聊天、策略、询价、导出按本人隔离，公共行情共享。业务授权、会话、命令和审计进入事务库。
- 首询按预览→明确确认冻结方向、数量和策略。Dify交付为 `advanced-chat` Chatflow，使用Agent、四个只读工具、持久上下文及记忆、受限历史补取、六次工具预算；模型提案经过后台规则及最终正文签证才入队。
- 自动谈价只在授权内推进；按买卖方向计算目标差距。实价未达目标可继续，达标、条件变化、净价不可靠、身份歧义或权限撤销转人工。一次中性澄清后仍模糊转人工，不允许成交或泄露内部信息。
- OSS与SMTP/IMAP共用持久命令、事件和回执合同，命令择一主用、人工切换；未知结果先对账。接管、停止、恢复和采集端人工屏障贯穿执行链，旧命令不能靠更高版本绕过人工屏障。
- STS依据HTML及 `its-accesspoint` 源码使用SM4-CBC生成一分钟内的认证参数，支持指定MQ地址；OSS对象正文保留原AES-GCM格式。没有实际MQ密钥、没有真实STS请求。
- 生产行情模块移至 `server/`；撤价、净价口径、源版本、跨日和事务水位已修复。行情筛选/count/分页下推SQL，聊天分段读取，导出为真正XLSX。前端不再把接口失败包装成成功或补造业务数据。
- 已删除mock服务、启动脚本、造数种子及生产前端mock数据，共9个文件；真实缓存数据库、邮件、采集状态均保留。新生产入口不挂旧无身份路由。部分历史PoC仍保留作兼容参考，不作为正式运行入口。

原始40项问题逐项映射见[整改矩阵](remediation-matrix.md)。实施终审另外发现的18项问题均已独立关闭，见[第四轮评审结论](final-review.md)。

## 最终验证证据

精确命令：

```bash
cd /home/anlin/work/xunjia-agent
/tmp/xunjia-clean-install-20261013/bin/python tests/run_verification.py --mysql --load --browsers-path /tmp/xunjia-playwright-20261013
```

本次唯一最终证据为 `/tmp/xunjia-verification-5mksioj3/result.json`，`passed=true`、`source_drift=[]`，八个阶段退出码均为0；持久副本由独立QA归档至 `test-logs/final-original/`。

| 验证门 | 本次结果 |
|---|---:|
| 全量默认pytest | 661通过 |
| 独立MySQL事务/并发 | 6通过 |
| 正式服务构建产物 | 1通过 |
| 独立浏览器 | 32通过 |
| SQLite/MySQL负载 | 2通过 |
| Node既有/开发者测试 | 8＋44通过 |
| vue-tsc/Vite | 通过 |

Python合计702项，Node合计52项，没有合并历史批次计数。Starlette/TestClient弃用警告如实保留；npm按当前策略阻止了两个依赖postinstall脚本，但实际构建与浏览器门通过。干净Python环境按 `requirements-dev.lock` 安装且 `pip check` 通过；Node依赖按lock执行 `npm ci`，该轮审计0漏洞。

原目录manifest为256项：其中252项与冻结清单完全一致，额外4项是原目录已有的历史性能JSON，保留并纳入前后漂移检查。冻结252项清单SHA256为 `af3b276f22512a73bbba2c324e4f22d52807641722301ea253cd8bfb073fc02f`。集成前逐文件核对基线，无冲突；185项更新及9项删除的原文件备份在 `/tmp/xunjia-integration-backup-20261013-6nl106go`，不覆盖原目录运行数据。

本轮负载每后端均为5个合成身份各10万历史，300次并发读请求及10万行导出：SQLite读P95为0.063928秒、导出5.298秒；隔离MySQL读P95为0.489910秒、导出6.667秒。数据及机器范围固定，不作为OceanBase生产容量承诺。

另有真实Dify 1.17.1新实例与实际后端整链证据：252文件及23个实际加载模块hash、原生Dify成功run、工具结果、最终签证摘要、持久命令及FakeTransport回执、工具预算和凭据隔离，均经独立只读复核。模型与业务账号为合成测试环境，详见[冻结版Dify报告](dify-final-freeze-report.md)。

## 明确的现场边界

1. 主数据视图/财汇资料尚待定，按安琳裁决保留 `ReferenceDataProvider` 接口；缺数据明确不可用，不补造发行人、余额、评级或授信。
2. 真实OceanBase、门户、MQ认证、OSS、企业邮箱及企点native执行需要现场配置与联调。本次没有使用真实 `auth.key` 或 `auth.sm4_key`；测试值不能作为部署密钥。
3. Dify目标交付版本为1.17.1；旧0.15.3实例未修改，正式升级需要按部署文档备份、迁移及绑定真实模型。真实模型的谈价语义质量尚未验收。
4. 采集端需安装v2 Bridge/ReceiverLedger、接好native ID及人工出站事件，并停用旧xunjia poller后切流，详见[采集端集成](collector-integration.md)。本机没有改动现役采集容器。
5. 行情来源采用管理员静态部署配置，普通交易员只读健康状态；未实现“在线测试配置→热应用”的额外状态机。

“0回归、0红”指本轮确定版本、所列测试与合同范围的结果，不表示未经现场验证的环境和功能已经上线，也不承诺不存在所有未知缺陷。
