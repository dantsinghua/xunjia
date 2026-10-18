> 2026-10-18 V2.2更新：现役入口固定新核心，公开Tool免Token，Dify使用普通A/B App Key。以下历史设计中的mock路径、旧表和单Agent流程不是启动入口；当前运行与交付以[部署说明](implementation/deployment.md)、[切换进度](implementation/2026-10-17-v22-cutover-progress.md)及[离线安装说明](../deploy/INSTALL.md)为准。

# 现券询价谈价机器人 —— 架构设计(v1.0 重设计)

> **2026-10-13 当前设计入口（尚未实施）**：[全面评审](2026-10-13-code-review.md) → [整体整改设计](2026-10-13-remediation-design.md) → [Dify多轮Chat Agent](2026-10-13-dify-chat-agent-design.md) 与 [STS/SM4认证对接](2026-10-13-sts-integration-design.md)。后续用户裁决优先：Agent可在授权内自主查证、比较及谈价，实价但gap>0可继续，gap≤0或触停转人工，始终不可成交。STS认证改SM4-CBC，OSS对象正文不变。下方旧v1.0中冲突的目标/模型分工作为历史背景，不用来覆盖最新方案。

南银理财现券询价项目。通道由 QQ/OneBot 切换为 **redroid 跑企点 APK + RPA**。本目录是整套框架/架构/数据流/工作流/dify/redroid 操作的设计。

## 文档索引
| # | 文档 | 内容 |
|---|---|---|
| 01 | [数据模型](01-data-model.md) | thread/session/指令/报价/快照/策略/转人工/审计 业务实体 |
| 01b | [债券基础字段](01b-bond-master-fields.md) | 141 字段目录、来源(财汇/中债/Oracle)、可用真实表、缺口 |
| 02 | [前端接口契约](02-frontend-contract.md) | 三屏 UI ↔ 后端 API + mock 数据规格 |
| 03 | [总体架构](03-architecture.md) | 分层、通道演进、内外网、数据流、复用、部署 |
| 04 | [redroid RPA 层](04-redroid-rpa.md) | 元操作、状态机、会话括号、双闸门、编排(借鉴 we-mp-bot) |
| 05 | [工作流状态机](05-workflow-statemachine.md) | 谈价状态机、L1-L5 归属、跨日、意图、转人工、话术 |
| 06 | [dify 工作流](06-dify-workflow.md) | 意图分类抽取 / 截图OCR / 智能问答 |

## 三条最重要的重新定位

1. **目标变了**:机器人不追成交,只**收窄价差 + 拿实价**,拿到即转人工。别用成交率考核。
2. **通道换了**:聊天传输层 = redroid + 企点 APK + RPA(frida 读 + adb 写)。与"ODS/财汇数据线"互补,两条线在 session 层汇合。
3. **分工写死**:模型只做意图分类+抽取;报价/让步/比大小全归规则引擎(中文歧义大,比大小绝不给模型)。

## 与现有材料的对账(设计时必须知道)

- **v1.0 需求书是权威**,已取代 策略说明v0.1 / 全面审视v0.2 / 配置项最小集。旧版的"单轮超时/并发上限/可成交性预检/成交底线"均已作废。
- **前端设计稿的策略参数过时**:`.dc.html` 里是 对手让步空间/最小可接受量/最大价差(旧),要改成 v1.0 的 **起始偏离/探价边界/步幅**。
- **谈价核心引擎复用 bondtrade demo**(`git.frontnode.net/anlin/bondtrade`),按 v1.0 改造(去成交、探价边界、拿实价即转)。gateway 抽象干净(bind_sender/handle_message),换 RPA 只换 gateway。
- **大量复用同业存款项目**(ibquote):邮件穿透管线、机构库+别名库、三级分流、工单复核台 P0/P1/P3、黄金样本、审计版本链、OCR、Oracle 授信。立项按"复用为主"。
- **部署**:生产=鲲鹏 ARM64+麒麟+GaussDB,ARM 上企点 arm64 APK 原生跑(无需翻译层);x86 开发机用 `qtrade-redroid-installer`。

## P0 待确认(不定就没法写真实代码,mock 用占位)

1. OT 推送报文字段口径(要带列名导出样例)。
2. 含/不含担保可投量计算口径(样例差 3.6 倍)。
3. 卖出(OFR)方向参数与话术是否与买入镜像。
4. 多券并发 vs 串行(**建议串行**)。
5. 交易员→QT/QQ号映射表来源(疑似缺失)。
6. 财汇实际表名/字段名(datadict.finchina.com 回填)。
7. 主键对齐:财汇发行人代码 ↔ 机构库人行联行号 ↔ QT机构名。
8. 内外网穿透能否不走邮件 / 策略引擎下沉外网 A/B/C(建议 C)。

## 目录
```
docs/    本套设计
web/     Vue3 工作台(三屏,重写 .dc.html)   ← mock 阶段建
server/  正式后端：认证、事务询价、Chat运行器和公共行情；mock服务已删除
redroid/ RPA 层实现                          ← 后续
dify/    三个 workflow                        ← 后续
data/    财汇/中债/Oracle 对接与建表           ← 后续
```
