# OSS 临时凭据与 SM4 认证接入设计

安琳，本稿按你提供的 `C:\Users\anlin\Desktop\work\STS接口调用说明.html` 和本轮确认准备对接。**SM4-CBC只用于向认证服务申请STS凭据的access_token；OSS对象正文保留既有密封格式。** 认证服务入口预留为 `https://nytoken.yingmi.com/api/sts`，可配置。本轮不调用服务、不读取认证服务源码、不修改业务实现。

日期：2026-10-13。状态：供整体方案评审的接入合同。安琳已明确无需访问 `infra/cloud-accesspoint` 源码；前面的仓库访问受限不再是本任务阻塞项。本文以所给HTML为协议依据，不声称验证过服务实现或采集端已部署版本。

**一、三层职责必须区分**

| 层 | 用什么 | 作用与持有者 |
|---|---|---|
| 申请临时凭据 | auth.key＋16字节SM4密钥生成access_token | 询价后端证明自己可访问STS；不发给OSS |
| OSS请求认证 | 同一响应的临时AK/SK/security_token | 后端签名GET/PUT等实际请求，按expiration失效 |
| OSS对象正文 | 既有AES-GCM密封及原封装协议 | 命令、回执、目录/消息按当前两端格式互解，不因改STS而换SM4 |

auth.key是业务口令，SM4密钥是加密密钥，OSS临时SK是请求签名密钥，对象密封密钥是另一项配置。四者不能混用或相互推导。所有凭据留在后端传输层，不进入浏览器、Dify输入、会话变量、工具结果或业务审计正文。

**二、配置接口**

| 配置 | 设计约束 |
|---|---|
| `XUNJIA_STS_URL` | 推荐值`https://nytoken.yingmi.com/api/sts`；按部署配置，可在测试注入Fake transport |
| `XUNJIA_STS_AUTH_KEY` | 私密配置，无默认业务口令；对应服务端auth.key |
| `XUNJIA_STS_SM4_KEY` | 私密配置，最终必须恰为16字节；按已确认编码取值，不自动猜hex/base64 |
| `XUNJIA_OSS_ENDPOINT` | 保留现有OSS访问endpoint配置；STS响应只给bucket，不能从认证域名推算OSS地域 |
| 对象密封密钥配置 | 保留现有询价用途和编码规则，与SM4密钥分开 |
| `expected_bucket`（可选部署约束） | 若配置，则STS返回bucket不匹配即拒绝，避免写入错误环境 |

生产不以长期AK/SK静默兜底。配置不全或不合法时显示认证未就绪，相关发送留在可核对状态，不伪造上传成功。本轮没有创建任何含密钥值的配置文件。

**三、access_token的确定协议**

请求：`GET /api/sts`，头名精确为 `access_token`。

1. 原文为UTF-8 JSON：`{"key":"业务口令","ts":"Unix秒字符串"}`。ts必须是字符串，不是JSON数字，不是毫秒；字段顺序/空格不影响服务校验。
2. 使用恰16字节的SM4密钥；每次认证请求随机生成16字节IV。
3. 对原文字节进行PKCS7填充，分组长度16字节。
4. 使用SM4-CBC加密，拼成`IV || ciphertext`。
5. 使用标准Base64编码，保留`=`填充；不是URL-safe Base64，也不加未知版本前缀。
6. 将结果放入`access_token`请求头。服务解密后核key以及ts和服务器Unix秒的绝对差≤60。

每次重新请求生成新令牌，不长期复用。该协议的1分钟时效不等于文档已承诺一次性nonce消费，客户端不能自称提供了完整防重放能力。跨机访问使用TLS并校验证书；不为调通而关闭TLS验证，不擅自给兼容报文追加MAC或改变字段。

令牌密文、业务口令、SM4密钥及解密后的JSON不得写日志。可以记录请求ID、时间偏差类别和无敏感值的状态码。采用经批准的成熟SM4实现及库内填充函数，不手写密码算法；具体依赖与离线包在实施阶段锁版验证。

**四、STS响应和缓存**

成功响应字段：`access_key_id`、`access_key_secret`、`security_token`、`expiration`、`bucket`。必须完整验证后**整组原子替换**缓存；不得一部分用新AK、另一部分用旧token。

expiration按RFC3339的UTC时间解析，转换为带时区时间比较，不能把带Z值当北京时间。缓存仅保存在服务进程内存，不落盘、不回显。服务端按所给文档在距过期超过5分钟时复用凭据，进入最后5分钟时重新申请；客户端不假设每次GET一定拿到新的一组。

客户端刷新遵循：

- 同一凭据作用域内合并并发刷新请求，5个账号同时需要凭据也不会触发5轮刷新风暴。
- 提前刷新窗口初值5分钟；失败后有限退避。重复拿到同expiration时不立即无间隔重试。
- 旧凭据尚有效且剩余时间覆盖请求预算/安全余量时可继续使用；已过期或无法覆盖请求期限时拒绝新请求，不能无限使用过期缓存。
- 请求一旦取到凭据快照，AK/SK/token/bucket整体固定到该次请求结束；并发刷新不改变正在签名的请求。
- 进程重启丢失缓存后重新认证；不会重新生成已提交的业务command_id。

