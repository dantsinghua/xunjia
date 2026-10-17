# B 侧(内网侧)—— 收邮件 → WF1 解析 → 规则引擎 → 话术 → 回邮件

契约:`docs/07-email-relay-poc.md`(信封)· 决策逻辑:`docs/05-workflow-statemachine.md` · WF1 输出:`docs/06-dify-workflow.md`。

```
poll("b") ─▶ wf1.classify ─▶ 归属(L1 券锚 / L3 单会话) ─▶ engine.decide ─▶ phrase + guard ─▶ send("a")
                 只分类抽数                                    比大小/让步/停/叫人      话术库+红线闸
```

## 运行(仓库根目录)

```bash
.venv/bin/python -m relay.side_b.run_b            # 常驻,2s 轮询 relay/mail/b/new
.venv/bin/python -m relay.side_b.run_b --once     # 处理完当前收件箱即退
.venv/bin/python -m relay.side_b.selftest         # 自测(隔离在 state/selftest/,不动真实收件箱)
```

环境变量(全部可选):

| 变量 | 含义 | 默认 |
|---|---|---|
| `B_TEMPLATE` | 策略模板 `起始,边界,步幅`(BP,绝对值;符号按方向写死) | `10,5,2` |
| `B_VOL_WAN` | 默认询价量(万) | `3000` |
| `B_STATE_DIR` | 状态目录 | `relay/side_b/state/` |
| `OPENAI_BASE` / `OPENAI_KEY` / `OPENAI_MODEL` | WF1 兜底(OpenAI 兼容 chat/completions);未配则纯规则 | 无 |
| `WF1_LLM_THRESHOLD` | 规则置信低于此值才调兜底 | `0.6` |

## 文件

| 文件 | 职责 |
|---|---|
| `wf1.py` | 意图分类+抽取(规则/关键词;OpenAI 兼容兜底)。输出 06 的 JSON。**不比大小。** |
| `sessions.py` | Session(意向×对手×券)状态:起始/边界/步幅/轮次/当前报价/已追问量;落盘 `state/sessions.json`;留痕 `state/trace.jsonl`(只增);幂等 `state/processed.json` |
| `engine.py` | 05 状态机:与边界比大小、让步钳边界、追问量一次、转人工、结束;组装 outbound 信封 |
| `phrase.py` | 话术库(`bid {code} {简称} {量}W 估值+{N}能出么` / ofr 镜像 / 追问量 / 请示)+ 红线闸(要约成交词、内部字段) |
| `run_b.py` | 常驻主循环 |
| `selftest.py` | 各意图端到端自测 |

## session_hint 扩展(A 侧/工作台可选传)

L1 归属只需 `windcode`+`简称`。B 侧还认以下可选键建会话:`方向`(bid/ofr,默认 bid)、`起始_bp`、`边界_bp`、`步幅_bp`、`当前_bp`、`量_wan`、`可投量_wan`(判加量超投,不外发)、`估值`(对方报绝对价时换算偏离用;缺则绝对价还价直接转人工)。
无 `session_hint` 的信封:该对手恰有 1 个未闭合会话 → L3 归属;否则归属不明 → `to_human`。

**PoC 假设**:B 侧第一次见到某 (对手,券) 时视为 A 侧已发出第 1 轮起始价(`round=1, current=起始`),因为 v1.0 由交易员在工作台触发首轮。

## 决策表(bid 方向;ofr 镜像)

| WF1 意图 | 规则引擎 | 回复 |
|---|---|---|
| 实价 | → TO_HUMAN | `to_human` + `meta.human_note`(`{code} {简称} {量}W,拿到实价:1.86/3000万,是否介入`) |
| 虚价 | 未追问过 → ASK_VOL;已追问 → TO_HUMAN | `send` `{简称} 这个价能出多少量?` |
| ASK_VOL 中给明确量 | 升级实价 → TO_HUMAN | `to_human` |
| ASK_VOL 中仍模糊/挂起/拒绝 | → TO_HUMAN | `to_human` |
| 还价(估值±N / 绝对价 / 让N) | 不劣于当前报价或边界内 → 锁价 + 追问量;劣于边界 = 拒绝 → 让一步幅(越界钳到边界);已到边界 → CLOSED_NOPRICE | `send` 下一轮 / 追问量 / 收口话术 |
| 明确拒绝(无数字) | 同"劣于边界" | `send` 下一轮 / 收口 |
| 非价格条件 / 反问 / 其他券 / 多券 / 加量超投 / 归属不明 / 图片附件 / 低置信 | → TO_HUMAN | `to_human` |
| 挂起 / 噪声 | 保持等待 | `none` |
| 会话已 TO_HUMAN/CLOSED 再来消息 | → TO_HUMAN | `to_human` |

轮次上限派生 `ceil((起始−边界)/步幅)+1`,默认 10/5/2 → 4 轮:+10 → +8 → +6 → +5(钳)。

## 与 05/06/07 的差异与备注

- `to_human` 信封同时带 `text="您好,等我内部请示下"`(05 §6 对对手方话术)与 `meta.human_note`(对本方交易员)。07 约定 A 侧 `to_human` 不回填,所以该 `text` 由 A 决定用不用。
- outbound 信封在 07 契约字段之外多了 `meta`(intent/extracted/券锚线索/置信/route/state/round/current_bp/human_note/blocked),纯观察用,A 可忽略。
- 05 把"只给方向不给数"(如"再高点")归虚价;这里归**明确拒绝**(→让一步幅),因为对它追问量不合语义。"再让 N"抽成 `基准=让步`(06 只有 估值|绝对),引擎按当前报价往对方方向让 N 换算。
- 还价落在边界内(如 +7)时:v1.0 不追成交、目标是拿实价,所以直接把当前报价锁到对方价并追问量,不再逐步让;拿到量即转人工。
- 尾数简报("86 能出")未实现(需估值整数位);对方绝对价且会话无 `估值` → 直接转人工。
- 跨日续谈(05 §4)、挂起超时(SUSPENDED)、WF2 图片转录未实现;图片/附件一律转人工。
- 错误信封 PoC 直接 ack 进 seen/ 防死循环,真实部署应改隔离箱。
