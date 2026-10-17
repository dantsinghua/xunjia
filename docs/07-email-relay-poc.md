# 邮件穿透 + RPA 采集/回填 端到端 PoC(Mac 本机)

验证 v1.0 现有链路(需求 12.1)的完整回路,全部在本机跑:

```
企点(redroid) ──RPA采集──▶ A ──发邮件──▶ B ──dify解析+决策──▶ B 生成回复
                                                                  │
企点对应会话 ◀──RPA回填── A ◀──收邮件(回复)──────────────────────┘
```

- **A 侧(外网侧)**:redroid + 企点 + RPA;把采集到的对方消息**发邮件**给 B;收到 B 的**回复邮件**后,用 RPA **回填到企点里对应会话**。
- **B 侧(内网侧)**:**收邮件** → dify 解析(意图+抽取,WF1)→ 规则引擎决策 → 生成话术 → **回复邮件**给 A。
- **邮件是内外网穿透的边界**(需求 12/17,复用存款项目邮件管线的思路)。PoC 用本机零凭据邮件总线 `relay/mailbus.py`,语义等价 SMTP 提交 + 收件箱轮询,后续可无缝换成真实 IMAP/SMTP/邮件网关。

## 共享邮件总线(我已写,两侧都用)

`relay/mailbus.py`:
- 本机 SMTP 服务(aiosmtpd)`127.0.0.1:8025`,按收件地址落到 `relay/mail/<box>/`。
- `send(to_box, env: dict)` 提交一封;`poll(box) -> [env,...]` 取新邮件并标记已读(移入 seen/)。
- 收件箱:`a@relay.local`→box `a`,`b@relay.local`→box `b`。
- 一封邮件 body = 一个 JSON 信封(下方)。
- 启动 SMTP:`python -m relay.mailbus serve`。

## 信封格式(A↔B 唯一契约)

**A→B 上行(采集)**:
```json
{
  "v": 1, "type": "inbound", "msg_id": "全链路幂等键(通道原始id或指纹)",
  "channel": "qidian", "thread": "对手标识(企点会话名/uin)",
  "session_hint": { "windcode": "102400895.IB", "简称": "24苏科技城MTN001" },
  "text": "对方消息完整正文", "ts": 1789450000.0, "cursor": "游标锚块"
}
```

**B→A 下行(回复)**:
```json
{
  "v": 1, "type": "outbound", "in_reply_to": "对应 msg_id",
  "thread": "对手标识", "session": { "windcode": "102400895.IB" },
  "action": "send | to_human | none",
  "text": "要回填的话术(action=send 时)",
  "reason": "转人工原因(action=to_human 时)", "ts": 1789450005.0
}
```

**幂等**:A 发出带 `msg_id`;B 回复带 `in_reply_to`;A 回填前按 `in_reply_to` 去重,**宁可漏发不可重发**(需求 16.5-20)。`action=to_human` 时 A 不回填,改为告警/挂起。

## A 侧职责(fable agent A)

1. **在本机把 Android 目标跑起来**(先 redroid,退模拟器):
   - 首选:`limactl` 起 Ubuntu VM → `apt install linux-modules-extra-$(uname -r)` → `modprobe binder_linux devices=binder,hwbinder,vndbinder` + 挂 binderfs → VM 内 docker 跑 **arm64 redroid**(`redroid/redroid:13.0.0-latest`,arm64 原生,无需翻译)→ adb 连。
   - 退路:装 Android SDK cmdline-tools + **arm64 系统镜像模拟器**(Apple Silicon 原生)当 adb 目标。
   - 装 adb、scrcpy(看画面)。企点 APK 若手头没有,先用一个可安装的占位 APK 或 QQ 验证 RPA 通路(标注)。
2. **RPA 层**(按 `docs/04-redroid-rpa.md` 的七件套):状态机 + `to_ready` + `chat_session` 会话括号 + **双发送闸门** + 券锚校验 + 红线闸 + 游标。读用 uiautomator/截图(Flutter 兜底),写用 adb input(中文走 ADBKeyboard)。
3. **上行**:轮询/hook 到对方新消息 → 组装 inbound 信封(带 session_hint 券锚)→ `mailbus.send("b", env)`。
4. **下行**:`mailbus.poll("a")` 取回复 → `action=send` 则用 `chat_session(thread)` 双闸门回填话术到企点对应会话;`to_human` 则告警不回填;按 `in_reply_to` 幂等去重。
5. 产出 `relay/side_a/`,一个 `run_a.py` 常驻,`README` + 自测(至少跑通:采集一条→发邮件;收一条回复→回填一条)。

## B 侧职责(fable agent B)

1. **收**:`mailbus.poll("b")` 取 inbound 信封。
2. **dify 解析(WF1,意图+抽取)**:PoC 可用**本地 dify 或 mock dify**(规则/关键词 + OpenAI 兼容端点兜底)输出 `docs/06` 的 JSON `{intent, extracted, 券锚线索, 置信}`。**只分类抽取,不比大小。**
3. **规则引擎决策**:复用 bondtrade 谈价核心的策略/让步/边界逻辑(或先写最小规则:按 起始/边界/步幅 与对方还价比大小 → 下一轮报价 / 追问量 / 转人工),按 `docs/05` 状态机。归属用 session_hint 券锚(L1)。
4. **回**:生成话术(`docs/05/10` 话术库格式,过红线闸)→ outbound 信封 → `mailbus.send("a", env)`;拿到实价/非价格条件等 → `action=to_human`。
5. 产出 `relay/side_b/`,一个 `run_b.py` 常驻,`README` + 自测(收一条 inbound → 解析 → 决策 → 发一条 outbound)。

## 联调验收(两侧 + 我合起来)

起 mailbus → 起 A(Android 目标 + RPA)→ 起 B → 在企点里(或模拟目标里)发一条对方消息,观察:A 采集→发邮件→B 解析决策→回邮件→A 回填到企点对应会话。全链路一次 round-trip 通即算 PoC 成功。真实企点未就绪时,A 侧可用模拟目标演示 RPA 收发闭环,并标注。
