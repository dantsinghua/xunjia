# IM 采集端对接说明（给采集端开发）

询价工作台已经按本文的字段在调用采集端。采集端按本文改，工作台不用再改配置项名字。

工作台代码：`imlink/client.py`（它实际发出的请求）、`imlink/store.py`（配置项）、`web/src/pages/Link.vue`（页面）。采集端仓库：`src/qtrade_agent`，前缀 `/api/v1`，默认端口 **17600**。

本文日期：2026-09-22。

---

## 0. 工作台已经做完的，和还没做的

已做完，采集端不要再改这些名字：

- 顶栏「采集通道」页。每台采集机一条记录，可同时保存多条。
- 三种方式：`local` 本机、`lan` 局域网、`public` 互联网。字段见第 2 节。
- 「保存并测试」只做读，不发消息。顺序见第 4 节。
- 测试通过后才能「设为当前」。意向询价、机器人监控共用这一条，用来显示企点号和读通讯录。
- 同一企点号（`self_uid`）后测通的机器占住名额，先测通的那条被标成「踢下线」。这是工作台自己的记账，采集端不用实现跨机器互踢。企点客户端本来就会踢：A 机登入 02，B 机上的 02 掉线。采集端要做的是把掉线后的 `state` 如实报出来。
- 发私聊走现有 `POST /api/v1/accounts/{id}/send`。互联网方式带 `"async": true`，本机和局域网不带（按采集端默认，同步等待）。

还没做，采集端本期不要依赖：

- `callback_url` 只被工作台存下来。工作台**没有** `POST /api/im/events`。现在不要把新消息推到这个地址，推了也没人收。回呼留到下一期。
- 「开始询价」还不会按通讯录自动发第一句。监控页的人工发送框也还没改走采集端。采集端先把第 4、5、6 节的读和发做通。

2026-09-22 实测：Windows 和 WSL 的 `127.0.0.1:17600` 都没有进程监听（`ERR_CONNECTION_REFUSED`）。Agent 没启动时，工作台测试的第一步就会失败。

---

## 1. 工作台怎么拼地址

| 工作台 `mode` | 实际请求的基址 |
|---|---|
| `local` | 配置项 `local.base_url`，去掉末尾 `/`。默认 `http://127.0.0.1:17600` |
| `lan` | `{lan.scheme}://{lan.host}:{lan.port}`。默认 scheme `http`，端口 `17600`。host 只许主机名或 IP，不许带 `://` 和路径 |
| `public` | 配置项 `public.base_url`，去掉末尾 `/`。页面要求是 `http` 或 `https` 地址 |

所有路径都接在这个基址后面，例如 `http://192.168.1.11:17600/api/v1/accounts?channel=qidian`。

工作台**不跟随 3xx**。采集端对这些接口不要返回跳转。跳了测试直接失败。

超时：读请求 8 秒，发消息 30 秒。

每个请求都带：

```
Accept: application/json
X-QT-Api-Min: 1.0
```

采集端现行 API 版本必须仍是 `1.0`。若因 `X-QT-Api-Min` 回 `426`，测试在「版本」这一步失败。给询价工作台建的调用方，`api_version_min` 填 `1`，不要填得比现行版本高。

---

## 2. 配置项一一对应

工作台一条通道里，三种方式的字段都存着，但**只有当前 `mode` 那一组会用来发请求**。密钥留空表示不修改。

### 2.1 三种方式共用

| 工作台字段 | 谁填 | 采集端要满足的 |
|---|---|---|
| `name` | 人，用来区分 A 机 / B 机 | 无。不传给采集端 |
| `mode` | `local` / `lan` / `public` | 决定下面用哪一组鉴权和监听 |
| `account_id` | 人，或测试时若该机只有一个企点账号则自动填上 | 必须等于 `GET /accounts` 返回的 `id`，例如 `qd01`。不是企点号。企点号在 `self_uid` |

### 2.2 `local` 本机

| 工作台字段 | 默认 | 采集端配置 / 行为 |
|---|---|---|
| `local.base_url` | `http://127.0.0.1:17600` | Agent 监听回环。现有键 `[api] port=17600`。浏览器和 curl 打的就是这个地址。WSL 里的工作台访问 Windows 上的 Agent 时，`127.0.0.1` 不是同一台，要把 `base_url` 改成工作台真能连上的地址，仍选「本机」方式 |
| `local.bearer_token` | 空 | 请求头 `Authorization: Bearer <这串>`。见第 3.1 节。空令牌时工作台不带头，只有回环上的健康检查能过，版本接口会 401，测试失败。所以本机方式也要填令牌 |
| `local.callback_url` | `http://127.0.0.1:8120/api/im/events` | **本期忽略。** 工作台未实现该接收地址 |
| （无 `tls_insecure`） | — | 本机按 `base_url` 的 scheme 连接，校验证书 |

