# 安琳：双通道持久协议与采集桥交付

工作区：`/home/anlin/.codex/worktrees/b372/xunjia-agent`。本批只写 `server/transports.py`、`server/receiver_ledger.py`、`relay/protocol.py`、后续获授权的 `relay/collector_bridge.py`、`tests/developer/test_transports*.py` 和本报告。没有修改 STS、business/runtime/app/inbox 或独立验收；没有提交、推送、访问真实邮箱/OSS/STS、启动真实账号或修改外部 QTrade 工作树。

本批最新裁决：**采集源事件持续发布到所有已配置上行通道，command.transport只决定业务命令及其回执的固定路线**。上行两路共用event_id/source_seq/事实账本，各有独立待发状态；不因一条网络失败跳过另一条。命令不自动换路、不双发价格。此裁决替代本批较早的“Bridge构造primary_transport决定唯一上行”方案。

## 实现及入口

### 严格v2协议

`relay.protocol.Binding(owner_user_id, account_uid, workbench_id, seal_key, sign_key)` 固定身份，密钥不进入repr。用户ID支持SSO的可打印Unicode身份，不混同对象路径字符集；企点号/工作台/命令对象标识另行限制。seal_key为16/24/32字节，HMAC钥至少32字节，两者不互相推导。

`Codec(binding, clock=...)` 的 `pack/unpack` 使用 AES-GCM，保留原 `nonce(12) || ciphertext || tag(16)`、空AAD的二进制布局；用原 `imlink.crypto.open_sealed` 做过互解测试。JSON内部另有HMAC-SHA256，覆盖全部签封字段。规范JSON为UTF-8、键排序、无空白、禁止非有限数；重复JSON键拒绝。旧无版本/身份/摘要/签名的对象与回执不接受。**密码布局兼容不等于旧采集器业务协议兼容；两端必须同时升级。**

完整签封包括：`version=2, namespace=xunjia, kind, message_id, owner_user_id, account_uid, workbench_id, peer_id, command_id, content_hash, payload_hash, control_version, created_at, expires_at, payload`，以及封内signature。事件管理员重放或过TTL续签另有顶层 `transport_replay=true`。

- `kind`：command/cancel/query/receipt/event/catalog/contacts/manifest。
- `content_hash`：命令/回执沿用业务服务冻结摘要；事件/目录为原始payload规范摘要。另有payload_hash绑定实际载荷。
- 下行command仅支持内部kind=send_text/manual_send；op只能send_text，不能借人工入口改成其他操作。真实人工/机器人权限由根业务服务核验，采集端仍核身份、版本、有效期。
- command/query/cancel的payload保留原命令ID、身份、正文、摘要、控制版本和原expires_at。查询/取消签封可以续期，**不能刷新发送动作本身的授权期限**。
- receipt载荷为四身份字段`command_id/account_uid/peer_id/content_hash`、status、evidence，可额外有明确native_id；不携带业务正文。只有实际native证据能成为sent，SMTP/OSS接受只能accepted/pending，异常或超时为unknown。

### 传输对象

```python
OSSTransport(binding, oss_http_client, state_path,
             clock=time.time, ack_timeout=90, envelope_ttl=604800)
MailTransport(binding, mail_config, state_path,
              smtp_factory=None, imap_factory=None, 同上时间参数)
TransportRouter({(owner_user_id, account_uid, 'oss'|'mail'): adapter})
```

Router也允许单绑定的route键，但每次仍核owner/account/workbench，不能靠键别名跨绑定。`send(command)/reconcile(command)/cancel(command)`返回四身份字段、status和evidence。未知结果不重投业务send；每30秒可以产生新只读query控制message_id，查询的command_id/hash/version保持原样。终态冲突按unknown处理。

OSS生产注入现有STS `OSSHTTP`；适配器只使用其 `request(method, name, body=None, timeout=20)`，没有长期AK或备用路由回退。外部采集桥的LIST/GET由现役已认证对象store显式注入，不在本批偷偷另启采集服务。

