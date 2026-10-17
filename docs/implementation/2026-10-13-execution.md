# 安琳的询价平台改造执行合同

实施授权：2026-10-13安琳明确“开始改造、多个子agent并行、设计pytest、针对性与全量测试、goal确保0回归0红”。设计已准予实施，不再重复索要开始许可。

唯一实施目录：`/home/anlin/.codex/worktrees/b372/xunjia-agent`，分支`codex/xunjia-chat-remediation-20261013`。原目录`/home/anlin/work/xunjia-agent`不由子agent写入。188个当前源码/文档文件已从脏工作树覆盖到隔离区，原始哈希与副本在`docs/implementation/baseline.json`指向的位置。不要基于旧HEAD重写用户未提交内容。

规格：`docs/2026-10-13-remediation-design.md`、`docs/2026-10-13-dify-chat-agent-design.md`、`docs/2026-10-13-sts-integration-design.md`及最新用户裁决优先。任务状态只由claude-mem work_state维护；本文件记录接口和裁决，不维护第二份进度表。

共同约束：中文汇报称安琳；模型继承；不再派子agent；不commit/push/pull/reset；不删除运行数据、不连接真实STS/OSS/Dify/企点、不发真实价格、不重启既有容器/WSL/Windows。所有网络发送用替身，测试数据用临时目录。原`.venv`不改，隔离区使用独立`.venv`。禁止以PYTEST_CURRENT_TEST或关闭生产校验使测试变绿。

裁决：用户要求并行，覆盖技能默认顺序执行；按文件所有权避免冲突。用户禁止自动提交，覆盖技能自动commit。D10/D11此前未单选，本次授权按文档推荐采用可逆的50行服务端分页和真实xlsx导出。所有关键旧行为以独立验收规格决定；验收者拥有测试门，实现者不改松门。

第一批所有权与接口：

| 角色 | 独占实现/文件 | 交付与测试 |
|---|---|---|
| STS实现 | `imlink/sts_credentials.py`、可新增`imlink/oss_http.py`、`imlink/oss_catalog.py`、`imlink/oss_send.py`；开发者测试`tests/developer/test_sts_*.py` | SM4令牌、原子短期凭据缓存、OSS token签名；不得改commands/routes业务状态或原验收测试 |
| 前端实现 | `web/src/**`、`web/package*.json`、`web/tests/developer/**` | 构建修复、真实状态/错误、选择/分页/实时、认证请求、策略DTO；不得改后端或独立验收 |
| 独立验收 | `tests/acceptance/**`、`tests/conftest.py`、`web/tests/acceptance/**`、测试运行脚本；既有测试契约修订仅该角色提案并记录理由 | 先跑基线、设计红测，覆盖HTTP/组件入口而非复制实现；不得改产品代码 |
| 主审实现 | 其余后端，尤其`server/**`、`imlink/routes.py,commands.py,dify_inquiry.py,inquiry_rules.py,rfq_status.py`、`mock/mock_server.py`及行情；文档、依赖配置 | 事务/认证/Chat运行器整合，后续有界委派工具/双通道；不能改独立验收门 |

STS公共接口先锁定：

```python
@dataclass(frozen=True)
class CredentialSet:
    access_id: str
    access_secret: str
    security_token: str
    expiration: datetime
    bucket: str

def build_access_token(auth_key: str, sm4_key: bytes, timestamp: int, iv: bytes | None = None) -> str: ...
class STSCredentials:
    # 参数和注入细节可实现者补全，但须先通知验收者；无真实认证默认。
    def get(self, *, force: bool = False) -> CredentialSet: ...
def credentials_from_env() -> STSCredentials: ...
```

普通HTTP测试可直接挂功能router并注入临时Store；生产认证在`server.app:create_app`统一装配，独立验收必须测实际生产入口而非只测裸router。新认证协议为存款项目兼容`POST /api/auth/sso`、`GET /api/auth/me`；Header采用Bearer及X-Auth-Token兼容；无合法配置默认拒绝，不开开发后门。后端最终返回本人user_id/display_name/account_uid/binding及版本。

前端暂保留当前API路径，明确策略DTO转换，后端新实现保持路径并补本人域状态。写响应失败不fallback；发送请求生成稳定request_id并在同次重试沿用。新增接口需求先发主审固定合同，不自行造成功模拟。

后续批次边界：

1. 事务仓储与身份、停用旧GET副作用；读改写的控制与版本形成真值。
2. Chat会话/SSE/受控提案/目标/澄清额度/记忆，四只读工具与现场数据接口。
3. OSS/邮件统一命令和事件身份、未决对账、取消屏障；采集端同等合同独立联调。
4. 行情撤价/类型/估值/游标恢复；真实详情和导出。
5. Dify锁版Chatflow交付包、独立测试环境能力验证；保留当前服务运行状态。
6. 全量pytest、前端构建/测试、受控端到端、终审、精确集成回原工作树。

每项验收需记录命令、退出码、通过/失败/跳过数量。已有红测不能隐藏；规格变化造成的旧测试失配由独立验收者记录裁决、改成实际用户合同。不会把数据库/主数据未接入或真实通道未验证假称完成；现场主数据本期授权为接口留口，因此可验证NOT_CONFIGURED行为。

只提交到版本库的代码/文档不是本轮自动动作。全部实现与验证完成前，goal保持active；没有完成的事项持续交接，不能因一批测试绿就提前结束总目标。
