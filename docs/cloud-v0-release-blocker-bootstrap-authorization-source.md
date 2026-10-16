# Cloud V0 — Release Blocker Record
## RB-C3-01：Bootstrap Authorization Source not formally represented

> 本文件是 **Release Blocker 台账条目**，不是 backlog item，也不是设计文档。
> 权威依据：`docs/cloud-v0-release-freeze.md` §3（Triage Rule）。
> **本文件不修改冻结文档本身。**

---

## 1. Blocker 标识

| 项 | 内容 |
|---|---|
| **ID** | `RB-C3-01` |
| **标题** | Bootstrap Authorization Source not formally represented |
| **状态** | **CLOSED**（关闭条件已达成，见 §5） |
| **引用号** | `cloud-v0-release-freeze.md` §3.1（Release Blocker 判定） |
| **关闭条件** | **ADR-12 已签发** |
| **关闭证据** | `docs/architecture/adr-12-first-message-bootstrap-arm.md`（本轮签发） |
| **关联** | `docs/architecture/adr-11-conversation-grant-authorization.md`（补签，配套） |

---

## 2. Blocker 陈述（精确）

```
Until ADR-12 is signed, ACP has no *formally represented* authorization source
for a first-time conversation:

  授权体系要求「允许必须显式成立」（ADR-6），
  但 V0 的唯一 Golden Path 中**不存在任何建立授权的步骤**，
  且 Discovery **不得**成为授权来源（ADR-6 / Package 2-C §4）。

  ⇒ 一个零知识用户的第一条真实 Channel Message 无法取得授权
  ⇒ Cloud-0.1.5 的 G5（First Success）无路径可达
  ⇒ 「授权来源」这一架构要素在冻结台账上是**空白**的。
```

**这不是「缺少一个功能」，而是冻结范围内两项已冻结事实之间的治理空缺。**

---

## 3. 证据链（逐条可查）

| # | 事实 | 证据 |
|---|---|---|
| E-1 | V0 无法通过 Discovery 授权会话；「除非用户知道其 `external_conversation_id` 并显式创建 Grant」 | `docs/architecture/implementation-package-2-c-…-freeze.md:204-216`（原文登记） |
| E-2 | Golden Path = `Sign Up → Connect Agent → Connect Channel → Send WeCom message → First Success`，**其中无授权建立步骤** | `docs/cloud-0.1.5-…-gate.md:15-27` |
| E-3 | First Success 必须由真实 Interaction 的 `processing=succeeded × delivery=delivered` 推导；「配置成功 / Channel 创建成功」**绝不算** | `docs/cloud-0.1.5-…-gate.md:42`、`:175` |
| E-4 | 授权默认 deny（缺行 = 不允许）；允许必须显式成立 | ADR-6（`authorization-and-access-model.md:344`） |
| E-5 | 运行时零创建 + Identity 未命中即 Fail Closed ⇒ 首条消息在无授权时**必然**停在 `IDENTITY_RESOLUTION_FAILED` | `app/application/identity_resolution.py:56-73` |
| E-6 | 未绑定 Identity 的创建主体在冻结台账上是 **Nobody**（Gate 2.6 D4 三问未被任何 ADR 回答） | `docs/architecture/implementation-gate-2-6-…-decision.md:180-217`；CP-1 §6.2 |

⇒ §3.1 条件 ①「**阻断发布**：不修则 V0 无法对外交付 / Golden Path 阻断」成立
（Golden Path 阻断是 §3.1 列举的典型 blocker 形态，`cloud-v0-release-freeze.md:71`）。

---

## 4. Triage 判定（诚实记录，不得含糊）

### 4.1 RB-C3-01 本体（治理空缺）—— **RELEASE BLOCKER**

| §3.1 条件 | 判定 | 理由 |
|---|---|---|
| ① 阻断发布 | ✅ | G5 无路径可达（§3 E-1~E-6） |
| ② 范围收敛（不引入新语义/字段/能力） | ✅ | **关闭动作 = 签发 ADR-12**：一个**文档动作**，零代码、零迁移、零端点、零字段 |
| ③ 可证成（回到既有基准，不移动基准） | ✅ | ADR-12 未移动任何基准：ADR-5 本体 / ADR-6 / ADR-9 / ADR-11 窄豁免全部保留；被取代的仅是 ADR-5 的**推论性**限制（且方式合规：新增编号 + 显式取代关系，ADR `:702-703`） |

⇒ **RB-C3-01 = release blocker，且由 ADR-12 签发关闭。**

### 4.2 RB-C3-01b（实现物）—— **不属于本 blocker，独立排队**

```
实现物 = bootstrap_arms 新表 + Arm 端点 + bootstrap.py 组件

按 cloud-v0-release-freeze.md §3.2：
  「新功能 / 新端点 / 新配置项」→ **不是** release blocker → post-release backlog

⇒ 实现物**不得**借用 RB-C3-01 的 blocker 身份进入 V0。
   若项目决定在 V0 内实现，须另行提交**独立的 blocker 裁定**
   （引用本文件的证据链，并单独论证 §3.1 三条件）。

否则默认：ADR-12 生效（语义合法），实现排入 post-V0。
```

> 这一区分是本条目的**核心**：
> **「授权来源未表达」是 blocker；「把 Bootstrap 实现出来」不是。**

---

## 5. 关闭记录

```
关闭条件：ADR-12 已签发
达成时间：本轮（CP3-E）
证据文件：docs/architecture/adr-12-first-message-bootstrap-arm.md
          docs/architecture/adr-11-conversation-grant-authorization.md（配套补签）

ADR-12 签发后：
  ✅ 授权来源已被正式表达（Arm = Authorization Source，消息 = Trigger）
  ✅ Gate 2.6 D4 三问已正式回答
  ✅ 「谁有权创建未绑定 Identity」已有唯一主体
  ⇒ RB-C3-01 CLOSED
```

---

## 6. 关闭后仍然成立的限制（不得被淡化）

```
① Bootstrap 语义合法 ≠ 实现已进入 V0（§4.2）
② Arm 的残余语义（窗口内首条未知单聊会话即被 Bootstrap）必须由 UI 如实说明（ADR-12 §8）
③ 实现阶段仍须同步调整既有精确守卫（见 docs/M6_6_C3_CP3_E_GUARD_CHANGE_MANIFEST.md）
④ 未 Arm 路径必须与今天逐字节等价（ADR-12 §1）—— 这是回归基线，不是可选项
```

---

## 7. 本文件未做的事

```
❌ 未修改 docs/cloud-v0-release-freeze.md
❌ 未修改任何 ADR / Package 2-C / Gate 2.6
❌ 未创建 migration / 端点 / 代码 / 测试
❌ 未把实现物标为 blocker（§4.2 明确禁止）
```
