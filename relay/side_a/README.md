# A 侧(外网侧):Android 目标 + RPA 采集/回填 + 邮件收发

契约见 `docs/07-email-relay-poc.md`(信封、A↔B 职责),RPA 七件套见 `docs/04-redroid-rpa.md`。
邮件总线用 `relay/mailbus.py`(`send("b", env)` / `poll("a")`)。

## 目标最终是什么(如实)

- **redroid 13(arm64,`redroid/redroid:13.0.0_64only-latest`)跑在 colima 的 Lima VM 里**(Ubuntu 24.04 / 6.8 内核,Apple Silicon 原生,无翻译层)。不是模拟器。
- **目标 APP 是自建的 MockQidian**(`mock_app/`,原生 View 写的最小"会话列表 + 会话页 + 输入框/发送"),不是真实企点。
  手头没有企点 APK;QQ 需要账号登录,无法无人值守自测。Mock 的好处是 resource-id 完整、可用 `am broadcast` 注入"对方消息",能做全自动的收发闭环自测。
  真实企点是 Flutter,uiautomator 大概率只见一个 FlutterView(04 §1),届时读消息要加截图+多模态兜底,`config.PROFILES["qidian"]` 的定位项需实机 dump 后补齐;RPA 其余层(状态机/会话括号/双闸门/游标/上下行)不动。

## 怎么起

```bash
# 0) 一次性:VM 内 binder 模块(colima 重启后要重做 modprobe)
colima ssh -- sudo apt-get install -y linux-modules-extra-$(uname -r)
colima ssh -- sudo modprobe binder_linux devices=binder,hwbinder,vndbinder
#    注意:**不要**在 VM 里手工 mount binderfs;redroid 的 init 自己会挂(见"坑")。

# 1) redroid(数据卷在 VM 内)
docker run -d --name redroid13 --privileged --memory=2500m \
  -v /home/anlin.linux/redroid-data-64:/data -p 5555:5555 \
  redroid/redroid:13.0.0_64only-latest \
  androidboot.redroid_gpu_mode=guest androidboot.use_memfd=true \
  androidboot.redroid_width=720 androidboot.redroid_height=1280 androidboot.redroid_dpi=320
# 约 30s 后 sys.boot_completed=1;adb 会把 localhost:5555 自动识别成 emulator-5554
adb devices -l            # 期望看到 emulator-5554  ... model:redroid13_arm64_only
scrcpy -s emulator-5554   # 看画面(可选)

# 2) 装 IME + mock 目标(APK 在 apk/;mock 用 mock_app/build.sh 重编)
adb -s emulator-5554 install -r relay/side_a/apk/ADBKeyboard.apk
adb -s emulator-5554 install -r relay/side_a/apk/MockQidian.apk
adb -s emulator-5554 shell ime enable com.android.adbkeyboard/.AdbIME
adb -s emulator-5554 shell ime set com.android.adbkeyboard/.AdbIME
adb -s emulator-5554 shell am start -n com.xunjia.mockqidian/.MainActivity

# 3) 自测(采集→send('b');构造回复→回填;闸门负例)
cd /Users/anlin/work/xunjia-agent && .venv/bin/python -m relay.side_a.selftest

# 4) 常驻
.venv/bin/python -m relay.side_a.run_a            # 默认 4s 一轮,只扫有未读的会话
.venv/bin/python -m relay.side_a.run_a --once     # 单轮
```

环境变量:`SIDE_A_SERIAL`(默认 `emulator-5554`)、`SIDE_A_TARGET`(默认 `mockqidian`)、`SIDE_A_INTERVAL`、
`SIDE_A_ADB`(adb 可执行文件;宿主 adb 起不来时指到 `relay/side_a/vadb.sh`,经 colima VM 内的 adb 转发,配 `SIDE_A_SERIAL=<容器IP>:5555`)。

给 mock 注入一条"对方消息"(联调时模拟对手说话):
```bash
adb -s emulator-5554 shell am broadcast -a com.xunjia.mockqidian.PEER_MSG \
  -n com.xunjia.mockqidian/.PeerMsgReceiver --es peer "'张三-华泰固收'" --es text "'3000W 估值+8 能出么'"
adb -s emulator-5554 shell am broadcast -a com.xunjia.mockqidian.RESET -n com.xunjia.mockqidian/.PeerMsgReceiver   # 重置种子数据
adb -s emulator-5554 shell run-as com.xunjia.mockqidian cat files/store.json   # APP 落盘,验证回填
```

## 代码结构(对应 04 §7 落地清单)

