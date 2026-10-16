# Cloud-0.1.2.3.1 —— Agent Endpoint Contract Alignment（极小 Gate）

> 阶段：Architecture Gate（设计 + 影响审计）。**本次只交付裁决 + 机器化护栏，不实现。**
> 方法：只读审计 + 机器化护栏（`tests/test_cloud_0_1_2_3_1_agent_endpoint_contract_alignment.py`）。
> 基线：Cloud-0.1.2.2（Connect Agent Architecture Gate，GO：凭据归 Connection）
> → Cloud-0.1.2.2.1（credential_ref 可选对齐，已落地）
> → Cloud-0.1.2.2.2 / 0.1.2.2.2.1（Connection Health，已落地）
> → Cloud-0.1.2.3（Connect Agent Golden Path Gate，GO：纯编排 / 接纳孤儿 Agent）
> → **本 Gate 0.1.2.3.1（兑现 0.1.2.3 §11 的 F-1）**。
>
> **本 Gate 的中心问题（单一裁决标尺）**：
>
> ```
> Agent.endpoint            到底是什么？
> Connection.config.endpoint 又是什么？
> 二者会否构成同一个「访问 endpoint」的双重事实（double fact）？
> ```
>
> 一句话：**`endpoint` 是「到目标的访问地址」，属于路径（Connection.config），
> 不属于目标（Agent）。`Agent.endpoint` 只是 Agent Identity 的可选描述性元数据，
> 在 runtime 从不被消费。必须在写 Connect Agent UI 之前裁决清楚，
> 因为它决定 Agent Identity 与 Connection Access Path 的数据边界是否真正成立。**

---

## 0. 结论先行（GO / BLOCKED）

**GO。** 只读审计后判定——**这个问题当前已是（近似）正确的，本 Gate 把它正式化并机器锁死，
不需要修改 OSS 合同。**

**裁决：**

```
Agent.endpoint              = Agent Identity 的「可选描述性元数据」
                              （可省略；即使填写也不参与任何连接）
Connection.config["endpoint"] = 唯一权威的「实际访问 endpoint」
                              （ACPAdapter.send / probe 只读它）
```

- **不是双重事实**：只有 `Connection.config["endpoint"]` 驱动连通性；`Agent.endpoint` 是死元数据
  （与 0.1.2.2 中 `Agent.credential_ref` 完全同构）。
- **Agent.endpoint 不再是（也从来不是）Cloud Connect Agent 的必需输入**：
  `AgentCreate.endpoint: str | None = None`（schema 层可选）、`Agent` 领域字段默认 `None`、
  应用层只拒「空白串」而**允许 `None`**；WorkBuddy 配方 `POST /api/agents {"name","type"}` 早已如此。
- **现有 OSS 合同允许**：管理合同（11.2/11.3）本就声明 endpoint 可选；OSS `type="local"`
  Local First Success 路径（15.2.3）**从不读** `Agent.endpoint`（用 `Connection.config["prefix"]`）。
  故本裁决无需任何合同变更即可成立。

**一处极小残留不对称（follow-up 0.1.2.3.1.1，**已落地**）**：`endpoint=""` / `"   "` 原为 422，
而省略（`None`）201。这是「可选元数据」语义下的唯一毛刺。**0.1.2.3.1.1 已镜像 0.1.2.2.1 的
credential_ref 归一化（create 侧 `None/""/纯空白 → None`）抹平**，只处理 create、不碰 PATCH。

---

## 1. 审计事实（readonly，逐条可核对）

