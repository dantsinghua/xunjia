# 安琳的询价改造执行交接

2026-10-18 最新安排：先完成[非Dify阶段交付](2026-10-18-non-dify-checkpoint.md)，Dify DSL、生成器和紧耦合测试的本地修改暂留。shim + qwen-max 两阶段的新规则与流程现已按用户最新修订获准实施；先处理执行故障，再做有界API测试，不启动全量环境或继续打包。下文的模型/包记录属于完整本地工作树历史，不能当作拆分提交或最终发布的通过证明。

## 2026-10-18 V2.2 当前执行

最新发布阻断是实际模型语义：v22代表16项为11成功、5失败、11个clean样本，四Tool资料内容补证1/4，零超时及源码漂移。成交承诺、重复BP计算、额外公开估值、超步长报价和重复事实字段均被原门拒绝；资料传递链已通，但模型内容尚未全部可靠。当前模型源清单为 `ea35be715a61f4304c3c418d3ff043252e59a45bf84ee94fe6093f91dd087985`。失败保留，不进入正式47或生成profile；正在评估提示职责精简，不改原校验或测试预期。运行详情在用户指定外部 `STATUS.md`，该目录不推Git。

已关闭两个程序缺口：明确报价片段遗漏后沿用旧FINAL凑量的问题，通过独立220项验证；A实际公开Tool结果与事实同事务持久并提供给同分析版本B，已通过独立218项及四工具内容门的反例验证。当前18core清单 `core-model-freeze-analysis-handoff.json` SHA256 `f317913368c77b6bdbc2ac928632fd344c498968a32caf666ad78baaaac9c4b5`，Engine `b5c8eb363ad7c4f9e093116cd5d4606ca4887de6da0490ab9846047cba2ff17a`、Store `a01cafa1102172c0fb1dd46a82e2ae820010a4e2ba25b4b9a4fe68f586f95521`；同版真实OceanBase原13项7.877秒通过、19输入无漂移、专用schema已清理。下文bcc及更早清单是修复前历史，不能冒充当前验收。

安琳已授权本仓库main实际切换、测试、提交及GitHub发布。最新业务规则见[Codex交接](2026-10-17-codex-handoff.md)，实装与验收见[切换进度](2026-10-17-v22-cutover-progress.md)。现役app及部署CLI已使用V2核心，15张新表；历史统一轮1029唯一Python、69Node及构建全绿，但后续变更仍须最终同版全量。

此前18核心清单 `core-model-freeze-model-interface-final.json`，SHA256 `bcc6532f8ccd543adfab4658fb06e189f25cc6a14b97e370c5c943faef9e0d16`，包括A证据限定修复、B正文限定重生、实际写事务租约屏障、报价范围投影及现金原文口径核验。该快照新接口及相邻合同独立303唯一节点通过，真实OceanBase原13门6.107秒通过；其后已由上面的覆盖与资料传递修复取代。

模型按安琳指定使用本机nanyin-deposit-agent的shim/盈米内网Workflow。历史v18正式47例42成功；v19至v22为继续改造后的代表16例，不拼入原47统计。当前显式价格别名与资料/询价双段wire已通过54项新增独立合同，原证据、经济和全文校验保持。尚无生产聚合profile，新的完整47、实际持久RETRY及最终全量未完成。

官方麒麟V10SP3 aarch64用户态中的离线依赖、运行时及系统Python3.7引导已实测并独立复核。全部工作完成后重建含CPython3.12、全部应用依赖、构建前端与配套组件的新包，再跑包内全门和实际安装启动；QEMU/PRoot用户态验证不冒充客户物理ARM主机验收。GitHub基线仍为外部发布的aad6863，本轮增量提交/推送未执行；安琳已停止追加敏感资料审查，旧历史保留。下方2026-10-13数字均为历史限定证据。


## 2026-10-13 最终交付状态（优先于下方历史）

改造已精确集成原目录 `/home/anlin/work/xunjia-agent`，不是仅存在于隔离worktree。集成185项更新、9项mock文件删除，逐项基线核对无冲突；原文件备份 `/tmp/xunjia-integration-backup-20261013-6nl106go`。原目录真实cache DB、mail、state及用户原有修改均保留，没有commit/push。此后若继续开发，以原目录当前状态为准，不要再将隔离根旧文档覆盖回来。

