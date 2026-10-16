# Step 11.11 — Connection Management API

> **第三条 Management Vertical Slice 落地（镜像 11.3/11.8 的形态，但按 11.10
> 五焦点裁决贴合 Connection 语义，刻意不机械复制）。**
>
> 11.10 已冻结：Connection 后端 100% 就绪、Channel 前置解除、唯一缺口 =
> `list_connections`。本步补齐该缺口并叠加 HTTP 合同面。

## 1. 端点面（冻结）

```
POST   /api/connections         201 / 404 / 409 / 422
GET    /api/connections         200
GET    /api/connections/{id}    200 / 404
PATCH  /api/connections/{id}    200 / 404 / 422
```

不做（11.11 边界，与 11.8 同一裁决表）：**DELETE**（软下线 = PATCH status=
inactive）/ **修改 channel_id / agent_id**（immutable binding）/ **status=error**
（纯管理态白名单）/ **Connection Health Check** / **Routing**。

## 2. HTTP 合同（11.10 五焦点 → 代码）

| 焦点 | 落地 |
| --- | --- |
| ① 下拉状态 | 后端 create 保持 **existence-only**：Channel/Agent 只校验存在（`ChannelNotFoundError / AgentNotFoundError → 404`），不做 status 门禁；active-only 下拉是 UI 过滤（11.12） |
| ② config 语义 | config = Access Path 参数：**不脱敏、无哨兵、无敏感键集合**（测试断言 schema 源码无 sanitize/SENSITIVE/`********` 字样）；`credential_ref` 写-only——Create/Update 可入（引用入），`ConnectionResponse` 字段面「不在场」排除（引用不出，复制 Agent 11.3 护栏；引用轮换 = PATCH 非空串，缺省/null = 保留） |
| ③ status | **schema 级白名单** `CONNECTION_MANAGEMENT_STATUSES = {active, inactive}`——`error`（及任何其它值）在合同面直接 422，DB 不被污染；`ConnectionCreate` 无 status 字段且 extra=forbid（领域默认 active） |
| ④ immutable binding | `ConnectionUpdate` 无 channel_id/agent_id，且 **Create/Update 均 `extra="forbid"`**——「改绑定」= 422 而非静默忽略；换绑 = 旧 Connection 置 inactive + 新建（无硬删除） |
| ⑤ 后端缺口 | `ConnectionRepository` Port + Impl + Service 补 **`list_connections`**（management 陈列读：全部、含 inactive、按 id 排序）；runtime 读路径 `find_active_by_channel`（9.4）原样保留，两者互不替代 |

## 3. 与 Channel 11.8 的三个刻意差异（不是机械复制）

1. **无脱敏层**：Channel 因「凭据住 config + 响应脱敏回显」采用
   `sanitize_channel_config()`；Connection 的 config 是 Access Path 参数，
   响应所见即所存 → **不引入 sanitize**。
2. **PATCH config = 整段替换**（Channel 是 load→merge→persist）：merge 的存在
   理由是「防止脱敏回显清空密键」——Connection 无脱敏即无该问题；整段替换语义
   对连接参数更清晰（改 timeout 不必携带整个 protocol_options 结构以外的历史键）。
3. **列表只返回绑定 ID**：`ConnectionResponse` 字段面恰为
   `{id, name, channel_id, agent_id, status, config}`，**无 channel_name /
   agent_name**——名称解析由 UI 经 `/api/channels`、`/api/agents` 解耦完成，
   Service 绝不做跨实体 join（11.10 §5 冻结）。

另有差量：status 白名单在 **schema 层**强制（`field_validator`），而 Agent 的
status 白名单在 Service 层——理由同 ChannelResponse 的脱敏：值域是**合同保证**
而非业务规则，模型层强制无法绕过。

## 4. 错误映射（唯一 API 层职责）

```
DuplicateConnectionError                                  → 409 Conflict
ConnectionNotFoundError                                   → 404 Not Found
ChannelNotFoundError / AgentNotFoundError（绑定目标缺失）   → 404 Not Found
InvalidConnectionInputError（正整数/非空/config dict/引用非空） → 422
```

credential_ref 非空校验补进 Service（镜像 Agent 11.3：空串不是合法引用）。

## 5. 交付清单

生产代码：
- `app/schemas/connections.py`（新）——四模型 + status 白名单；
- `app/api/management/connections.py`（新）——`build_connections_api` 四端点；
- `app/repositories/connection.py`——Port + `list_connections`；
- `app/persistence/repositories/connection.py`——Impl + `list_connections`；
- `app/application/connections.py`——Service + `list_connections`、
  credential_ref 非空校验、config 替换语义冻结注释；
- `app/main.py`——组合根装配 `/api/connections`。

测试：
- `tests/test_step11_11_connection_management_api.py`（34 项）：六条冻结合同
  （响应字段面 / credential_ref 写-only / config 不脱敏+整段替换 / status
  白名单 / immutable binding 422 / existence-only+409）+ openapi 契约冻结
  （`ConnectionResponse` properties == 6 字段）+ AST 边界守护。
- 翻转的既有冻结守卫（13 处，全部为预期演进）：5.6 / 5.7 / 6.1 / 6.5 / 6.7 /
  7.1(×2) / 7.2 / 7.3(×2) / 9.4 / 11.6 b1 / 11.7 b1 / 11.10 g2——统一语义：
  「api/management、schemas 文件面 + connection 端口/服务方法面随 11.11 演进」；
  9.4 守卫改为「runtime 读 + management 读两条读路径并存，仍禁 find_all /
  规则表查询」。

全量回归 **1114 passed**。

## 6. 下一刀

Step 11.12 — Connection Management UI（镜像 11.5/11.9）：列表 + 表单 + 停用/启用；
新建下拉只列 active Channel/Agent（经 `/api/channels`、`/api/agents`），编辑态
绑定为只读文本（immutable binding），config 不透明 JSON 文本框，
credential_ref 写-only 占位（"留空=保留当前引用"）。
