# Cloud-0.1.2.2.1 —— Credential Ref Optionality Alignment

> 基线：Cloud-0.1.2.2（Connect Agent Architecture Gate，GO）→ **本步（最小实现）**。
> 本步只做一件事：**把 `credential_ref` 对齐成「可选的不透明外部引用」**。
> 对应 0.1.2.2 §9 的 D1 / D2。

一句话目标：**`credential_ref` 不再因为「自己」而失败。**

```
credential_ref = optional opaque external reference   ← 可选、不透明、不解析
config         = current V0 connection configuration   ← 真实连接配置 / 凭据载体
```

即：

```
credential_ref = None      → 合法（无外部引用）
credential_ref = ""        → 合法（归一为 None）
credential_ref = "   "     → 合法（归一为 None）
credential_ref = "vault://…"  → 合法（原样保留）
```

**不是**：

```
credential_ref = credential itself        ← 错误语义（它只是引用）
```

---

## 1. 变更面（只动 create）

| 文件 | 变化 |
|---|---|
| `app/application/agents.py` | `create_agent()`：删除 `_require_non_blank(input.credential_ref, …)`；新增 `_normalize_credential_ref()`，`None / "" / 纯空白 → None`；docstring 编码语义。 |
| `app/application/connections.py` | `create_connection()`：同上（删除强制非空白 + 归一化）；模块 docstring / 方法 docstring 编码语义。 |

两处归一化函数同名、各自使用本模块的错误类型（`InvalidAgentInputError` /
`InvalidConnectionInputError`），不跨模块共享实现（保持既有模块边界）。

---

## 2. 语义冻结

| 概念 | 归属 | 说明 |
|---|---|---|
| `credential_ref` | **optional opaque external reference** | `None` / `""` / 纯空白 → `None`；非空字符串原样保留（不解析、不校验格式）。**不是**凭据本身。 |
| `config` | **current V0 connection configuration** | 真实连接所需信息与凭据载体（如 WorkBuddy 的 `runtime_id` / `bearer_token` / `endpoint`）。运行时经 `ACPAdapter` 直读，响应读时脱敏。 |

**这不取消凭据校验**——它只是不再因为 `credential_ref` 本身（空/缺省）失败：

- Channel / Agent 存在性校验（`create_connection`）不变；
- `config` 必须是 JSON 对象（dict）不变；
- 真实凭据的缺失 / 非法由 `AgentAdapter` 在 dispatch 时 **Fail-Closed**
  （`AgentDispatchFailedError`，如缺少 `endpoint` / `bearer_token`）——这是真正的
  「连接校验」发生地。

---

## 3. 明确不改（边界）

| 项 | 状态 | 理由 |
|---|---|---|
| `update_agent()` / `update_connection()` | **仍要求 `credential_ref` 非空白** | 这是 11.11 冻结的 PATCH 合同（`缺省/null=不变、""=422、非空=覆写`）。改 PATCH 语义需要单独裁决（是否引入「清空引用」语义），不在本步。 |
| `credential_ref` 非空值 | 原样保留 | opaque：不做 URL/格式校验，不做 stripping（除纯空白判空）。 |
| credential 校验（config / adapter） | 不变 | 见 §2。 |
| Domain / ORM / schema / Runtime / Adapter | 零改动 | `credential_ref` 字段本就是 `str | None`；本步只调应用层输入契约。 |

---

## 4. 测试演进

| 文件 | 变化 |
|---|---|
| `tests/test_step11_3_agent_management_api.py` | create-invalid 参数用例移除 `empty-credential-ref` / `blank-credential-ref`；新增 `test_create_credential_ref_optional_normalized`（`None` / `""` / `"   "` → 201，落库归一为 `None`，响应不含 `credential_ref`）。 |
| `tests/test_step11_11_connection_management_api.py` | 同上：create-invalid 参数用例移除两个 `credential_ref` 用例；新增 `test_create_credential_ref_optional_normalized`（每个用例独立 `(channel_id, agent_id)` 绑定，规避 UNIQUE 约束）。 |
| PATCH 用例 | 保持不变（`credential_ref=""` → 422，冻结合同）。 |

---

## 5. 验收

| 类别 | 验收项 | 结果 |
|---|---|---|
| Create Agent | `credential_ref` = `None` / `""` / `"   "` 均 201，落库为 `None` | ✅ |
| Create Agent | 非空 `credential_ref` 原样落库、响应永不出现 | ✅ |
| Create Connection | `credential_ref` = `None` / `""` / `"   "` 均 201，落库为 `None` | ✅ |
| Create Connection | Channel/Agent 存在性校验、`config` dict 校验不变 | ✅ |
| Update（PATCH） | `credential_ref=""` → 422（冻结合同不变） | ✅ |
| 回归 | 受影响 8 个测试文件全绿（148 passed） | ✅ |
| 回归 | 全量 `pytest -q`：**1798 passed / 5 skipped**；17 errors 均为 `test_step15_2_3` 的 Windows `tmp_path` `PermissionError`（环境问题，与本步无关） | ✅ |

---

## 6. 与 0.1.2.2 Gate 的关系

本步兑现 0.1.2.2 §9 的 **D1 / D2**（`credential_ref` 创建期强制 → 可选）。
0.1.2.2 的核心裁决 ①（凭据属于 Connection、不属于 Agent）由此**代码化落地**：
Agent 与 Connection 的 `credential_ref` 都不再是「必须填的凭据」，而只是可选引用；
真实凭据继续只落在 `Connection.config`（脱敏）。D3（Runtime Health 派生诊断端点）
仍待后续步骤。