原目录最终独立统一门 `/tmp/xunjia-verification-5mksioj3/result.json`：passed=true、source_drift=[]，Python702项、Node52项、vue-tsc/Vite全部通过。256文件前后完全相同，其中252文件与冻结清单一致，额外4个是原目录历史性能JSON。冻结清单SHA256为 `af3b276f22512a73bbba2c324e4f22d52807641722301ea253cd8bfb073fc02f`；证据已归档 `test-logs/final-original/`。

精确命令为 `/tmp/xunjia-clean-install-20261013/bin/python tests/run_verification.py --mysql --load --browsers-path /tmp/xunjia-playwright-20261013`，cwd原目录。Python干净环境来自完整开发lock，pip check通过；原目录npm ci和build实际通过。不要把历史候选绿灯相加。两后端各5×10万合成历史负载通过；详见[最终交付](final-delivery.md)与[独立验收](acceptance-report.md)。

终审第四轮APPROVE，F01–F18全部关闭。真实新Dify1.17.1加载冻结模块后的整链及独立只读复核通过：原生run、30BP真实工具结果、最终签证digest、SQLite持久命令/FakeTransport回执、六次工具预算及四项403。既有Dify0.15.3、现役采集和其他业务容器不动。自建验证project的精确停止及保护对象核验见[dify-final-freeze-report.md](dify-final-freeze-report.md)的收尾记录；保留数据及证据。

本轮没有使用实际auth.key/auth.sm4_key，没有真实MQ请求。MQ开发测试值是 `synthetic-business-key` / `0123456789abcdef`，只能用于测试，不能部署。用户提供的临时token和AK/SK没有复制入仓库或拿去调用。

现场边界：真实OceanBase/门户/MQ/OSS/企业邮箱/企点native端仍待现场配置联调；主数据按用户“先留口子”裁决通过ReferenceDataProvider接入，缺数据不造数；Dify需正式升级并绑定真实模型。采集v2上线需替换旧xunjia poller，行情配置采用管理员静态部署。本轮0红结论仅限已列测试合同及版本，非生产已上线声明。部署复现使用 `requirements.lock` / `requirements-dev.lock`，不更改原目录旧venv。

## 下方为实施历史快照

下方“active”“未集成”“待冻结”等均为当时状态，当前结果以上节、最终验收报告及任务列表为准。

本文件记录已发生事实；任务状态以claude-mem的implementation-20261013列表为准，隐藏goal保持active。用户已明确授权开始、多子agent并行和0回归/0红，不要回到方案审批阶段。

实施根：`/home/anlin/.codex/worktrees/b372/xunjia-agent`（原生工具创建，branch `codex/xunjia-chat-remediation-20261013`）。当前任务cwd仍是原目录，所有命令显式workdir。原目录`/home/anlin/work/xunjia-agent`尚未改业务实现；最终精确集成前核对baseline哈希，不能覆盖用户并行编辑。基线188源码文件和未提交改动已复制，路径见baseline.json。没有提交或推送。

当前测试环境为隔离区`.venv`，Python3.12.3。已安装FastAPI/httpx/pytest/pymysql/pyyaml/cryptography50.0.2/openpyxl/playwright。初次误用系统Python3.14的bootstrap环境已移到独立/tmp保存，工作区不再使用它。当前FastAPI0.143/Starlette1.7/pydantic2.14略新于原环境0.141.1/1.6/2.13.5，依赖最终需锁定并核兼容，未修改原环境。浏览器缓存`/tmp/xunjia-playwright-20261013`。

三个长期角色槽按批复用，最多3子agent且不递归。`acceptance_tests`独占tests/acceptance、tests/conftest.py、web/tests/acceptance与测试runner，主审和实现者不能改松这些门。所有真实网络被测试guard禁止，仅允许本测试创建的端口。它已记录121绿10红基线，并按主审基于规格裁决修正旧fixture和源码字面量断言；新行为检查是真实入口。

已交付可读报告：sts-report.md、market-report.md、frontend-report.md、acceptance-report.md。STS实现`implement_sts`完成，市场`implement_market`完成；前端`implement_frontend`正在第二批授权预览UI；`implement_bond_tools`正在四工具（server/bond_tools.py/reference_data.py）与开发者测试。首批STS独立+开发+旧目录58绿，市场32绿，前端旧8+开发22绿/build与audit绿，独立浏览器14绿。一次统一验证发现源码正在新增，正确以source_drift拒绝最终通过，不能引用作总验收。

