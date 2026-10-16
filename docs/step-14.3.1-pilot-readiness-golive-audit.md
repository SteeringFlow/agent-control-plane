# Step 14.3.1 — Pilot Readiness / Go-Live Audit

> 不是大架构审计，而是回答 6 个非常现实的问题。结论先行：**GO**——
> 附 1 个 P1 建议（compose restart 策略，属 Pilot 期间允许的部署修复）。

---

## Q1. 外部 Operator 能否按 docs/pilot-walkthrough.md 独立部署？

**能（结构核验通过）。**

- Walk-through 12 步 + 常见问题表完整覆盖：域名 → 证书 → 容器 → nginx → 配置链
  （Agent/Channel/Connection）→ WeCom 回调 → 真实消息 → ACK → 执行 → Delivery →
  Observation → 持久化验证；
- 引用的全部工件存在（`_deploy/setup_nginx_pilot.sh`、vhost 模板等）✅；
- 无步骤依赖"读源码"；每步都有可执行的验证命令（`docker compose ps`、curl 断言）；
- 剩余的外部依赖只有两样：**域名/DNS** 与 **WeCom 管理后台权限**——这两样本来就不
  可能预先自动化。

**残留缺口（P1，唯一）**：`docker-compose.yml` **无 `restart` 策略**——宿主机重启后
ACP 不会自动拉起，Operator 必须手动 `docker compose up -d`。
修复 = compose 加一行 `restart: unless-stopped`（部署修复，零应用语义）。

## Q2. 第一次真实 WeCom 消息能否完整走通？

**链路每一环都有真实证据**（非推演）：

| 环节 | 证据 |
| --- | --- |
| WeCom 入站 wire（验签/加解密） | 12.8.5 分类 / 12.8.6 真实 wire 冒烟 |
| ACK 边界 | 12.9.4.6（ACK ≠ 执行；WeCom 5s 超时内先落 PENDING） |
| Worker → CAS → 六段 Runtime | 12.9.4.13 E2E 闭环 / 12.9.4.17 可靠性 |
| Delivery（WeCom 出站） | 10.2.2 + 真实部署冒烟（`_deploy/` 脚本在真机跑通过全链） |
| Observation 还原 | 13.4 验收（真浏览器点击闭环）+ 14.2.x 部署后复验 |
| webhook 公网可达性 | C.3 G9（422 穿透证据）；历史部署同链路真实通过 |

**唯一未在本机复现的环节** = 走一遍真实域名上的证书签发与 WeCom 后台配置——
这是部署日操作（walk-through 步骤 1/2/8），架构上无未知数。

## Q3. 失败后 Operator 能否从 Observation 判断发生了什么？

**执行期失败：能。** 双状态独立呈现（`succeeded × failed` 不合并）、失败三元组挂
真实侧、时间线 1:1 发生序（`stage_failure` 行带 metadata.failure_code）、卡死提示
（`processing_started_at` 客户端时间差）、孤儿回收自动重跑。

**两个已知盲区（诚实记录，属已知限制而非 bug）**：

1. **入站失败不可见**：验签失败 / token 错误的 webhook 请求在 intake 之前就被拒，
   不产生 Interaction / Evidence——Operator 需看 nginx access log 与
   `docker compose logs backend`。walk-through 常见问题表已写明处置路径；
2. **投递前系统级失败**（如 Agent endpoint 不可达）会落为 `processing=failed` +
   `failure_stage=agent_dispatch`——可见 ✅；但**网络层故障**（容器挂掉、代理故障）
   本身不产生任何记录，只能从容器/代理日志判断。

结论：**能判断"业务执行发生了什么"；基础设施故障走日志**——这是 Pilot 的合理边界。

## Q4. 已知限制清单（不是 bug，勿当 bug 修）

