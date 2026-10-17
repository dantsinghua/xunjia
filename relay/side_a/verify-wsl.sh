#!/usr/bin/env bash
# WSL2 x86_64 上 redroid 起来之后的 A 侧验证清单(按 README-wsl-x86.md「验证清单」逐步执行,任一步失败即停)。
# 只做:等设备 → 装 IME/mock → 设 IME → 拉起 mock → 起 mailbus(若没起)→ selftest。不碰 redroid 容器本身(那是 compose 的事)。
#
#   bash relay/side_a/verify-wsl.sh              # 全部
#   bash relay/side_a/verify-wsl.sh --no-selftest # 只到"目标 APP 在前台",不跑 5 分钟自测
#
# 环境变量:SIDE_A_SERIAL(默认 emulator-5554)、SIDE_A_ADB(默认 adb)、SIDE_A_TARGET(默认 mockqidian)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ADB="${SIDE_A_ADB:-adb}"
SERIAL="${SIDE_A_SERIAL:-emulator-5554}"
PY="$ROOT/.venv/bin/python"
IME="com.android.adbkeyboard/.AdbIME"
MOCK_ACT="com.xunjia.mockqidian/.MainActivity"
A() { "$ADB" -s "$SERIAL" "$@"; }
step() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

step "0. 前置:adb / python / venv"
"$ADB" version | head -2 || fail "adb 不可用(apt-get install android-tools-adb)"
[ -x "$PY" ] || fail "缺 $PY(uv venv .venv --python 3.12 && uv pip install aiosmtpd)"
"$PY" -c "import aiosmtpd" || fail "venv 里没有 aiosmtpd"

step "1. adb devices -l(期望:$SERIAL  device ... model:redroid*)"
"$ADB" start-server >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  if "$ADB" devices | grep -qE "^${SERIAL}[[:space:]]+device$"; then break; fi
  [ "$i" -eq 30 ] && { "$ADB" devices -l; fail "30s 内没看到 $SERIAL 处于 device 态(offline=adbd 还没起;空列表=容器没监听 5555 或端口没映射到 127.0.0.1)"; }
  sleep 1
done
"$ADB" devices -l | grep "$SERIAL"
echo "提示:不要 adb connect 127.0.0.1:5555(README 坑 4,Linux 同样成立)"

step "2. 等 sys.boot_completed=1 + 记录 ABI(x86_64 redroid 期望 ro.product.cpu.abi=x86_64,含 arm64 翻译层时 abilist 里应有 arm64-v8a)"
for i in $(seq 1 90); do
  [ "$(A shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  [ "$i" -eq 90 ] && fail "90s 内 sys.boot_completed 未变 1(看 docker logs;binder 缺失时 init 会在此前反复重启)"
  sleep 1
done
echo "abi=$(A shell getprop ro.product.cpu.abi | tr -d '\r')  abilist=$(A shell getprop ro.product.cpu.abilist | tr -d '\r')  sdk=$(A shell getprop ro.build.version.sdk | tr -d '\r')  model=$(A shell getprop ro.product.model | tr -d '\r')"

step "3. 装 ADBKeyboard + MockQidian(两者纯 dex 无 native 库,x86_64 直接可装)"
A install -r "$HERE/apk/ADBKeyboard.apk"
A install -r "$HERE/apk/MockQidian.apk"
A shell pm list packages | tr -d '\r' | grep -E 'adbkeyboard|mockqidian' || fail "pm list 里没看到两个包"

step "4. ime enable/set ADBKeyboard(期望 default_input_method=$IME)"
A shell ime enable "$IME" >/dev/null
A shell ime set "$IME" >/dev/null
sleep 0.5
got="$(A shell settings get secure default_input_method | tr -d '\r')"
[ "$got" = "$IME" ] || fail "默认 IME 是 $got,不是 $IME"
echo "default_input_method=$got"

step "5. am start mock(期望前台 = com.xunjia.mockqidian)"
A shell am start -n "$MOCK_ACT" >/dev/null
sleep 2
fg="$(A shell dumpsys window 2>/dev/null | tr -d '\r' | grep -E 'mCurrentFocus|mFocusedApp' | head -2)"
echo "$fg"
echo "$fg" | grep -q mockqidian || fail "mock 没在前台"

step "6. uiautomator dump 能拿到 mock 的 resource-id(RPA 读类原语的根)"
A exec-out uiautomator dump /dev/tty 2>/dev/null | tr -d '\r' | grep -o 'com.xunjia.mockqidian:id/[a-z_]*' | sort -u | head -8 \
  || fail "dump 里没有 mockqidian 的 resource-id"

step "7. screencap 出 PNG(取证原语)"
A exec-out screencap -p > /tmp/side_a_verify.png
head -c 8 /tmp/side_a_verify.png | od -An -c | head -1
[ "$(stat -c %s /tmp/side_a_verify.png)" -gt 1000 ] || fail "screencap 太小"

if [ "${1:-}" = "--no-selftest" ]; then echo; echo "到 7 步全通,按 --no-selftest 停止。"; exit 0; fi

step "8. mailbus(127.0.0.1:2526)没起就起一个"
if ! ss -ltn | grep -q ':2526 '; then
  (cd "$ROOT" && nohup "$PY" -m relay.mailbus serve > "$HERE/logs/mailbus.out" 2>&1 &)
  sleep 2
  ss -ltn | grep -q ':2526 ' || fail "mailbus 没起来,看 $HERE/logs/mailbus.out"
  echo "已后台起 mailbus(结束后自行 kill)"
else
  echo "mailbus 已在监听"
fi

step "9. selftest(期望 15/15 passed,约 5 分钟;会 RESET mock 种子数据并清 state/cursor.json、done.json)"
cd "$ROOT" && exec "$PY" -m relay.side_a.selftest