| 类型 | OSS对象 |
|---|---|
| 原命令 | `wb/{wb}/in/xunjia/{command_id}.bin` |
| 取消 | `wb/{wb}/in/xunjia/cancel-{command_id}.bin` |
| 对账查询 | `wb/{wb}/in/xunjia/query-{command_id}-{30秒轮次}.bin` |
| 执行回执 | `wb/{wb}/ack/{command_id}.bin` |
| 上行事件 | `wb/{wb}/out/xunjia/{account_uid}/{source_seq:020d}.bin` |
| 有序清单 | 同上目录的`manifest.bin`，payload为first_seq/last_seq |
| v2本人目录 | 同上目录的`catalog.bin` |
| 通讯录/组织 | `wb/{wb}/qidian/{account_uid}/contacts.json`（内容仍是v2密封对象） |

本人catalog未复用全工作台index.json：同工作台多个绑定会互相覆盖。两个账号同工作台的独立测试已覆盖此边界。

邮件使用真实`smtplib` SMTP_SSL或强制STARTTLS、`imaplib.IMAP4_SSL`；TLS证书验证保持开启。From/To只来自配置白名单，入站同时核固定发件/收件地址、唯一头、受限MIME结构及签封。固定附件`xunjia-v2.bin`，不解压、不落任意文件路径，不接受HTML/嵌套邮件/多附件。可见正文没有报价内容。协议邮件最大8MiB、密封对象默认4MiB。

`MailConfig`参数：smtp_host/user/password、imap_host/user/password、outbound_from/to、inbound_from/to；可设smtp_port/imap_port/smtp_tls/mailbox/timeout。生产字段缺失明确NOT_CONFIGURED，没有本地maildir投递伪成功。根`server/transport_config.py`负责加载仅部署用户可读的配置及secret环境引用，并与业务库绑定核对；该文件不是本批写入。

### 读取、确认与持久发布

- `poll(limit=50)`返回`[{source_token, kind, envelope}]`，最大200。收到时先验证密码、身份、摘要、版本与期限，再持久stage。**调用方业务事务提交后才能`ack(source_token)`**，poll本身不确认成功内容。
- OSS按连续source_seq读取，缺对象、过期对象、清单回退或保留窗口缺口明确报错，包含可定位的seq；不会越过缺口推进游标。IMAP只读BODY.PEEK，保存UIDVALIDITY/UID；UIDVALIDITY变更重扫但保留业务event_id去重。坏邮件持久隔离后继续其他可处理件，`quarantined()`可读受限元数据和原始邮件摘要，不输出正文或密钥。
- `queue_event(event, routes=...)`先持久原生事实、源序号及指定每route待发状态；`flush_events()`恢复发布。两adapter必须共享同一state_path。
- `queue_receipt(command, outcome)`只持久签封/待发回执；`publish_receipt`是入队后尝试flush的便捷入口。`flush_publications()`恢复回执/目录待发队列。
- `publish_catalog/publish_contacts`与`catalog()/contacts()`支持绑定范围内的目录和`org.departments/members`。所有字典均经过同一v2身份边界。
- event的业务hash不包含transport、source_token、mail_uid、uidvalidity、object_key、received_at或transport_replay等到达元数据。相同event_id不同原生事实拒绝，不按同文合并。

## 采集端账本与实际循环

`ReceiverLedger(path, binding, clock=...)`持久命令、取消墓碑、peer控制版本底线、原生ID映射及输入处理标记。短事务先落pending，再进入注入的native动作；数据库事务不跨网络/RPA等待。账号OS文件锁和SQLite claim让两输入共享一次执行；崩溃/异常后pending变unknown，仅允许查询证据，不盲目再调用native_send。

```python
ledger.execute(command, native_send)
ledger.reconcile(command, native_reconcile)
ledger.cancel(command)
ledger.receive(blob, codec, native_send, native_reconcile)
ledger.command_for_native(native_id, peer_id)  # 本绑定内精确关联，不看正文/时间
```

