"""Deployment Provenance API（M5.2 —— Cloud Product Entry）。

端点面（最小集，无任何业务语义）：

    GET /api/version   200   {"version": "<VERSION 文件内容>", "commit": "<GIT_SHA>"}

职责边界（与 api/auth、api/health 同一纪律）：

    FastAPI（本模块：事实读取 + 应答序列化）
        ↓
    部署事实来源：
      * version ← 仓库根 `VERSION` 文件（构建时随镜像进入 /app/VERSION）
      * commit  ← 构建期注入的环境变量 `GIT_SHA`（Dockerfile ARG → ENV）
                  缺失时回退 "unknown"（绝不在运行期读取 git history）

刻意不做：
    * 不读取 git repository runtime history（容器内无 .git）；
    * 不新增数据库字段 / migration；
    * 不暴露任何 secret / 凭据 / 主机信息（纯构建事实）。

公开性：本端点是**公开** deployment provenance（无认证依赖）——它只回答
「这个 Cloud runtime 跑的是哪个 version + 哪个 commit」，不携带任何敏感面。
公网可达性由 nginx 边界决定（M5.2：product surface 公开 + 应用层 session 鉴权）。
"""

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

# 仓库根：backend/app/api/version.py → parents[3] = repo root
# （容器内布局同构：/app/backend/app/api/version.py → parents[3] = /app，
#   VERSION 经 Dockerfile COPY 落在 /app/VERSION —— 与 FRONTEND_DIR 同一布局契约）。
REPO_ROOT = Path(__file__).resolve().parents[3]
VERSION_FILE = REPO_ROOT / "VERSION"

_UNKNOWN = "unknown"


class VersionResponse(BaseModel):
    """部署事实（仅构建事实，无 secret）。"""

    version: str
    commit: str


def read_version() -> str:
    """读根目录 VERSION 文件；缺失/为空 → "unknown"（绝不抛错影响路由装配）。"""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return _UNKNOWN
    return text or _UNKNOWN


def read_commit() -> str:
    """读构建期注入的 GIT_SHA；缺失/为空 → "unknown"。不读 git runtime history。"""
    return os.environ.get("GIT_SHA", "").strip() or _UNKNOWN


def build_version_api() -> APIRouter:
    """构造 Deployment Provenance router（与 build_auth_api 同一装配纪律）。"""
    router = APIRouter(tags=["version"])

    @router.get("/api/version", response_model=VersionResponse)
    def version() -> VersionResponse:
        """Cloud runtime → version + commit（M5.2 deployment provenance 契约）。"""
        return VersionResponse(version=read_version(), commit=read_commit())

    return router
