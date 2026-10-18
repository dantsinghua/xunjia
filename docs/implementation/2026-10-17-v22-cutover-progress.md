# 安琳的 V2.2 现役切换进度（2026-10-17）

2026-10-18 阶段交付安排以[非Dify检查点](2026-10-18-non-dify-checkpoint.md)为准：先交付非Dify代码，Dify修改留本地按已批准修订另批实施验收；以下历史记录不代表本次拆分提交的全量或正式部署完成。

最新状态（2026-10-18 r20）：真实shim/qwen-max完整47/47、四Tool4/4、现金4/4及同job恢复通过；最终全仓2320唯一Python、69Node与前端构建通过；新麒麟aarch64包实际1257项通过，包源码无漂移。当前源码OceanBase验收被QA容器旧地址配置阻断，未执行数据库用例，不能沿用下方旧13项成绩。按最新下一阶段监督进行代码、测试和四DSL的完整增量交付。以[最终交付记录](2026-10-18-final-delivery.md)为当前结论；下方逐版记录保留为历史，不代表r20全环境通过。

## 已接线

- `server.app:create_app` 固定装配 `ApplicationStore / Inquiry / Engine / Inbox / Dispatcher`，现役业务只写15张 `xj2_*` 表。旧实现仅保留历史离线回归，不由生产开关选择。
- 部署管理入口 `server.manage` 同样使用ApplicationStore：显式migrate新建新表、bind写现役绑定并默认暂停、check核完整新schema。旧表/数据保留，连接成功但缺新表不能误报就绪。
- A解析与B决策分别调用两只普通 Dify Chatflow App Key；原文先落库，事实独立事务，B拒绝不回滚A；生产字段/原文片段核对与正文价格/数量/授权校验已接入。
- 分券分对手双边事实、字段证据、共享目标整量覆盖、`>=`目标交人工、停止发送仍分析、UNKNOWN只对账、三阶段旧候选屏障已联通。
- 归属纠正有投影版本核对与审计，旧错券贡献撤销，不凭纠正捏造新券价格；模型corrections解决历史UNRESOLVED。撤回按仍有效字段证据重放。
- 非法解析关闭相应经济发送但可以澄清；错误观察不使同消息另一券事实丢失。消息/决策租约、发送回执、原生回显与前端revision异步更新使用同一持久状态。
- 每个本地新日期要求逐对手确认，绝对限价沿用；BP必须有最新发布日历和可信中债快照。缺日历仍会跨日失效，提交/dispatch门也挡午夜旧报价。
- 净价保留原始口径；结算日须有对手文本证据，真实资料snapshot+公开bond_calc才可生成可追溯yield，模型自填computed_yield拒绝。
- 公开4个资料Tool免Token；HTTPReferenceProvider按显式URL/字段映射取资料，未配置明确返回缺口。未接通任何假定客户库表。
- 前端统一V2 API、双边价量/来源、原文先显、ID+projection_revision、认证SSE与断线补拉；一券暂停不影响其他券。
- 采集端ReceiverLedger新增最终permission检查；QTrade宿主/Bus/UI的最后原生动作前复核由独立批次完成，详见采集交接。
- V2 OSS通讯录使用独立 `wb/{workbench}/out/xunjia/{account}/contacts.bin`，避免覆盖仍服务其他业务的旧共享 `qidian/.../contacts.json`。
- 部署SQL、健康检查、干净env模板、包内新核心验收对齐；后台分析与真实外发开关分离，默认真实外发关闭。
- A/B 均支持私有 `history_requests` 按真实消息 ID 补取；固定快照水位、同所有者/对手/券隔离、默认20条保留，共享6次请求预算。新授权轮可引用旧轮真实历史，事实仍只写当前轮。
- 阶段总预算与持久租约联动；真实 HTTP 超时后保留同一任务、退避并可跨进程恢复，B失败不重跑已提交A。
- 新批次确认只授权本批券组，不能顺带恢复原全局暂停的旧券组。
- A对可定位的价格证据错误允许同一阶段预算内一次仅证据修复；冻结正确兄弟、事实值与解释，不增加历史/工具请求。修复期间新发生的撤回/编辑仍在事实提交时生效，旧快照不能恢复已失效报价。
- B不得把当日已确认、满足限价的最终对手报价向我方不利方向继续让价；整组尚缺数量时保留该价格，可询量或交人工处理该对手。
- B仅对已经通过经济前置校验的REPLY正文百分号/方向错误，每次任务认领在原阶段预算和租约内反馈一次TEXT_ONLY重生；冻结动作、结构化价格和其他非正文字段，仍由Dify重写并经过完整原校验。失败保持拒绝、A事实保留；修复尝试与耗时单列，不进入窗口校准。

