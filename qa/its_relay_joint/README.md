# its-relay 两端隔离联验

本目录验证 Windows IM 采集端与 xunjia-agent 的文件通道。只替换外部边界：its-relay 网关、Dify 返回、原生发送终点均为显式替身；生产 HTTP 客户端、SM4 鉴权、协议封装、账号路由、业务 API、SQLite 账本、归档、ChatflowClient 和 Vue 页面使用实际代码。

## 不会做的操作

不读取真实机构密钥，不访问真实网关、OceanBase、ADB、WinAgent，不启动现役账号，不发送真实 IM，不改现役配置或旧账本，不安装候选包。Python 网络连接限制为 loopback。每次使用临时 SQLite 和合成身份；测试完成后关闭自建服务。Dify 替身不是模型准确率验收，原生替身回执不是企点送达证明。

`real_native_http.py` 是本轮仓库外单独授权的真实主库只读证据，不包含在这个可重复的通用测试入口中。该脚本没有复制到此目录，通用测试不会默认读取用户消息。

## 网关合同

按用户提供的新版 HTML：`/its-relay/v1/api/files/{pending,upload,download}?path=...`；真实 SM4-CBC 令牌解密与时间校验；key/path 分队列；multipart 字段 file；同名不覆盖；FIFO 下载即取走；空队列204；400/401/413/502、取走后404或断流均按明确场景模拟。待取24小时按用户部署要求模拟。

不提供远端合同没有的 ACK 接口。可靠性确认由两端原有签名协议处理，不能把 pending=0 当作业务成功。

## 执行

使用依赖完整的 Python 测试环境，需要 pytest、PyYAML、cryptography、FastAPI、httpx、uvicorn。浏览器组另需 Playwright 及 Chromium；构建组需要项目已安装的 node_modules。入口不会联网安装依赖。

```bash
python qa/its_relay_joint/run.py \
  --collector-root /path/to/windows-im-project \
  --output /path/to/isolated-evidence \
  --suite all --build-web
```

分组：`--suite http`（20项），`--suite media`（16项），`--suite browser`（1项）；browser 需要该 output 下已构建的 web-dist，或同时指定 --build-web。用独立输出目录保留每轮 XML/日志，不重复提交同一长任务。

## 覆盖矩阵

- 机构隔离、同名文件FIFO、令牌错误与过期、超限文件拒绝。
- 目录发现、每登录工作台单账号、多账号同对手隔离、公共组织架构与个人好友、显式通讯录刷新请求。
- 实时消息、重复去重、原生保护门、初始指令与回执、停止/取消、状态查询、UNKNOWN跨重启及只读查证、会话重置握手。
- 上传成功但响应丢失、下载取走后断流、24小时远端过期后本地数据补传、坏文件隔离。
- 最近7天原生数据库格式夹具、私聊文件-2005、群文件-2017、有/无扩展名图片、原图摘要与大小校验、缩略图不能冒充原图。
- 分块字节传输、哈希验证、按账号授权下载；历史不触发议价。
- 真实 Chromium 页面：图片解码、文件下载字节、离线置灰、页面异常检查。未拦截业务 API。
- 发送侧历史缓存清理及新引用保留；不触碰命令账本、实时游标或 UNKNOWN。

## 已知边界

不证明真实网关502已恢复、不证明所有企点原生附件格式都通过实样验收、不覆盖Windows安装/UAC冷启动。当前原生主库实读样本为文本；图片/文件字节来自按已核对格式构造的夹具。旧完整采集端套件的12项合同红测、南银接收适配的2项兼容缺陷不由本目录自动取消或修改断言。

所有产品修复及发布仍须按完整回归、包内源码核对、真实链路和旧状态接续要求验收。