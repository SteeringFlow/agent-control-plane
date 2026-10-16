# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

> Changes on `master` after the `v0.1.0-rc.8` tag. Not yet part of any release
> candidate; the next candidate is planned as `v0.1.0-rc.9` (not yet tagged).

### Added

- **Production deployment runbook** for the rc.8 cutover (an internal
  deployment file, not part of this repository): authorization-gated cutover /
  rollback procedure with clean-build-context and image-identity discipline
  (`a251e94`).
- **Early-access feedback contact entry** in the web UI (`e86c4ce`).

### Fixed

- **Auth register route exposure boundary** enforced
  (`deb6b8f`, `backend/app/api/auth.py`).

## [0.1.0-rc.8] - 2026-10-10

> SteeringFlow ACP V0.1.0-rc.8 release candidate (fail-closed
> `POSTGRES_PASSWORD`, deployment assets integration, OCI image provenance).
> Tagged at `b3f53fc`; release date and final regression results are recorded
> at the RC tag.

### Added

- **Versioned website deployment assets**: `_deploy/nginx/website-vhost.conf.template`,
  `_deploy/deploy-website.sh` (explicit `--repo/--src`, fail-closed source
  resolution, git-archive artifact with no `.git` and no TLS material,
  provenance file, isolated `nginx -t` gate), `_deploy/certbot/` renewal +
  deploy hooks, and `docker-compose.website.yml`.
- **OCI image provenance** (`Dockerfile`): image labels carry the build commit
  and version so a deployed image can be tied back to its source revision.

### Changed

- **`docker-compose.cloud.yml` / `docker-compose.production.yml`**:
  `POSTGRES_PASSWORD` (and the app-role password) are now **fail-closed** —
  compose refuses to start when they are not provided via the environment
  (`${POSTGRES_PASSWORD:?...}`); no default password exists anywhere.
- **Machine guard** for the above boundary:
  `backend/tests/test_m7_aui_10_8_12_fail_closed_compose.py`.

## [0.1.0-rc.7] - 2026-10-10

> SteeringFlow ACP V0.1.0-rc.7 release candidate (M6.6 + M7-AUI series,
> verified). Tagged at `aac835f`; release date and final regression results
> are recorded at the RC tag.

### Added

- **WorkBuddy agent adapter** (`backend/app/adapters/agents/workbuddy.py`):
  a new agent runtime adapter alongside the existing Tencent ACP adapters.
- **Connection verification**: `backend/app/adapters/connection_verify.py` plus
  per-connection management endpoints for probing/verifying a bound agent
  connection (`GET/POST /api/connections/{id}/verify`, connection health).
- **Bootstrap arms**: new `bootstrap_arm` entity across domain, application,
  persistence (SQLAlchemy model + repository + mapper) and management API, with
  schema migration `backend/alembic/versions/0008_bootstrap_arms.py`.
- **OAuth callback endpoint** (`backend/app/api/oauth_callback.py`) and agent
  identity application service (`backend/app/application/agent_identity.py`).

### Changed

- **WeCom bot transport**: expanded WebSocket bot adapter
  (`backend/app/adapters/channels/wecom_bot.py`) and runtime channel intake
  (`backend/app/api/runtime/channels.py`).
- **Auth / browser session**: signup/login flow updates
  (`backend/app/api/auth.py`, `backend/app/application/browser_auth.py`).
- `.gitignore` additions tightening the local-artifact boundary.

## [0.1.0-rc.6] - 2026-09-20

> SteeringFlow ACP V0.1.0-rc.6 release candidate (Cloud UX simplification:
> Show the Flow. Hide the Machinery.).
> Release date and final regression results are recorded at the RC tag.

### Added

- **Flow Home (`/home.html`)**: authenticated landing surface expressing the
  `Channel → SteeringFlow → Agent` task model, with five states (Empty /
  Partially Connected / Connecting / Connected / Error · Action Required).
