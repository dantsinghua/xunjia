# 企点组织读取接线

复用同账号既有 `adapters/qidian/org.py`，公共 `/accounts/{id}/contacts` 列表契约保持不变。仅 V2.2 私有 provider 返回 `contacts` 与组织状态，由既有文件中转发布。

受保护的当前 collector JSON 可增加以下**可选引用**，不把真实口令放入源码、报告或前端：

```json
{
  "account_uid": "REPLACE_CURRENT_QIDIAN_UID",
  "org_password_file": "/absolute/private/current-account-org-password.bin",
  "org_password_format": "getPassword"
}
```

`org_password_file` 相对路径按 collector JSON 所在目录解析，非 Windows 部署须为0600。`getPassword` 使用既有 `read_password_blob` 记录格式；`raw` 表示文件字节本身就是组织 SQLCipher 口令，不是登录密码。不得猜口令、读取其他账号或把缺口令当成真实空组织。

`org_state` 为 `ready/empty/unreported/error`。`org_updated_at` 是最后成功读取时间，源文件元数据未变时不抬新时间；读取错误保留上次已发布组织及其时间。host_meta 只保存 publication ID，正文沿用原持久发布记录。超出现有文件载荷限制时报 `ORG_DOCUMENT_TOO_LARGE`，不截断成员。

交付包含精确 org reader 镜像、host 镜像以及 app.py 私有 provider 的定点补丁。`app.py` 不整文件覆盖，必须核前像SHA与实际源一致再应用，保留BOM/换行及其他未提交修改。现役进程由负责运行切换的任务统一启用；代码解析或合成用例通过不代表真实组织解密已验证。
