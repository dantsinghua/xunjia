# 安琳现场页面升级交付记录

**以下为初版历史记录，已由全量测试后的845449版本取代。** 最新交付见 `2026-10-30-upgrade-verification.md`。旧队列qaupui9d730c中的已核对初版包已撤下，pending=0；新队列qaupui62ab10，pending=1。不要继续使用下面初版包的哈希安装。

当前现场：158.188.1.200 的 Innodealing 登录修复 cb2811 已由安琳确认页面测试通过。原始错误为网关 IP URL 导致 Cookie 域校验错误。本轮不再跑测试。

本轮完成下载留存与页面升级。新目标 `be5d23d4be49a2c3378c528e5a3e292e58c06b5470c93072158f9f9a15db9a61`，仅接受 cb2811 基线或幂等目标。前端 `088215df-0ed7-47f4-a089-240257ed3765`。未提交或推送。

交付目录 `/home/anlin/.local/state/xunjia-remote-upgrade-20261030`：target、delivery、build.json、build_delivery.py、signed-feature.zip。首次启用 ZIP `xj-remote-upgrade.zip`，705853 字节，SHA256 `153156a6a6e365f1dc5393439bef8913b59dc6ebffe98119fade228182d04b1c`。

首次启用独立队列 `qaupui9d730c` 已上传一次，pending=1。发送记录 `bootstrap-upload.json`。未在本机消费；不重复上传。现场下载保存 `/root/xj-remote-upgrade.zip`，核哈希后解压至 `/root/xjbridge/remote`，进入 `xj-remote-upgrade` 执行 `sh install.sh`。脚本固定以 `/home/Xunjia-new-20261026/xunjia-update-cb2811cfe8468f48` 为完整基线，再启用公共通道配置。成功标志 `PAGE_UPGRADE_AND_RETENTION_ENABLED`。

后续固定队列 `xjup_158_v1`，站点 `yingmi-158`。安琳明确选择所有已登录用户可升级。Ed25519 公钥摘要 `6d7c44077161f17dbb6a3dec0efa695ced61c4da422cb709d6663c3a99a8d8fb`；私钥及网关凭据仅在本机受保护目录 `/home/anlin/.config/xunjia-upgrade`，不进仓库或158。命令 `/home/anlin/.local/bin/xunjia-upgrade-upload /绝对路径/upgrade.py` 签名并上传一次；其标记防止结果不明时自动重发。尚未向固定生产升级队列发送该特性包，避免首次安装后重复升级。

静态证据：12个Python文件语法通过，vue-tsc及Vite构建通过，签名包与目标manifest校验通过，打包源码无漂移。独立静态复核提出的删除链接竞态、凭据包排除、未完成升级恢复、超时、重复成功文案、配置相对路径及证书链接保护、游标饥饿、缺失旧对象和损坏候选版本读取均已修正。未跑pytest、未执行本机真实清理、未替代158安装/现场按钮验收。

清理边界：受管缓存30天；结构化记录与附件完整内容归SQLite并保留，业务仍走原OceanBase。7天展示及协议TTL保留。未ACK/UNKNOWN/不完整附件、当前及直接回滚release、当前基线和配置引用对象受保护；未知用途文件不批量删除。SQLite释放页供复用，文件大小不一定立即下降。

待现场：下载哈希确认、首次安装成功、Ctrl+F5看到采集配置页升级卡片、读取当前build/清理状态；以后真实新版本包通过固定队列+按钮升级另验收。网络维持脚本172的cron继续运行，不在此次包内重配网络。
