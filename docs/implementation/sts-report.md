# STS / OSS 认证批次交接

安琳，本批实现位于 `/home/anlin/.codex/worktrees/b372/xunjia-agent`，分支 `codex/xunjia-chat-remediation-20261013`。没有写原工作区、提交、推送、重启或访问真实 STS/OSS。环境为独立 Python 3.12.3、cryptography 50.0.2；依赖文件由主审维护。

## 已实现

- `imlink/sts_credentials.py`：SM4-CBC、PKCS7、随机 16 字节 IV、标准 Base64，明文为 UTF-8 的 `key` 与字符串秒级 `ts`。认证头名为 `access_token`。HTTPS 校验保持开启，拒绝转发认证头的重定向。
- `CredentialSet` 为冻结快照，AK/SK/token 不进入 repr；响应字段完整校验、RFC3339 带时区时间转换 UTC、可选 bucket 约束。进程内按配置共享缓存；5 分钟提前刷新、5 秒重试间隔、并发 singleflight、原子替换；请求预算不足则刷新或拒绝。显式 force 刷新失败也遵守退避。过期 token 仅撤销对应旧快照。
- `imlink/oss_http.py`：GET/PUT 使用一组快照构建 URL/头/签名，`x-oss-security-token` 参与 V1 canonical headers。仅明确 `SecurityTokenExpired`/`ExpiredToken` 的 403 最多刷新重试一次；AccessDenied/SignatureDoesNotMatch 不重试。重试保持对象 key、正文与 command_id 不变；拒绝 HTTP 明文 endpoint。显式注入 CredentialSet 同样检查过期及请求预算。
- `imlink/oss_catalog.py`：目录和通讯录通过统一入口读取，默认生产依赖只从 STS 获取凭据，bucket 取 STS 响应。原 `_open_request` 工作区改动保留。
- `imlink/oss_send.py`：上传、ACK GET 使用统一入口，保留原 AES 密封、对象名和业务状态逻辑。OSS 错误日志只保留状态和白名单错误 Code，不记录远端 Message/底层异常文本。
- `tests/developer/test_sts_credentials.py`：开发者红绿测试 20 项。独立验收由 `acceptance_tests` 维护，未修改验收用例或放松测试门。

## 接口与配置

```python
STSCredentials(
    *, auth_key, sm4_key: bytes,
    url="https://nytoken.yingmi.com/api/sts",
    transport=None, clock=time.time,
    refresh_window=300, request_budget=65,
    retry_backoff=5, timeout=20, expected_bucket=None,
)
provider.get(*, force=False, min_validity=None) -> CredentialSet
provider.invalidate(snapshot)
# STS transport(url, headers, timeout) -> (status, body)

OSSHTTP(*, endpoint, credentials=None, transport=None)
client.request(method, name, body=None, timeout=20) -> (status, body)
# OSS transport(method, url, headers, body, timeout) -> (status, body)

load_live_catalog(*, transport=None, now=None, credentials=None)
load_qidian_book(workbench_id, account_uid, relative, *, transport=None, credentials=None)
LiveUplink(*, credentials=None, get_transport=None, put_transport=None)
```

`XUNJIA_STS_AUTH_KEY` 必填；`XUNJIA_STS_SM4_KEY` 按 UTF-8 原文字节读入且恰好 16 字节，不猜 hex/base64。`XUNJIA_STS_URL` 默认文档地址但本批没有请求它；`XUNJIA_STS_EXPECTED_BUCKET` 可选。`XUNJIA_OSS_ENDPOINT` 优先于旧 `OSS_ENDPOINT`。对象密封仍使用 `OSSBOX_KEY_XUNJIA`。

经主审同意保留一处有界旧调用兼容：目录函数**明确注入 transport**、没有 STS 配置且有旧 AK fixture 时，离线依赖入口可用旧凭据签名；默认生产调用与 LiveUplink 不走该路径。纯 authorization / put_authorization 仍允许不传 security_token，新增可选参数支持短期凭据签名。无 `PYTEST_CURRENT_TEST` 或其他测试环境特判。

## 验证证据

工作根均为上述独立 worktree；测试外网 DNS/连接由 QA `tests/conftest.py` 禁止，所有请求使用替身。

1. 初始开发红测：13 failed，原因均为 STS/统一 OSS 模块尚未实现。后续 GET/PUT 接入、force 退避、快照过期、HTTPS、日志白名单和动态预算均先观察红测再修复。
2. 最新定向命令：`.venv/bin/python -m pytest -q tests/acceptance/test_sts_protocol.py tests/developer/test_sts_credentials.py tests/test_oss_catalog.py` → **58 passed，退出 0**（独立验收 27、开发者 20、原目录 11）。独立验收包含 OpenSSL 固定中文向量和完整块 PKCS7 校验。
3. `.venv/bin/python -m compileall -q imlink/sts_credentials.py imlink/oss_http.py imlink/oss_catalog.py imlink/oss_send.py` → **退出 0**。
4. `git diff --check -- imlink/sts_credentials.py imlink/oss_http.py imlink/oss_catalog.py imlink/oss_send.py tests/developer/test_sts_credentials.py` → **退出 0**。
5. `timeout 1800 .venv/bin/python -m pytest -q` → **退出 2**，在 `relay/side_a/echo_loop_test.py` 收集时 `import db_reader` 失败；原始日志 `sts-full-pytest.log`。这是整个仓库的收集边界问题，已交主审/QA，未自行排除脚本。
6. `timeout 1800 .venv/bin/python -m pytest -q tests` → **172 passed / 9 failed / 14 errors，退出 1**，原始日志 `sts-tests-pytest.log`。该次在多角色并行实现中运行，不能作为最终整仓验收。失败涉及：
   - `tests/acceptance/test_auth_entrypoint.py` 的未配置认证，以及匿名访问、身份隔离、SSO 校验、篡改/health 用例 setup 错误（主审认证入口尚在实现）；
   - `tests/developer/test_auth_repository.py` 的 proof/token expiry、binding/account isolation、transaction rollback 三项（主审开发红测）；
   - `tests/test_org_tree_spec.py::test_frontend_tree_sorts_and_renders_only_the_open_branch`（前端源码文字断言）；
   - `tests/test_rfq_oss_send.py::test_deposit_key_alone_does_not_upload`、`test_failed_ack_is_not_delivered_and_hides_text`（主审失败状态/提示路径）；
   - `tests/test_rfq_send_gate.py::test_upload_without_receipt_is_not_ok`；
   - `tests/test_rfq_status.py::test_send_failure_and_delivery_persist_without_touching_live_files`（旧 status 路径 404）。

## 未决边界

STS 批次定向绿灯不代表整仓零回归。主审继续完成上列跨批次红项及最终冻结版本全量验证。真实认证地址可达性、证书链、bucket 策略/V1 是否允许、现场上传/ACK 互通仍按本次限制未执行。生产 secrets 未配置，不能声称真实发送成功。
