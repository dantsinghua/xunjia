# 172.26.64.36 网络维护

安琳明确要求完成网络维护保障功能。本次将原有 jump_probe.sh 的手工探测扩展为独立维护服务，和询价业务机升级包分开部署。保留现役 nginx 转发配置，不安装额外 Python 包，不修改路由、DNS、防火墙或证书。

固定检查 50443、11443、12443、13443 四条既有 TLS 透传映射，核对 nginx systemd 启动命令、真实 master 进程及有效配置。30080 按已有本地 HTTP 服务只读观察。陌生配置停止自动操作并给出诊断码，不猜测现场配置。

检查域名解析、直接上游和本机转发。认证拒绝和要求客户端证书属于不同于 TCP 不通的结果，不发送账号密码和业务请求。持续 DNS 地址变化或直连正常而本机转发连续失败才尝试平滑重载；nginx 未运行时可在校验配置后启动。每次动作前重新核验主机、进程与配置，先运行 nginx -t；300 秒冷却，每小时至多三次，预算落盘避免守护进程重启后重置。

安装器只操作专用维护服务和固定安装目录。记录原文件及启动状态，安装失败恢复；升级、卸载保留运行状态、动作预算及日志。安装成功需确认新进程已产生本轮状态，不把 Type=simple 的短暂 active 当成完成。

本机验证采用合成 systemd、隔离文件系统、真实回环 HTTP/TLS 与隔离 nginx。交付文件独立存放桌面；未经现场运行，不声称银行出口已恢复。历史抓包存在公网 SYN 无回包，重载无法修复上游防火墙或出口故障；这类情况明确降级并保留证据。

官方依据：
- https://nginx.org/en/docs/control.html
- https://nginx.org/en/docs/switches.html
- https://nginx.org/en/docs/stream/ngx_stream_upstream_module.html#server
- https://www.freedesktop.org/software/systemd/man/249/systemd.service.html

最终交付：桌面 `Xunjia-Jump-Network-Maintenance-20261029.zip`，SHA256 `b59b6d74dbf10341483502bbcdd42b8e63f0f6388121aa17fb48353d1ddb861f`。在跳板机解压目录中以 root 执行 `sh install.sh`；只读预检为 `sh install.sh --check`。维护服务初次巡检就绪等待上限为 240 秒，避免恢复流程尚在运行时误报安装完成。

最终独立验收 30 条与开发测试 37 条，共 67 passed；真实回环 TLS/mTLS、隔离 nginx 平滑重载及错误配置阻断通过，源码漂移为空。证据：`/home/anlin/.local/state/xunjia-network-acceptance-20261029/final-frozen/FINAL.json`。Python 3.7 语法、systemd unit 静态验证与入口篡改拒绝通过。真实现场 systemd 与网络仍待安琳执行安装后核验。