- **Unified Connect Flow (`/connect.html`)**: a single context-aware user task
  replacing the split Connect Agent / Connect Channel entry points — completed
  steps are skipped and only missing steps are filled (Empty / existing Channel
  / existing Agent / both present).
- **Progressive Disclosure**: platform credentials stay secondary; Agent type,
  endpoint, binding name and Connection config are demoted to Advanced/Internal
  and are not shown to first-use users.
- **Agent 409 semantics (C3)**: a same-name Agent conflict is never silently
  reused — the user decides (use the existing Agent / rename); an `inactive`
  same-name Agent is not offered as a "use" action.
- **Connection 409 semantics (C4)**: an existing binding is never treated as
  connected — it is resolved by `(channel_id, agent_id)` and judged by status
  (`active` → connected, `inactive` → action required); no second Connection is
  created. No Saga / rollback / new idempotency semantics.
- **Cloud UX contract guards** (`test_cloud_m6_3_ux_contract.py`, 34 tests).

### Changed

- **Management navigation regrouped**: Flow Home / Agents / Channels are primary;
  Connection / User / Identity / Interaction Observability moved into a collapsed
  "Advanced" group. All existing links and capabilities are preserved.
- **`workspace.js`**: authenticated users are redirected to `/home.html`.

### Notes

- **No Runtime changes. No API changes. No DB / migration changes.** No new
  endpoints, entities, migrations, or idempotency semantics.
- Frozen and untouched: `connect-agent.*`, `connect-channel.*`, `workspace.html`.

### Validation

- **Cloud UX verification (browser end-to-end)**: J1–J8 executed against a
  production-form Docker Compose stack (PostgreSQL 16 + `alembic head` + nginx
  TLS) built from this candidate. J1–J7 pass through the real nginx path
  (Empty / Partial A / Partial B / Partial C / Connected / Agent 409 /
  Connection 409); J8-A First Success confirmed from a real
  `processing=succeeded × delivery=delivered` Interaction. J8-B (real WeCom
  inbound) is not applicable in the local environment and is deferred.
- **Guards**: 34 new M6.3 contract tests + 188 existing guards = **222 passing**.
- **Full regression**: **2246 passed / 33 skipped / 0 failed / 0 errors**
  (independent runs consistent; executed with a one-time `--basetemp`).

## [0.1.0-rc.5] - 2026-09-20

> SteeringFlow ACP V0.1.0-rc.5 release candidate (Cloud product entry integration).
> Release date and final regression results are recorded at the RC tag.

### Release provenance

- Annotated tag `v0.1.0-rc.5` → tag object `4b01598` → commit `cb0e5f5`
  ("release: prepare v0.1.0-rc.5"), tagged by `XiangDong`.
- Tag created **2026-09-20 11:10:41 +0800**; source commit dated
  2026-09-19 19:29:41 +0800 (both from git tag / commit metadata).
- Production provenance (read-only observation of the public endpoint):
  `GET /api/version` → `{"version":"0.1.0-rc.5","commit":"cb0e5f52b89ba5609482ee2ec0593fe172698b39"}`
  — the deployed Production build matches this tag's commit.
- **Not independently documented**: no `M5.8-B` / `M5.8-C` release-verification
  artifact was found in this repository (docs and test names were searched), and
  no in-repo deployment record exists. Only the provenance above and the M5.2
  regression coverage below are traceable; anything beyond them is left
  unstated rather than inferred.

### Added

- **Cloud Product Entry / Workspace login UI**: `GET /` now issues a product entry
  redirect to `/workspace.html`; the workspace UI serves the browser login surface.
- **Public `/api/version` provenance endpoint**: exposes `version` + `commit` for
  production release provenance.
- **Production version/commit provenance**: the running build reports its exact
  release version and source commit.
- **Public product surface + Ops BasicAuth boundary**: the product surface is public
  while operational documentation (`/docs`) remains behind `ACP Ops` BasicAuth.

### Changed