主审已建立server/auth.py、db.py、schema.py、repository.py、business.py、domain.py、routes.py、app.py。SSO兼容门户MD5签名，应用会话为独立HMAC凭证；生产/api统一认证，默认缺配置拒绝。Repository测试显式SQLite、生产mysql/OceanBase；MySQL不自动建表，initialize_schema是显式迁移入口。业务8表已定义，真实MySQL/OceanBase尚未验。当前app成功挂preview/send/status/receipt/bot/monitor只读入口，**还不是完整生产运行器**。

固定HTTP合同：

```text
create_app(database_url=...,auth_config=AuthConfig(...),enable_background=False,transport=None,chat_client=None)
app.state.repository.bind_account(owner_user_id,account_uid,workbench_id,transport='oss')
POST /api/auth/sso {userName,nickName,ts:string,sign}
GET /api/auth/me -> {ok,data:{user_id,display_name,account_uid,binding}}
POST /api/im/rfq/preview {request_id,items:[{code,name,peer_ids,our_side,volume_wan,opening_yield_pct,limit_yield_pct,step_bp}]}
POST /api/im/rfq/send {batch_id,expected_version,request_id}
  生产拒绝旧{items}直发；确认仅queued，不直接发外部。
GET /api/im/rfq/status; GET /api/monitor/{summary,counterparties,chat}; POST /api/bot/toggle {status:'正常'|'停止'}
新增约定待挂：POST /api/bonds/resolve {selection} -> /api/bonds同形raw rows，由前端统一normBond
新增约定待挂：GET /api/im/inquiry/floor -> {ok,data:{items:[{code,kind:'bp'|'yield',value,revision}]}}
WS /ws/quote：首帧{op:'auth',token}，回{op:'auth',ok:true}后订阅，失败4401/4403；token不进URL。
```

FilterSpec已和前端锁定：keyword/chips(现有中文title→索引)/prices(PRICE_KEYS→lo,hi字符串)/channel/broker/groups；GET filter=JSON与selection.filter一致，except为市场code。策略GET保留templates/active_template_id/system，POST使用start_offset_bp/boundary_bp/step_bp/strict_match及S1/S2，无reply_timeout。

最新主审定向47绿覆盖auth/db/batch，但完整dispatcher、ChatRunner、双通道、正式market routes、私有底价、导出仍未落地。InquiryService.preview/confirm/commands/sessions/set_bot/receipt已存在；需要继续强化未决claim、停止屏障、版本一致性、社交/澄清/记忆，并让API接真实后台。current set_bot需进一步核幂等版本/预览失效；receipt已有账号peer验证和旧控制版本不推进。

必须接主审的市场联动：_to_delta保留存在字段的null；_qb_loop刷新STATIC_GROUPS并发布旧/新组并集；_qb_mode显式激活且共用gateway.load_creds；quote_probe共用merge_sid、握手后关闭不能判通过。独立市场实现已把静态/流水/checkpoint同事务、写失败回滚/重放、类型/日切修好。collector/migrate_quote_truth.sql是显式迁移，启动不ALTER。

旧imlink/routes裸router用于离线兼容：主审已修上传无ACK不能ok、失败具体原因保留、legacy状态GET/DELETE。新生产app不挂旧无身份router/DELETE；旧mock.mock_server入口仍需封住真实外发/GET副作用，不能遗漏绕过入口。

整体尚需完成：事务后台和Chat会话/SSE/上下文/行动审核，Dify锁版可导入交付包与隔离验证，现场数据薄接口，真实邮件/OSS统一收发与采集端取消/去重联动，market/WS与owner数据隔离，xlsx，部署依赖锁/迁移/安全入口。真实主数据按用户明确只留现场接口，不伪造。现有Docker有0.15.3 Dify与真实采集容器，只做过docker ps，不得重启它们；若做升级验证，应新建隔离环境，不能替换原卷。

最终统一命令由QA维护：`.venv/bin/python tests/run_verification.py --browsers-path /tmp/xunjia-playwright-20261013`，验证前冻结所有写者，检查pytest/前端unit/dev/build/browser及源码hash稳定。旧手工ADB echo脚本不是pytest用例，pytest.ini已明确testpaths=tests，不以skip隐藏业务红测。

## 13:48续记（仍在实施）

