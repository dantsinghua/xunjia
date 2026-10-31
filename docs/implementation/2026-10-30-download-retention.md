# 下载留存与本地归档

安琳要求下载文件按 30 天留存、内容尽可能入库。此模块只管理产品已有的机构状态布局；`/root` 手工升级包、诊断文件及升级工作目录由部署侧独立管理，不能把本模块等同于全机文件删除器。

功能仅启用于 158 服务端：`server/retained_archive.py` 与 `server/retained_directory.py` 继承共享归档/目录实现，`InstitutionRuntime` 使用这两个子类。`relay/institution_archive.py`、`relay/institution_directory.py`、`server/file_gateway_transport.py` 保持 cb2811 已交付字节；采集端 wheel 的固定输入与原有 7 天清理合同不变。

## 入库和清理边界

- `fg_download` 已先接收原包 BLOB，`tx_message` 保存解码内容。仅 `STAGED`、`tx_source.acked=1`、`tx_message.acked=1` 且解码正文身份一致的 30 天旧包才清空冗余 BLOB。消息内容、原文件名、来源位置、去重记录、上传账本和 UNKNOWN 命令不删除。包 SHA256、大小和清理时间记录在 `download_retention`。
- `ip_input` 目录原包在应用时同时写 `ip_input_data` 保存完整解码信封。当前机构的旧 `APPLIED` 原包每小时最多补归档 50 个，验证认证后保存正文，不重新应用旧目录快照。只有已归档的 `APPLIED` 旧包允许清空原包 BLOB。旧机构密钥不可用时保留原包并报保护原因。
- 附件形成完整文件时，在 `archive.db.ip_media_content` 保存经大小和 SHA256 校验的完整 BLOB。30 天后只删磁盘缓存，保留内容和元数据。旧版完整附件在清理前补归档，先提交数据库，再删除磁盘副本；缺失、校验失败、不完整附件保持原状并记录原因。
- 附件重建与最终清理在同一 SQLite 写锁下更新缓存状态，删除前复核缓存时间。仍在历史展示窗口内的引用（另加 1 小时读请求宽限）保护缓存，记录 `RECENT_MEDIA_REFERENCE`；这类文件可能超过 30 天，避免清理与正在读取的响应争用路径。
- 历史展示继续使用既有 7 天窗口，协议全局 `HISTORY_SECONDS` 不改。原先直接删除的历史正文和附件引用现在保留在数据库，不因清理缓存而消失。近期合法引用需要旧附件时可从归档重建磁盘缓存，并重新记录缓存创建时间。
- 无有效接收时间的历史原包和附件先登记 `observed_at`，至少观察 30 天，不按文件系统 mtime 猜测接收时间。

上述归档使用产品已有本地 SQLite，不宣称全部已进入 OceanBase。清空冗余 BLOB 释放 SQLite 可复用页，不在线执行全库 VACUUM；数据库文件尺寸可能不会立即缩小。完整附件及业务记录在库中保留，因此本规则不是数据库总容量上限。

## 后台范围与周期

`InstitutionRuntime` 每小时调用一次，单轮最多 8 个数据库，每个传输库扫描最多 100 个候选、每个附件库最多 50 个；持久化行游标和轮转目录游标避免一直卡在最早的保护记录。行扫描固定每一轮的最大序号，新到记录进入下一轮，避免持续新增输入使旧无时间记录始终无法复查。

固定扫描范围：

```
XUNJIA_STATE_DIR/institution-path-v1/<32位机构ID>/accounts/<64位账号上下文ID>.db
XUNJIA_STATE_DIR/institution-path-v1/<32位机构ID>/archive/archive.db
XUNJIA_STATE_DIR/institution-path-v1/<32位机构ID>/directory.db
已配置的directory_state_dir/<32位机构ID>/directory.db
```

包含已退出选择的旧账号和旧机构目录。拒绝符号链接和布局外路径，不删除数据库本体、配置、密钥、现役 release。清理错误与保护计数在采集配置状态的 `download_retention` 返回；异常不向业务接收主循环传播。保护文件不会为了满足“30 天”而被强删。

## 验证范围

最初按安琳要求仅进行了 Python AST/语法编译与差异检查；后续收到限定清理验收授权，运行 `tests/acceptance/test_download_retention_20261030.py`。服务端归档、目录、清理和运行时隔离用例通过；该次完整结果为 57 项通过、3 项手工下载白名单用例失败，已交主任务处理。未执行现场清理或现场升级。

原采集端 7 天删除验收合同保持不变。独立验收仅将新增服务端清理用例导入切换为 `server.retained_archive` 和 `server.retained_directory`，产品实现方未修改测试或固定 18 文件输入门。