## 八门对应验证入口

| 门 | 持续维护的验收 |
|---|---|
| 非空解析与真实语义 | `tests/cutover/test_acceptance_contract.py`、真实Dify测量（尚未终验） |
| 多券归属与纠正 | `test_acceptance_contract.py`、`test_corrections_and_delivery.py` |
| 双边价量、字段修订、并发共享量 | `tests/negotiation/`、`test_acceptance_contract.py` |
| 最终性、问句/意向/转述、版本 | `test_acceptance_contract.py`、`test_daily_and_semantic_positive.py` |
| 停止、pending/unknown仍分析、租约恢复 | `tests/negotiation/`、`test_corrections_and_delivery.py` |
| 新入站与旧候选、原生执行许可 | `test_corrections_and_delivery.py`、`test_v22_native_permission.py`、QTrade独立22门 |
| 事实/决策分事务与可重生 | `test_acceptance_contract.py`、`test_v22_recovery.py` |
| 前端异步、控制、终态与重连 | `web/tests/acceptance/test_v22_projection.py`及全浏览器门 |

## 当前已取得的证据与边界

本轮改前全tests基线805 passed；中间整合全tests861 passed，均有正常结束标记。其后继续改动，所以两者均不代表最终冻结版本。前端本批69 Node、35浏览器、类型/构建通过；采集独立22门与3条真实组件合成联合链通过，安装出的collector wheel已替换源码路径再跑联合验证。

真实模型初测36例27成功；第二轮42例33成功、9失败。后续v4完整47例38成功、9失败；v5完整47例43成功、4失败，均保留严格失败门、原始结果和失败率，未生成生产profile。已据实修正字段合同、只读工具调用、长历史补取和B状态优先级；公开资料比较数值留交易员视图，正文仍受当前报价门约束。不同版本、定向探针与正式标定不合并；真实模型持久RETRY另列。

OceanBase既有QA容器已获许可启动。在同一容器新的隔离临时cluster/租户中，15表DDL及13项真实事务验收通过；最终Store冻结后原13门再通过（5.68秒、19源输入零漂移），专用schema验后删除，其他schema及原数据保留。实际DDL发现并修复MySQL模式LONGTEXT默认值不兼容。两次13不合并计数。真实STS/OSS及客户资料源未标通过，没有真实对手IM发送。

GitHub `origin/main` 已由外部会话发布 `aad6863`；旧历史保留本地branch及已验证bundle。本轮以此为基线做普通增量提交推送。安琳已明确资料属于本人并停止追加敏感审查，不再以该审查阻塞交付。

运行记录：用户指定的外部运行目录 `HANDOFF-ACK.md / STATUS.md / RESULT.md`，不进入Git。安琳已确认其他会话停止写入；终验仍检查前后哈希，不拼接不同版本绿灯。

### 后续检查点

