# A 侧在 WSL2 x86_64 上跑(补充 `README.md`,原 README 是 Mac/colima/arm64 口径,不改)

核对日期 2026-09-16。本机:Windows 上的 WSL2 Ubuntu 24.04,x86_64,内核 `6.6.87.2-microsoft-standard-WSL2`。
**当前内核没有 binder,redroid 还起不来**(见 `docs/`/`qtrade-redroid-installer` 的内核编译线);本文是「redroid 一起来就能跑 selftest」的静态核对结论 + 无设备干跑结果 + 设备起来后的验证清单。

## 本机已就绪的部分(已实测)

| 项 | 状态 | 证据 |
|---|---|---|
| adb | `android-tools-adb` 34.0.4-debian(`/usr/bin/adb` → `/usr/lib/android-sdk/platform-tools/adb`),server 能起,`adb devices` 空列表 | `adb version`、`adb devices -l` |
| scrcpy | `scrcpy` 1.25 + `scrcpy-server` 1.25(apt,较老;redroid 11 是 Android 11,1.25 支持 5.0+,够用) | `scrcpy --version` |
| WSLg | 可用:`DISPLAY=:0`、`WAYLAND_DISPLAY=wayland-0`、`/mnt/wslg` 存在 → scrcpy 能在本机开窗口 | `echo $DISPLAY; ls /mnt/wslg` |
| Python | `.venv` = CPython 3.12.3(uv 建,`/usr/bin/python3.12`),唯一三方依赖 `aiosmtpd==1.4.6`;`compileall relay` 通过;11 个模块逐一 `import` 通过 | `.venv/bin/python -m compileall -q relay` |
| APK | `apk/MockQidian.apk`、`apk/ADBKeyboard.apk` **均无 `lib/` 目录**(只有 `classes.dex` + 资源)= 纯 Java,**x86_64 redroid 直接能装,不需要 arm 翻译层** | `unzip -l apk/*.apk` |
| 目录权限 | `state/`、`logs/` 在 `$HOME` 下 ext4,属主 anlin,可写;`logs/` 不存在时 `run_a.setup_logging` / `screencap` 会 `mkdir(parents=True)` 自建 | 干跑后 `logs/side_a.log` 已生成 |
| mock 重编工具链 | 本机无 aapt2/javac/d8/apksigner,**不需要重编**,APK 已在 `apk/`(Mac 上编好) | — |

> scrcpy 1.25 若嫌老(3.x 才有 `--audio`、更好的 H.265),官方 Linux 预编译包:
> `https://github.com/Genymobile/scrcpy/releases/latest` → `scrcpy-linux-x86_64-v3.x.tar.gz`,解压后直接跑里面的 `scrcpy`(自带 `scrcpy-server`),不装也行。

## 无设备干跑(三种形态,失败点原文)

mailbus 起在 `127.0.0.1:2526`(`python -m relay.mailbus serve`,与 Mac 一样有一条 `DeprecationWarning: There is no current event loop`,无害)。

| 形态 | `selftest` / `run_a --once` 表现 | 干净? |
|---|---|---|
| **adb 二进制不存在** | 裸 traceback:`primitives.py:54 in run → subprocess.run … FileNotFoundError: [Errno 2] No such file or directory: 'adb'`,rc=1 | ❌ 不干净(`Adb.connect()` 没兜 `FileNotFoundError`)。装了 adb 即不会遇到;要改的话在 `Adb.run` 加一句 `except FileNotFoundError: raise RuntimeError(f"adb 不可执行: {self.adb}")`,本轮**未改**(不动 RPA 代码) |
| **adb 有、设备无**,默认 `SIDE_A_SERIAL=emulator-5554` | 约 10s 后 `ERROR side_a.selftest adb 未就绪` / `ERROR side_a.run adb emulator-5554 未就绪`,rc=2 | ✅(`connect()` 10 次 `getprop sys.boot_completed` 轮询后返回 False) |
| **adb 有、设备无**,`SIDE_A_SERIAL=127.0.0.1:5555` | 多走一次 `adb connect`(失败),同样 `adb 127.0.0.1:5555 未就绪` rc=2 | ✅ |

副作用:干跑只创建 `logs/side_a.log`,`state/` 未被改动。⚠️ `state/cursor.json`、`done.json` 是 **Mac 上 9/15 自测留下的旧游标**(mock 种子数据的锚块);`selftest` 开头会自己删掉,但直接跑 `run_a` 会沿用旧锚——首轮想干净建基线就先 `rm state/*.json`。

