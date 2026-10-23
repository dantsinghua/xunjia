# 企点组织读取接线

安琳，当前链路复用同账号的 `adapters/qidian/org.py`，由 `app.py` 的 `_xunjia_v22_contacts` 读取，再由 `InstitutionCollectorHost.metadata_once` 调用 `SnapshotPublisher.organization` 发布到机构组织路径。接收核心不再包含目录发布分支。

组织凭据引用独立于文件通道认证，路径由 `_xunjia_org_config(uid)` 固定为采集端数据目录下的 `organization/<uid>.json`。示例仅说明格式：

```json
{
  "account_uid": "REPLACE_CURRENT_QIDIAN_UID",
  "org_password_file": "/absolute/private/current-account-org-password.bin",
  "org_password_format": "getPassword"
}
```

相对口令文件路径以该组织配置所在目录解析；非 Windows 文件须为 0600。`getPassword` 使用已有 `read_password_blob` 记录格式；`raw` 表示文件字节本身是组织 SQLCipher 口令。源码、前端及报告不携带真实口令。读取范围始终绑定当前账号，缺凭据或读取错误不能冒充真实空组织。

reader 保留 `ready/empty/unreported/error` 语义与成功读取时间缓存；源文件未变时不抬高读取时间。只有成功得到完整 `departments/members` 的 `ready/empty` 才发布组织快照；真实 `empty` 才发布空组织。缺凭据、坏源、provider 异常及超出协议载荷限制时，不发布替代组织，不改变上次成功组织与发布记录时间，保留待同步请求，并记录固定错误码。好友快照走独立路径，组织失败不截断其已成功发布内容。

`source/tests/acceptance/test_current_org_directory.py` 的五例用临时 SQLite、真实当前 provider 与真实签名 Codec 验证上述链路及源文件原字节不变。仓库开发测试另保留组织过大时的旧组织和好友保留断言。它们是离线合成验证，真实加密源、账号、网络及现场权限仍需单独验收。

本目录提供完整 `app.py`、`institution.py`、reader 和接收核心的精确参考文件及 manifest，不再交付 app 补丁，不自动应用此子集。实际部署使用完整采集应用及匹配的共享 runtime，保留既有 dirty、BOM/换行和所有现场数据。
