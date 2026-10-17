# redroid RPA 层设计 —— 企点 APK 元操作与动作编排

蓝本:`dantsinghua/we-mp-bot` 的 RPA 元操作抽象(经真实误发/失明/掉线事故打磨)。**抄设计不抄代码**:交互原语从 微信Linux(pyatspi/xdotool)换成 redroid+企点(uiautomator/adb),定位判据全部重定、优先 `resource-id`/`content-desc`。

## 0. 为什么这层必须先立规矩

现券询价是「**说出去收不回**」的场景:发错对手、发错价、把实价挂错券,都是重大事故。so RPA 层的首要目标不是"能操作",而是"**永不错乱、可恢复、每步可追溯**"。we-mp-bot 的七件套恰是这个最小完备集,直接作骨架。

## 1. 交互原语映射(微信Linux → redroid+企点)

| 能力 | we-mp-bot | redroid+企点 |
|---|---|---|
| 读 UI 树 | AT-SPI `pyatspi` | `uiautomator dump` / uiautomator2;**优先 resource-id/content-desc**,次 text,再坐标 |
| 点击/滑动 | xdotool | `adb shell input tap/swipe/keyevent` |
| 文本输入 | xclip + Ctrl+V | **ADBKeyboard IME 广播**(中文 + `!` 必走它,`adb input text` 不支持中文) |
| 截图 | ffmpeg x11grab / import | `adb exec-out screencap -p` |
| 读不到内容 | 截图 + 离线OCR + 拒答检测 | dump 因常驻动画**永不 idle** 拿不到(**非 Flutter**,见 §6bis③)→ screencap → **离线 OCR**(§11.13,严禁多模态)+ `REFUSAL_RE` 重试 |
| 返回/清浮层 | Esc | `adb shell input keyevent BACK` |

> **⚠️ 本行原判断已被实测推翻(见 §6bis③):企点【不是 Flutter】,是原生 View(`QQTabHost`+`RecyclerView`+`MainFragment`/`ChatFragment`)。** 真正的读取障碍不是 Flutter 自绘,而是**主界面/聊天页常驻动画致 `uiautomator dump` 永不 idle**(`could not get idle state`)。⇒ 读消息/行情卡片走 **frida hook(见 §6)** 或 **截图+离线OCR**(§11.13,严禁多模态);控件树只在静态页(登录页/名片页)可用。这仍是本层最大风险,已 PoC 验证。

## 2. 元操作清单(primitives)

### 2a 读类(零扰动)
- `list_threads()` → 左侧会话列表可见项(对手名 + 坐标/节点)
- `find_thread(peer)` → 按 peer 前缀定位会话,**区域约束防误命中**(只认列表区,别命中聊天区引用行——对应 we-mp-bot 7/9 事故)
- `read_history(n=30)` → 最近 n 条 `{dir,text,ts}`,区分我方/对方;**空读重试 3 次**
- `current_chat_title()` → 当前会话标题(发送闸门的锚点)
- `chat_loaded()` → 会话是否真打开
- `detect_state()` → 纯只读状态分类

### 2b 写类(有扰动,动作→sleep→验证→重试)
- `open_thread(pos)` → 点开会话,点后 sleep + 验 `chat_loaded()`,失败重试 3 次
- `scroll_list(dir, times)` / `find_thread_scroll(peer)` → 滚动找会话(幂等友好)
- `send_text(text, peer)` → **发送元操作,带双重闸门**(§3.3);ADBKeyboard 写入 + 回车
- `screencap(region?)` → 截图
- `back()` → 归一化用

### 2c 兜底类
- `read_by_vision(region)` → 截图 → **离线 OCR 转录**(§11.13 [NOLLM],PaddleOCR 类传统引擎,**严禁多模态**;数字零改动沿用存款项目 OCR 铁律)+ 拒答检测重试
- `sender_of(msg)` → 判定某条是我方/对方(气泡左右+颜色),截图失败保守判"对方"(不漏收)

## 3. 状态规范(七件套的地基)