## Mac / colima / arm64 假设逐条清单(文件:行 → WSL x86 改法)

| 位置 | 内容 | WSL x86 结论 | 处理 |
|---|---|---|---|
| `config.py:19` `ADB_SERIAL` 默认 `emulator-5554` | Mac 上 adb 把 localhost:5555 自动识别成 emulator-5554 | **Linux 同样成立,默认值不用改**。依据:①adb 客户端源码 `adb/client/transport_local.cpp` 的 `PollAllLocalPortsForEmulator()` 在 `#if ADB_HOST` 下对所有宿主 OS 生效,扫 5555~5585 偶数端口,序列号按 `emulator-<port-1>` 命名,不看 adbd 是不是真模拟器;②**本机实测**:在 127.0.0.1:5555 放一个假 TCP 监听,`adb start-server` 后 8 秒内收到 25 次来自 adb server 的 `CNXN` 握手(一起来就 ~1.2s 一次),期间没有执行任何 `adb connect`。所以 redroid 一旦在 5555 应答,就会以 `emulator-5554` 出现 | 只改了注释(记录依据);保留 `SIDE_A_SERIAL` 环境变量可覆盖 |
| README 坑 4「不要 `adb connect 127.0.0.1:5555`」 | Mac 经验:connect 与扫描互相卡死 | **推断在 Linux 同样成立**(同一段客户端代码;connect 会以 `127.0.0.1:5555` 为序列号再注册第二个 transport 指向同一个 adbd)。设备起不来无法实证「卡死」本身,验证清单第 1 步顺带确认 | 只记录 |
| `config.py:16` `SIDE_A_ADB` 注释指向 `vadb.sh`/colima | Mac 宿主 adb 卡死时的替代通道 | WSL 直接用 apt 的 adb,`vadb.sh` **无意义** | 注释已改为「macOS 专用;Linux/WSL 直接用 apt 的 adb」 |
| `vadb.sh:5` `ssh -F ~/.colima/ssh_config colima` | 经 colima VM 转发 | **Mac 专用,WSL 不用**(WSL 里 docker 与 adb 同一网络命名空间,没有 VM 那一层) | 不动,本文标注 |
| `run_a.py:3`、`selftest.py:7` docstring `cd /Users/anlin/work/xunjia-agent` | Mac 路径硬编码(仅文档串) | 已改成 `cd <仓库根>` 并指向本文 | **已改** |
| `README.md:8/16-29` redroid 13 `_64only` arm64 启动命令、`colima ssh … modprobe binder_linux` | Apple Silicon 专用(M 系无 AArch32 故 `_64only`;binder 走 VM 内 modprobe) | 换成 agent-2 的 **redroid 11 x86_64 compose**(见下节);binder 在 WSL 是**编进内核**(`=y`,`/dev/binder` 字符设备),不是 modprobe | 本文给替代段落 |
| `README.md:28` 期望 `model:redroid13_arm64_only` | 镜像型号串 | x86 上 `adb devices -l` 的 model 会是 `redroid11_x86_64` 之类(以实机为准),**代码不看这个字段** | 只记录 |
| `README.md:39` `cd /Users/anlin/…` | 同上 | 用 `cd /home/anlin/work/xunjia-agent` | 本文替代 |
| `README.md:97-99` 坑 3 brew 的 adb 卡死 `_dyld_start` | macOS 26 + brew cask 专属 | WSL 不存在 | 只记录 |
| `README.md:101` 坑 5 Lima 端口自动转发 | Lima 专属 | WSL 不存在;但注意 WSL 的 `localhostForwarding` 会把 WSL 里的 5037/5555 也暴露给 Windows,若 Windows 侧还装了 Android Studio 的 adb,**两个 adb server 版本不同会互相 kill**——本机排查过:Windows 侧目前没有 adb 在跑,先记着 | 只记录 |
| `README.md:104` 坑 8 colima VM 内存 | Lima 专属 | WSL 的内存上限在 `.wslconfig`(**红线:不许改**);redroid 11 x86 稳定 ~700MB~1G,当前机器够用 | 只记录 |
| `mock_app/build.sh:3/7/10` brew 路径、`/opt/homebrew/...` | 重编 mock 用 | 本机无 JDK/SDK,**不需要重编** | 不动 |
| `docs/07:52-53` limactl / arm64 模拟器 | 文档层的 Mac 路线 | 与本文并列,不改 | 只记录 |
| arch 相关(截图 PNG 解析 / `screencap` 参数 / `uiautomator dump` 路径 / `input` / `am broadcast` / `run-as`) | — | **无**:`primitives.py` 全部是 adb shell 文本协议,`screencap -p` 直接落字节不解析,`uiautomator dump /dev/tty` 走 exec-out,`input_text` 走 ADBKeyboard base64 广播;没有任何 CPU 架构或 OS 依赖 | — |

