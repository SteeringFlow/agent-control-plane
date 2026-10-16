# Cloud-0.1.2.3.1.1 —— Agent Endpoint Optionality Normalization

> 基线：Cloud-0.1.2.3.1（Agent Endpoint Contract Alignment Gate，GO）→ **本步（最小实现）**。
> 本步只做一件事：**把 `create_agent` 的 `endpoint` 空白输入对齐成「可选描述性元数据」**。
> 对应 0.1.2.3.1 §5 的 **D1**。

一句话目标：**`endpoint` 不再因为「自己为空」而失败——create 侧完全镜像 `credential_ref`。**

```
endpoint = Agent 的可选描述性元数据   ← 可选、可空、非访问地址
config   = Connection 的访问参数载体  ← 真实访问 endpoint 在 Connection.config["endpoint"]
```

即（与 `credential_ref` 0.1.2.2.1 规则逐字一致）：

```
endpoint = None      → None      （无元数据）
endpoint = ""        → None
endpoint = "   "     → None
endpoint = "https://…" → "https://…"  （原样保留，仍须合法 URL）
```

**不是**：

```
endpoint = 实际访问地址   ← 错误语义（访问地址属 Connection.config，0.1.2.3.1 裁决）
```

---

## 1. 变更面（只动 create）

| 文件 | 变化 |
|---|---|
| `app/application/agents.py` | 新增 `_normalize_endpoint()`（`None / "" / 纯空白 → None`，非空原样保留）；`create_agent()` 删除 `_require_non_blank(input.endpoint, …)`，改为 `endpoint = _normalize_endpoint(input.endpoint)` → `_require_valid_endpoint_url(endpoint)` → 落库 `endpoint`。docstring 编码语义。 |

只改 `create_agent` 一处路径；归一化函数与 `_normalize_credential_ref` 同形同纪律
（同一模块、同一错误类型 `InvalidAgentInputError`）。

---

## 2. 语义冻结

| 概念 | 归属 | 说明 |
|---|---|---|
| `Agent.endpoint` | **optional descriptive metadata** | `None` / `""` / 纯空白 → `None`；非空字符串原样保留（**仍**由 `_require_valid_endpoint_url` 校验 http/https）。**不是**实际访问地址。 |
| `Connection.config["endpoint"]` | **实际访问 endpoint** | ACPAdapter `send` / `probe` 唯一读取源（0.1.2.3.1 裁决）。 |

**这不取消 URL 校验**——只是不再因为「空」失败：非空 endpoint 仍必须是合法 URL，否则 422。

---

## 3. 明确不改（边界）

| 项 | 状态 | 理由 |
|---|---|---|
| `update_agent()`（PATCH） | **仍要求 `endpoint` 非空白** | PATCH 合同（`缺省/null=不变、""=?、非空=覆写`）中，`""` 是「是否清除已有值」的**独立合同问题**，不在本步（与 0.1.2.2.1 对 `credential_ref` 的处理完全一致）。 |
| `AgentCreate` / `AgentResponse` schema | 零改动 | `endpoint: str | None = None` 早已可选（11.2/11.3）。 |
| Domain / ORM / Runtime / Adapter / Health | 零改动 | `Agent.endpoint` 本就不被 runtime 消费；访问 endpoint 属 `Connection.config`。 |
| Connection 全树 | 零改动 | 不新增顶层 endpoint 字段/列。 |

---

## 4. 测试演进

| 文件 | 变化 |
|---|---|
| `tests/test_step11_3_agent_management_api.py` | 新增 `test_create_endpoint_optional_normalized`（`None` / `""` / `"   "` → 201，落库归一为 `None`，响应 `endpoint` 为 `None`）；新增 `test_create_endpoint_nonblank_preserved_and_still_validated`（非空原样保留 + 非法 URL 仍 422）。既有 `test_create_invalid_input_422` 的 endpoint 用例（`not-a-url` / `ftp://` / `https://`）不变。 |
| `tests/test_cloud_0_1_2_3_1_agent_endpoint_contract_alignment.py` | 新增 `test_create_normalizes_blank_endpoint_but_patch_does_not`（静态锁：create 用 `_normalize_endpoint`；PATCH 仍 `_require_non_blank(change.endpoint)`）。 |
| PATCH 用例（`test_step11_3`） | 保持不变（冻结合同）。 |

---

## 5. 验收

| 类别 | 验收项 | 结果 |
|---|---|---|
| Create Agent | `endpoint` = `None` / `""` / `"   "` 均 201，落库为 `None` | ✅ |
| Create Agent | 非空 endpoint 原样落库、响应可见 | ✅ |
| Create Agent | 非法 URL（`not-a-url` / `ftp://` / `https://`）仍 422 | ✅ |
| Update（PATCH） | endpoint 合同不变（`_require_non_blank` 保留） | ✅ |
| 回归 | `test_step11_3_agent_management_api.py` + `test_step6_1` / `test_step6_2` 全绿 | ✅ |
| 回归 | 全量 `pytest -q` 与基线一致（无新增失败） | ✅ |

---

## 6. 与 0.1.2.3.1 Gate 的关系

本步兑现 0.1.2.3.1 §5 的 **D1**（create 侧空白归一化）。0.1.2.3.1 的核心裁决
（`Agent.endpoint` = 可选描述性元数据；`Connection.config["endpoint"]` = 唯一权威访问
endpoint；无双重事实）由此**代码化落地**：create 侧 `endpoint` 与 `credential_ref` 一样
「可选、空白即无」，真实访问地址继续只落在 `Connection.config`。