### 3.1 两稳态状态机
```
READY        — 在会话列表(顶部/可定位)、无弹窗        稳定态
LOGGED_OUT   — 企点登录页                              稳定态
CHAT_OPEN    — 瞬态,只允许存在于 chat_session 括号内
UNKNOWN      — 自愈失败 → 截图取证 + 限频告警(1h一次)
```

### 3.2 `to_ready()` 归一化重力
每轮主循环、每次操作前后,把系统拉回 READY:`back()` 清浮层 + 滚到列表顶 + 验证;列表为 0 且见登录页 → LOGGED_OUT;连续 2 轮自愈失败 → UNKNOWN 告警。解决"列表漂移导致失明"。

### 3.3 `chat_session(peer)` 会话括号 + 双重发送闸门(★最核心)
唯一合法进入 CHAT_OPEN 的方式:
```
进入: to_ready → find_thread(peer) → open_thread → 【标题强校验 current_chat_title()==peer,4次缓冲重试】,失败 raise,绝不在错误会话上继续
括号内: read_history / send_text 自由串接
退出: finally → back + 滚顶,无论正常异常都交还 READY
```
`send_text` 发送前后各校验一次 `current_chat_title()==peer`(闸门1点输入框前、闸门2回车前),任一不符立即中止。**宁可不发,绝不发错。** 对应现券"发错对手=灾难"。

### 3.4 券锚一致性闸门(现券特有,叠加在发送闸门之上)
每条外发消息必带券锚 `bid {code} {简称} {量}W 估值+{N}能出么`(需求 7.3)。发送前校验:待发文本里的 `{code}` == 当前 session 绑定的 windcode。防止把 A 券的报价发进 B 券的语境。

### 3.5 成交红线闸(出口词表)
外发文本过合规闸:命中要约/成交词(要了/成交/TKN/GVN/done/按这个价做)→ 拦截,不发,记 `guard_block`。机器人永不发成交意思表示(需求 10.4)。沿用 bondtrade demo 的 guard。

### 3.6 游标锚块(幂等/断点续传)
企点无稳定消息 id → 用"最后 K=3 条 (文本,发送方) 序列"做游标。下轮从可见历史倒序找锚块,其后为新消息;找不到(洪峰顶出)→ 全部可见视为新 + `truncated=True` 标注。**发送成功才前进游标**,失败不动下轮重收。配合消息指纹去重(需求 16.5-23)。

### 3.7 原子动作 trace
每次会话操作串一个 `tid`,把 `open→verify→send→exit` 串成链写日志。对应需求的归属/留痕可追溯。

## 4. 动作编排(串行 + 括号护栏)

不引 DSL,三层串行:元操作 → 中层任务 → 顶层调度。

**中层任务 `run_round(session)`**(发一轮询价):
```
with chat_session(session.peer):
    assert 券锚(session.windcode)          # 3.4
    text = 话术引擎.render(session)          # 规则引擎算价,非模型
    guard(text)                            # 3.5 红线闸
    send_text(text, session.peer)          # 3.3 双闸门
    snapshot(session)                      # 跨日快照
# 退出归一化
```

**中层任务 `pull_and_route(thread)`**(收 + 归属):
```
with chat_session(thread.peer):
    hist = read_history() or read_by_vision()   # dump 不可用(动画不 idle)时截图+离线OCR 兜底
    new, truncated = diff_since_cursor(hist)     # 3.6;600字截断先拼接
    for msg in new:
        intent, extracted = dify.classify(msg)   # 模型只分类抽取
        session = route(msg, thread)             # L1~L5 归属阶梯(见 05)
        if session is None: to_human("归属不明"); continue
        决策 = 规则引擎(session, intent, extracted)  # 比大小归规则
        执行(决策)  # 下一轮 / 追问量 / 转人工 / 结束
```

**顶层调度**(主循环,默认数秒一轮):
1. `to_ready()` 自愈;
2. 扫会话列表 diff(预览变化/未读);
3. 有新消息 → `pull_and_route`;有待发轮次 → `run_round`;
4. baseline 期只建基线不动作(吸收登录后历史洪峰);
5. 任务返回 `wrong_chat`/异常 → 降级(仅记录,不崩)。

> **本项目 v1.0 推荐串行**(同对手同时只保持一个未闭合 session),so 编排天然简单,L3 归属即覆盖绝大多数。若并发再加短锚前缀 + 归属打标。