| # | 限制 | 依据 |
| --- | --- | --- |
| 1 | TOCTOU 并发窄窗：并发重复 webhook 之一得 5xx，WeCom 重试后收敛（无双重执行） | 13.5 Q6 拍板 C 类 |
| 2 | 无 Retry/Replay：失败后人工恢复（修配置 + 用户重发） | 13.5 Q5 |
| 3 | Observation 列表无过滤/只看失败；deep-link 落列表页不定位行 | 13.3/13.5 B 类 |
| 4 | 无应用层 Auth：仅代理层 Basic Auth；单 Operator 假设 | 13.5 Q4 |
| 5 | SQLite 单实例：并发写上限低、无在线备份命令（可 `docker cp`/停机拷贝卷） | 14.1 Q3 |
| 6 | bounded limit=50 列表，无分页 | 13.1 冻结 |
| 7 | 卡死阈值 300s / Driver 节奏（2s 轮询、30 轮回收、stale 300s）为默认常量，不可运行时调 | Settings 面冻结 |
| 8 | Legacy `PersistingCoordinator.execute()` 死代码仍在 | 13.5 Q7 延后 |
| 9 | `/docs` `/openapi.json` 公网 401（在 auth 边界内），不可匿名查阅契约 | C.3 拍板 |
| 10 | 依赖锁定仅保证 Linux 容器（uvloop Linux-only） | 14.2.3.C.1 |

## Q5. Pilot 阶段允许修什么 / 必须冻结什么？

**允许修（Pilot 期间 hot-fix 类，不需要新 Step）**：

- compose / nginx 配置参数（端口、口令、restart 策略）；
- 文档与 walk-through 勘误；
- frontend 展示缺陷（不改变 API 契约与数据语义）；
- `.env`/环境变量值；容器重建、卷数据清理（业务数据除外）；
- 日志定位后的小型配置问题。

**必须冻结（触碰即升级为正式 Step，不得顺手改）**：

- Runtime / Worker / Driver / 六段 Executor / Attempt Fence / 孤儿回收语义；
- DB schema（任何字段/表变更都影响卷内 Pilot 数据 → 必须先做数据迁移裁决）；
- API 契约（Observation / Management 端点与字段面）；
- 双状态模型、Evidence append-only、幂等/duplicate 语义；
- exposure boundary 的放宽（新增公网路径必须走审计）。

**升级触发器**（出现即记录并停下）：同一 Operator 请求连续两次卡死回收、
WeCom 回调持续 5xx 超过 10 分钟、卷磁盘增长异常、任何疑似数据丢失。

## Q6. 第一个 Pilot 应该观察什么指标？

**不建监控系统**（13.5/14.1 冻结）。手工 + 现有面即可，按优先级：

| 观察项 | 观察方式 | 频率 |
| --- | --- | --- |
| 端到端成功率（`succeeded×delivered` 占比） | Observation 列表人工盘点 | 每日 |
| 端到端时延（`created_at` → `updated_at`） | Observation 详情两个时间戳 | 抽查 |
| 失败分布（failure_stage / failure_code） | Observation 失败三元组 | 每日 |
| 卡死回收事件 | `docker compose logs backend` 中 driver reclaim / fence REJECTED | 异常时 |
| 入站拒绝（验签失败） | nginx access log 4xx on webhook path | 异常时 |
| auth 边界探测 | nginx log 401 频率（异常升高 = 被扫） | 每周 |
| 卷磁盘增长 | `docker system df -v` / 卷目录大小 | 每周 |
| 容器健康 | `docker compose ps`（healthy）| 每日 |

记录载体：一张人工表格即可。**任何"需要 Prometheus 才能回答"的问题，在 Pilot
规模下都不应该出现**——出现了说明用户量已超出 Pilot 定位，那本身就是升级信号。

---

## Go-Live Checklist（部署日）

| # | 项 | 判定 |
| --- | --- | --- |
| 1 | `docker compose up -d --build` → healthy | `docker compose ps` |
| 2 | `restart: unless-stopped` 已加（P1 修复） | `docker inspect` |
| 3 | `setup_nginx_pilot.sh <domain>` 跑通，自检 401 输出 | 脚本输出 |
| 4 | G9 等价公网抽检（webhook 非 401 / agents 401 / health 404） | curl |
| 5 | Management 配置链完成（Agent/Channel/Connection） | UI |
| 6 | WeCom 回调保存成功（GET 握手通过） | WeCom 后台 |
| 7 | 真实消息 → 用户收到回复 | WeCom |
| 8 | Observation 可见完整执行故事 | UI |
| 9 | `down && up` 后数据仍在 | Observation |

**8/9 项已有预验证证据，2 项是部署日动作（域名证书 + WeCom 后台配置）。**

## 最终拍板

> **GO。** 按 `docs/pilot-walkthrough.md` 执行即可上线；上线前建议顺手落掉 P1
> restart 策略（一行 compose）。上线后按 Q6 的手工观察表运行；触碰 Q5 冻结面的
> 任何需求，一律停下立 Step，不做"顺手修"。
