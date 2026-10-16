# Step 14.2.3.C.1 — Dependency Reproducibility

> 目标只有：干净环境可以获得**确定、可复现**的 Python runtime dependencies。
> 不引入 Poetry / uv / Pipenv 等新工具链（14.1 拍板：MVP 只用 pip + `==`）。

## 1. 关键事实（本步发现）

`requirements.txt` 旧形态（全部 `>=`、dev/test 混入）存在一个更深的问题：
**其 `>=` 下界从未约束过实际验证过的组合**——本机完整测试套件（1472 passed）
运行在更低版本上（如 fastapi 实测 `0.115.0` vs 声明 `>=0.139.2`、uvicorn 实测
`0.30.6` vs `>=0.51.0`、httpx 实测 `0.27.2` vs `>=0.28.1`）。也就是说：
「声明」与「验证」是两套版本，任何一次干净安装得到的都是未经验证的组合。

**pin 依据因此定为：完整测试套件实际验证过的版本**，而非旧下界。

## 2. 依赖文件体系（三层，全部 pip，无新工具链）

| 文件 | 角色 | 纪律 |
| --- | --- | --- |
| `backend/requirements.txt` | 运行时**直接依赖**（人维护的事实源），5 项全部 `==` | dev 依赖绝不混入 |
| `backend/requirements.lock.txt` | **完整传递依赖锁定**（Docker 实际消费），24 项全部 `==`，由 Linux 容器内 `pip freeze` 生成 | 再生成流程写在文件头；`uvloop` 为 Linux-only（目标平台即 Linux 容器） |
| `backend/requirements-dev.txt` | `-r requirements.txt` + `pytest==8.3.4` / `httpx==0.27.2` | **绝不**进入运行时镜像 |

运行时直接依赖（`==` 实测版本）：`fastapi==0.115.0`、`uvicorn[standard]==0.30.6`、
`pydantic-settings==2.4.0`、`SQLAlchemy==2.0.52`、`cryptography==50.0.0`。

**`Dockerfile`**：改为 `COPY backend/requirements.lock.txt` +
`pip install -r requirements.lock.txt`——镜像只装锁定集。

## 3. 验收实测

| # | 验收 | 结果 |
| --- | --- | --- |
| 1 | **确定性**：两次 `docker compose build --no-cache`，容器内 `pip freeze` 逐行对比 | **IDENTICAL** ✅ |
| 2 | **锁定一致**：容器内 `pip freeze` vs `requirements.lock.txt` | **IDENTICAL**（24/24 行一致）✅ |
| 3 | dev 隔离：镜像内 `find_spec('pytest')` / `find_spec('httpx')` | **False / False** ✅ |
| 4 | 降版冒烟：锁定版本（fastapi 0.115.0 等）下容器功能 | `/health` 200、`/interactions.html` 200、`GET /api/interactions` 200、`POST /api/agents` **201**、`healthy` ✅ |
| 5 | 守卫测试 `test_step14_2_3_c1_dependency_reproducibility.py` | **8 passed** ✅ |
| 6 | 全量回归 `pytest -q` | **1480 passed**（1:51）✅ |

守卫测试锁死的不变量：运行时/dev/lock 三文件全 `==`；dev 依赖只出现在 dev 文件；
lock 与直接依赖版本一致（比较时剥离 extras，`uvicorn[standard]` vs freeze 的
`uvicorn`）；Dockerfile 只消费 lock；三个文件内容行无任何新工具链痕迹。

## 4. 改动面

`backend/requirements.txt`（重写）、`backend/requirements.lock.txt`（新增）、
`backend/requirements-dev.txt`（新增）、`Dockerfile`（仅依赖安装段 2 行）、
`tests/**`（新增守卫）——
**application code / Runtime / Worker / Driver / Persistence / Domain / DB schema
0 改动**；❌ PostgreSQL ❌ CI platform ❌ package manager migration 全部为零。

## 5. 再生成流程（写死在 lock 文件头，无需任何新工具）

```
1. 修改 backend/requirements.txt（直接依赖，== pin）
2. docker compose build && docker compose up -d
3. docker compose exec backend pip freeze --disable-pip-version-check
   → 整体替换 requirements.lock.txt
4. docker compose down && docker compose up -d --build
   → 容器内 pip freeze 与 lock diff 必须为空
```

## 6. 遗留（14.2.3.C 后续）

`.env` 单一来源、反向代理（暴露面隔离完整形态）、Pilot walk-through。
