# 研究：自建 STS 认证与 OSS 临时凭据协议边界

安琳，检索日期 **2026-10-13**。本轮仅核验官方公开文档；没有请求自建 STS、OSS 或真实账号，没有读取或打印配置密钥。repo-local 新版接线属于 explore 职责，已向主任务说明，本报告不声称找到了新版采集端实现。

## 请求类型

协议事实核验与实现交接。技术已指定为自建 SM4 认证 STS 服务及阿里云 OSS，不选择新依赖。

## 直接结论

**必须区分三层协议。新增 STS 认证，不等于更换 OSS 对象正文加密算法。**

| 层次 | 已知事实 | 不能据此推导 |
|---|---|---|
| 调用自建凭据服务 | 主任务已读取 `STS接口调用说明.html`，摘要为 GET `/api/sts`，`access_token=Base64(IV16 + SM4-CBC(PKCS7(JSON {key:string,ts:string})))`，Unix 秒时间窗 ±60 秒 | 这不是阿里云 OSS 的 Authorization，也不是对象正文封装 |
| 调用 OSS | 使用同批临时 AccessKeyId、AccessKeySecret、SecurityToken 签名，受 Expiration 与权限限制 | 不能只替换 AK/SK、漏掉 SecurityToken；不能把自建 access_token 当 OSS token |
| 读写对象正文 | 本轮没有正文新协议、生产者新版源代码或互通样本证据 | 不能假定正文变成 SM4-CBC，也不能仅凭旧 `crypto.py` 仍 AES-GCM 就断言新版接线不存在 |

自建协议内容来自主任务转述的已读文档 `/mnt/c/Users/anlin/Desktop/work/STS接口调用说明.html`，本轮未二次读取原文；`key` 的业务语义与 SM4 密钥来源不自行推断。OSS 鉴权部分由以下官方来源核实。

## 官方请求签名事实