## 5. 与整体架构的接口

RPA 层对上只暴露两个方法(同 bondtrade gateway 契约,谈判核心不变):
- 入:`on_inbound(peer, text, ts)` → 谈判核心 `desk.handle_message`
- 出:`bind_sender(peer, text)` → RPA 排队 `run_round`/直发,限速 ≥1.5s + 随机延迟

## 6. 读取方案:frida hook vs 纯 RPA(权衡)

| 方案 | 读 | 写 | 优劣 |
|---|---|---|---|
| **A 纯 RPA(蓝本这套)** | uiautomator + 截图**离线OCR**(§11.13,PaddleOCR 类,不许多模态) | adb UI | 通用、不碰协议;**但企点主界面/聊天页 `uiautomator dump` 拿不到(见 §6bis③),读消息只剩截图OCR**,有识别误差 |
| **B frida 读 + adb 写(原推荐)** | frida hook 企点消息落地函数拿完整正文 | adb UI | 读最准无缩略;写仍 RPA。⚠️**落点见 §6bis②:老 hook 教程本版全失效,需重新逆向定位** |
| C frida 读 + frida 调 send | hook | 调 app 内 send(本地签名) | 最稳但需额外逆向 send 方法 |

**原建议 B 起步**:frida 解决"读全读准",adb UI 解决"写",双闸门+会话括号照样套在写侧。**方向不变,但下方实测把"读"这一半的落点问题暴露出来了。**

## 6bis. 实测结论(2026-09-19,anlin/qidian-rpa 真机验证)

> 环境:redroid11_x86_64 容器 `qtrade-redroid`,企点 arm64 APK 经 `libndk_translation`(libnb.so)翻译层;真实企点账号登录。

**① frida 在 arm64 翻译层下【可用】,头号风险排除。** frida-server 选 **x86_64** 版(attach 的是宿主 `app_process64`,`Process.arch=x64`);**Java 层 hook 零影响**——枚举到 **27268 个**已加载 Java 类,`Java.perform` 正常。原因:`libndk_translation` 只翻译 native `.so`(arm64→x86_64),而 dex 由宿主 x86_64 ART 执行、与翻译无关。⇒ **读取优先走 Java 层 hook**;native hook 才受翻译影响(未验,按高风险对待)。⚠️ frida 17 移除了内置 `Java` bridge,用 **frida 16.x**(本次 16.7.19)`Java.perform` 才开箱可用;server 与 python 端大版本必须一致。

**② 企点新版【不走 mobileqq 老消息门面层】——阴性结论,一次性排除全部老教程。** 把 `QQMessageFacade`/`MsgProxy`/`MessageHandler`/企点`MsgUtils` 共 **545 个方法**全 hook,自己发、对方发,**一次都没触发**;类枚举确认**也不是 QQNT kernel 架构**(无 `com.tencent.qqnt.kernel.*`)。⇒ 网上手Q/企点 frida hook 老教程(hook `QQMessageFacade.addMessage`)在本版**全部失效**,收发落点搬走了,**需专门逆向定位**——下一步:**C 内存反查**(拿已收到的消息在 Java 堆里反查承载类)+ **A hook 数据库写入**(持久化必经),**B 渲染回溯**兜底。⚠️ **翻译层下企点主进程对 frida 重负载很脆弱——两次实证把主进程搞崩:①广谱 hook(545 方法)、②全量 `Java.choose('java.lang.String')` 堆遍历**。⇒ 定位落点**必须用轻量、聚焦手段**(小范围 hook、按已知类名 choose、DB 层 hook、setText+滚动定位),**严禁广谱 hook / 全量堆扫描**(代价是把主进程搞崩、连带安琳的会话)。全量内存反查(C)这条路实测**不可行**,已排除。