**五、OSS签名与security_token**

OSS临时凭据请求必须携带 `x-oss-security-token`，且按实际签名版本将它纳入签名。仅把AK/SK换成临时值而遗漏token不能完成接入；也不能把STS的access_token当作OSS安全令牌。[OSS V1 Header签名](https://www.alibabacloud.com/help/en/oss/developer-reference/include-signatures-in-the-authorization-header)、[OSS V4 Header签名](https://www.alibabacloud.com/help/en/oss/developer-reference/recommend-to-use-signature-version-4)

本仓当前是手写签名，GET与PUT有重复路径。整改为一个小的OSS请求入口：取得同批CredentialSet→构造bucket/key及headers→按明确版本签名→发送。目录/通讯录、命令、消息和回执统一使用该入口；实际使用的HEAD/LIST同样处理token，不为未用功能建设通用云SDK框架。

若继续V1，CanonicalizedOSSHeaders必须包含安全令牌；若目标bucket及正式客户端要求V4，使用相应CanonicalHeaders和版本规则。STS接口本身不决定签名版本，不混用V1/V4算法。优先沿用已验收的两端访问方式，具体bucket策略在现场验收。

本期无需预签名URL就不新增它。若后续实际需要，URL有效期不得超过STS剩余有效期，token参数还须按签名版本处理。[OSS V4 URL规则](https://www.alibabacloud.com/help/en/oss/developer-reference/add-signatures-to-urls)

bucket使用STS响应，不继续无条件写旧硬编码桶；对象命名仍按已授权workbench/consumer范围构造，不能让Dify或浏览器提交任意bucket/key。访问密钥能访问的范围不等于业务用户有权操作的范围。

**六、对象正文保持兼容**

本次不改变既有`nonce(12) || ciphertext || tag(16)` AES-GCM密封布局、用途密钥、关联数据及外层封装；也不改变命令ID、事件ID、对象键或回执业务语义。若实现中以成熟库替换手写AES，只属于等价实现替换，必须通过两端已知向量和旧报文互解，不能借此更换线格式。

不采用“先试SM4、失败再试AES”的猜算法降级。STS认证失败、OSS认证失败、对象解密失败、业务回执失败分别报错，不能互相冒充。加密成功也不代表对象字段满足权限/有效期或业务发送已经成功。

**七、失败、刷新和业务重试**

| 失败 | 对接行为 |
|---|---|
| STS 401 缺令牌/令牌无效/key不匹配 | 配置/编码错误，禁止无界重试；不退回长期AK |
| STS 401 ts过期 | 检查本地时钟与秒字符串；有限重建令牌，不自行修改系统时间 |
| STS 502/暂时网络故障 | 按预算退避；仍有效旧凭据按规则可用，否则认证未就绪 |
| OSS明确令牌过期 | 失效该缓存并合并刷新；按请求及命令幂等边界最多有界重试 |
| OSS 403权限不足/签名不符 | 按错误码区分，不把所有403当凭据过期循环刷新 |
| PUT后响应丢失 | 业务结果仍可能已提交；同对象键/同command_id对账，不新UUID重发 |
| ACK读取失败 | 保留pending/unknown，与STS恢复后继续对账；不永久假判对方未收到 |
| 对象密文校验失败 | 不返回明文、不换算法碰运气；保留无敏感值的诊断信息 |

令牌过期与授权不足的区别有官方错误定义；具体SDK/HTTP错误按现场响应核验。[OSS令牌过期说明](https://www.alibabacloud.com/help/en/oss/user-guide/0002-00000007)

认证重试不是业务消息重试。即使更换一批STS凭据，也必须继续使用原command_id、对象内容和业务版本。凭据更新不重置取消墓碑、去重记录、会话停止或收信游标。

**八、最小代码改动与验收**

建议只新增一个凭据模块和一个SM4令牌函数，收敛现有请求签名，不再复制一套OSS客户端：

- `imlink/sts_credentials.py`：配置校验、令牌生成调用、GET /api/sts、响应验证、内存缓存与并发刷新。
- `imlink/oss_catalog.py`、`imlink/oss_send.py`：改从CredentialSet取凭据，统一token与签名构造；保留现有目录、密封和对象语义。
- `imlink/crypto.py`：对象密封格式不变；SM4认证与其保持清晰职责边界。
- 所有网络入口可注入Fake HTTP/时钟/随机IV，离线验证合同，不需要真实密钥或请求生产认证服务。

独立验收至少覆盖：固定测试向量互解；中文业务口令UTF-8；非16字节密钥拒绝；PKCS7整块填充；ts字符串与秒单位；随机IV与标准Base64填充；UTC过期判断；5个并发请求合并刷新；同批凭据原子性；GET/PUT均带token并纳入签名；过期一次刷新和403权限错误不循环；刷新后原命令ID不变；AES-GCM旧对象双向互解；日志和错误无秘密；STS不可达时不存在静默长期AK兜底。

本轮只准备对接设计，正式地址的可达性、证书、bucket权限及两端运行版本尚未测试。按安琳要求，不以读取认证服务源码作为推进前置条件；所给HTML、用户裁决和受控互通测试构成交付依据。