### 2.3 `lan` 局域网

| 工作台字段 | 默认 | 采集端配置 / 行为 |
|---|---|---|
| `lan.scheme` | `http` | `https` 时采集端要在 `[api] https_port`（默认 17601）上提供 TLS，工作台端口填 17601。`http` 时填 17600 |
| `lan.host` | 空 | 采集机的局域网 IP 或主机名。Agent 必须听在非回环地址上：`[api] bind="0.0.0.0"`（这是现有默认）。只听 `127.0.0.1` 时，别的机器连这个 host 会被拒绝 |
| `lan.port` | `17600` | 与 scheme 对应的监听端口 |
| `lan.bearer_token` | 空 | 同 2.2，`Authorization: Bearer`。局域网来的健康检查**不能**免鉴权（免鉴权来源只有回环和 WSL 网关）。工作台看到健康接口 401 会继续打版本接口，版本接口必须凭该令牌返回 200 |
| `lan.tls_insecure` | `false` | `true` 时工作台不校验 TLS 证书（自签名）。`false` 时证书必须被工作台那台机器信任 |
| `lan.callback_url` | 空 | **本期忽略** |
| 防火墙 | 页面文案要求放行 | 现有规则名 `QTrade-Agent-17600-LAN`（`[net] firewall_rule_agent_lan`）。安装器默认不建入站规则。局域网方式要调用 WinAgent `POST /wa/v1/firewall/ensure`，body `{"lan": true}`，否则 Windows 防火墙会把 17600 拒掉，工作台表现为连不上 |

A 机、B 机各建一条 `lan`。A 机改登另一个企点号，只改这条的 `account_id` 并重测，不要新建一条去代表「同一台机器的另一个号」，除非这台机器上的 Agent 同时登录了多个企点账号（每个 `id` 一条）。同一企点号不要两台同时 `state=running`。

### 2.4 `public` 互联网

| 工作台字段 | 默认 | 采集端配置 / 行为 |
|---|---|---|
| `public.base_url` | 空 | 完整基址，建议 `https://域名:17601`。采集端 API **不要绑公网 IP**，用域名或反代指到 17600/17601 |
| `public.app_id` | 空 | HMAC 头 `X-QT-AppId`。必须是 `api_clients.app_id`，且 `auth_kind=hmac`、`enabled=1`、未吊销 |
| `public.hmac_secret` | 空 | HMAC 密钥明文。对应建客户端时**只返回一次**的 `secret`。工作台用它按第 3.2 节签名。采集端用 Vault `vault://api/<app_id>` 里的同一串验签 |
| `public.tls_insecure` | `false` | 同局域网 |
| `public.callback_url` | 空 | **本期忽略** |
| Bearer | 工作台互联网方式**不送** `Authorization` | 中间件必须允许「只有 HMAC、没有 Bearer」通过全部 `/api/v1`。这是现有实现（`hmac_inbound.py`：HMAC 客户端不需要 Bearer） |

互联网与局域网的账号互斥相同：后登录的机器踢掉先登录的同一企点号。工作台靠两次测试读到的 `self_uid` 是否相同来记账。

---

## 3. 鉴权：工作台发出的头，采集端要认的客户端

### 3.1 本机、局域网 → Bearer

工作台在有令牌时附加：

```
Authorization: Bearer <local.bearer_token 或 lan.bearer_token>
```

采集端现有校验：`sha256(token)` 等于 `api_clients.secret_hash`，且 `auth_kind='bearer'`、`enabled=1`、`revoked_ms` 为空。

在采集端控制台（管理员）建调用方，**一次性**把返回的 `token` 填进工作台的 Bearer 令牌。现有接口：

```
POST /api/v1/settings/api-clients
```

本机 / 局域网用这份 body：

```json
{
  "name": "询价工作台-A机",
  "auth_kind": "bearer",
  "level": "write",
  "allow_accounts": ["qd01"],
  "allow_ops": ["*"],
  "ip_allow": [],
  "api_version_min": 1
}
```

