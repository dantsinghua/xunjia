#!/usr/bin/env bash
# ============================================================================
# qidian_cold_start.sh —— 企点「冷启动 → 免登 → 搜索 → 私聊 → 发送」端到端骨架
# ----------------------------------------------------------------------------
# 2026-09-19 实测跑通(anlin/qidian-rpa)。环境:
#   redroid11_x86_64 容器 qtrade-redroid,屏 720x1280@320dpi;
#   企点是 arm64 APK 经 libndk_translation(libnb.so)翻译层运行;
#   ADBKeyboard 为默认 IME(中文输入必需)。
#
# 设计原则(与 04-redroid-rpa.md / 00-共享基线 §11.3 一致):
#   * 分步可单独执行,排障时只跑某一段;
#   * 每步带判据,失败即 exit 非 0、不往下走;
#   * 发送是唯一会「离开本机」的动作,默认关闭 —— 只有子命令 send / all --send 才发;
#     其余全部只到「进入聊天页 + 标题留证」为止。
#
# ⚠️ 三个必踩坑(不写下来下次必再踩):
#   [坑1] `adb root` 后设备常卡 offline,`adb reconnect` 救不回 ——
#         必须容器内 `stop adbd; start adbd` + 宿主 `adb kill-server; adb start-server`。
#         (现象是"adb 挂了"、根因在容器内 adbd,本脚本 _ensure_root 已封装)
#   [坑2] frida-server 在 /data/local/tmp(持久卷),容器重启后二进制还在,
#         但进程要重起、且需 adb root。
#   [坑3] 中文只能走 ADBKeyboard 的 ADB_INPUT_B64 base64 广播,`input text` 打不了中文。
#
# ⚠️ 坐标依赖分辨率(720x1280@320dpi 实测)。换屏/换版本需重定位(见 C_* 变量)。
# ⚠️ 标题强校验的限制:企点主界面/聊天页有常驻动画,`uiautomator dump` 永远 idle 不了,
#     拿不到聊天页标题文本。本脚本 open-chat 用「前台 Activity 弱校验 + 名片页 best-effort
#     dump 校验 peer + 聊天页截图留证」三管齐下;真正的标题==peer 强校验需接离线 OCR(TODO)。
# ============================================================================
set -uo pipefail

SERIAL="${SIDE_A_SERIAL:-emulator-5554}"
CONTAINER="${QIDIAN_CONTAINER:-qtrade-redroid}"
PKG="com.tencent.qidian"
SPLASH="$PKG/com.tencent.mobileqq.activity.SplashActivity"
FRIDA_BIN="/data/local/tmp/frida-server"
HERE="$(cd "$(dirname "$0")" && pwd)"
SHOT_DIR="$HERE/logs/shots"
mkdir -p "$SHOT_DIR"

# 坐标(720x1280@320dpi 实测)
C_SEARCH="360 189"     # 消息列表页顶部搜索框
C_RESULT="207 300"     # 搜索结果第一条
C_SENDMSG="527 1096"   # 好友名片页「发消息」按钮
C_INPUT="320 1123"     # 聊天页输入框
C_SENDBTN="647 1086"   # 聊天页「发送」按钮(输入框有内容时出现)

A(){ adb -s "$SERIAL" "$@"; }
say(){ printf '\n\033[1;34m[%s]\033[0m %s\n' "$(date +%T)" "$*"; }
ok(){ printf '\033[1;32m  \xe2\x9c\x93 %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m  \xe2\x9c\x97 %s\033[0m\n' "$*" >&2; exit 1; }
fg(){ A shell dumpsys window 2>/dev/null | tr -d '\r' | grep -oE 'mCurrentFocus=Window\{[^}]*\}' | head -1; }
shot(){ local n="$1"; A exec-out screencap -p > "$SHOT_DIR/$n.png" 2>/dev/null && ok "截图 $SHOT_DIR/$n.png"; }