| 文件 | 职责 |
|---|---|
| `primitives.py` | 元操作:`uiautomator dump` → 节点(resource-id/content-desc/text/bounds);`screencap` 取证;tap/swipe/back;文本走 ADBKeyboard `ADB_INPUT_B64` 广播(中文) |
| `state.py` | `State{READY,LOGGED_OUT,CHAT_OPEN,NOT_FOREGROUND,UNKNOWN}`、`detect_state`、`to_ready` 归一化(back 清浮层+滚顶+验证,4 轮失败→截图+限频告警+raise)、`list_threads`(只认列表区)|
| `session.py` | `chat_session(rpa, peer)`:to_ready→find_thread→open→**标题强校验(4 次缓冲)**→yield→finally back+to_ready;`Chat.send_text`:红线闸→券锚闸→**闸门1(点输入框前校验标题)**→ADBKeyboard 写入→**回读输入框==待发文本**→**闸门2(点发送前校验标题)**→发送后在历史里验证到我方同文本;每步串 `tid` 写日志 |
| `gate.py` | 红线出口词表(要了/成交/TKN/GVN/done/按这个价做…)→`GuardBlock`;券锚:文本里的 `\d{6,9}.(IB|SH|SZ)` 必须==会话 windcode→`AnchorMismatch` |
| `cursor.py` | 游标锚块 K=3 `(dir,text)` 序列;倒序找锚,找不到→全部视为新+`truncated`;首次只建基线;`state/cursor.json` 持久化 + 指纹去重 |
| `uplink.py` | 扫会话(默认只扫有未读徽标的)→`chat_session`→`read_history`→游标 diff→每条对方新消息组 inbound 信封(`session_hint` 从我方历史外发的 `bid {code} {简称}` 回读,兜底 `config.THREAD_SESSIONS`)→`send("b")`→发送成功才前进游标 |
| `downlink.py` | `poll("a")`→`outbound`:`send` 先把 `in_reply_to` 写入 `state/done.json`(inflight)再回填(**宁可漏发不可重发**),成功 `sent`、闸门拦截 `blocked`、其他 `failed`+告警;`to_human` 告警不回填;重复 `in_reply_to` 跳过;都 `ack()` |
| `run_a.py` | 常驻主循环:上行→下行→sleep;`Unrecoverable` 退避 |
| `selftest.py` | 自测脚本(见下) |
| `mock_app/` | MockQidian 源码 + `build.sh`(aapt2/javac/d8/apksigner,不用 gradle)|
| `vadb.sh` | 经 colima VM 转发的 adb(备用通道)|

## 自测结果(2026-09-15,redroid 13 64only + MockQidian)

`python -m relay.side_a.selftest` → **15/15 passed**(全程约 5 分钟,日志 `logs/side_a.log`,截图 `logs/shots/`):

| 项 | 结果 |
|---|---|
| IME / to_ready / list_threads / screencap | 通(ADBKeyboard 为默认 IME;列表 3 个会话;720x1280 PNG)|
| 基线 | 三个会话各建游标锚块,不上行 |
| 上行 | 注入 `3000W 估值+8 能出么` → `relay/mail/b` 出现 inbound 信封,`session_hint={"windcode":"102400895.IB","简称":"24苏科技城MTN001"}`(从我方历史外发文本回读)|
| 上行幂等 | 再扫一轮 0 条 |
| 下行 | 构造 `outbound{in_reply_to=同 msg_id, action=send}` → 双闸门回填 `bid 102400895.IB … 估值+6能出么`,APP `store.json` 末条 `dir=me` 同文本(`logs/shots/evidence-chat-after-backfill.png`)|
| 重复 in_reply_to | 跳过(`duplicate`)|
| 红线 `好的 成交` | `blocked`,未发 |
| 券锚 `102400896.IB` ≠ 会话 `102400895.IB` | `blocked`,未发 |
| `to_human` | 告警不回填 |
| 结束 | APP 我方消息数 = 种子 1 + 回填 1;回到 READY |

## 坑(按踩到的顺序)

1. **redroid 官方镜像 `13.0.0-latest` 在 Apple Silicon 上起不来**:init 执行 `/vendor/bin/boringssl_self_test32` 报 `Exec format error` →
   `reboot,boringssl-self-check-failed`。M 系列 CPU 没有 AArch32,32 位半边跑不了。**用 `_64only` 镜像**。
   之前看到的一串 `Binder driver could not be opened` 是它的后果不是原因。
2. **Ubuntu 6.8 的 `binder_linux` 只有 binderfs**,`devices=` 参数不会生成 `/dev/binder`;redroid 的 `init.rc` 自己 `mount binder /dev/binderfs` + symlink。
   若在 VM 里手工挂了 `/dev/binderfs`,docker `--privileged` 会把宿主 `/dev` 整棵复制进容器,反而干扰;**不要手工挂**,也不要 `--device` 映射。
3. **宿主 adb(brew cask `android-platform-tools`)在 macOS 26 上卡死在 `_dyld_start`**:进程只映射了 adb 与 dyld,任何子命令(连 `adb version`)都不返回;
   去掉 `com.apple.quarantine` 也没用,但**同一个二进制复制到别处就能跑**。已把 `/opt/homebrew/bin/adb` 换成实体文件(原来是指向 Caskroom 的软链)。
   `brew upgrade` 后可能被重新链回去,复发就再 `cp` 一次;或用 `vadb.sh` 走 VM 内 adb。
4. **不要 `adb connect 127.0.0.1:5555`**:adb 自带 emulator 扫描已把 localhost:5555 当成 `emulator-5554`,再 connect 同一端口会互相卡死。直接用 `-s emulator-5554`。
5. **Lima 会把 VM 里所有监听端口自动转发到宿主**:VM 里一起 adb server(5037)宿主的 adb 客户端就会连到 VM 的 server 上。可用(`vadb.sh` 就靠这个)但要知道。
6. **Android 8+ 隐式广播到不了 manifest 注册的 receiver**:mock 的测试钩子必须 `-n pkg/.Receiver`。
7. **ADBKeyboard 弹出时第一个 BACK 只关键盘**,`to_ready` 的多轮 back 已覆盖。
8. VM 内存只有 ~10G 且被别的容器占满(可用 <1G 时启动),redroid 13 稳定在 ~700MB;若 OOM 需 `colima stop && colima start --memory 14`(会重启所有容器,慎)。
9. `uiautomator dump` 每次 ~2.5s,一次 `chat_session` 含进/出归一约 15–20s;真实企点若走 frida 读(04 §6 方案 B)可大幅缩短。
