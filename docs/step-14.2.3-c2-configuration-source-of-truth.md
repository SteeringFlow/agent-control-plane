# Step 14.2.3.C.2 — Configuration Source of Truth（最小 Patch）

> 只解决两个真实问题：① `DATABASE_URL` 默认值三处不一致；② `.env.example` 键面
> 与 Settings 字段面不对齐（test-only 键冒充 runtime 配置）。
> **compose 不改**（Pilot deployment overrides 冻结）。

## 1. 三层配置源（本步冻结的定性）

```
config.py        = 配置契约（字段面 + 默认值 + fail-fast 边界校验）
.env.example     = 开发配置模板（复制为 backend/.env 生效；.env 已 gitignore）
docker-compose   = Pilot deployment overrides（部署入口，明确声明 Pilot runtime 配置）
```

三个**层次**，不是三个互相竞争的配置源；不追求机械的「全项目只有一个配置文件」。

## 2. 改动面（严格限定）

| 文件 | 改动 |
| --- | --- |
| `backend/app/core/config.py` | `DATABASE_URL` 默认值 → `sqlite:///./data/agent_control_plane.db`（与已验证的 Pilot 持久化路径统一；仍可被 `backend/.env` / 环境变量显式覆盖） |
| `backend/.env.example` | 8 键 = Settings 8 字段（3 core + 5 DRIVER，值=代码默认）；**删除** 4 个 `TENCENT_*` test-only 键；文件头写明三层配置源与值纪律 |
| `backend/data/.gitkeep` | 新增——SQLite 需要父目录存在；该目录即 volume 挂载点（容器）/本地默认 DB 目录。**边界说明**：允许清单未列此项，但它是「本地默认 = ./data/...」这一拍板的必要落地物（空目录标记，非代码） |
| `tests/test_settings.py` | 默认值断言更新为新统一路径 |
| `tests/test_persistence_smoke.py` | `DEFAULT_DB_PATH` → `backend/data/agent_control_plane.db` |
| `tests/test_step10_1_5_repository_credential_safety.py` | 安全模板白名单更新为 8 键；新增不变量「`TENCENT_*` 不得出现在模板」 |
| `tests/test_step14_2_3_c2_configuration_alignment.py` | 新增守卫（见 §4） |
| `docker-compose.yml` / Dockerfile / requirements / Runtime / Worker / Driver / Persistence / Domain / schema / API / frontend | **0 改动** ✅ |

## 3. 验收核对（六项全过）

| 验收项 | 结果 |
| --- | --- |
| Settings fields | **8**（3 core + 5 DRIVER）✅ |
| `.env.example` keys | **8** ✅ |
| 两者集合 | **完全一致**（守卫 `test_env_example_aligned_with_settings_fields`：差集为空）✅ |
| `DATABASE_URL` 默认/模板 | 均为 `sqlite:///./data/agent_control_plane.db`（compose overrides 同值，冻结）✅ |
| `TENCENT_*` 在模板中 | **0 个键**（仅注释说明为何不含；37 处真实引用全部在 tests，live-test 消费面不受影响）✅ |
| 全量回归 | **1486 passed**（2:35）✅ |

## 4. 新增守卫（`test_step14_2_3_c2_configuration_alignment.py`，6 项）

1. `.env.example` 键集合 == `Settings.model_fields` 键集合（差集断言）；
2. `Settings` 默认 `DATABASE_URL` == Pilot 路径；
3. 模板 `DATABASE_URL` == Pilot 路径；
4. compose overrides 块冻结（三键原样 + `DATABASE_URL` 同值）；
5. `TENCENT*` 键不得出现在模板；
6. TENCENT live-test 消费文件仍存在（清理模板不清理测试语义）。

## 5. 容器复验（额外确认项）

```
docker compose down → up -d --build（clean build）
STATUS: Up 25 seconds (healthy)          ← healthcheck 通过
GET /health → 200 {"status":"ok"}
GET /api/agents → 200（卷内 2 条历史数据完好，持久化再次复验）
```

## 6. 语义说明

- 本地默认与 Pilot 默认现在**同路径**：`./data/agent_control_plane.db`。
  本地历史 DB（`backend/agent_control_plane.db`）保留在磁盘但不再被默认消费；
  需要旧数据时在 `backend/.env` 显式覆盖 `DATABASE_URL` 即可；
- `TENCENT_ACP_*` 从模板移除 ≠ 删掉 Tencent 测试能力：它们是 env-gated live test
  的 **test-only configuration**，由测试自身的环境变量管理
  （`tests/test_step10_1_3_tencent_acp_wire.py` 等 37 处引用原样保留）；
- `DRIVER_*` 5 键进入模板：它们是 Settings 的一等字段（带 fail-fast 边界校验），
  之前却完全不可见于任何配置模板——新人无法发现它们的存在。

## 7. 遗留（14.2.3.C 后续）

反向代理（暴露面隔离完整形态）、Pilot walk-through。