| # | 事实 | 证据 |
|---|---|---|
| F1 | `Agent.endpoint` 领域字段**可选**（`str \| None = None`） | `app/domain/agent.py:23` |
| F2 | API 契约 `AgentCreate.endpoint: str \| None = None` —— **schema 层已非必填** | `app/schemas/agents.py:36` |
| F3 | `AgentUpdate.endpoint: str \| None = None` —— PATCH 亦可选 | `app/schemas/agents.py:48` |
| F4 | `AgentResponse.endpoint` 在响应字段面（**可见元数据**，非写-only） | `app/schemas/agents.py:66` |
| F5 | 应用层 `_require_non_blank(input.endpoint)` **只拒空白串，允许 `None`** | `app/application/agents.py:68-70,147` |
| F6 | `_require_valid_endpoint_url(None)` 直接返回（`None` 合法）；**提供**则须 http/https | `app/application/agents.py:87-106` |
| F7 | **运行时从不读 `Agent.endpoint`**：全 app 只见 `request.agent_id`（int），无 `agent.endpoint` / `request.agent.endpoint` | 全 app grep |
| F8 | ACPAdapter 只从 `Connection.config["endpoint"]` 取访问 endpoint（`send` + `probe`） | `app/adapters/agents/acp.py:469,663,377,419` |
| F9 | LocalAgentAdapter（OSS `type="local"`）不读 `Agent.endpoint`，用 `Connection.config["prefix"]` | `app/adapters/agents/local.py:63-66` |
| F10 | MultiAgentAdapter 只读 `agent.type`（选协议实现），**不读 endpoint** | `app/adapters/agents/registry.py:58-62` |
| F11 | WorkBuddy 配方：`POST /api/agents {"name":"workbuddy","type":"acp"}`（**不传 endpoint**）；endpoint 落在 `Connection.config.endpoint` | `examples/wecom-workbuddy/README.md:49-55` |
| F12 | **Connection 域 / 模型 / schema 均无顶层 `endpoint`**——endpoint 只在 `config` JSON 内 | `app/domain/connection.py`、`persistence/models/connection.py`、`schemas/connections.py` |
| F13 | `AgentModel.endpoint` 列 `nullable=True`（纯元数据列，无 FK / 无约束语义） | `app/persistence/models/agent.py:21` |

---

## 2. 裁决 A —— `Agent.endpoint` = Agent Identity 的可选描述性元数据

**语义冻结：**

```
Agent.endpoint:
  - 归属：Agent Identity（目标身份的「公开描述」）
  - 可选：None 合法；提供时须为 http/https URL（F6）
  - 可见：出现在 AgentResponse（F4）——它是描述信息，不是凭据（故非写-only）
  - 不被消费：runtime / adapter 从不读它（F7/F9/F10）
```

- 与 0.1.2.2 ① 一脉相承：**Agent 承载「公开可达的注册/描述信息」**（name / type / endpoint /
  非凭据类 config），**不承载任何参与连接的秘密或地址事实**。endpoint 作为「对外描述地址」
  可以留在 Agent 上，但它**不等于**连接时真正使用的地址。
- 与 `Agent.credential_ref` 同构：都是「看起来像连接要素、实则是死字段」——0.1.2.2.1 已把
  `credential_ref` 降级为可选引用，本 Gate 把 `endpoint` 明确为可选元数据。

**为何不做「彻底删除 `Agent.endpoint`」**：删除会破坏既有管理合同（11.2/11.3 已冻结其字段面
与 `AgentResponse` 回显），且它是无害的纯描述元数据。**保留但明确其非连接语义** 是更小、
更安全、且不触碰 OSS 合同的裁决。

---

## 3. 裁决 B —— `Connection.config["endpoint"]` = 唯一权威访问 endpoint（无双重事实）

**语义冻结：**

```
Connection.config["endpoint"]:
  - 归属：Connection（Access Path 的「实际访问参数」）
  - 权威：ACPAdapter.send / probe 唯一读取源（F8）
  - 与 Agent.endpoint 无任何同步 / 回退 / 优先级关系
```

- **铁律**：**连通性只有一个事实来源 = `Connection.config["endpoint"]`。**
  `Agent.endpoint` 既不作为 fallback，也不与 config 做「取非空者」之类合并——
  任何「Agent.endpoint 兜底 Connection.config.endpoint」的实现都是**双重事实**，一律禁止。
- 这与 0.1.2.2 ④「config 承载真实连接参数（含 secret）」完全一致：endpoint 与 bearer_token /
  runtime_id 一样，是**访问路径参数**，归 Connection。

---

## 4. 裁决 C —— 现有 OSS 合同是否允许？允许，且已是现状

| 检查项 | 结论 | 依据 |
|---|---|---|
| 管理合同是否允许省略 `Agent.endpoint` | **允许** | `AgentCreate.endpoint: str \| None = None`（F2）；`create_agent` 接受 `None`（F5/F6） |
| OSS `type="local"`（Local First Success）是否依赖 `Agent.endpoint` | **不依赖** | LocalAgentAdapter 只读 `Connection.config["prefix"]`（F9） |
| Runtime 链是否依赖 `Agent.endpoint` | **不依赖** | F7/F10 |
| 是否需要改 OSS 合同 | **不需要** | endpoint 可选本就是 11.2/11.3 冻结合同；本 Gate 只**正式化 + 锁死** |

**结论**：`Agent.endpoint` 作为「可选描述性元数据」**本就被 OSS 合同允许**；
本裁决不引入任何合同变更，故对 OSS 用户零影响。

---

## 5. follow-up 0.1.2.3.1.1 —— 空白归一化（已落地）

