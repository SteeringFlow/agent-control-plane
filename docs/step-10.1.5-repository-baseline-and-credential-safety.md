# Step 10.1.5 — Repository Baseline & Credential Safety

> **定位**：不是 Runtime 功能，也不重新打开 Runtime Freeze
> （10.1.4 §10 Freeze Rule 不受任何影响）。
>
> 目标链：
>
> ```
> 当前已验证 Runtime
>         ↓
> Git 可回滚基线
>         ↓
> Credential 安全边界
>         ↓
> 可继续开发产品层
> ```

---

## 1. 10.1.5.1 Git Baseline

`git init` + 第一份正式 baseline commit：

```
023767e  BASELINE-001: Runtime MVP baseline (Step 5.1-10.1.4) + credential boundary
         170 files changed, 22722 insertions(+)
```

这是 Agent Control Plane **第一个可审计的工程状态**。此后才真正拥有：

```
Step 5.x → Step 6.x → ... → Step 10.1.4 → BASELINE-001
```

之后任何架构变化都可以 diff / rollback / bisect / audit——与 Architecture Freeze
纪律完全一致。

BASELINE-001 的内容边界：

- Step 5.1~10.1.4 的全部资产（生产代码 / 662 tests / 38 份契约文档）；
- **已加固的 `.gitignore` 与 SAFE TEMPLATE `.env.example`**（见下节——凭证边界
  必须先于 git 历史存在，而不是事后补救）；
- `backend/.env` **不在其中**（从未 staged、从未 committed）。

---

## 2. 10.1.5.2 Credential Boundary

### 2.1 三层 `.gitignore` 规则

```
.env            # 精确匹配任意目录下的 .env
*.env           # 覆盖 .env.local / .env.production 等所有变体
!.env.example   # 安全模板白名单（必须保持可入库，供新人照抄）
```

同时补齐 Python / cache / build / 覆盖率 / 编辑器等常规本地产物
（`build/`、`dist/`、`.coverage*`、`htmlcov/`、`coverage.xml`、`.tox/`、`*.log` 等）。

### 2.2 操作时序（安全优先，不可倒置）

```
1. 加固 .gitignore                    ← 必须在 git init 之前
2. 补 .env.example（空值模板）
3. git init
4. git check-ignore + git add --dry-run 验证   ← 必须在 git add 之前
5. git add . && git commit（BASELINE-001）
```

### 2.3 暂存前与提交后的实测证据

| # | 验证 | 命令 | 结果 |
| --- | --- | --- | --- |
| 1 | .env 命中 ignore 规则 | `git check-ignore -v backend/.env` | `.gitignore:26:*.env  backend/.env` |
| 2 | dry-run 不含真实 .env | `git add --dry-run .` | 170 files，仅 `backend/.env.example`；**`SAFE: no real .env would be staged`** |
| 3 | 类型分布干净 | dry-run 扩展名分布 | 125 py / 40 md / 配置；无 .db / .pyc / .sqlite3 |
| 4 | 索引零泄漏 | `git ls-files \| grep env` | 仅 `backend/.env.example` |
| 5 | 历史零泄漏 | `git log --all -- backend/.env` | 空（从未 committed） |
| 6 | 工作树干净 | `git status --short` | 空 |
| 7 | ignore 生效 | `git status --ignored` | `!! backend/.env` |

### 2.4 `.env.example`（SAFE TEMPLATE）

新增 4 个 Tencent ACP 键，**全部空值**（真实值只存在于本机 `backend/.env`）：

```
TENCENT_ACP_URL=
TENCENT_ACP_TOKEN=
TENCENT_RUNTIME_ID=
TENCENT_ACP_TIMEOUT_S=
```

留空 = 对应 env-gated live test 自动 skip，不影响其余测试。

### 2.5 机器化守护（防未来误操作）

新增 `backend/tests/test_step10_1_5_repository_credential_safety.py`，
8 项静态断言（纯只读 git 命令 + 文件内容白名单校验）：

- A. git 仓库已初始化 + 根提交是 BASELINE-001；
- B. `.gitignore` 三层规则存在；
- C. `backend/.env` 被忽略（本机无 .env 时 skip）；
- D. `.env.example` 被 track 且逐行命中白名单、TENCENT_* 恒空值；
- E. 索引无 `.env`（`test_repository_does_not_track_local_env`）；
- F. 全部历史无 `.env`；
- G. `git add --dry-run .` 永不暂存 `.env`（复刻最高频事故场景）。

**这不是"安全平台"**——只是防呆断言。真实的 secret management 属于生产部署议题。

---

## 3. 10.1.5.3 README Baseline Sync

- 进度行：`当前进度 10.1.3` → `10.1.5`（并明确 Runtime 最新完成于 10.1.4，
  已冻结为 Runtime MVP Baseline）；
- 补 Step 10.1.4 / 10.1.5 两段描述（含 business task NOT VERIFIED 的诚实边界）；
- 目录树：`acp.py` 展开为 ACPAdapter 双 binding
  （`minimal`（10.1.2 默认）/ `tencent-acp-sse`（10.1.3））；
- 路线图：10.1.3 ✅ / 10.1.4 ✅ / 10.1.5 🔄。

---

## 4. 10.1.5.4 不做什么（边界冻结）

| ❌ 不引入 | ❌ 不修改 |
| --- | --- |
| Alembic | Runtime（六 Executor / Coordinator / 契约） |
| Secret Manager / Vault | Session 生命周期 |
| OAuth / Secret Rotation | ACPAdapter（含 tencent binding） |
| CI/CD / GitHub Actions | persistence / Repository surface |
| Docker 多服务 | API / schemas |
| 凭证轮换自动化 | 既有 README 结构（只做基线同步，不重写） |

**当前只需要 `.gitignore + .env.example`**。部署到生产时再按部署方式决定
credential management。

---

## 5. 10.1.5.5 最终验收

| 类别 | 验收项 | 结果 |
| --- | --- | --- |
| Repository | git repository initialized | ✅ |
| Repository | initial baseline commit created | ✅ `023767e`（BASELINE-001） |
| Repository | working tree clean | ✅（交付物提交后复验） |
| Credential | .env ignored | ✅（check-ignore + status --ignored） |
| Credential | no secret in .env.example | ✅（白名单逐行校验） |
| Credential | no secret staged | ✅（add --dry-run） |
| Credential | no secret committed | ✅（log --all 为空） |
| Documentation | README current Step = 10.1.5 | ✅ |
| Documentation | 10.1.4 reflected | ✅ |
| Documentation | roadmap synchronized | ✅ |
| Regression | pytest -q 全绿 | 见 10.1.5 验收记录 |
| Regression | 语法 / 静态检查 | ✅（新增文件 py_compile 通过；仓库无独立 lint 工具链） |
| Regression | production behavior unchanged | ✅ production code change = 0 |

---

## 6. 与 Runtime Freeze 的关系

BASELINE-001 冻结的是 **Runtime MVP Baseline 的工程状态**；本 Step 未改动任何
生产代码（production code change = 0），未触碰 Runtime Contract / Session /
Failure 语义。10.1.4 §10 的六问 Freeze Rule 自此有了可执行的回滚锚点：

```
git checkout 023767e -- backend/app/   # 随时恢复 Runtime MVP 状态
```