**③ 真正的分水岭不是「是不是 Flutter」,是「dump 拿不拿得到」。** 企点主界面/会话列表/聊天页**是原生 View**(`QQTabHost` + `RecyclerView` + `MainFragment`/`ChatFragment`,非 Flutter、非 QQNT),但标准 `uiautomator dump` 在主界面/聊天页**永远 `could not get idle state`**(常驻动画:动图头像、红点、99+ 徽章,界面永不 idle,5 次重试全败)。⇒ **「原生 View ≠ 控件树可用」**;方案 A"纯 RPA + uiautomator"在企点上**读消息不成立**,只有相对静态的页面(登录页、名片页)能 dump。判架构用零扰动的 `dumpsys activity top`(不依赖 idle)看 `flutter/webview/recyclerview` 命中与 Fragment 结构。

**④ 对象校验(基线 §11.3 [GATE] 第一道闸)的能力边界:**
- **锚点从「聊天页标题」改为「进入路径」**——聊天页标题因 idle 读不到,故对象校验落在**名片页**(名片页较静态、能 dump,可校验 peer)+「从名片页点『发消息』进入」这条**路径本身**构成校验。**这是刻意的设计选择,不是将就。**
- **风险边界**:路径校验挡得住"搜错人",**挡不住"进对了人但中途会话被切换"**(如新消息弹窗把前台顶掉)。故**发送前那张截图不只是留证,是最后一道人工可核的闸门**。
- **真·聊天页标题强校验需接离线 OCR**(读标题栏文本)。⚠️ 按基线 §11.13 [NOLLM],**OCR 必须是离线传统引擎(如 PaddleOCR),严禁多模态大模型**。

**⑤ 冷启动 → 免登 → 搜索 → 私聊 → 中文发送 全链路已实测跑通**,固化为脚本 `relay/side_a/qidian_cold_start.sh`(见 §7)。其中**免登恢复**验证了 05「开机自恢复」的前提从假设变实测:**企点无无人扫码机制,登录态从 `/data` 卷持久化恢复(冷启免登)是唯一可行路径**——重启容器后 `am start` 冷启动即直接进消息列表,不停登录页。

**⑥【读取正线:直接读消息库——已验证成立(2026-09-19,安琳授权),取代 hook 逆向】** 全程只读、零 attach、零注入、零崩溃。
企点消息落库 `/data/data/com.tencent.qidian/databases/3007373675-IndexQQMsg.db`(前缀=登录 uin,读私有目录需 root)。实证:
- **明文 SQLite**(文件头 `SQLite format 3`,**非 SQLCipher**;同目录仅 `-org.db` 组织库加密,消息库不加密);
- 它是消息 **FTS4 全文索引库**:**`IndexContent`=全部消息(单聊+群)全文索引**、`TroopIndex`=群成员名片索引(全 type=2、content=null,**不是消息**);建表带 `compress=qqcompress`——但**本版 qqcompress 实测就是 Base64**(不是 zlib、不是加密,更不是手Q老版的复杂压缩)。**⇒「手Q加解密方法是否适用」的答案:企点这版根本不需要解密,base64 解码即明文。**
- FTS 虚表因缺 `qqcompress` 自定义函数无法直接 `SELECT`(报 SQL logic error),**改读底层影子表**(普通表):单聊 `IndexContent_content`、群 `TroopIndex_content`;
- **各字段 = base64(明文/数字)**,解码即用。单聊 `IndexContent_content` 字段语义(dantsinghua 测试会话实证):

  | 列 | 含义 |
  |---|---|
  | `c1content` | base64 → 明文正文 |
  | `c5ext2` | 发送方 uin(== 登录 uin → 出向 / == 对端 uin → 入向,据此判 in/out) |
  | `c4ext1` | 会话标识 `{对端uin或群号}ZzZ{n}`:**`ZzZ0`=单聊(前缀=对端uin)、`ZzZ1`=群(前缀=群号)** |
  | `c13exts` | 前 8 字节大端 uint64 = 消息时间戳(秒),实测与消息时间吻合 |
  | `c0type` | 类型(1=文本);`c3oid` 会话内序号 |
  | `c2contentindex` | base64 → 分词索引 |

  ⚠️ `TroopIndex_content` 实测是**群成员名片索引**(全 type=2、content=null、存成员 uin/昵称/昵称分词),**不是群消息**——**群消息正文在 `IndexContent` 里(`ZzZ1` 会话),读消息只用 IndexContent、不用 TroopIndex**。