前端第二批停写，定向33绿/build绿/独立浏览器13绿，报告已追加。该executor已复用下一批transports/receiver_ledger/relay.protocol；勿与其文件冲突。债券工具executor正在dify_client与dify/chatflow_1171交付包；公共工具预算扩tool_budget_ref独立票据由root后台计数。QA持续独立扩门。

root新增server/market.py、market_routes.py、quote_runtime.py：公共行情投影、本人状态叠加、同FilterSpec先筛选再分页/resolve/xlsx；当前在provider公共快照上过滤，大表SQL优化未落。行情/静态/WS投影严格白名单，独立11绿；WS首帧auth、Origin/过期关闭。QBMarket显式adapter已写，但尚未接app lifecycle/真实源；旧mock入口还没封。

root新增runtime.py Dispatcher短事务claim/网络外发/unknown对账/stop cancel，QA扩展13绿。新增chat_runtime.py ChatRunner：turn事务、context/validate/history、BUY/SELL边界和动作检查、最终正文签证、记忆/命令原子提交；首批11独立绿。**尚待HTTP能力端点和后台生命周期，BP目前缺估值先handoff，需接可靠工具估值；工具预算未落、history日期范围未实现、更多额度与多券歧义门待QA。** 不可称完整Chat已交付。

InquiryService新增ingest（private归一message）/conversations/takeover/resume与源水位，schema新增xj_turn；set_bot幂等并使binding version失效，首次确认固定授权快照；receipt把真实sent文本落xj_message。生产routes已给state中文stage/quote/phase和交易板字段，market_bid/ofr暂空待真实行情叠加。异常回执只补事实；控制/会话版本一致才推进。

近期测试是模块证据，未冻结写者，未再跑全量终验。所有改动仍在隔离worktree，原目录未实施。无真实外发、无commit。

## 14:17最新裁决和实施

用户明确“mock服务直接删掉，以后不会再用”。已在worktree精确删除mock/mock_server.py、start.sh、README.md、build_seed.py、data/bonds_seed.json；生产模块迁为server/market_cache.py、server/qb_source.py、server/quote_channel.py，无compat shim。QA已改独占验收及旧渠道测试imports，19绿；运行cache DB没删。此前retired stub已删除，不要恢复。原目录最终集成仍待baseline冲突核验。

ChatRunner独立已36绿，另真实SSE→Runner、HTTP私有能力/公共预算、后台lifespan均有独立证据。修复了源文本方向/超量/交割变化被忽略、社交重置澄清额度、授权前迟到消息、跨日旧授权、history+WAIT合法分支被拒。context含独立tool_budget_ref，每turn6次跨history/重启共享。memory_patch严格仅expected_revision/summary/本轮唯一evidence_refs。BP已增加可信provider snapshot中CNBD verified maturity估值(0~3日)换算，缺/过期/净价/行权转人工，尚待专项QA BP正反例。

app.py已装固定AgentHTTP、公共工具票据、后台dispatch/chat/market/inbox lifecycle（enable_background显式/env默认关闭）；transport_config.py显式最多5个本人profile，密钥用env引用、0600配置，新inbox.py落业务事务后ack。transports和receiver_ledger仍由implement_frontend executor收尾；Dify executor正做新隔离1.17.1真实导入，官方镜像daemon代理不通，采用用户态archive+docker load，不动既有容器。

独立MySQL8.0.46六门真实通过：RC隔离/回滚/绑定唯一/并发confirm/并发dispatch/同message+turn一次。测试新容器已精确停删；证据/tmp/xunjia-mysql-acceptance-az2phz_a。生产db.py改READ COMMITTED；schema增加所有业务索引与utf8mb4_bin，真实OceanBase现场仍未验。QA runner新增--mysql同run哈希门。

root新增manual_send：同事务接管、稳定requestID，允许明确人工消息在bot关闭时发，仍禁成交/泄密；Dispatcher manual_send只允HUMAN同version。新增link_routes.py仅本人固定OSS/mail档案，目录/通讯录读取、只读test、expected_version切换须bot停止且无queued/pending/unknown。旧Link.vue已简化成这两个固定通道卡片，移除编辑账号/机密与旧模式表单；通道机密改部署配置。root也接了底价POST(/api/im/inquiry/floor)、pause/resume、停止/归档、monitor真xlsx下载；移除监控“人工授权成交”和假限速文案，底价放宽改提示先停/对账后重新确认。相应前端build/开发回归还须跑最新根。