- **`/` product entry redirect**: `GET /` now redirects to `/workspace.html`.
- **Browser product surface uses application-layer session auth**: workspace access
  is gated by the ACP session cookie (HttpOnly / SameSite=Strict / Secure).
- **Operational documentation remains behind `ACP Ops` BasicAuth**: `/docs` continues
  to require operator credentials.

### Tests / validation

- M5.2 dedicated regression coverage exercises the cloud product entry end-to-end
  (entry redirect, workspace UI, `/api/version` provenance, session auth, and the
  Ops BasicAuth boundary).

## [0.1.0-rc.4] - TBD

> SteeringFlow ACP V0 release candidate (OSS + Cloud).
> Release date and final regression results are recorded at the RC tag.

### Added

- **Cloud production deployment baseline**: production PostgreSQL profile with the
  Alembic migration chain, split admin/app database roles, and a readiness healthcheck.
- **WeCom Bot WebSocket transport** (`type=wecom_bot`) with outbound delivery ACK
  validation.
- **External Identity decoupled from the ACP User lifecycle**: channel-side
  participants no longer require an ACP User (`identities.user_id` nullable;
  migration `0006`).
- **Conversation → Connection access authorization** enforced in the Runtime pipeline.
- **ConnectionConversation Grant persistence** (`connection_conversations`), delivered
  by migration `0007` (schema + tenant RLS + backfill from existing sessions).
- **Grant Management resource**: create / list / delete grants, plus the read-only
  `known-conversations` projection.
- **WeCom Bot group chat support**: `chattype=group` is accepted and mapped to a group
  conversation key.
- **Real WeCom authorization end-to-end acceptance coverage** (authorization positive,
  negative, and cross-conversation isolation exercised against a real WeCom bot).

### Changed

- **Runtime pipeline ordering**: access authorization is now performed **before**
  routing (`Identity → Authorization → Routing → Session Resolution → …`).
- **Authorization is default-deny**: a Conversation reaches a Connection only through
  an explicit Grant; an absent grant yields `UNAUTHORIZED`.
- **Golden Paths explicitly provision Identity + Conversation Grant** for OSS Local,
  Cloud WeCom (single chat), and WeCom Bot (group chat).
- **WeCom group conversation semantics** use `chatid` as `external_conversation_id`
  (the sender `userid` remains `external_identity_id`).
- **Local First Success procedure** documented as a canonical, current-version guide
  (`docs/step-15.2-local-first-success.md`).

### Fixed

- **Test infrastructure — WeCom WebSocket transport tests**: each unit of work now uses
  an isolated, file-backed SQLite engine, removing cross-thread transaction interference
  between the WS transport execution thread and the test thread. This had produced
  nondeterministic (false pass / false fail) results. No production code change.

### Validation

Validation categories exercised for this release candidate:

- PostgreSQL authorization closure (against a real PostgreSQL instance)
- Row-level security / tenant isolation (authorization table RLS enabled + forced)
- Authorization contract guards (architecture / ADR guards)
- Real WeCom E2E acceptance (authorization: positive, negative, cross-conversation isolation)
- WeCom Bot group chat (group callback handling + per-member identity isolation)
- Local full regression (SQLite)
- PostgreSQL full regression

> Final RC regression (exact pass/fail counts) to be recorded at the release candidate tag.

## [0.1.0-rc.3] - 2026-09-14

Cloud V0 release candidate. Annotated tag message: "Cloud V0 Release Candidate
v0.1.0-rc.3 (signup/sequence blocker fixed)". The release-blocker fix was
`fix(cloud): advance tenants_id_seq after bootstrap insert`.

## [0.1.0-rc.2] - 2026-09-14

Cloud V0 release candidate. Annotated tag message: "Cloud V0 Release Candidate v0.1.0-rc.2".

## [0.1.0-rc.1] - 2026-09-12

Initial release candidate. Annotated tag message: "ACP V0.x Release Candidate 1 —
freeze 16.1.3 Deployment Boundary".