- **增量游标**:`docid` 单调递增(实测连续),`WHERE docid > last_docid` 即增量读新消息,比 `SyncCursor` 表更简单直接;
- **覆盖面**:`IndexContent` 37525 条全 `type=1` 文本(单聊 670 + **群 36855**,按 `ZzZ0/ZzZ1` 区分),**群里的债券报价都在此表、明文可读**(实证读出多条群报价);非文本(图片/文件/语音)不在此索引库——债券报价是文本,足够;
- **只读铁律**:绝不写库。企点运行时直读主库可能读到写一半的页,**生产读取器用 `.backup` / `VACUUM INTO` 出一致副本再读**(验证阶段单行只读用 `?mode=ro&immutable=1`,风险低)。

**设计意义(建议同步进 05/06)**:企点通道的「消息本地存取」可走**旁路读库**、不依赖运行时 hook,**与微信 chatlog 同架构**(直读解码后的库)——企点 + 微信两通道读取路线**统一**;且比微信更简单(微信要 SQLCipher 密钥,企点连密钥都不用)。
⚠️ **读到的是真实业务数据,严守「不出本机」**:正文原文不外发、不贴进任何离开本机的地方,样例一律用 dantsinghua 测试会话或脱敏。对照字段语义用测试会话、不翻业务群。

## 7. 落地清单
- **`relay/side_a/qidian_cold_start.sh`(已交付,2026-09-19)—— 自动化骨架**:冷启动→免登→搜索→私聊→发送的端到端脚本,与七件套同目录。分步可单独执行(`restart-redroid`/`wait-boot`/`frida`/`qidian-start`/`check-login`/`search`/`open-chat`/`send`)、每步带判据失败即停、**发送默认关闭**(仅 `send` 或 `all --send` 才外发)、`restart-redroid` 刻意不进 `all`。三个必踩坑写在**脚本头注**:①`adb root` 后设备卡 `offline`、`adb reconnect` 救不回→容器内 `stop adbd;start adbd` + 宿主 `adb kill-server/start-server`(现象在宿主、根因在容器内);②frida-server 在 `/data/local/tmp` 持久卷,容器重启后二进制在但进程要重起+需 root;③中文必须走 ADBKeyboard `ADB_INPUT_B64` base64 广播,`input text` 打不了中文。
- `redroid/rpa/primitives.py` 元操作;`state.py` 状态机+to_ready;`session.py` chat_session 上下文管理器 + 双闸门(对象校验锚点见 §6bis④,聊天页读不到标题、落在名片页+进入路径);`cursor.py` 游标;`vision.py` 截图**离线OCR**兜底(§11.13 [NOLLM]:PaddleOCR 类传统引擎,**严禁多模态大模型**);`gate.py` 券锚+红线闸。
- **`relay/side_a/db_reader.py`(读取正线,已实现并验证,取代 frida 读)**:旁路只读 `IndexQQMsg.db`(§6bis⑥)——adb `.backup` 出一致副本 → pull → 只读影子表 `IndexContent_content`(单聊+群消息都在此表,**不用 TroopIndex**)→ base64 解码 → 出参对齐 §7.4 Message `{text, sender uin(判 in/out), session(ZzZ0 单聊/ZzZ1 群), ts, type, ext_msg_id=qdidx:docid, fingerprint}`;增量按 `docid > last_docid`。实证:单聊 + 群债券报价均能「给会话 → 吐结构化消息」。全程只读、零 attach、零崩溃。CLI:`--snapshot`/`--db`/`--session`/`--since-docid`/`--tail`。
- ~~`redroid/rpa/read_hook.js` frida 读(方案 B)~~ **搁置**:企点本版不走 mobileqq 老门面层、frida 重负载会崩主进程(§6bis②),读取已改走旁路读库(⑥),此文件不再是读取正线;仅当读库覆盖不到的场景(如非文本消息)才考虑轻量 hook 补充。
- 一律「动作→sleep→读态验证→重试」,不假设即时生效。
- 独立 `login_sentinel` 守护企点掉线,带二维码告警;掉线只能人工重登(§6bis⑤:企点无无人扫码)。
