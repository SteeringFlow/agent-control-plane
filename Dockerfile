# Step 14.2.1 —— Pilot 容器布局基线（**只解决目录布局，不改任何应用语义**）。
#
# A1 注（OSS vs Cloud 边界，docs/strategy/oss-vs-cloud-boundary.md）：初始 OSS
# 无前端构建变体 = `Dockerfile.oss`（仅少 `COPY frontend/` 一行，其余逐行一致）。
# 修改本文件时请同步该变体；Cloud/既有 compose 一律继续使用本文件。
#
# 唯一目标：让仓库根下的 frontend/ 正确进入镜像，使 main.py 中
#
#     FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
#
# 在容器内解析到一个**真实存在**的目录。不修改该行代码，只调整布局：
#
#     容器内布局 = 仓库根布局（/app 扮演 "repo root"）
#         /app/backend/app/main.py      ← main.py
#             parents[0] = /app/backend/app
#             parents[1] = /app/backend
#             parents[2] = /app          ← 仓库根
#         /app/frontend/                 ← parents[2] / "frontend"  ✅ 真实存在
#
# 因此 WORKDIR（运行期）= /app/backend，与本地 `cd backend` 等价。
#
# 本步刻意不做（留给 14.2.2 / 14.2.3）：
#   ❌ 数据库初始化 / volume / healthcheck / 依赖锁定 / 反向代理 / 去 --reload

FROM python:3.14-slim

# 构建期工作目录 = 容器布局的 "repo root"
WORKDIR /app

# 依赖预装（构建期完成，不再是启动期 pip install）。
# 14.2.3.C.1：消费**完整锁定集** `requirements.lock.txt`（全部 `==`，Linux 容器
# 内 `pip freeze` 生成）——保证每次构建得到同一运行时依赖集。
# 人维护的事实源是 `requirements.txt`（直接依赖）；dev 依赖（pytest/httpx）
# 在 `requirements-dev.txt`，**绝不**进入运行时镜像。
COPY backend/requirements.lock.txt /app/backend/requirements.lock.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.lock.txt

# 后端代码（含 app/）与前端静态资源，按仓库根布局对齐落位
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# ---------------------------------------------------------------------
# M5.2 —— Deployment Provenance（Cloud runtime → version + commit）：
#   * VERSION 文件随镜像进入 /app/VERSION（/api/version 的 version 事实源；
#     与 main.py 的 FRONTEND_DIR 同一「/app = repo root」布局契约）；
#   * GIT_SHA 构建期注入（--build-arg GIT_SHA=$(git rev-parse HEAD)），
#     转为 runtime ENV 供 /api/version 读取；缺失时回退 "unknown"。
#     绝不在运行期读取 git history（容器内无 .git）。
# ---------------------------------------------------------------------
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}
COPY VERSION /app/VERSION

# ---------------------------------------------------------------------
# M7-AUI-10.8.12 —— OCI 镜像身份标签（构建来源可追溯）：
#   * revision ← GIT_SHA build-arg（与 /api/version 的 commit 字段同源）；
#   * version  ← IMAGE_VERSION build-arg（部署 runbook 以 VERSION 文件内容注入：
#     IMAGE_VERSION=$(cat VERSION)；缺失时回退 "unknown"）。
#   注意：仅 GIT_SHA 不足以证明镜像内容与该提交一致——构建上下文必须来自
#   git archive / 干净快照目录，见部署 runbook 与 10.8.11 §D。
# ---------------------------------------------------------------------
ARG IMAGE_VERSION=unknown
LABEL org.opencontainers.image.revision=${GIT_SHA} \
      org.opencontainers.image.version=${IMAGE_VERSION}

# 运行期工作目录：uvicorn 以 app.main:app 启动，等价于本地在 backend/ 下执行
WORKDIR /app/backend

EXPOSE 8000

# 14.2.3.A（I1）：**无 --reload**。ProductionDriver 是 resident 生命周期组件，
# 挂在应用 lifespan；reload 的 monitor + worker 进程模型会让 resident 组件的
# 生命周期被反复打断/重建，因此不作为 Pilot 运行方式。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