取消先持久cmd墓碑及同账号+peer的`min_control_version=旧版本+1`；旧ID/旧版本晚到不执行。正在native动作时立即持久屏障并返回unknown，不等到动作完成后假称已取消；之后真实sent证据仍是原任务事实。仅取消某cmd但不抬peer版本的未来业务语义不能复用此停止屏障，需另行明确。

native回调须返回有界、已核实的`{status, evidence, native_id?}`。`True/False`、HTTP200或入队回执不能当sent；本批明确测试bool回调只得到unknown。native_id只有明确实际出站ID才存映射，也通过回执透传。事件回流由该映射附source_command_id；关联缺失时不猜。原生echo先于执行回调落映射的情况，需要根库用已验native_id做迟到精确合并，不能按文本/时间匹配。

`CollectorBridge.run_once(limit=50)`实际执行：

1. 从注入的OSS `store.list(prefix)/get(name)`以及mail.poll收取签封，优先处理cancel，再query，再command。
2. 解密验真、检查绑定与期限，共用ReceiverLedger执行/核对；不启动账号或原生服务。
3. 先持久ledger outcome和待发receipt，再标记OSS输入或ack邮件；写回执队列失败不会ack，重跑也不会重做已执行native动作。
4. 分别恢复两路回执/事件队列。一条失败返回明确errors，不能阻塞另一条或迫使业务命令换路。

`publish_native`要求真实原生消息ID、peer、direction、秒级occurred_at；event_id可由账号/peer/方向/native ID稳定派生，绝不由正文派生。同文两条不同native ID仍是两事件。历史回灌显式historical=True，不能拿当前时间替代原发生时间。

## 安装桥示例（须由主审精确集成，未执行）

以下是可运行构造方式，所有外部能力均为显式参数，不猜QTrade现有私有函数名：

```python
from pathlib import Path
from server.transports import OSSTransport, MailTransport
from server.receiver_ledger import ReceiverLedger
from relay.collector_bridge import CollectorBridge


def install_bridge(*, binding, dedicated_state_dir, oss_http, oss_inbox_store,
                   mail_config, native_send_verified, native_reconcile_verified):
    root = Path(dedicated_state_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)  # 只能是本桥独占状态目录，不能对共享QTrade目录递归改权限
    shared = root / 'transport.sqlite3'
    channels = {
        'oss': OSSTransport(binding, oss_http, shared),
        'mail': MailTransport(binding, mail_config, shared),
    }
    ledger = ReceiverLedger(root / 'receiver.sqlite3', binding)
    return CollectorBridge(
        ledger=ledger, adapters=channels, object_store=oss_inbox_store,
        native_send=native_send_verified,
        native_reconcile=native_reconcile_verified,
    )
```

在现役采集端已有调度器调用`await asyncio.to_thread(bridge.run_once)`；真实消息落库后调用`bridge.publish_native(record, historical=is_backfill)`。native回调必须走现役同一bus/账号写守卫，并在实际动作前核授权/有效期、得到原生出站证据；如果需跨事件循环，宿主负责有界调度和结果查询，不能把协程对象/布尔值包装成成功。

部署**必须先精确停用旧xunjia poller**再让新桥读取相同下行前缀，否则旧消费者可能执行或用旧格式覆盖v2 ACK。没有在外部脏QTrade目录执行这一动作；现役业务/主程序/容器均未重启。移植最小协议/账本/桥需要本报告四个Python模块及`cryptography`；传输类的OSS客户端可注入，生产必须提供已按当前STS合同认证的客户端和LIST/GET对象store。没有声称现役旧collector已直接兼容。

## R07有界历史恢复操作

`CollectorBridge.replay_events(first_seq, last_seq)`是部署管理员入口，不是公网HTTP写接口，单次最多200条：

