#!/bin/bash
# adb 经 colima VM 转发:宿主 adb 起不来时的替代通道(VM 内 apt 装的 adb 直连 redroid 容器 IP)。
# 用法:SIDE_A_ADB=relay/side_a/vadb.sh SIDE_A_SERIAL=172.17.0.6:5555 python -m relay.side_a.run_a
# ssh 会把 argv 拍平成一个字符串交给 VM 的 bash,这里逐个 %q 转义,保证中文/引号/管道符原样到达 adb。
exec ssh -F "$HOME/.colima/ssh_config" colima -- "$(printf '%q ' adb "$@")"