1. STS 凭据由临时 AK、临时 SK、security token、expiration 组成，expiration 为 UTC；临时权限受角色及会话策略约束，不能仅凭获得 token 推断具有任意 bucket/prefix 读写权限。[STS 使用说明](https://www.alibabacloud.com/help/en/oss/developer-reference/use-temporary-access-credentials-provided-by-sts-to-access-oss)
2. **Header 鉴权**：用临时 AK/SK 计算 Authorization，并同时发送 `x-oss-security-token`。V1 明确要求把 `x-oss-security-token:<token>` 放进 `CanonicalizedOSSHeaders`；不能签好名后才追加 token。[V1 官方签名](https://www.alibabacloud.com/help/en/oss/developer-reference/include-signatures-in-the-authorization-header)
3. **V4 Header 鉴权**：Authorization 为 `OSS4-HMAC-SHA256`，签名 scope 包含日期、region、`oss`；存在的 `x-oss-*` headers 包括 security token 进入 canonical headers。V4 需要正确 region，不能从 bucket 名猜 region。[V4 官方签名](https://www.alibabacloud.com/help/en/oss/developer-reference/recommend-to-use-signature-version-4)
4. **预签名 URL 分版本**：V1 常见字段为 `security-token`，V4 为 `x-oss-security-token`，都需在计算签名前纳入对应规范化查询参数。旧 V1 字段不可直接套到 V4。[V1 token 问题说明](https://www.alibabacloud.com/help/en/oss/user-guide/0002-00000006)、[V4 URL 签名](https://www.alibabacloud.com/help/en/oss/developer-reference/add-signatures-to-urls)
5. AK/SK/token 必须来自同一 STS 响应。token 与 AK 不匹配属于凭据错误，不是对象正文解密错误；官方有 `AccessKeyIdAndSecurityTokenNotMatch` 与 `InvalidSecurityToken` 分类。[OSS 403 错误](https://www.alibabacloud.com/help/en/oss/user-guide/http-403-error-code)
6. OSS SDK 通常替调用方完成签名，但“使用 SDK”不等于所有静态 STS 初始化方式都自动刷新；必须核具体 SDK/凭据 provider。Python V1 官方示例为 `StsAuth(AK, SK, token, auth_version="v4")`，使用 V4 的 Bucket 还需 region。此处仅说明现有选定 SDK 的接线形式，不建议更换依赖。[Python SDK V1 官方指南](https://www.alibabacloud.com/help/en/oss/developer-reference/python-sdk-v1/)

## 缓存刷新与失效边界

官方支持有效期内复用、到期前刷新，以减少 STS 请求；每次响应必须包含 expiration 才能正确安排刷新。[临时上传授权指南](https://www.alibabacloud.com/help/en/oss/developer-reference/authorize-access-6)

- **硬失效**：expiration 到达后不再发起使用旧凭据的新请求。`SecurityTokenExpired` / OSS 错误编号 `0002-00000007` 表示凭据过期，需获取新的一整组凭据。[过期凭据错误](https://www.alibabacloud.com/help/en/oss/user-guide/0002-00000007)
- **URL 双期限**：使用 STS 生成的预签名 URL 不能活过 STS 凭据本身；URL 的 expires 尚未到也无效。[临时上传授权指南](https://www.alibabacloud.com/help/en/oss/developer-reference/authorize-access-6)
- **本项目服务规则与 SDK 默认值须分开**：主任务文档摘要称服务端在过期前五分钟刷新；这不等于阿里云保证所有 SDK 在那个时点刷新，也不等于本地缓存可以不检查 expiration。Browser.js 的默认 `refreshSTSTokenInterval=300000ms` 是间隔五分钟，和“到期前五分钟”语义不同。[Browser.js 初始化](https://www.alibabacloud.com/help/en/oss/developer-reference/initialization)

以下为基于官方边界的最小工程建议，**不是声称阿里云替应用实现了这些逻辑**：

1. 凭据缓存以调用身份/授权范围为键；整组保存并原子替换 `{AK, SK, token, expiration, bucket, endpoint/region}`，一个 OSS 请求固定使用同一份快照，不混搭两个 refresh generation。
2. 提前刷新设置可配置余量，至少考虑时钟偏差和请求延迟；并发请求共享一次刷新，避免所有任务同时调用 `/api/sts`。服务端返回仍有效的同一组缓存时不忙循环刷新。
3. 提前刷新失败时，只有未过期且剩余有效时间足够的旧快照才可继续服务；到硬截止时间后停止，不回退长期 AK 或无 token 签名。
4. token 过期错误可使缓存失效并有界刷新一次；`AccessDenied`、`SignatureDoesNotMatch`、bucket/region 错误应分类诊断，不能把所有 403 都变成无限刷新重试。读请求可有界重试；写请求仍遵守原有幂等/确认语义。
5. `access_token`、SM4 密钥、临时 AK/SK/token 与完整含 token 的预签名 URL 都不能进入日志；记录脱敏凭据代次、UTC expiration、请求 ID、错误类别即可。±60 秒属于自建服务认证窗，不替代 OSS 自身时间/签名规则。

## 缺失协议点与源代码交接

要完成“采集端已更换”的互通证明，主任务仍需提供/核验：

- **准确生产源版本和路径**：产生 STS access_token 的函数、刷新缓存、OSS client/token 注入路径及调用点；本报告未检查两个本地源树，不把旧快照当现状。
- **自建 STS 完整返回 schema**：字段命名/大小写、是否有 data 包装、expiration 格式、bucket 是否同时返回 endpoint/region、授权 prefix/操作范围、错误 HTTP/业务码、缓存与强制刷新语义。主任务摘要未列全这些点。
- **认证编码互通向量**：SM4 key 的字节编码/长度、`key` 字段语义、JSON UTF-8 与 ts 字符串约定、Base64 类型、IV 生成规则、密钥轮换标识；只使用无真实凭据的固定测试向量。
- **对象正文协议是否变化**：明确回答仍 AES-GCM、改 SM4，或有版本并存；若变化，必须给出算法模式、IV/nonce、padding、认证 tag/MAC、AAD、key derivation/key id、二进制/JSON 封装顺序、压缩顺序、版本标识及旧对象兼容规则。STS 文档本身不能填补这些内容。

## 最小集成交接

能力上只需分清两个接口：`CredentialProvider` 负责自建 STS 认证、缓存、整组刷新；现有 OSS 访问层使用该临时凭据快照并把 security token 纳入签名。**对象正文 codec 以已核生产者协议为准单独适配**，不得因为 access_token 用 SM4 就批量替换 AES-GCM。这里是职责边界示例，不新增依赖、不实现代码。

独立验收应至少覆盖：文档固定向量、±60 秒边界与 ts 秒/毫秒误用；请求签名确实包含 token；AK/token 代次混搭被拒绝；缓存并发刷新；到期及刷新失败；已有对象解密与新版对象互通。当前均未实际运行。

## 版本与证据限制

官方页面检索于 2026-10-13；采用 OSS V1/V4 明确版本区分。自建 STS 协议为主任务提供材料，不是阿里云标准 API。未选择依赖或升级 SDK，未声明代码已迁移、真实 OSS 已连通或正文已换 SM4。
