# 158 页面升级与下载保留

安琳授权所有已登录用户操作升级，保留业务数据、现役配置及回滚能力。现场基线是登录修复 `cb2811cfe8468f485f4445cd5efde3899c6c29a42afd0fe88ad6fb0d5bb0c2bd`。后续明确要求全量测试，已完成专用测试、完整七门与 ARM 离线安装回滚；见 `2026-10-30-upgrade-verification.md`。这些证据不替代158真实systemd与客户数据库现场验收。

## 页面及后台

采集配置页“升级包同步”调用已登录接口 `GET/POST /api/system/upgrade`。POST 不接受路径、命令或任意下载地址。通道配置位于 `PREFIX/config/upgrade-channel.json`，0600。此现场配置 `operators:["*"]`，其它部署可填明确用户 ID。

本地 `scripts/upgrade_upload.py` 提供 keygen、pack、upload。Ed25519 私钥仅存在上传机；158 仅保存公钥。独立升级队列与业务队列隔离。包固定三项：manifest.json、signature.bin、upgrade.py。服务先验证签名、文件摘要、站点、30 天有效期及实际基线，再执行现有增量安装器。普通 ZIP 或旧版未签名包不能由页面执行。有效期以两端系统时钟为准。

任务及结果保存在 `PREFIX/state/upgrade/upgrades.db`。下载接口具有消费语义，每次任务最多下载一次，网络结果不明不自动重试。systemd-run 启动独立服务 `xunjia-upgrade-<job>`，不随主服务重启退出。安装器沿用哈希校验、单实例锁、保留配置、服务健康检查及失败回滚。单次检查或安装子进程上限 3600 秒；超时安装结果标记 UNKNOWN，核对后才能再次提交。

页面每 5 秒查询，断连后等待服务恢复，不重发 POST。成功表示安装器本地版本/健康检查通过；不代表 Dify 业务、实盘数据及消息发送已经验收。Dify DSL 如有变化仍需独立发布，页面不自动操作 Dify。

## 中断核对

先核对本任务日志 `PREFIX/state/upgrade/jobs/<job>/apply.log`、systemd 单元及 `PREFIX/deployments`。日志仅服务端可读，页面只返回安全错误码。不得盲目重发已被消费的包。

核对结果后可用：

```
/home/xunjia/venv/bin/python -I -B /home/xunjia/app/deploy/enable_remote_upgrade.py --resolve-job JOB_ID --outcome succeeded
```

或明确未安装时 `--outcome failed`。工具要求 worker 已停止、部署日志无未完成事务；成功还要求运行版本及健康吻合。解除只更新审计结果，不自动下载或重放。遇未完成部署，先按既有 `03-recover.sh` 流程处理，不能改数据库状态绕过。

## 30 天保留范围

传输和附件实现见 `2026-10-30-download-retention.md`。内容归档于本地 SQLite；业务消息仍走原有 OceanBase 路径。不是全部文件迁入 OceanBase。结构化正文、附件内容、命令和去重账本不随缓存过期删除；SQLite 页可复用，暂不在线 VACUUM。

文件清理每小时执行，目录为固定产品命名空间。升级任务文件、旧完整 bundle、旧 release、已识别手动 ZIP/诊断报告超过30天后清理，最多每轮10个登记对象。`/root/xjbridge`、`/root/loginfix`、`/root/xj-watch-1029` 仅识别升级产物名称和 net-result 报告，不删除整目录、维持脚本或任意新增文件。凭据/Private 包排除；未识别文件不推测用途。

清理前获取增量锁、部署锁，核对对象清单；固定父目录 fd，使用不跟随符号链接的删除实现。保护当前 release、直接回滚 release、当前基线 bundle、共享配置及仍被配置引用的路径。部署未完成或升级 UNKNOWN 时暂缓清理。审计元数据留在 SQLite，记录已删内容及保留原因。

首次需通过已有文件通道安装启用包，再运行 `enable_remote_upgrade.py --channel <公钥通道配置>`。公钥配置不会覆盖已有不同通道，凭据继续复用现场 institution 配置。后续上传签名包后，158 即可在页面同步并升级。
