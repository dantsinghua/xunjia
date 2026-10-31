# 安琳远程升级与清理完整验收

本文记录845449版本；后续按安琳要求补齐自动基线发现，最新自动版5eab3189及队列见 `2026-10-30-automatic-base-upgrade.md`。本文所列qaupui62ab10旧包已核对撤下。

安琳明确要求全量测试，覆盖此前不运行测试的限制。代码与测试独立分工，失败断言保留，产品修复后再冻结完整运行；未提交或推送。

## 最终证据

最终七门报告：`/home/anlin/.local/state/xunjia-upgrade-tests-20261030/final/xunjia-verification-nh5_t0b7/result.json`。`passed=true`，`source_drift=[]`。

| 门 | 结果 |
|---|---|
| Python全量 | 4589 passed，0 failed/skipped/incomplete |
| 官方前端单元 | 8 passed |
| 前端开发 | 90 passed |
| 前端Python开发 | 2 passed |
| 前端生产构建 | 通过 |
| 生产资源合同 | 1 passed |
| 真实浏览器 | 216 passed，0 failed/skipped/incomplete |

额外执行所有 `web/tests/*.test.ts`：80 passed（包含官方8项，不能再叠加计数）。新增专用用例：升级70、清理60、页面开发8、浏览器17、真实HTTPX Cookie策略9；专用用例已纳入对应全量门，不另加总。旧登录网关测试由独立维护者改为真实Cookie容器，原Host/端口断言保留。

## 实测发现与修复

- 通道配置在校验前落盘，失败留下半成品：现在先验证后原子写入。
- 清理失败仍展示旧成功，以及HTTP汇总覆盖错误：持久失败状态并保留提示。
- 外部配置文件别名造成相对证书路径保护遗漏：按实际配置别名所在目录解析，保留链接目标。
- 损坏的过期bundle元数据阻断整个清理：保护该对象并继续轮转。
- 首次启用包的install.py/install.sh/channel.json未清理：仅纳入已知下载目录的固定文件名，其他位置仍保护。
- 第一轮全量暴露collector wheel边界越界：归档扩展移到server/retained_archive.py和retained_directory.py。三个共享模块恢复HEAD=cb=已交付wheel字节，18文件冻结门和协议未改。最终包再次逐项核对wheel/source哈希。

第一轮4583通过、1失败的报告保留在 `full/xunjia-verification-h8qgf98c`，不与最终结果混算。

## ARM包验收

`/home/anlin/.local/state/xunjia-upgrade-tests-20261030/arm/work/upgrade-result.json`：在隔离ARM/QEMU、Python3.12.13/glibc2.28上从cb2811实际安装目标并回滚，178.62秒；目标173文件及回滚164文件逐项哈希匹配；配置、UNKNOWN和财务SQLite哨兵保留。真实Ed25519签名校验、loopback首页/live/ready与鉴权升级状态接口通过。

网络命名空间无外部路由，服务管理为模拟停止态。没有真实客户数据库DDL、现场清理、真实systemd或158实机升级。此前初版be5基线也已实际materialize并核对完整目标清单。

## 最新交付

- 目标：`845449f7ef5c7086944729cd987a15a978a643f04c2c308ab42246a0dcfddb25`
- 前端：`4bf66b16-dc30-44b0-95d6-dcc738aae55e`
- 支持cb2811及初版be5d23两个基线。
- 包：`/home/anlin/.local/state/xunjia-upgrade-tests-20261030/release/xj-remote-upgrade-verified.zip`
- 708705字节；SHA256：`bfe6919fabf49d1cd0077c4ed97be27d8e5c29dfbfe0442414a95a8d972676be`
- 新独立队列：`qaupui62ab10`，已上传一次，pending=1。
- 旧独立队列qaupui9d730c：核对文件名及旧哈希后撤下，pending=0。
- 后续固定签名队列仍为xjup_158_v1；本地上传入口不变，未向其重复发送本次包。

现场将新包存为 `/root/xj-remote-upgrade-verified.zip`，核对哈希后解压到 `/root/xjbridge/verified`，进入 `xj-remote-upgrade` 运行 `sh install.sh`。成功输出 `PAGE_UPGRADE_AND_RETENTION_ENABLED`。这一步仍待安琳现场操作，不能由本地全量通过替代。