1. 从subscriber的明确缺口/过期诊断确认seq范围及同一绑定，核对源journal仍持有这些记录。
2. 显式调用replay_events；它只将已存在源记录排入两route，不创建新event_id、不改source_seq/occurred_at/正文/业务hash。
3. 事件签封重新加密签名并带**顶层transport_replay=true**；过TTL的普通event续签也自动带该标记。缓存中同ID同事实的新签封替换旧expiry，且只读标记不能降级。send命令的原expires_at不刷新。
4. 根Inbox必须将此标记当本轮只读历史处理，同时不得把重放元数据写进原生事实hash造成重路冲突。根业务在已见ID、授权前、恢复前及跨日等场景仍独立阻断自动回复。
5. 方法返回`state=queued`而非“恢复已完成”。检查每route errors，再由正常run_once恢复失败队列；以subscriber成功落库并ack后的连续水位证明缺口已闭合。

原语与操作已在本模块交付；根Inbox消费标记及业务“不唤醒机器人”门由主审和独立QA核验，不能用本模块测试替代根入口最终验收。

## 原生 out 先到时的持久处理

`CollectorBridge` 增加 `echo_wait_seconds=5, native_verification_budget=5`；等待必须大于0、不超过5秒且不超过宿主明确提供的native验证预算。宿主正常调度 `run_once()` 会在处理命令前、每次ledger动作后调用 `flush_deferred()`，也可直接调此恢复方法；不额外启动线程或真实账号。

仅同peer仍有native pending、out没有精确native ID映射且并非可信 `role=human` 时，`publish_native()`将事实存入rx_deferred并返回None，暂不分配上行source_seq。可信human立即发布并建立控制版本屏障；不从相同正文、相近时间猜归属。

| 持久状态 | 触发 | 下一状态与效果 |
|---|---|---|
| waiting | 精确native ID + peer + owner映射可用 | ready，附source_command_id；原生事实不变 |
| waiting | native结束无映射、等待超时或ReceiverLedger新实例恢复 | ready，按manual发布；提高peer最小control_version，旧自动授权不能继续 |
| ready | 两路待发意图已持久 | published；各route独立失败重试 |
| published | 重复事件、迟到映射 | 分类保持，不撤销人工接管、不自动复谈 |

waiting期间新自动命令存为独立 **held（从未进入native）**；外部返回status=pending、evidence=native-echo-classification-held。Bridge不ack该command/query原来源，query不调用native也不改为unknown。确证echo后原来源重试可执行；判manual后旧版本命令cancelled。取消held直接落tombstone；已claim的pending崩溃仍走unknown/reconcile，不可重发。manual_send不受临时echo hold，但仍受既有版本/有效期/取消屏障约束。

已判manual的屏障不会因迟到关联自动解除；只有后台观察当前人工事件后，明确授权并携带当前receiver_fence_id/version且满足control_version门才可能继续。人工屏障是控制事实，历史重放保留原屏障对，后台须按其单调版本防止旧屏障倒灌。root业务库receipt.native_id与出站echo精确去重由主审实现和QA验收，本模块只交付可信映射。

新增开发测试覆盖：回调内echo先到且后续正确关联；不同native ID人工事件阻止held自动发送；可信human立即发布；重启/超时后迟到映射不能改判；held邮件原来源不ack、重启query不触发native、echo解锁后仅执行一次；技术等待预算校验。红测`/tmp/xj-deferred-red.log`复现未关联echo及旧账号锁等待，最终绿测见下。

## 终审 R17 / R18 必要修复（2026-10-13）

- R17：held是已持久证明尚未native执行的事实。query在SQLite事务内核授权：到期持久failed；墓碑、旧control_version或未观察当前人工屏障持久cancelled；均不调用native。真正已claim的pending不能仅凭到期判未执行，仍对账unknown/sent。
- R18：ledger新增rx_manual_fence（peer当前控制对）和rx_event_fence（event固定控制对）。每次新manual事实生成稳定ID并递增该peer版本，重复事件不重建。发送闸同时要求command payload中的receiver_fence_id/receiver_fence_version精确匹配当前控制对，以及原control_version有效；更高epoch、旧人工草稿、旧屏障都不能绕过。cancel/query仍能对账，已claim native不倒称取消。
- 事件签封顶层携manual_fence_id/manual_fence_version（正整数），仅允许out、无source_command_id、peer身份一致；两者不写入原生payload/content_hash。无屏障command可省略两字段或成对空字符串/0；有屏障两字段必须有效且匹配。新事实与屏障均经原AES-GCM/HMAC签封验证。
- Transport的queue_event(event, control=...)将屏障元数据与事件同事务存入独立tx_event_control，再排两路。恢复、TTL续签及管理员重放始终使用该事件原控制对，不重算版本。根Inbox必须先验证控制对，再以单调版本更新conversation.receiver_fence_id/version；同版本不同ID拒绝，较旧控制对不能覆盖新值。历史消息不回复，但未观察过的新控制屏障仍须暂停。根resume/confirm/manual发送才从该conversation快照携带新授权，本模块不自行恢复人工会话。

