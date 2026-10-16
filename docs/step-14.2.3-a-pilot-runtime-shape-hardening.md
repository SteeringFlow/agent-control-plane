# Step 14.2.3.A — Pilot Runtime Shape Hardening（B3 部署形态三项）

> 只处理部署形态：**去 `--reload` / SQLite 持久卷 / 端口仅本机暴露**。
> **不动应用代码**（backend/app/\*\*、frontend/\*\*、Runtime、Worker、Driver、
> Persistence、Domain、DB schema 全部零改动）。

## 1. 执行前只读检查（机器给出的真实值，不靠假设）

| 检查 | 结果 |
| --- | --- |
| `docker compose config` | `working_dir: /app/backend`、`DATABASE_URL=sqlite:///./agent_control_plane.db`、`published: "8000"`（即 0.0.0.0）、**无 volumes** |
| `docker compose ps` | `0.0.0.0:8000->8000/tcp` |
| `docker inspect … --format '{{json .Mounts}}'` | `[]`（无挂载） |
| 容器内 SQLite 实际路径 | **`/app/backend/agent_control_plane.db`**（`exists = True`） |

**关键推论**：SQLite 直接位于代码目录 `/app/backend/` 下，因此**不能**把 named volume
挂到 `/app/backend`（会遮蔽镜像内的后端代码）。故把 DB 指向工作目录下的 `data/`
子目录，并将该子目录作为卷挂载点。

## 2. 实施（仅 2 个文件）

**`docker-compose.yml`**：

- `command: ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`（**I1**，显式单一来源，可 `docker compose config` 验证）；
- `DATABASE_URL: sqlite:///./data/agent_control_plane.db` + `volumes: acp_data:/app/backend/data`（**I2**，DB 绝对路径 `/app/backend/data/agent_control_plane.db` 位于卷内），顶层声明 `volumes: acp_data:`；
- `ports: "127.0.0.1:8000:8000"`（**I3**）。

**`Dockerfile`**：`CMD` 去掉 `--reload`（与 compose command 一致），注释说明 resident
Driver 与 reload 进程模型不兼容的理由。

## 3. 验收实测

| # | 项 | 结果 |
| --- | --- | --- |
| ① | 端口绑定 | `docker compose ps` → **`127.0.0.1:8000->8000/tcp`**（不再是 `0.0.0.0:8000`）✅ |
| ② | 无 reload | 容器 `Config.Cmd` = `["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`，`--reload` 出现次数 **0** ✅ |
| ②b | 卷挂载 | `Mounts` = `volume agent-control-plane_acp_data → /app/backend/data`（`RW: true`）✅ |
| ③ | Driver / 启动 | `Application startup complete.`、`Uvicorn running on http://0.0.0.0:8000`；`Traceback`/`no such table`/`OperationalError` 计数 **0**（日志中无因部署形态变化产生的新异常）✅ |
| ④ | **数据持久化** | 写入前 `GET /api/agents` = 空 → `POST /api/agents` → **201**（id=1, `persist-check-agent`）→ 读回 1 条 → **`docker compose down` + `up -d`（容器完全移除重建）** → `GET /api/agents` **仍返回同一条（id=1，时间戳一致）** ✅ |
| ⑤ | 回归 | `pytest -q` → **1467 passed**（2:03）✅ |

补充核对：SQLAlchemy 实际 URL = `sqlite:///./data/agent_control_plane.db`；
容器内 `Path('data/agent_control_plane.db').resolve()` = `/app/backend/data/agent_control_plane.db`，`exists = True`。

## 4. 改动面核对（本步禁止项）

- 应用代码：`backend/app/**` **0 改动**；`frontend/**` 0 改动；
  Runtime / Worker / Driver / Persistence / Domain / DB schema 0 改动。
- 按修改时间核对，本步仅 `Dockerfile`（20:00:04）与 `docker-compose.yml`（20:00:02）被改写；
  期间出现的 `app/**.py` mtime 变动经 `git diff` 核验**内容未变**
  （`delivery.py`/`outbound_response.py` 对 HEAD 零 diff）。

## 5. 刻意未做（14.2.3.B/C）

healthcheck、依赖版本锁定、`.env` 单一来源、反向代理（暴露面隔离的完整形态）、
Pilot walk-through。

> 注意：本步把端口收窄到 `127.0.0.1` 后，**从宿主机以外已无法访问**——
> Pilot 对外访问路径由后续反向代理步提供（webhook 需公网、Management/Observation 不裸暴露）。