- 新一轮全tests899 passed/1 warning，45.26秒（仍为终验前快照）；随后增加真实引用方向反例并完成63项定向回归。
- 真实初测36例中27例完整成功，未满足30例门，失败全部保留，不输出生产聚合profile；已定位prompt/超时/程序引用方向问题，下一轮42例使用完整冻结根。
- 当前唯一测量及交付A/B包是 `dify/negotiation_v22/`；同时出现的 `dify/chatflow_v22/` 为未评审外部候选，原文件保留、不纳入主发布包，不能与实测产物混用。
- QTrade首次完整tests 3296通过/1失败（init_db新目录0750受进程umask裁为0700），修复仅新建leaf权限后最终3309通过、0失败、0跳过、339输入零漂移；独立22门与安装版wheel的3条联合链通过。QTrade工作树未提交或推送。
- 第四轮统一门946唯一Python、69 Node及构建全部通过且零漂移；其后增加history/预算/新轮/DDL修复，故仍需最终同版全量，不能沿用该快照结论。
- 最终核心冻结后第五轮统一门 `/tmp/xunjia-verification-aujvd4l3/result.json` 为 `passed=true`：1021唯一Python（主975、MySQL6、前端Python2、静态Web1、浏览器35、SQLite/MySQL负载2），69Node、类型检查与构建全部通过，356源输入零漂移，收集清单无重复。
- 同一工作树候选包真实离线新建环境、安装依赖、`pip check`及包内200项测试通过。包内200是1021的部署子集，不再累加；候选包后的部署说明修订及最终测量配置将在最终包中交付。
- HTTP上限调整前的核心清单 `core-model-freeze-final.json` SHA256为 `64f87624a8048b73f9ed0c4495da4da7ebbe50fae55a636aea5c67b4717ca006`；对应v4模型92源清单为 `8465e1684163ca87ada617766e8bc0cda05ae165d5ff9f7d87b5364fa3a3c18b`，整轮38/47成功，保留失败，不当最终成功证据。
- 其后实际模型出现175/240秒客户端超时而同一workflow于207/277秒成功的证据。HTTP可配置上限改为300秒，默认90秒不变，实际请求仍受剩余阶段预算限制；独立8项边界先3红后全绿，与重定向等联合31绿。当前核心清单SHA256为 `11b357c7f5485823c76c72130d87eb38c8701bb3ddc05904d7fb8264a0fae4db`，只变客户端一行上限；此版真实OceanBase原13门5.597秒通过、19输入零漂移。
- 第六统一轮 `/tmp/xunjia-verification-sjlvk22p/result.json`：1029唯一Python、69Node及构建全绿，364输入零漂移。之后仅测量fixture/提示词继续完善，最终全量还将核对后续差异。
- 合成测量资料日期固定2026-10-17；QA时钟现以该日中午为起点、按真实monotonic经过时间推进，恢复进程读取同一持久时钟锚。真实耗时/退避/UTC报告及生产跨日门均保持；避免未来重放被过期fixture误判。独立三进程实际退避证明保留。
- 当前v7源清单SHA256为 `62f4a77be073ea567c8d52692da81ca765cd39640d0a51c1920d3f1bbd421781`；A/Code/原47案例及统计门不变。真实跨日B-only已证明先CLARIFY、生产入队、事实不变且仍待跨日确认；该探针不计A+B标定。

### 2026-10-18最新验证与交付状态

- v7完整47例最终45成功、2失败：一次省略实际引用锚，一次B超过300秒客户端预算（同一workflow实际346.446秒结束）。原失败保留，未生成生产profile。后续A证据修复32门、B最终价守卫17项及来源撤改并发67项独立组合通过；不同组合有重叠，不相加成全量计数。
- 来源撤改修复时的18核心清单 `core-model-freeze-source-fence.json` SHA256为 `491d6c5bb651fb40cec8cb7865b11bed38db252f0c8079c4ad751b9aa560c33d`，该快照真实OceanBase原13项8.355秒全绿；其后B正文恢复已更新Engine，不能将这个旧清单当最终源码。
- 安琳指定复用本机nanyin-deposit-agent的模型桥：本地Dify使用既有 `dify-test-proxy`，经shim调用盈米内网Workflow。完整SSE探针已通过，两只专用QA已切换；物理上游模型未由普通App接口确认，shim返回的usage零值是占位，不能作为真实token计量。
- 新统计分别报告初次拒绝、一次证据修复、最终成功和总耗时。校准只采集无修复、无失败HTTP/Tool且A/B轨迹完整的样本，至少30个干净成功样本并保持整轮语义全部成功，才输出聚合profile；失败或修复耗时不用于抬高窗口。
- 最终交付增加麒麟aarch64完整独立离线包。参考现场Kylin V10 SP3/glibc2.28/系统Python3.7.9，包内携带CPython3.12、全部离线wheels、已构建Web及采集配套。当前正补旧系统Python可用的包内运行时引导；ARM/glibc仿真与客户麒麟原生实机验收分别报告。旧x86候选包不充当本次ARM交付结果。
- 部署CLI漏用旧Repository已由6个开发回归先全部失败复现；接线后与release/现役入口联合58项通过。随后独立6+开发6共12项通过，真实API已读取CLI绑定，旧库/缺索引check明确失败且保留数据；最终全量仍待完成。主README和部署文档已同步普通A/B Key、公开Tool免Token及最新发布日历口径。
- shim实际提示词与上游逐字SHA、system优先级已验证。v10引用确认完整A+B通过；v11/v12长历史A真实补锚20→21并正确提交，B仍因正文混方向/漏百分号被拒，v12两例0/2。失败保留，未运行新源完整47、未发布profile。
- 最终事务屏障修复前的正文恢复核心清单 `core-model-freeze-reply-text.json` SHA256为 `6b3d48e33d1515eeb13a69cd460664fa15eb92365a6fbb9380a0eac86c94676d`，Engine为 `6c8f3322e57d6b5fd0b8a513b4bf3e96b7475ae7a5f14db01c8d10da2010c300`。定向139项通过，真实OceanBase原13项6.453秒全绿、19输入零漂移，专用schema清理且其余4库保留；B修复校准原20+新增9独立通过。实际新模型效果、最终同版全量和新包仍待后续验证。
- [麒麟ARM交付记录](2026-10-18-kylin-arm-delivery.md)已有官方V10SP3 aarch64用户态运行、全部离线依赖/SM4/旧Python引导及独立复核证据；最终tar、包内全门和真实安装启动仍未执行，不使用旧candidate代替。

