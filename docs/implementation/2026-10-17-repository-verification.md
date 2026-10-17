# 本机仓库定位核验：2026-10-17

- 实测用户：anlin；可用工具：本机 PowerShell。
- 开发发行版：Ubuntu-24.04。
- Git 工作区根目录：/home/anlin/work/xunjia-agent。
- 本发行版中未找到：/home/anlin/work/xunjia_agent。
- origin：SSH，git.frontnode.net，anlin/bondtrade.git；不是 GitHub。
- 核验时 HEAD：83c7e0f；原有未提交修改保留。
- WSL SSH 连接检查：22端口连接超时，尚未进入认证。
- Windows TCP 连接检查：22、443端口各3秒超时；不能据此断言密码、密钥或仓库权限错误。
- 本轮未遇到工具安全拦截，未改远端、未拉取合并、未提交推送、未启动停止中的 qtrade 发行版。
- 后续开发与测试继续使用上述 WSL 工作区，不另建桌面源码副本。
- 新业务核心目录 server/negotiation 已存在；现役旧入口尚未切换。
