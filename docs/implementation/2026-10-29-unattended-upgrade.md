# 20261027 → 20261029 离线增量升级

安琳本轮授权：保留现役数据和配置，应用及所需数据库增量无人值守执行；Dify 仍独立导入发布。不提交、不推送，不在本机代替银行现场执行升级。

目标源码为 `ff3d14e` 加当前工作区已保存功能，不等同于该提交本身。1027 桌面正式增量目标 build_id 为 `4bf10ab493abadc1da663db42bda762615eab5732eb8438b9c7c81ef4f2d57de`。交付必须以实际文件清单和 SHA256 标识目标，不能仅靠日期判断兼容。

范围包括 Innodealing 用户名密码自动登录、后台行情采集、QT FIX 后台及设置、QT 用户数据隔离与报价视图、企点引用回复和对应 Dify 合同。1027 已有的子路径入口、24 小时会话及谈价修复继续保留。数据库差异仅 `xj2_message.reply_json LONGTEXT NULL`，禁止以 fresh-install 代替增量迁移。

验收分工：产品实现维护部署模块和开发测试；独立验收维护资源合同与升级故障用例；主控冻结后构建、核对增量及汇总证据。

原报告 `xunjia-verification-vp47mp5g` 的结果已核实：七门中仅 production-web 失败，原因是合同未识别 `./assets/`。缺失的 mysql/load 合同在旧代码清理时删除，依赖已废弃模型；不得把未执行的真实数据库门标为通过。

本轮构建与复核证据保存在 `/home/anlin/.local/state/xunjia-unattended-20261029`。最终机器可读交付清单记录本轮测试结果及现场未验证范围，历史包的通过数不移植为本轮证据。

最终结果：官方七门 `passed: true`、`source_drift: []`；pytest 4383、web-unit 8、web-developer 82、web-python-developer 2、production-web 1、web-browser 199，web-build 通过。证据为 `/home/anlin/.local/state/xunjia-verification-review-20261029/final-current-tree/xunjia-verification-xjcafebk/result.json`。

最终目标 `545cc045cc4fe4be885c03e6700012e80da4a5901c716dd0307bf7753f2c6246`，升级脚本 SHA256 `36c9d57ce16f98276112613f85bd16d60081330d3959590afbaabcee26f8a961`。三个基线完整还原一致，六个前端文件与本轮构建一致。精确包在隔离麒麟 ARM 用户态的安装、启动及回滚验证通过，191.13 秒；配置及历史状态哨兵保持。

桌面交付：`Xunjia-Offline-Incremental-20261029-Unattended.zip` 及同名目录。复制目录中的 `upgrade.sh`、`upgrade.py`、`upgrade.py.sha256` 到现场同一目录，以 root 执行 `sh upgrade.sh`；`--check` 只做检查。现役账号需具备 ALTER 权限，保留原始完整包。Dify 单独导入发布。真实 OceanBase DDL、银行现场服务及物理 ARM 尚未执行，不在本机通过范围内。
