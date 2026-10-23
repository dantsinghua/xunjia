# 现役采集端集成参考

安琳，本目录是当前采集端源码及测试的最小参考闭包，不是第二套采集程序，也不自动覆盖安装。实现权威是采集端仓库的冻结工作树；`manifest.json` 记录基线提交、冻结输入摘要、每个交付文件的精确 SHA256 与字节数。包含的 `app.py` 仅用于说明真实接线，不能以此子集替换完整应用。

唯一现役通道由 `InstitutionCollectorHost` 从采集端自有 `CollectorReportingSettings` 装配，`XunjiaV22Host` 只负责带授权守卫的原生消息与持久状态，不选择传输版本、不承担目录或组织发布。旧环境开关不能启用第二个 factory，也不能禁用现役 host。

当前 `app.py` 参考包含微信目录显式读取最多5000条：读取前后检查真实登录身份，达到上限报告 `WECHAT_DIRECTORY_TRUNCATED` 并保留上一份成功目录，不插入专用会话。`collector_settings.py` 同步当前配置保存链路，旧保存记录缺少新 TLS 字段时可通过完整凭据与证书路径明确替换，继续保留 mTLS 校验和稳定工作台身份。

正式地址为 `https://its-relay.yingmi.cn`，文件 API 使用 `accesstoken`，配置客户端证书与私钥路径，保留服务器证书链和主机名校验。认证值、证书与现场配置均不随参考包交付。

组织链路为 `adapters/qidian/org.py` → `AgentApp._xunjia_v22_contacts` → `InstitutionCollectorHost.metadata_once` → `SnapshotPublisher.organization`。`source/tests/acceptance/test_current_org_directory.py` 保留五例真实合成 SQLite 链路：正常读取且不改源、真实空组织，以及缺凭据、坏数据、provider 异常时保留之前已签名发布的组织与原时间，并保留待同步请求。详见 `ORGANIZATION-INTEGRATION.md`。

交付的 host、组织与 native guard 测试随完整采集源码及匹配的共享 runtime 执行；reference app-wiring 测试额外核对旧开关不再影响唯一 host。参考测试通过不代表真实账号、组织解密、传输或 native 回执验收完成。安装和现场切换仍须使用完整冻结应用。
