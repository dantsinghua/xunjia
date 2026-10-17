# STS MQ接入更新

安琳2026-10-13提供了新仓库和MQ请求示例。本次通过已有SSH身份只读克隆`its/its-accesspoint`，核对提交`ac26e9ed72f816e2a19b9e1d01c9572bd8d214a4`；副本在`/tmp/xunjia-its-accesspoint-20261013-ac26e9ed`。未修改认证服务源码或部署。

核对`internal/token/token.go`与`internal/handler/handler.go`：令牌算法和前次HTML一致。JSON为key+字符串秒级ts，SM4-CBC/PKCS7，随机16字节IV前置，标准有padding Base64；校验正负60秒。接口成功返回access_key_id/access_key_secret/security_token/expiration/bucket，expiration为RFC3339。服务内部路由是`/api/sts`；MQ通过用户给出的网关完整地址访问，客户端不自行追加/裁掉前缀。

- 默认HTTPS地址继续保留：`https://nytoken.yingmi.com/api/sts`。
- MQ显式配置：`http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts`。
- HTTP只放行上述完整MQ地址；相似域名、错误路径、追加query/fragment均拒绝。不开放通用HTTP降级，不跟随携凭据重定向。
- OSS对象正文仍采用原AES-GCM密文布局；此次未更换对象正文算法。

独立部署profile中的`oss.sts_url`可直接填MQ完整地址；使用全局凭据工厂时设置`XUNJIA_STS_URL`。机密通过`XUNJIA_STS_AUTH_KEY`和`XUNJIA_STS_SM4_KEY`注入；SM4必须原始16字节，不猜hex或Base64。

为避免一分钟令牌被复制后失效，新增本地请求生成入口（只生成，不自动请求）：

```bash
# 先通过部署环境注入认证key与SM4 key，勿把真实值写入仓库或命令历史。
export XUNJIA_STS_URL='http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts'
.venv/bin/python -m imlink.sts_request --output /PRIVATE_PATH/sts-request.curl
# 生成后一分钟内执行；返回包含临时凭据，保存位置必须私有。
umask 077
curl --config /PRIVATE_PATH/sts-request.curl --output /PRIVATE_PATH/sts-response.json
```

生成的请求文件为0600且拒绝覆盖已有文件。安琳贴出的示例token、临时AK/SK及security_token均未写入仓库、测试夹具或报告。新增验收使用合成值，不把样例临时凭据当成固定配置。

当前验证是源码协议核对与注入传输回归；尚未用真实环境认证key调用MQ服务，不能声称MQ网络/部署已通过。本机未配置Go编译工具，本次没有声称跑过认证服务Go测试。