- `level` 必须是 `write`。测试只读，但工作台发私聊要写。`read` 能测通，一点发送就 403。
- `allow_accounts` 写这台 Agent 上允许工作台使用的企点账号 `id`。工作台选中的 `account_id` 必须在里面。想让这台机器 01、02 都能被选，就写成 `["qd01","qd02"]` 或 `["*"]`。
- `allow_ops` 用 `["*"]` 即可（星号只展开非危险操作，`send_text` 属于可放行的写操作）。不要漏掉发文本。
- `ip_allow` 空数组表示不限制来源 IP。若要限制，填**询价工作台**访问采集端时的源 IP，不是采集机自己的 IP。
- 响应顶层有 `token`（不在 `data` 里）。明文只此一次。填进工作台后，采集端再也读不回。轮换用 `POST /api/v1/settings/api-clients/{app_id}/rotate`，把新 `token` 再填进工作台，否则旧测试会因指纹变化作废，版本接口 401。

不要把控制台自己的 `console` 令牌拿来填。给工作台单独建一个调用方。

### 3.2 互联网 → HMAC

工作台**不送 Bearer**。四个头：

| 头 | 值 |
|---|---|
| `X-QT-AppId` | `public.app_id` |
| `X-QT-Timestamp` | 秒级 Unix 时间，十进制字符串 |
| `X-QT-Nonce` | 32 个十六进制字符（16 字节随机） |
| `X-QT-Signature` | `v1=` 加小写 hex |

签名字符串（六行，用 `\n` 连接，末行后没有额外换行）：

```
METHOD
PATH
CANONICAL_QUERY
sha256hex(BODY 或空字节)
TIMESTAMP
NONCE
```

- `METHOD` 大写。
- `PATH` 是路径，不含域名和 query。例：`/api/v1/accounts`。
- `CANONICAL_QUERY`：按 key 字典序，`k=v` 用 `&` 连接，key 和 value 都做 RFC3986 编码（`quote(s, safe="-._~")`）。无 query 时这一行是空串，但分隔用的 `\n` 仍在。`GET /api/v1/accounts?channel=qidian` 的这一行是 `channel=qidian`。
- BODY：无 body 时对**空字节串**做 SHA256，不是空字符串本身省略。GET 的 body 哈希是 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
- `HMAC-SHA256(hmac_secret, 上面的字符串)`，hex 小写，前面加 `v1=`。

时钟差超过 `[api] hmac_clock_skew_s`（默认 300 秒）回 401，`error.message` 为 `timestamp skew`。nonce 在 600 秒内对同一 `app_id` 不可重复。

建客户端：

```
POST /api/v1/settings/api-clients
```

```json
{
  "name": "询价工作台-公网",
  "app_id": "xunjia-panel",
  "auth_kind": "hmac",
  "level": "write",
  "allow_accounts": ["qd01"],
  "allow_ops": ["*"],
  "ip_allow": [],
  "api_version_min": 1
}
```

响应顶层的 `secret` 填进工作台 `public.hmac_secret`，响应里的 `app_id` 填进 `public.app_id`。采集端把同一 secret 放进 Vault `api/<app_id>`。`ip_allow` 空 = 不限（现有 `hmac_inbound._ip_allowed`）。公网若要限制，填工作台出口 IP。

失败顺序保持现有实现，工作台只把 HTTP 状态和 `error.message` 显示出来：app 不存在 401，IP 不在名单 403，时间戳 401，nonce 重放 401，签名错 401，限流 429，账号或权限不够 403。

---

## 4. 测试时工作台按这个顺序打采集端

全部是 GET。任何一步连接失败（拒绝、超时）测试结束。版本失败则不再往下。通讯录失败则测试不通过。

### 4.1 `GET /api/v1/system/health`

- 本机回环、无令牌：允许 200（现有免鉴权来源含 `127.0.0.1`）。
- 局域网 / 互联网、无令牌或令牌无效：可以 401。工作台把「401 且不是本机方式」当成可继续，改看版本接口。
- 其它 HTTP 状态：工作台记一笔但仍继续打版本接口。不要靠健康接口的 500 来表达「未登录」。

### 4.2 `GET /api/v1/system/version`

必须 **HTTP 200**，JSON 里 `ok` 不为 `false`。工作台读取：

- `api_version`（字符串，现行 `1.0`）
- `agent.version`

401/403 时测试停止，页面显示 `error.message`。这是令牌或 HMAC 对不对的判定点。

### 4.3 `GET /api/v1/accounts?channel=qidian`

必须 HTTP 200，体形：

```json
{
  "ok": true,
  "data": [
    {
      "id": "qd01",
      "channel": "qidian",
      "label": "现券A台",
      "self_uid": "30002",
      "state": "running"
    }
  ]
}
```

工作台只保留 `channel` 为 `qidian` 或空的行。用到的字段只有这四个：