结论:**A 侧 Python 代码一行业务逻辑不用改**。Mac 假设全部在注释/文档/`vadb.sh`/`build.sh` 这几层。

## 怎么起(WSL x86)

```bash
# 0) 前置(一次性,已做):
sudo apt-get install -y android-tools-adb scrcpy          # adb 34.0.4 / scrcpy 1.25
cd /home/anlin/work/xunjia-agent
~/.local/bin/uv venv .venv --python 3.12 && ~/.local/bin/uv pip install --python .venv/bin/python aiosmtpd

# 1) redroid 11 x86_64(由 agent-2 提供,替代原 README 的 redroid 13 arm64 段落)
#    compose 路径:/home/anlin/work/qtrade-redroid-installer/rootfs/out-wsl-local/(docker-compose.yml + up.sh + install-qidian.sh + down.sh,2026-09-16 已就绪)
#    要点(与 Mac 段落的差异):镜像 redroid/redroid:11.0.0-latest **官方自带 libndk_translation**(libnb.so 是其软链),不必重打 ndk 镜像;真企点 6.9.7 是 arm64-only 靠它跑,mock/IME 纯 Java 不需要;
#    内核 binder 编进 =y(/dev/binder 直接有,不 modprobe、不手工挂 binderfs——README 坑 2 的教训在这里同样适用:不要 --device 映射 /dev/binder*);
#    androidboot.use_memfd=true(无 ashmem)、androidboot.redroid_gpu_mode=guest;端口 127.0.0.1:5555:5555。
#    前提:.wslconfig 换成带 binder 的内核并 wsl --shutdown —— 这两步是红线,由用户手动做。
cd /home/anlin/work/qtrade-redroid-installer/rootfs/out-wsl-local && ./up.sh   # 起容器并等 sys.boot_completed=1;真企点再 ./install-qidian.sh;收工 ./down.sh

# 2) 一键验证(下面清单的脚本化版本;--no-selftest 只做到第 7 步)
bash /home/anlin/work/xunjia-agent/relay/side_a/verify-wsl.sh

# 3) 手动等价:
adb devices -l                                   # 期望 emulator-5554  device ... model:redroid11_x86_64(型号串以实机为准)
scrcpy -s emulator-5554                          # WSLg 可用,能开窗口
adb -s emulator-5554 install -r relay/side_a/apk/ADBKeyboard.apk
adb -s emulator-5554 install -r relay/side_a/apk/MockQidian.apk
adb -s emulator-5554 shell ime enable com.android.adbkeyboard/.AdbIME
adb -s emulator-5554 shell ime set   com.android.adbkeyboard/.AdbIME
adb -s emulator-5554 shell am start -n com.xunjia.mockqidian/.MainActivity
cd /home/anlin/work/xunjia-agent
nohup .venv/bin/python -m relay.mailbus serve > relay/side_a/logs/mailbus.out 2>&1 &
.venv/bin/python -m relay.side_a.selftest        # 期望 15/15 passed(Mac 上约 5 分钟)
.venv/bin/python -m relay.side_a.run_a --once
```

### 环境变量在 WSL 的取值

| 变量 | WSL 取值 | 说明 |
|---|---|---|
| `SIDE_A_ADB` | **不设**(默认 `adb`,即 `/usr/bin/adb`) | `vadb.sh` 是 Mac 的 colima 通道,WSL 不用 |
| `SIDE_A_SERIAL` | **不设**(默认 `emulator-5554`) | 前提是 compose 把 5555 映射到 **127.0.0.1**(或 0.0.0.0)——adb 只扫 loopback。若 agent-2 映射到别的端口(如 5557),设 `SIDE_A_SERIAL=emulator-5556`(=端口−1);若映射到非 loopback 地址,才设 `SIDE_A_SERIAL=<ip>:<port>` 让 `connect()` 走 `adb connect` 分支 |
| `SIDE_A_TARGET` | `mockqidian`(默认) | 真企点接入时 `qidian` |
| `SIDE_A_INTERVAL` | 默认 4 | — |

