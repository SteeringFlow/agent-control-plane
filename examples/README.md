# Examples

面向 OSS 开发者的最小可运行示例。**每个示例都是独立目录**：README +
standalone 示例进程（如有），不 import ACP backend 的任何代码（无 SDK），
不改 `backend/app`。

| 目录 | 内容 | 外部依赖 | clean clone 可跑 |
|---|---|---|---|
| [`custom-agent/`](custom-agent/) | 用一个**你自己的 HTTP Agent**（standalone，任意语言可实现）接入 ACP | 无（Local Channel） | ✅ |
| [`multi-agent/`](multi-agent/) | 两条 Local Channel 各接一个独立 Agent，验证隔离（A ≠ B，零串路） | 无（Local Channel） | ✅ |
| [`wecom-workbuddy/`](wecom-workbuddy/) | 接入**真实企业微信**渠道的配置配方（recipe，仅文档） | 企业微信 + 公网 HTTPS | ❌（配方） |

前置：先按仓库根 README「快速开始」启动 ACP（`cd backend && uvicorn app.main:app`）。

> 自定义 Agent 的 wire 契约见 `backend/app/adapters/agents/acp.py` 模块 docstring
> §1（minimal binding）。示例只用**已存在**的 `acp` / `local` 类型，不新增
> Adapter / SDK / CLI。