- 独立审阅实证最后租约检查与实际写事务间的窗口：新worker已接管并WAIT，旧SEND仍可QUEUED，旧HANDOFF仍可停peer。SEND/WAIT/HANDOFF三项同步红门已经锁定；现改为真正写事务内核job归属、request、RUNNING/token及expiry，旧worker返回STALE_LEASE而不覆盖新job。
- 新18core清单 `core-model-freeze-reply-fence.json` SHA256 `26afd6a9d2f3731f808afaf2f50052667e29a970334330f5d48473fd7874db2c`，Engine `5d76a9b7b457d9b64e47b015873a7bf19b178287e25953517a761e1bd362f393`、Commands `5eab3d2963f135922575f5cec864cae9aa6de65f292dd5f16f1e85c0f4ce3125`。开发定向138项通过，Dify邻接240与事务3门分别通过；原真实OceanBase13门6.32秒全绿，19输入零漂移、清理成立。最终全量/真实模型和新包仍未据此宣称完成。

### 模型接口与现金口径当前检查点

- v14正式47完整执行为3成功、44失败，零超时与源漂移，主要是A字段缺证据和B超步长。未产生profile，未把定向成功样本加入正式统计。
- A模型输出改为field/value/evidence配对，Code只无损解码成原服务端合同；窄证据/正文补丁从可信原响应复制未改字段，原校验仍执行。B报价范围使用原限价、真实已发价或授权开价、步长与最终价守卫的同源Decimal计算，不替模型选动作或报价。
- 现金净价/全价字段核原始文本口径；截短片段不能消除原标签，纠券仍绑定片段唯一原位置，跨券同数值标签或冲突片段不能洗白类型。新增反例均先失败再修复，旧合同保留。
- 当前18core清单 `core-model-freeze-model-interface-final.json` SHA256为 `bcc6532f8ccd543adfab4658fb06e189f25cc6a14b97e370c5c943faef9e0d16`；Engine `ea8beb7c1b1022156ad23684cb005fc4494c9c160650cb64a0b474bb5b2c586a`，validation `3bcd40f21e6ec2563ebcd94022aa28e91ec087e96e8c7b38d2abb0c45623d7c1`。独立联合303唯一节点通过、57输入无漂移；同核心真实OceanBase原13门6.107秒通过、19输入无漂移，专用schema清理、其余4schema保留。
- v15代表8例完整4成功、4个干净样本、零超时/漂移。双边、真实报价锚、超20条历史和公开计算通过；普通报价两例误把解释字段放入事实字段，净价/全价两例B仍违反既有先澄清合同。失败保留，不自动重映射非法字段，不以新增交易限制或改测试预期凑绿。新的完整47、真实恢复、最终全量与新麒麟包仍待完成。