## redroid 起来后的验证清单(逐条,期望值)

`verify-wsl.sh` 就是这份清单的脚本化,每步失败即停并打印原因。

1. **`adb devices -l`** → 一行 `emulator-5554  device product:redroid… model:redroid11_x86_64 … transport_id:N`。
   - 空列表 = 容器没监听 5555 或端口没映射到 loopback;`offline` = adbd 还没起完(等 30s);**出现两行**(`emulator-5554` + `127.0.0.1:5555`)= 有人跑了 `adb connect`,`adb disconnect 127.0.0.1:5555` 去掉那行。
2. **`getprop sys.boot_completed` = 1**(90s 内);顺手记 `ro.product.cpu.abi`(期望 `x86_64`)与 `ro.product.cpu.abilist`(带翻译层的镜像应含 `arm64-v8a`,真企点才需要)。
3. **`install -r` 两个 APK** 成功,`pm list packages` 见 `com.android.adbkeyboard`、`com.xunjia.mockqidian`。两者无 native 库,x86_64 上**不该**出现 `INSTALL_FAILED_NO_MATCHING_ABIS`;若出现说明镜像的 abilist 异常。
4. **`ime set`** 后 `settings get secure default_input_method` = `com.android.adbkeyboard/.AdbIME`。
5. **`am start`** 后 `dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'` 含 `com.xunjia.mockqidian`。
6. **`uiautomator dump /dev/tty`** 能拿到 `com.xunjia.mockqidian:id/thread_list|thread_name|chat_title|msg_list|msg_text|input|send`(= `config.PROFILES["mockqidian"]` 的 7 个定位项)。
7. **`screencap -p`** 出 PNG(`\x89PNG` 头,>1KB)。
8. mailbus 在 `127.0.0.1:2526`。
9. **`selftest` 15/15 passed**:ime / to_ready / list_threads / screencap / baseline / uplink→b / session_hint / uplink idempotent / downlink→app / dup in_reply_to / redline block / anchor block / to_human no send / app me-count / final READY。日志 `logs/side_a.log`,截图 `logs/shots/`。
   - 若只有 `downlink→app` 失败且 `logs/shots/sendverify-*.png` 里文本已在输入框但没发出:先怀疑 x86 redroid 的 `input tap` 落点/密度(720x1280@320dpi 与 Mac 段落一致就不该有差),再怀疑 ADBKeyboard 没成为默认 IME(第 4 步)。
   - `uiautomator dump` 在 x86 redroid 上每次 ~2.5s 与 arm64 相当(README 坑 9),整套约 5 分钟。
10. **`run_a --once`** rc=0,日志出现 `A 侧启动 target=mockqidian serial=emulator-5554`。
11. **真企点接入时**(agent-2 把 arm64 企点 APK 装进带翻译层的镜像之后):
    - `SIDE_A_TARGET=qidian`;`config.PROFILES["qidian"]` 除 `package` 外全是 `None`,`config.profile()` 会直接 `RuntimeError("profile qidian 缺定位项 …")`——这是**设计如此**,必须先实机 `adb exec-out uiautomator dump /dev/tty` 把 `list_root/thread_name/chat_title/msg_text/input/send` 的 resource-id 填进去;
    - 企点是 Flutter(`libflutter.so`),dump 大概率只见一个 `FlutterView` 无 resource-id → 读消息走 `docs/04 §1` 的截图+多模态兜底(`read_by_vision`,当前代码尚未实现,04 §7 的 `vision.py`),或 §6 方案 B 的 frida 读;写侧 RPA 七件套不变;
    - `dumpsys window` 的前台包名判定(`state.detect_state` → `NOT_FOREGROUND`)对企点同样适用,不依赖 resource-id。

## 结束时清理

```bash
# mailbus:ss -ltnp | grep 2526 拿 pid 后 kill;或
kill "$(ss -ltnp | grep ':2526 ' | grep -oP 'pid=\K[0-9]+' | head -1)"
adb kill-server
```