| 字段 | 要求 |
|---|---|
| `id` | 非空。工作台把它存成 `account_id`，后面所有路径用它 |
| `label` | 显示名，可空。空则工作台改显示 `self_nick`（若你放在行里） |
| `self_uid` | **登录成功后的企点号，纯数字字符串。** 工作台用它判断「是不是同一个号」。A 机与 B 机读到同一个 `self_uid`，后一次测试把前一台标为被踢。未登录时不要编一个号 |
| `state` | 现有账号状态。登录可用请用 `running`。被另一台踢下线后不要继续报 `running`，应变成 `login_required` 或你们现有的掉线态 |

`data` 为空：测试失败，文案「这台采集端上没有企点账号」。

多个账号且工作台这条没填 `account_id`：测试失败，把 `data` 里的账号画出来让人点选。点选后工作台把 `id` 写入 `account_id` 再测一次。

`account_id` 不在 `data` 里：测试失败。

只有一个企点账号且 `account_id` 为空：工作台自动采用那个 `id`。

### 4.4 通讯录

先请求：

```
GET /api/v1/accounts/{account_id}/contacts
```

**这个路径采集端现在没有，会 404。** 404 时工作台改打：

```
GET /api/v1/sessions?account_id={account_id}&kind=private&limit=200
```

会话接口现有，但只有聊过的人，不是通讯录。要让「读取通讯录」名副其实，必须新增 4.4 的 contacts，返回 200。返回 200 之后工作台不再读会话。

`contacts` 的 `data` 每项：

```json
{
  "uin": "415011447",
  "name": "李静",
  "remark": "国泰君安",
  "kind": "private"
}
```

| 字段 | 要求 |
|---|---|
| `uin` | 对端企点号，纯数字。不要加 `qd01:` 前缀，不要加 `g_` |
| `name` | 显示名。优先备注，没有备注再用昵称。空则工作台显示 uin |
| `remark` | 备注，可空串 |
| `kind` | `private`。`group` / `troop` 会被工作台丢掉 |

也接受会话那种形状（仅作兼容，新接口不要用）：`native_id` 当 uin，`id` 为 `qd01:415011447` 时工作台取冒号后半段。`kind=group` 或 uin 以 `g_` 开头的群不要放进这个列表。

最多被工作台采用 200 条。空列表算测试通过（「通讯录 0 人」）。

实现要点（采集端还没做，这是本期要写的代码）：

- 只读企点主库 `{uin}.db` 的好友表，不要打开聊天页，不要发消息。
- SQL 与询价仓库 `relay/side_a/qidian_monitor_all.py` 的 `Names.SQL` 一致：`Friends` 的 `uin/remark/name`，以及 `QidianExternalInfo` 的 `uin/nickname`。
- `uin` 用逐字节 XOR，密钥 17 字节 `02:00:00:00:00:00`（现有 `adapters/qidian/xor.py`）。
- **名称列不是逐字节。** 按 UTF-16 码元逐个 XOR 同一把密钥。现有 `xor.py` 只有逐字节，直接拿去解中文名会是乱码。算法以 `qidian_monitor_all.py` 的 `cxor` / `name_of` 为准。备注优先于昵称。
- 账号未登录、主库打不开：不要回 200 空列表冒充成功。回 503，body 含 `error.message`，工作台会把测试判失败。
- 该接口是读操作，走现有 Bearer 或 HMAC，`level=read` 即可。不要要求单独的危险确认。

---

## 5. 发私聊（工作台「采集通道」页的发送，以及以后询价发送）

```
POST /api/v1/accounts/{account_id}/send
Content-Type: application/json
```

本机、局域网的 body：

```json
{
  "session": "qd01:415011447",
  "text": "bid 102584288.IB 25宜春发展MTN003 5000W 估值+10能出么",
  "idempotency_key": "xj-lk_ab12cd34-415011447"
}
```

互联网在同一对象里多一个 `"async": true`。

| 字段 | 要求 |
|---|---|
| `session` | `{account_id}:{对端uin}`。uin 来自通讯录的 `uin`。工作台若收到的 uin 已经带 `qd01:` 前缀，会先去掉再拼，避免双前缀 |
| `text` | 工作台已做过红线闸（成交词、内部字段）。采集端仍按现有发送闸门校验：打开的会话必须是这个 uin，校验不过就不要发 |
| `idempotency_key` | 必填，≤128 字符。同一把键重复提交必须按现有幂等返回，不能再发一条 |
| `async` | 只有互联网为 `true`。此时请按现有公网语义尽快回 **202**，体为 `{"ok":true,"trace_id":"...","accepted":true}`。本机和局域网不传 `async`，工作台等 HTTP 200 或 202，最多 30 秒 |