# 中文/任意文本输入:走 ADBKeyboard base64 广播(坑3)
input_text_b64(){
  local txt="$1" b64
  b64="$(printf '%s' "$txt" | base64 | tr -d '\n')"
  A shell am broadcast -a ADB_INPUT_B64 --es msg "$b64" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# step: restart-redroid —— 物理重启 redroid 容器(⚠️ 会中断登录态外的负载;需授权)
# 判据:docker restart 返回 0
# ---------------------------------------------------------------------------
step_restart_redroid(){
  say "[1] restart-redroid —— 重启容器 $CONTAINER(不碰 WSL)"
  docker restart "$CONTAINER" >/dev/null 2>&1 || die "docker restart 失败(权限?容器名?)"
  ok "容器已重启"
}

# ---------------------------------------------------------------------------
# step: wait-boot —— 等 redroid 引导完成
# 判据:设备在线 + sys.boot_completed=1(120s 内)
# ---------------------------------------------------------------------------
step_wait_boot(){
  say "[2] wait-boot —— 等 sys.boot_completed"
  A wait-for-device 2>/dev/null || true
  local i
  for i in $(seq 1 40); do
    [ "$(A shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { ok "boot_completed=1(约 $((i*3))s)"; return 0; }
    sleep 3
  done
  die "120s 内 sys.boot_completed 未变 1"
}

# ---------------------------------------------------------------------------
# _ensure_root —— adb root 提权,并处理 offline(坑1)
# ⚠️ 这里的 `adb kill-server` **仅限本单实例开发环境**:本机 adb 靠启动时自动扫描 loopback:5555 得到
#   emulator-5554,没有 `adb connect` 这条连接可单独重连,只能重置 server。
#   产品口径(基线 §15g R6-28 / 06 §2.9.5 / 04 §2.7.4③):多账号走 `adb connect 127.0.0.1:160NN`,
#   提权后的 offline 恢复 = 容器内重启 adbd + **只 disconnect/connect 该账号**,绝不 kill-server
#   (kill-server 会把同机其它企点账号一并打 offline)。移植到 Agent 时别照抄下面这行。
# ---------------------------------------------------------------------------
_ensure_root(){
  [ "$(A shell whoami 2>/dev/null | tr -d '\r')" = "root" ] && { ok "已 root"; return 0; }
  A root >/dev/null 2>&1 || true
  sleep 3
  local i st
  for i in $(seq 1 6); do
    st="$(adb devices | grep "$SERIAL" | awk '{print $2}')"
    if [ "$st" = "device" ] && [ "$(A shell whoami 2>/dev/null | tr -d '\r')" = "root" ]; then ok "adb root 成功"; return 0; fi
    warn "设备态=$st,按坑1恢复(容器内重启 adbd + 宿主重置 adb server)"
    docker exec "$CONTAINER" sh -c "stop adbd; sleep 1; start adbd" >/dev/null 2>&1 || true
    adb kill-server >/dev/null 2>&1; sleep 2; adb start-server >/dev/null 2>&1; sleep 4
  done
  [ "$(A shell whoami 2>/dev/null | tr -d '\r')" = "root" ] || die "adb root 无法就绪(坑1 未恢复)"
  ok "adb root 成功"
}

# ---------------------------------------------------------------------------
# step: root —— adb root 提权(★标准动作,2026-09-19 安琳定:冷启动必做)
# 为什么是标准动作:读取正线 = 旁路读库(db_reader.py),读 /data/data/com.tencent.qidian 下的
#   *-IndexQQMsg.db 必须 root;冷启动后 adbd 默认非 root ⇒ 不提权则「能发不能读」、发送后无法读回确认。
#   此前提权藏在 frida 步里,而 frida 已不在读取链上(会被跳过)⇒ 提权跟着被跳过。现拆成独立步。
# 判据:whoami == root,且能列到 *-IndexQQMsg.db(企点未登录过时只告警不失败)
# ---------------------------------------------------------------------------
step_root(){
  say "[3] root —— adb root 提权(读库正线的前置)"
  _ensure_root
  if A shell "ls /data/data/$PKG/databases/ 2>/dev/null" | tr -d '\r' | grep -q 'IndexQQMsg.db'; then
    ok "读库前置满足(*-IndexQQMsg.db 可见)"
  else
    warn "已 root,但暂未见 *-IndexQQMsg.db(企点未登录过/库路径变更?读库会失败,以 db_reader.py 报错为准)"
  fi
}

# ---------------------------------------------------------------------------
# step: frida —— 启动 frida-server(坑2)。⚠️ 非标准动作:读取已改旁路读库,frida 不在读取链上,
#   `all` 默认不跑;仅在需要 frida 辅助排障时单独手动跑。
# 判据:设备端 ps 里有 frida-server
# ---------------------------------------------------------------------------
step_frida(){
  say "[3b] frida —— 启动 frida-server(可选,非标准动作)"
  _ensure_root
  A shell "ls $FRIDA_BIN" >/dev/null 2>&1 || die "$FRIDA_BIN 不存在,先 adb push frida-server(见 relay/side_a/frida/)"
  if A shell "ps -A | grep [f]rida-server" | tr -d '\r' | grep -q frida-server; then ok "frida-server 已在跑"; return 0; fi
  A shell "chmod 755 $FRIDA_BIN; setsid $FRIDA_BIN >/dev/null 2>&1 < /dev/null &"
  sleep 2
  A shell "ps -A | grep [f]rida-server" | tr -d '\r' | grep -q frida-server && ok "frida-server 已起" || die "frida-server 未起"
}

# ---------------------------------------------------------------------------
# step: qidian-start —— 冷启动企点
# 判据:企点主进程出现 + 前台是 qidian
# ---------------------------------------------------------------------------
step_qidian_start(){
  say "[4] qidian-start —— 冷启动企点"
  if A shell "ps -A | grep [q]idian" | tr -d '\r' | grep -q "$PKG"; then
    ok "企点进程已在"
  else
    A shell am start -n "$SPLASH" >/dev/null 2>&1 || die "am start 失败"
    warn "冷启动中,等 12s..."; sleep 12
  fi
  A shell "ps -A | grep [q]idian" | tr -d '\r' | grep -q "$PKG" || die "企点主进程未出现"
  fg | grep -q "$PKG" || { warn "前台暂非企点,再等 3s"; sleep 3; }
  fg | grep -q "$PKG" && ok "企点在前台" || die "企点未在前台"
}

# ---------------------------------------------------------------------------
# step: check-login —— 检查登录态(免登恢复)
# 判据:前台 Activity 属于 qidian 且不含 login(在登录页=未免登)
# ---------------------------------------------------------------------------
step_check_login(){
  say "[5] check-login —— 检查是否免登进入主界面"
  local f; f="$(fg)"
  echo "  前台: $f"
  shot "login_check"
  echo "$f" | grep -qi "$PKG" || die "企点不在前台"
  if echo "$f" | grep -qi "login"; then
    die "停在登录页(登录态未恢复)—— 企点无无人扫码,需人工扫码/短信登录(截图见上)"
  fi
  ok "已免登进入主界面(登录态从 /data 卷恢复)"
}

# ---------------------------------------------------------------------------
# step: search <peer> —— 搜索联系人
# 判据:进入搜索页 GroupSearchActivity
# ---------------------------------------------------------------------------
step_search(){
  local peer="${1:-}"; [ -n "$peer" ] || die "search 需要 peer 参数"
  say "[6] search —— 搜索 '$peer'"
  A shell input tap $C_SEARCH; sleep 1.5
  fg | grep -qi "SearchActivity" || { warn "未进搜索页,重试一次"; A shell input keyevent BACK; sleep 1; A shell input tap $C_SEARCH; sleep 1.5; }
  fg | grep -qi "SearchActivity" || die "未能进入搜索页"
  input_text_b64 "$peer"; sleep 3
  shot "search_$peer"
  ok "已输入 '$peer' 并触发搜索(结果见截图)"
}

# ---------------------------------------------------------------------------
# step: open-chat <peer> —— 点结果 → 名片页 → 发消息 → 聊天页
# 判据:①名片页 best-effort dump 校验含 peer;②进入聊天页;③截图留证
# ---------------------------------------------------------------------------
step_open_chat(){
  local peer="${1:-}"; [ -n "$peer" ] || die "open-chat 需要 peer 参数"
  say "[7] open-chat —— 进入与 '$peer' 的私聊"
  A shell input tap $C_RESULT; sleep 2
  local f; f="$(fg)"; echo "  点结果后前台: $f"
  # 名片页 best-effort 校验(名片页较静态,dump 可能成功)
  if echo "$f" | grep -qi "Card"; then
    shot "card_$peer"
    if A shell uiautomator dump /sdcard/_card.xml >/dev/null 2>&1; then
      A pull /sdcard/_card.xml /tmp/_card.xml >/dev/null 2>&1
      grep -q "$peer" /tmp/_card.xml 2>/dev/null && ok "名片页校验含 '$peer'" || warn "名片页 dump 未见 '$peer'(以截图为准)"
    else
      warn "名片页 dump 失败(idle),以截图 card_$peer.png 为准"
    fi
    A shell input tap $C_SENDMSG; sleep 2
  else
    warn "点结果未进名片页(可能直接进聊天/结果布局变化),继续尝试"
  fi
  f="$(fg)"; echo "  当前前台: $f"
  echo "$f" | grep -qi "$PKG" || die "未在企点内(前台=$f)"
  shot "chat_$peer"
  warn "标题强校验(标题==$peer)受聊天页 idle 限制无法自动做 —— 请核对截图 chat_$peer.png(TODO: 接离线 OCR)"
  ok "已进入聊天页(截图留证)"
}

# ---------------------------------------------------------------------------
# step: send <peer> <text> —— 发送(⚠️ 唯一会离开本机的动作;默认不跑)
# 判据:发送前后各截图;发送后输入框应清空
# ---------------------------------------------------------------------------
step_send(){
  local peer="${1:-}" text="${2:-}"
  [ -n "$peer" ] && [ -n "$text" ] || die "send 需要 <peer> <text> 两个参数"
  say "[8] send —— ⚠️ 外发动作:给 '$peer' 发「$text」"
  A shell input tap $C_INPUT; sleep 1
  input_text_b64 "$text"; sleep 1.5
  shot "send_before_$peer"
  warn "发送前留证 send_before_$peer.png —— 请确认聊天对象==$peer、内容==「$text」"
  A shell input tap $C_SENDBTN; sleep 2.5
  shot "send_after_$peer"
  ok "已点发送(前后截图 send_before/after_$peer.png)。请核对气泡是否出现、输入框是否清空"
}

usage(){
  cat <<EOF
用法: $0 <step> [args]   —— 企点 冷启动→发送 骨架(分步可单独执行)

  restart-redroid            重启 redroid 容器(⚠️ 需授权,会中断容器内其它负载)
  wait-boot                  等 sys.boot_completed=1
  root                       ★标准动作:adb root 提权(读库正线前置;自动处理 offline 坑)
  frida                      (可选)起 frida-server —— 读取已不用 frida,all 不含此步
  qidian-start               冷启动企点
  check-login                检查是否免登进主界面(停登录页则失败)
  search <peer>              搜索联系人
  open-chat <peer>           进入与 peer 的私聊(到聊天页 + 留证为止)
  send <peer> <text>         ⚠️ 发送(唯一外发动作,须显式调用)

  all <peer> [--send "文本"] 串起 root→qidian-start→check-login→search→open-chat
                             默认只到「进入聊天页」;仅当带 --send 才执行发送。
                             (刻意不含 restart-redroid —— 那步太重,单独手动跑)

环境变量: SIDE_A_SERIAL(默认 emulator-5554)、QIDIAN_CONTAINER(默认 qtrade-redroid)
EOF
}

main(){
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    restart-redroid) step_restart_redroid ;;
    wait-boot)       step_wait_boot ;;
    root)            step_root ;;
    frida)           step_frida ;;
    qidian-start)    step_qidian_start ;;
    check-login)     step_check_login ;;
    search)          step_search "${1:-}" ;;
    open-chat)       step_open_chat "${1:-}" ;;
    send)            step_send "${1:-}" "${2:-}" ;;
    all)
      local peer="${1:-}"; shift || true
      [ -n "$peer" ] || die "all 需要 <peer>"
      local sendtext=""
      while [ $# -gt 0 ]; do case "$1" in --send) sendtext="${2:-}"; shift 2;; *) shift;; esac; done
      step_root
      step_qidian_start
      step_check_login
      step_search "$peer"
      step_open_chat "$peer"
      if [ -n "$sendtext" ]; then
        step_send "$peer" "$sendtext"
      else
        say "all 完成(到进入聊天页为止)。未传 --send,不发送。"
      fi
      ;;
    ""|-h|--help|help) usage ;;
    *) usage; die "未知步骤: $cmd" ;;
  esac
}
main "$@"