待办明显缺口：双通道接线的独立端到端、采集端v2集成、Dify真实导入运行、私有history复合游标与UI往前翻、聊天交易板真实数据/详情DTO核验、每findings整改对照/依赖锁和部署CLI文档、终审/最终源码冻结全量runner含mysql、原目录精确集成。仍禁止汇总不同批绿灯宣称全量0红。

## 15:50后续收口（隐藏goal仍active，未冻结）

用户最新另给its-accesspoint源码与MQ地址，随后问“实际auth.key/auth.sm4_key”。root已清楚答：**没有使用真实密钥，也未调用真实MQ**；最新开发测试仅synthetic-business-key / 0123456789abcdef。勿把这些合成值说成部署key。源码已SSH只读clone到/tmp/xunjia-its-accesspoint-20261013-ac26e9ed，HEAD ac26e9ed72f816e2a19b9e1d01c9572bd8d214a4。token.go/handler.go协议同原HTML，new MQ_STS_URL仅允许精确完整http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts，默认nytoken HTTPS保留。imlink/sts_request.py按env生成0600拒覆盖curl配置，不打印token/不自动请求；docs/implementation/sts-mq-update.md。QA MQ16+既有STS27共43独立绿；Go工具未安装，未声称跑Go测试。

当前角色：acceptance_tests独占QA/runner；implement_frontend先完成47采集开发门、又完成44前端+4真实QA路由开发门，现因终审R17/R18重新启动仅修采集窄项；market_pagination已完成停写（SQL分页3文件），implementation_docs已完成停写（remediation-matrix.md原始40/40及collector-integration.md）。final_security_review完成第三轮：原F01–F16关闭，新增F17 held过期query永久pending、F18人工屏障不能挡“接管前授权但尚未到达”的更高版本cmd，仍REQUEST CHANGES。QA已复现held红；不能提前冻结或complete。

**F18最新最小合同待root与worker落地**：采集端每peer持久manual_fence_id + manual_fence_version（单调控制版本，不猜MAX后台cmd版本）。可信manual事件签封顶层携这两项，两路/重放固定，不改变native payload/hash。采集端拒绝所有不携当前fence的send/manual_send，即使cmd.control_version更高；cancel/query仍能核对。根在可信manual控制事实收到时更新xj_conversation.receiver_fence_id/version并HUMAN，旧fence版本不能覆盖新；只有明确resume/新batch confirm/明确人工消息之后构造的command从conv快照携这对fence才可发。root还计划human_lock字段，区分未授权初始HUMAN(可首次确认)和显式API人工接管（不能被普通confirm绕过）；现代码仍用social_authorized=0+control_version=1判断初始状态，新增fence时必须改好。worker负责protocol/ledger/Bridge；root负责schema/business/Inbox/command body。不要只MAX+2，不能自动清人工屏障。

采集echo已47开发绿+20独立绿：exact native ID关联；callback中out暂存，held无native且query不执行；超时/重启/unknown外发按manual并持久闸；精确echo释放原始输入；可信human立即上行。worker正修held过期从未执行应明确终态且不调native（F17）。Bridge有replay_events≤200/双路journal，各自恢复；顶层transport_replay=true。root Inbox传force_historical而不改native event_hash，旧回放不触自动回复。