开发红测`/tmp/xj-r17-r18-red.log`复现held到期死结、高epoch越过人工屏障、上行缺控制字段；`/tmp/xj-fence-peer-red.log`复现屏障peer不一致未拒绝。正反覆盖held过期/旧版本/人工屏障查询、真正pending过期仍unknown、更高epoch与旧manual草稿拒绝、明确当前屏障经真实Codec→Ledger可执行、重复/新manual版本、两路同控制对及历史重放事实hash不变、错peer及HMAC篡改拒绝。最终开发53绿；根业务接线与独立QA/终审另行证明。

## 本批验证与冻结

最新命令（2026-10-13）：

- `.venv/bin/python -m pytest -q tests/developer/test_transports_bridge.py tests/developer/test_transports_io.py tests/developer/test_transports_ledger.py tests/developer/test_transports_protocol.py` → **53 passed，退出0，5.51秒**。日志`/tmp/xj-r17-r18-final.log`。
- `compileall`四个产品模块 → 退出0；AST解析和逐行尾空白检查通过；`git diff --check`通过。新文件尚未跟踪，因此另做了实际文件检查，不只依赖git diff。
- 真调用链替身覆盖：业务preview/confirm→Dispatcher→OSS和SMTP/IMAP→CollectorBridge→native fake→签名执行回执→backend sent；两路重复只执行一次；取消优先；回执队列落盘前不ack；单上行故障另一条可继续、恢复后同ID/seq；expired已stage未ack后续签能恢复；有界管理员只读重放。
- 并发SQLite初始化、未知状态、cmd期限到期后不能改判未执行、native崩溃、在途取消、manual_send整链、原生ID归属、错账号/peer/hash/签封拒绝、UIDVALIDITY重扫、坏邮件隔离、固定收发白名单、源发布/回执断点恢复均有开发者回归。
- 本批未执行全量项目回归；独立验收/终审仍由各自角色执行，未修改其测试或放松门。

冻结源码SHA256：

| 文件 | SHA256 |
|---|---|
| server/transports.py | 2a4cfeecb668266bf94812a3b27fa72a762e550517f3c8600ff26b042e0f2b50 |
| server/receiver_ledger.py | 4ce7e258eabbca9bb921bd5e2dcfa1ec24daa10886d413fba7889e8d4be3e609 |
| relay/protocol.py | 8f2e95c27c99e33be33d3b349e6828b103e812b14cea25cffca80e109b27910e |
| relay/collector_bridge.py | bb2108c8f05b51e0b9c3268bcf7ed8c0d450ac3b3a4ebf7f9caedec24210bf7d |

## 现场仍须核实

真实SMTP/IMAP认证（当前支持账号密码TLS，OAuth/企业网关需另适配）、固定白名单、证书和网络；STS/bucket权限与LIST接线；两端v2密封/HMAC钥分发及轮换窗口；源消息保留与恢复窗口；真实native ID/出站查询能力；固定且受保护的ledger/state路径恢复；旧poller精确停用与新桥加载；Windows文件锁分支（本机实际验证的是Linux）。

这些均未通过真实账号/外部服务执行。当前证明的是可运行协议、持久状态机、可注入桥以及假端到端行为，不是现役采集部署完成，更不承诺非事务性RPA的数学意义exactly-once。