| # | 偏差 | 影响 | 修复（0.1.2.3.1.1，已落地） |
|---|---|---|---|
| D1 | `endpoint=""` / `"   "` → 422，而省略（`None`）→ 201 | 与「endpoint = 可选元数据」语义不对称（空串被拒、缺省被收） | `create_agent` 侧镜像 0.1.2.2.1：新增 `_normalize_endpoint`（`None/""/纯空白 → None`，非空原样保留且仍 URL 校验）。**只处理 create**；`update_agent`（PATCH）保持既有冻结合同（`_require_non_blank(change.endpoint)` 不变）。 |

D1 不改变安全边界（endpoint 仍非连接事实、仍不参与 runtime），只是把「空串 vs 缺省」的
输入语义抹平。已由 0.1.2.3.1.1 落地。

---

## 6. 机器化护栏（Verification Gate）

新增 `tests/test_cloud_0_1_2_3_1_agent_endpoint_contract_alignment.py`
（**静态 + 轻行为护栏，当前即可绿灯**，锁死 §2/§3/§4）：

1. `test_agent_endpoint_is_optional_metadata`：`AgentCreate`/`AgentUpdate` 的 `endpoint` 非必填
   （`is_required() is False`）；`Agent` 域 `endpoint` 默认 `None`；`AgentResponse` 含 `endpoint`（可见元数据）；
   `AgentModel.endpoint` 列 `nullable=True`；`AgentCreate(name, type)` 可构造（无 endpoint）。
2. `test_runtime_never_reads_agent_endpoint`：`acp.py` / `local.py` / `registry.py` / `application/agents.py`
   源码中**无** `agent.endpoint` / `request.agent.endpoint` 读取。
3. `test_actual_access_endpoint_is_connection_config`：`acp.py` 只从 `connection.config` 读 endpoint
   （含 `config.get("endpoint")`）；`Connection` 域 / `ConnectionCreate` / `ConnectionResponse` **无顶层 `endpoint` 字段**
   （endpoint 只在 config 内）。
4. `test_no_double_endpoint_fact`：`AgentModel` 有 `endpoint` 列、`ConnectionModel` **无** `endpoint` 列；
   二者不构成同一访问 endpoint 的两个持久事实。
5. `test_workbuddy_recipe_omits_agent_endpoint`：WorkBuddy 配方 `POST /api/agents` 不传 endpoint，
   endpoint 落在 `Connection.config`（证据锁定，防配方回退）。

进入实现期时，本护栏保持绿；若实施 D1（0.1.2.3.1.1），把「`""`/空白 → `None`」归一化补入该测试。

---

## 7. NOT TOUCHED（冻结边界）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Agent 域 / ORM 字段面** | `app/domain/agent.py`、`persistence/models/agent.py` | 不删除 / 不改 `endpoint` 列；仅明确其「可选元数据」语义。 |
| **Agent 管理合同** | `app/schemas/agents.py`、`api/management/agents.py` | 字段面与端点不变（11.2/11.3 冻结）；D1 若实施仅调应用层归一化。 |
| **Connection 全树** | `app/domain/connection.py`、`schemas/connections.py`、`persistence/models/connection.py`、`adapters/agents/acp.py` | endpoint 继续只在 `config` 内；不新增顶层 endpoint 字段/列。 |
| **Runtime / Adapter / SDK** | `app/adapters/*`、`agent_runtime_sdk/**`、Worker/Driver | 只从 `Connection.config` 读 endpoint，逻辑与签名不变。 |
| **OSS Local First Success** | `app/adapters/agents/local.py` | 不读 Agent.endpoint；endpoint 可选性对其零影响。 |

---

## 8. GO / 边界闭合

**GO。** 单问裁决完成且可证：

- **Agent.endpoint = 可选描述性元数据**（F1–F4）：schema 可选、领域默认 None、响应可见、
  runtime 不消费（F7/F9/F10）。
- **Connection.config["endpoint"] = 唯一权威访问 endpoint**（F8/F12）：ACPAdapter 唯一读取源，
  无 fallback / 无合并 → **无双重事实**。
- **OSS 合同允许**（F2/F9/F11）：endpoint 可选本就是冻结合同，Local 路径不依赖它，
  **无需任何合同变更**。
- **不阻塞 GO 的极小残留**：D1（空白 vs 缺省的不对称）列入 0.1.2.3.1.1，纯输入语义对齐。

机器化护栏（§6）当前即绿灯，锁死「endpoint 归属路径（Connection.config）、
Agent.endpoint 仅为可选元数据、二者不构成双重事实」。