工作台把下列状态当成发送失败并展示 `error.message`：除 200 和 202 以外的所有状态，包括 401、403、404、400、429、503。

`trace_id` 放在响应顶层（现有 `result_view` / 202 体都是顶层）。工作台读的是顶层 `trace_id`，不要只放在 `data` 里。

登录态不是 `running` 时，保持现有行为：拒绝发送并回 `LOGIN_REQUIRED`。被踢下线的那台必须走这条，不能把消息发进已经失效的会话。

---

## 6. 账号互斥：采集端要保证的状态，不是新协议

业务事实：同一个企点号不能两台同时在线。A 机登录 02 时，B 机上已登录的 02 被企点踢下线。

采集端：

1. 登录成功后，`GET /accounts` 里该行的 `self_uid` 为这个企点号，`state` 为 `running`。
2. 被踢后，同一行 `state` 改为非 `running`（`login_required` 即可），`self_uid` 不要改成另一个号。主库若已不可读，通讯录接口回 503，不要回这份账号的旧好友列表冒充在线。
3. 一台 Agent 上可以有多个企点账号槽（`qd01`、`qd02`）。工作台一条通道只绑定其中一个 `id`。操作员在 A 机把登录从 01 换成 02 后，在工作台把该条的 `account_id` 改成新的 `id` 再测。B 机那条若 `self_uid` 与 A 机这次相同，工作台会把它标为被踢，并从询价页、监控页的下拉框移除，直到 B 机重新登录并重测。
4. 不要在采集端做「发现另一台 Agent 也登了这个号就远程踢它」。踢人是企点自己的登录行为。

---

## 7. 采集端本期开发清单

按这个顺序做。前四项是配置和启动，第五项是要写的新接口。

1. **把 Agent 拉起来并监听 17600。** 现在 `127.0.0.1:17600` 连接被拒绝，工作台无法测试。本机方式下，`curl -sS http://127.0.0.1:17600/api/v1/system/health` 应返回 200。
2. **局域网：确认 `[api] bind` 为 `0.0.0.0`，并建防火墙规则 `QTrade-Agent-17600-LAN`。** 从询价工作台所在机器 `curl http://<采集机IP>:17600/api/v1/system/health` 应连得上（401 可以，连接拒绝不行）。
3. **为工作台建 Bearer 调用方**（第 3.1 节），把一次性 `token` 交给工作台填进 `local.bearer_token` 或 `lan.bearer_token`。用该令牌 `GET /api/v1/system/version` 必须 200。
4. **互联网才需要：建 HMAC 调用方**（第 3.2 节），`app_id` 与 `secret` 交给工作台填 `public.app_id`、`public.hmac_secret`。用第 3.2 节的签名 `GET /api/v1/system/version` 必须 200，且不带 Bearer。
5. **新增 `GET /api/v1/accounts/{account_id}/contacts`**，形状见第 4.4 节。名称列用 UTF-16 XOR。未登录回 503，不要回 200 空数组。
6. **核对现有接口字段没改名：** `accounts[].id/channel/label/self_uid/state`，`sessions` 的 `native_id/kind/name`，`POST .../send` 的 `session/text/idempotency_key` 和顶层 `trace_id`。
7. **被踢下线后 `state` 离开 `running`，且发送回 `LOGIN_REQUIRED`。**

不要做：

- 不要新增工作台没调用的配置键去替换上表字段名。
- 不要让上述 GET 返回 302。
- 不要把通讯录做成 UI 自动化翻页。只读主库。
- 本期不要向 `callback_url` 推送。

---

## 8. 联调时工作台怎么填（给联调的人，不是给采集端改代码）

本机：方式选本机，地址 `http://127.0.0.1:17600`，Bearer 填第 3.1 节的 `token`，保存并测试。

局域网两台：

| 通道名 | 方式 | host | 端口 | Bearer | 账号 |
|---|---|---|---|---|---|
| A机 | 局域网 | A 的局域网 IP | 17600 | A 这台 Agent 上建的 token | 测通后点 `qd01`（或该机唯一账号） |
| B机 | 局域网 | B 的局域网 IP | 17600 | B 这台 Agent 上建的 token | 另一个企点号 |

两台都测通后，询价页和监控页的下拉框里出现这两条。A 机改登 B 正在用的那个企点号并重测之后，B 那条应变为「已被 A机 踢下线」，下拉框里只剩 A。

互联网：方式选互联网，基址填 `https://...:17601`，AppId 和 HMAC 密钥用第 3.2 节那一对。账号互斥与上表相同。
