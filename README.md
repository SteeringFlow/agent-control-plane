# SteeringFlow — an Agent Control Plane

**Bring Your Agents. Don't Rebuild Them.**

SteeringFlow is an Agent Control Plane (ACP): a self-hostable control plane that
connects **Channels** (inbound messaging surfaces such as enterprise WeChat bots,
or a local development channel) to **Agents** (external agent runtimes, or a
built-in local echo agent), routes every inbound message through a governed
runtime pipeline, and delivers the agent's response back to the channel.

> **Status: early-stage release candidate (`v0.1.0-rc.x`).** The core runtime,
> management control plane, and observation read surface are implemented and
> covered by a large automated test suite, but this project is **not yet
> production-hardened for every use case** — see
> [Current capabilities & limitations](#current-capabilities--limitations).

- **Language:** Python (FastAPI, hexagonal architecture) · **Persistence:**
  SQLite (default) / PostgreSQL + Alembic (cloud profile) · **Interfaces:**
  REST API + developer CLI + Agent Runtime SDK (the web management console is
  part of the cloud service — see [OSS vs. cloud service](#oss-vs-cloud-service))
- **License:** [Apache-2.0](LICENSE)

---

## Why SteeringFlow?

If you have built agents (on any runtime), wiring them to real users still means
rebuilding the plumbing around them for every channel: authentication of external
identities, conversation/session tracking, routing, access authorization,
delivery, retries, and an audit trail of what actually happened.

SteeringFlow owns that plumbing so your agents don't have to:

```
User (WeCom bot / local dev channel)
        │  inbound message
        ▼
┌─────────────────────────── SteeringFlow ACP ────────────────────────────┐
│ Channel Adapter (verify + normalize)                                    │
│   → Identity Resolution  (channel, external_id) → User    [fail closed] │
│   → Connection Routing   channel → the ONE active Connection → Agent    │
│   → Session Resolution   reuse active session / decide new session      │
│   → Access Authorization conversation grant check         [default deny]│
│   → Agent Dispatch       normalized AgentRequest → Agent runtime        │
│   → Delivery             response → channel outbound API                │
│   → Interaction + Activity persistence (what happened, stage by stage)  │
└─────────────────────────────────────────────────────────────────────────┘
        │  outbound response
        ▼
User gets the agent's reply; every step is observable via the API
```

You register your agent **once** (an Agent entity + a Connection binding it to a
channel); the control plane handles identity, sessions, routing, authorization,
delivery, and observability for every conversation.

## Architecture at a glance

Hexagonal (ports & adapters) layering — dependencies point inward only:

```
api  →  schemas / application / domain / core / persistence.database
application  →  domain / repositories (ports) / core
adapters     →  application / domain ports, core
persistence  →  domain / core          (the only layer allowed to use SQLAlchemy)
repositories →  domain / core          (ports; never depend on persistence)
domain       →  core / stdlib
```

These rules are enforced by machine-checked import-boundary tests
(`backend/tests/test_import_boundaries.py`), so the architecture cannot silently
erode. Key building blocks:

| Block | Where | What it does |
|---|---|---|
| Management control plane | `backend/app/api/management/` | CRUD + policy for the five entities: **Agent / Channel / User / Identity / Connection** (+ connection-scoped conversation grants) |
| Runtime pipeline | `backend/app/application/` | Fixed execution order: identity → routing → session (→ creation) → dispatch → delivery, fail-closed at every stage |
| Adapters | `backend/app/adapters/` | Real transports behind ports: `acp.py` (Tencent ACP HTTP/SSE), `wecom.py` (WeCom inbound verify/decrypt + outbound message API), `local.py` (zero-dependency echo agent / local channel for first success) |
| Persistence | `backend/app/persistence/` | SQLAlchemy ORM (8 core tables), Unit of Work, repositories |
| Observation | `backend/app/api/management/interactions.py` | Read surface: interactions + per-stage activity evidence |
| Web UI | `frontend/` (Cloud only) | Vanilla JS management console — **not included in the initial OSS snapshot** (A1); self-host via API/CLI |

---

## Current capabilities & limitations

**Implemented and tested:**

- Management control plane (5 entities, HTTP API), tenant-aware in the cloud
  profile; the management Web UI is part of the cloud offering, not this
  repository
- Full inbound→outbound runtime pipeline with fail-closed semantics and typed
  failure codes per stage (`failure_stage` / `failure_code` / `retryable`)
- Interaction + Activity persistence: every request is recorded with independent
  `processing_status` / `delivery_status` and an append-only evidence timeline
- Observation read API (interaction detail, activity timeline)
- Real channel integrations: WeCom (inbound webhook verify/decrypt, outbound
  proactive-message API); local JSON channel + echo agent for zero-dependency
  first success
- Real agent adapter: Tencent ACP (minimal HTTP binding and SSE binding)
- Async execution boundary: HTTP `202/200 accepted` ≠ execution; a background
  worker claims and runs interactions
- Delivery lifecycle separation: the runtime pipeline covers the six inbound
  stages; external delivery is an explicit seventh stage, started **after** the
  agent result has been persisted (a delivery failure never rolls back or
  re-judges the persisted result)
- Delivery observability: the WeCom bot ACK wait window (frame sent → channel
  `errcode` receipt) is configurable (`WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS`),
  and delivery failures carry an `outcome_known` axis — `False` means "the
  frame was written but no receipt arrived" (outcome **unknown**, not
  "not delivered")
- Guard-failure delivery protection: a fail-closed earlier stage delivers
  nothing — a failed interaction cannot emit a stale message
- Optional delivery recovery — **disabled by default**: a recovery driver can
  re-attempt delivery for failures that are *provably* undelivered, using an
  atomic claim, an attempt fence and a lease, plus a tenant-scoped idempotent
  backfill for legacy rows. It is off (`RECOVERY_ENABLED=false`) unless an
  operator explicitly enables it and follows the recovery runbook; end-to-end
  exactly-once is **not** provided
- Artifact *references* in outbound delivery: agent responses may carry
  references (never bytes — the control plane does not fetch, store or proxy
  them), rendered into outbound channel messages under an explicit URI policy
- Docker Compose deployment assets (SQLite OSS profile, PostgreSQL cloud
  profile, production topology behind nginx)

**Explicit limitations (by design, for this stage):**

- **WeCom only** as a production channel today; other channels require writing
  an adapter against the inbound/outbound ports
- **Tencent ACP only** as a production agent runtime today; other runtimes
  require an `AgentAdapter` implementation (see `examples/custom-agent`)
- One channel maps to **one active Connection/Agent** (multi-agent-per-channel
  routing is a recorded capability gap, fail-closed if violated)
- The normal delivery path does **not** auto-retry, and message-id
  deduplication policies are **not implemented** (carriers and semantics are
  designed but intentionally deferred)
- Delivery-failure *replay* exists only as the **optional** recovery mechanism
  described above: it is off by default, it re-attempts only failures that are
  provably undelivered, it does not provide end-to-end exactly-once, and it has
  **not** been validated against a production database
- The development history and design ledgers are mostly written in Chinese;
  the English README is the primary international entry point, and deeper
  `docs/` remain Chinese for now

---

## Prerequisites

- Python 3.14 (the version tested by CI and used by the Docker images). The local
  Quick Start flow has also been run end-to-end on Windows with Python 3.12.13;
  that is a spot check of that one path, not full Python 3.12 support across
  all integrations or environments.
- pip
- Docker + Docker Compose (optional — for the container path)
- No external services required for the quick start (SQLite + local channel +
  local agent are built in)

## Installation

```bash
git clone https://github.com/SteeringFlow/agent-control-plane.git agent-control-plane
cd agent-control-plane/backend
pip install -r requirements.txt
```

Configuration is optional for the quick start: sensible defaults are built in
(SQLite at `sqlite:///./data/agent_control_plane.db`, auth disabled for local
single-tenant use). To customize, copy the template and edit values — never
commit your real `.env`:

```bash
cp .env.example .env   # keys only; every value falls back to the code default
```

## Quick start (zero external dependencies)

This walkthrough exercises the complete control-plane loop — User → Agent →
Channel → Identity → Connection → conversation grant → message → observable
response — without Tencent ACP, WeCom, or a public HTTPS endpoint. The database
is created automatically on first boot; no migration needed.

**Run it once, against a fresh database.** This walkthrough is written for an
empty database and is meant to be executed once, in order. Repeating the
resource-creation steps (2-6) against a database where those resources already
exist returns `HTTP 409` (already exists) rather than silently creating
duplicates. To start over, point `DATABASE_URL` at a new, empty database file
(from `backend/`, for example `DATABASE_URL=sqlite:///./data/agent_control_plane_fresh.db`)
instead of deleting or resetting a database that holds data you want to keep.

Start the API (from `backend/`):

```bash
uvicorn app.main:app
```

Verify it is up:

```bash
curl http://127.0.0.1:8000/health
# -> {"status": "ok"}
# API docs (Swagger):  http://127.0.0.1:8000/docs
# NOTE: the web management console is part of the cloud service and is not
# included in the initial OSS snapshot — drive this walkthrough via the API
# (below) or the CLI (cli/acp_cli.py).
```

Then provision the world (IDs start at 1 on a fresh database; invalid `type`
values are rejected with 422):

```bash
BASE=http://127.0.0.1:8000

# 1. Create a User
curl -s -X POST $BASE/api/users -H 'Content-Type: application/json' \
  -d '{"name": "local-user"}'

# 2. Create a Local Agent (type=local → built-in deterministic echo agent)
curl -s -X POST $BASE/api/agents -H 'Content-Type: application/json' \
  -d '{"name": "local-echo", "type": "local"}'

# 3. Create a Local Channel (type=local → local JSON intake, local outbox delivery)
curl -s -X POST $BASE/api/channels -H 'Content-Type: application/json' \
  -d '{"name": "local-1", "type": "local"}'

# 4. Create an Identity: bind external identity local-user-1 to (user, channel).
#    The runtime resolves inbound senders by (channel_id, external_id); an
#    unbound sender fails closed.
curl -s -X POST $BASE/api/identities -H 'Content-Type: application/json' \
  -d '{"user_id": 1, "channel_id": 1, "external_id": "local-user-1"}'

# 5. Create a Connection: bind the Channel to the Agent (routing is
#    channel-scoped: exactly one active connection per channel).
curl -s -X POST $BASE/api/connections -H 'Content-Type: application/json' \
  -d '{"channel_id": 1, "agent_id": 1, "name": "local-path"}'

# 6. Grant the conversation access to the Connection. Access is default-deny:
#    without this grant the message fails at the authorization stage with
#    failure_code=UNAUTHORIZED. For the local single chat, the conversation id
#    equals the external_id.
curl -s -X POST $BASE/api/connection-conversations \
  -H 'Content-Type: application/json' \
  -d '{"channel_id": 1, "external_conversation_id": "local-user-1", "connection_id": 1}'

# 7. Send a local message. HTTP 200 only means "accepted and persisted";
#    execution happens asynchronously in the background.
curl -s -X POST $BASE/api/channels/1/local/message \
  -H 'Content-Type: application/json' \
  -d '{"external_id": "local-user-1", "text": "hello"}'
# -> {"interaction_id": 1, "status": "accepted"}

# 8. Observe the agent response (after a short wait)
curl -s $BASE/api/interactions/1
```

**First-success criteria:** the interaction shows
`processing_status=succeeded` **and** `delivery_status=delivered`, with
`response_payload.content == "Echo: hello"`. A `200/accepted` response alone is
*acknowledgement, not success*. The step-6 grant is not optional — omitting it
produces a `failure_stage=authorization / failure_code=UNAUTHORIZED`
interaction, which is the fail-closed design working as intended, not a bug.

The agent response is also appended to `backend/data/local_outbox.jsonl`
(local delivery sink; the durable source of truth is the Interaction API).

Run the test suite (from `backend/`):

```bash
python -m pytest -q
```

## Docker

```bash
# OSS / local development profile (SQLite, API only)
# The initial OSS snapshot excludes frontend/, so build with Dockerfile.oss
# (identical layout, minus the console static files):
docker build -f Dockerfile.oss -t steeringflow-acp .
docker run --rm -p 8000:8000 steeringflow-acp

# Cloud profile (PostgreSQL + Alembic-managed schema evolution, includes the
# web console via the root Dockerfile):
docker compose -f docker-compose.cloud.yml up -d --build
```

The two profiles coexist: SQLite uses `create_all` at boot; the PostgreSQL cloud
profile runs `alembic upgrade head` at container start and never uses
`create_all`. To run migrations manually against a local PostgreSQL:

```bash
cd backend
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<db> python -m alembic upgrade head
```

Production-style deployment assets (nginx TLS termination, PostgreSQL role
separation, fail-closed credential injection) live under `_deploy/` — see
`_deploy/nginx/README.md`. Deployment credentials are injected via environment
variables only and are never committed.

## Configuration reference

All settings have code defaults; see `backend/.env.example` for the annotated
key list. Common keys (values shown as placeholders):

| Key | Example value | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/agent_control_plane.db` | Persistence (SQLite default; `postgresql+psycopg://...` for the cloud profile) |
| `ENVIRONMENT` | `development` | Runtime profile |
| `AUTH_REQUIRED` | `false` | Browser-session auth gate (`true` for multi-tenant cloud) |
| `DRIVER_POLL_INTERVAL_SECONDS` | `2.0` | Background worker cadence |
| `DRIVER_PICKUP_LIMIT` | `10` | Max interactions claimed per poll |
| `DRIVER_STALE_THRESHOLD_SECONDS` | `300` | Orphaned-interaction reclaim threshold |
| `TENCENT_ACP_*` | *(empty)* | Test-only Tencent ACP credentials for env-gated live smoke tests — never application runtime configuration |

Secrets (WeCom `corpsecret`/`token`/`encoding_aes_key`, agent runtime tokens)
are stored per-Channel/Connection via the management API and are **write-only**:
they are accepted on create/update and are always redacted in responses.

## Repository layout

```
agent-control-plane/
├── backend/
│   ├── app/
│   │   ├── main.py            # application composition root
│   │   ├── core/              # settings/config
│   │   ├── api/               # HTTP surface (management + runtime data plane)
│   │   ├── schemas/           # HTTP contracts (Pydantic)
│   │   ├── application/       # use-case services + runtime pipeline executors
│   │   ├── domain/            # entities + frozen runtime contracts (pure Python)
│   │   ├── repositories/      # ports (repositories, UoW, agent/channel adapters)
│   │   ├── adapters/          # real implementations + type→adapter routing
│   │   └── persistence/       # SQLAlchemy models, mappers, repositories, UoW
│   ├── agent_runtime_sdk/     # minimal SDK for building agent runtimes that
│   │                          #   speak the ACP wire protocol
│   ├── alembic/               # PostgreSQL schema migrations (cloud profile)
│   └── tests/                 # 180+ test modules (unit, contract, E2E, AST guards)
├── cli/                       # developer CLI helper
├── frontend/                  # management console — Cloud only, NOT in initial OSS (A1)
├── examples/                  # custom agent, multi-agent, streaming SDK, WeCom recipe
├── docs/                      # step-by-step design/decision ledgers (Chinese)
├── _deploy/                   # reviewed deployment assets (nginx/postgres/certbot)
├── docker-compose*.yml        # SQLite / cloud / production / website profiles
├── Dockerfile
└── README.md
```

> Directories are architecture boundaries; a directory existing does not imply
> the feature exists — empty shells document their responsibilities and
> prohibitions in their `__init__.py`.

## Documentation & examples

- `examples/custom-agent/` — implement a custom agent runtime against the ACP
  wire protocol
- `examples/multi-agent/` — run two independent agents behind one control plane
  with strict isolation
- `examples/agent-runtime-sdk/stateful-streaming/` — stateful, streaming agent
  using the bundled SDK
- `examples/wecom-workbuddy/` — recipe for connecting a real WeCom self-built
  app (configuration only, no code; no real credentials included)
- `docs/` — the full engineering ledger (design contracts, audits, decisions;
  mostly Chinese)
- `_deploy/nginx/README.md` — deployment topology and credential-injection
  discipline

## Project status

SteeringFlow is developed in small, audited increments ("steps"); the current
line is the `v0.1.0-rc.x` release-candidate series. The current candidate is
`0.1.0-rc.11` — **not tagged and not published yet**. Recent milestones:

| Milestone | Status |
|---|---|
| Persistence foundation (SQLAlchemy, UoW, 8 core tables) | ✅ |
| Management control plane: Agent / Channel / User / Identity / Connection (HTTP API; Web UI in cloud offering) | ✅ |
| Runtime pipeline: 6-stage executors, fail-closed, fixed ordering | ✅ |
| Interaction & Activity persistence + observation read API | ✅ |
| WeCom channel (inbound + outbound) and Tencent ACP agent adapters | ✅ |
| Async worker execution (ACK ≠ execution), multi-agent isolation | ✅ |
| Local first-success loop (zero external dependencies) | ✅ |
| Deployment boundary (nginx/compose, credential fail-closed injection) | ✅ |
| Delivery lifecycle separation + delivery observability + guard-failure delivery protection | ✅ (rc.11 candidate) |
| Optional delivery recovery (atomic claim, attempt fence, backfill) — **off by default**, not production-validated | 🚧 rc.11 candidate (opt-in, operator runbook) |
| Artifact references in outbound delivery (URI policy, no byte transfer) | ✅ (rc.11 candidate) |
| Release hardening, docs, snapshot publication | 🚧 in progress (`rc.11` candidate — not tagged, not published; the public `v0.1.0-rc.10` prerelease on GitHub was published from an earlier snapshot and does not contain this candidate) |

Design/decision details for every milestone are recorded under `docs/`.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup, test expectations, and pull-request process. Please read and
follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Please report it responsibly — see
[SECURITY.md](SECURITY.md). Please do not open public issues for
unpublished security problems.

## OSS vs. cloud service

This repository is the **open-source, self-hostable core**: the control plane,
runtime, adapters, and deployment assets. A hosted cloud offering built on this
core may exist separately; cloud-specific operational services (managed
hosting, beta programs) are **not part of this repository** and are not required
to run it. Everything needed to run SteeringFlow yourself is in this repo.

## Trademarks

The name **SteeringFlow** and the project logo identify this project and its
cloud service. The Apache License 2.0 governs the **source code** in this
repository; it does not grant a license to use the SteeringFlow name or logo
as a product or service brand. Third parties may fork, modify, self-host, and
offer services based on the code under the terms of Apache-2.0, provided they
do not present them as official SteeringFlow offerings or imply endorsement by
the project. Brand usage beyond code licensing is handled separately by the
project owner; no trademark registration is asserted or claimed here.

## License

This project (Agent Control Plane / SteeringFlow) is licensed under the
**Apache License 2.0** — see [LICENSE](LICENSE). Third-party dependencies are
declared in `backend/requirements.txt` as separate PyPI packages and are **not
redistributed** in this repository; they remain under their own licenses (per
the audit: MIT / BSD-3 / Apache-2.0 for most, and **LGPL-3.0** for `psycopg`,
the PostgreSQL driver used by the cloud profile).

```
Copyright (c) 2026 Dong Xiang

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```