root最新实现：
- schema增xj_turn/tool_budget_ref，xj_command.next_check_at/batch_id索引、xj_message.native_message_id索引；db MYSQL READ COMMITTED+utf8mb4_bin。
- Dispatcher公平选到期未决每轮最多5并行，5秒复查，不固定前50；stop将next_check_at清0。
- receipt冲突sticky unknown；人工裁决需owner/confirmed/证据引用/expected_result_hash，HUMAN/HANDOFF+水位取消旧queued，不重发不复谈；迟到unknown仅审计不擦裁决；已裁决旧证据重放幂等，新真实冲突可重开。ReconcileModal/未知列表/HTTP都落并独立浏览器2绿。
- native_message_id+receipt.native_id合并真实out echo，仅按精确ID+正文/owner/peer，重复行标duplicate，聊天/模型不重复读；关联前已发生人工接管不自动撤销。
- Chat源价格不能靠模型省observations绕过；有新价须唯一校验口径，净价/行权/条件改变/超量硬handoff；无新价可引用仍有效未撤回已核对手价（300秒，验证时复查）。BUY/SELL、裸code/市场后缀、公开方向/数量单位均已补；每券authorized_at防新券授权前迟到消息。STRICT本轮刚加policy.strict_match默认true与Rfq保存策略传参，正在回归。
- 初始首句name限制并共用public_text.py红线；外部unassociated out接管；根原初始HUMAN按social0+control1可首次授权推进水位，显式takeover不绕过。
- 可运行Chat SQL仅有效绑定/会话、每会话首条、无未决/运行turn，停止终结旧queued，消除饥饿。
- Diagnostics安全code/error_type/计数/时点/turn关联、owner过滤/api/runtime/status；Inbox各route不吞异常且并行；health拆database_ready/automation_configured。QA6门绿（含真实模型异常）；不记录异常全文/秘密。
- MarketService正式QB完整FilterSpec SQL/count/keyset LIMIT51，精确code IN，单broker加锁快照2万上限，全选/导出10万；root已将monitor板面改可见券分批500 resolve，status_commands只读当前pending/unknown、汇总O(S+U)、confirm batch_id索引。market_pagination报告，独立真实SQL14绿。
- 聊天GET cursor(occurred_at,message_id)复合游标+nextCursor/hasMore，前端更早50/回最新、poll保历史页；独立9门+browser1绿。
- 原按code DELETE生产已移除，POST /api/sessions/{session_id}/archive，本人停止/关闭且无在途，审计不删；api.archiveSession已接，Monitor归档按钮root刚补。
- export start/end必填、可peer，MySQL SSDictCursor/SQLite fetchmany500，XLSX write_only、公式字符串，上限100k或20m字符。root刚接Monitor当前peer/可全本人、local日期；禁止宣传不存在的脱敏/标签导出。
- CSP/nosniff/referrer-policy统一secure()含401早退；XUNJIA_WEB_DIST可显式serve真实web/dist(同源/assets+页面)，有独立构建产物门。
- qa_routes.py已挂，只读明确“查券/报价/我的在谈概要”，真实Market/BondTools，不模拟成交/任意查询；Chat宣传已收敛。策略首次personal默认ID可保存，configured=false始终未保存草稿；S1自动量/S2自动跨日旧控件承诺移除。保存模板只改defaults，不动已确认政策/版本；root刚补start>=boundary>=0。

QA新增能力门：归档/导出/CSP/实际构建服务17绿；真实1001行500/500/1/0导出、100001行/20m+1字符400；策略browser2+真实HTTP1绿；QA HTTP7绿。R32负载**每环境**5账号×10万合成历史、5线程20轮共300 reads+10万行导出：SQLite P95 .051745s/export5.748s；隔离MySQL8 P95 .627508s/export11.343s；分别/tmp/xunjia-load-acceptance-xfzorxmd、itnzgs_q，都是合成环境，非OceanBase承诺。MySQL测试容器已精确stop/rm，原有库未碰。

Dify实际1.17.1新project xunjia-dify-verify-20261013（9新容器，报告完整ID），真实31节点导入/双轮epoch1→2+工具、root真实ChatRunner签证→queued→FakeTransport已验，独立QA又跑两轮及mode=ro账本复核。**旧root_backend进程只加载启动时源码，后续根改码不算同版本证据；最终freeze后须新root实例/新合成SQLite重验。** 不碰既有0.15.3。精确收尾停命令在dify-import-report.md；环境先保留供最终验证。

临时开发红不能误读：新增batch_id时confirm出现14values/15columns，已精确修为15个占位；对应后台82 fresh passed。曾per-bond SQL单引号SyntaxError也已即时修，53 fresh passed。旧日志保留。root当前运行session42071是严格量/前端最终contract定向测试+web test；session93918是归档/export/市场/HTTP定向；需收尾读结果。未跑冻结版全量。

最终runner已增强：`.venv/bin/python tests/run_verification.py --mysql --load --browsers-path /tmp/xunjia-playwright-20261013`，含pytest、两数据库负载、独立MySQL、web unit/developer/build/browser、build后production-web，前后源码哈希；所有写者冻结后执行。仍需R17/R18关闭、剩余代码/文档对照、格式简化可审阅、最终终审准予freeze、同版全绿和Dify root复验、原目录baseline哈希核对精确集成（不commit）。原始source目录至今未集成改造；不得写“原项目已改完”。
