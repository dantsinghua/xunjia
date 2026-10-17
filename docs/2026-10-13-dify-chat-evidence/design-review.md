# Chat Agent 设计审查记录

安琳，本记录仅描述2026-10-13的设计工作，不代表实现或真实业务验收。

- 新派 `dify_chat_capabilities`：核对本地0.15.3、官方固定版本/插件源码、Chatflow能力与限制；依据保存在capabilities.md。
- 新派 `dealing_chat_design`：设计单Chat Agent、目标计算、上下文/关系记忆、受控行动与最危险场景；正文由主审整合到主设计。该子agent遵守只读角色，未写源码或运行真实账号。
- 新派 `bond_tools_design`：核对字段链、缺失主数据、四工具合同和计算器前提；依据保存在bond-tools.md。
- 独立 `code_review`：审查主稿、能力证据和工具合同，提出2项HIGH、1项MEDIUM；主审修订后，限定复核确认三项全部关闭。

三项修订：

1. 澄清次数由服务端稳定ambiguity_issue_id和command事实维护，不由memory_patch或新消息重置；unknown不释放额度。
2. 任何非空公开正文，包括澄清/拒答/转人工礼貌语，都经过同一公开生成及最终检查；内部失败分支必须无正文。
3. history_request走固定HTTP、本人当前对话和有界一次补查；不得任意查询私有数据、递归取历史或使用过期控制版本。

另按用户追加要求准备STS接口合同：用户确认SM4-CBC仅STS认证，OSS正文保持旧格式；指定nytoken.yingmi.com，并明确无需读取认证服务源码、只预留对接。STS官方协议由能力研究子agent另作核验，**STS设计未冒认为上述Chat设计限定终审覆盖项**。

已做的数值检查只有goal-examples.json中的7个Decimal示例。没有执行项目新测试、Dify导入、服务升级、模型评测、STS/OSS请求或真实发信。
