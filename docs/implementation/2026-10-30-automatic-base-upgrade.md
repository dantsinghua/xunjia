# 安琳一键升级自动基线定位

本轮按安琳要求消除历史多目录、大小写、失效记录导致的手工 `--bundle` 步骤。保留签名、当前版本和完整文件清单检查；无法找到任何完整受支持基线时仍安全停止。

`deploy/incremental.py` 内置独立候选发现器：受管缓存、env提示、升级配置和只读SQLite记录、有限深度产品目录。当前版本优先，损坏副本跳过，其它明确受支持的完整基线可用于重建同一精确目标。未知当前版本在准备前拒绝。目标输出固定在 `PREFIX/state/upgrade/bundles`，已安装目标重跑可复用其完整包，无需旧基线仍在。

页面worker只把旧路径作为提示，不再传强制 `--bundle`；启用与结果核对共用候选发现器。首次交付 `install.py` 源码为 `deploy/upgrade_bootstrap.py`，核验交付清单后直接运行 `upgrade.py --prefix ... --apply`，成功后启用通道。`sh install.sh` 是唯一安装入口。

## 验证

- 独立新增38项自动定位测试，相关限定回归143通过。
- 完整七门通过，`source_drift=[]`；Python4627、浏览器216、前端开发90、官方前端单元8、前端Python2、生产资源1、构建通过。另全部顶层前端单元80通过（含官方8项，不重复加总）。
- 报告：`/home/anlin/.local/state/xunjia-auto-base-20261030/verification/xunjia-verification-bn7pgud_/result.json`。
- ARM实际验收176.83秒：三种提示均失效，自动发现大小写目录中的cb2811，无 `--bundle`；174项目标载荷匹配；隐藏旧基线后目标幂等复用；安装/loopback健康与鉴权API/Ed25519/回滚通过，回滚164项、配置及财务和UNKNOWN哨兵保留。
- ARM证据：`/home/anlin/.local/state/xunjia-auto-base-20261030/arm/work/upgrade-result.json`。隔离QEMU，无真实systemd或客户数据库，现场安装仍待用户操作。

## 最新交付

目标 `5eab3189d0fe54510deef4cf560aa6bec726b53942a0b85c5b1a5e1a55152703`，支持cb2811、be5初版、845449已测版。三个基线的重建清单均核验。

`/home/anlin/.local/state/xunjia-auto-base-20261030/release/xj-remote-upgrade-auto.zip`，718106字节，SHA256：

```
12e3160b29a939b44a2c7c3d7d727a7db227fe1c83c0f72a9f842a44a96a1192
```

新专用队列 `qaupauto30`，已上传一次，pending=1。旧队列 `qaupui62ab10` 中已核对原文件名与哈希的845449包已撤下，pending=0。后续固定签名队列 `xjup_158_v1` 及本地上传入口不变。

158保存 `/root/xj-remote-upgrade-auto.zip`，核对外层哈希后解压到 `/root/xjbridge/auto`，进入 `xj-remote-upgrade` 执行 `sh install.sh`。最后输出 `PAGE_UPGRADE_AND_RETENTION_ENABLED` 后刷新采集配置页。无需手工base或另做配置迁移。下载是消费操作，异常先保留Python会话/文件并核对，不能盲重下。
