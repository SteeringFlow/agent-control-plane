# Contributing to SteeringFlow

Thank you for considering a contribution! SteeringFlow is an early-stage
release candidate, so the bar for changes is: **small, tested, and honest
about limitations**.

## Reporting issues

- Open a GitHub issue describing the behavior you expected, what happened,
  and how to reproduce it.
- For security vulnerabilities, do **not** open a public issue — see
  [SECURITY.md](SECURITY.md).
- Include your OS, Python version, and the command(s) you ran. Never paste
  real credentials, tokens, or `.env` contents into an issue.

## Development setup

```bash
git clone <repository-url> agent-control-plane
cd agent-control-plane/backend
pip install -r requirements.txt

# optional: local settings (keys only; defaults are built in)
cp .env.example .env
```

Run the API locally:

```bash
uvicorn app.main:app
# health check:
curl http://127.0.0.1:8000/health   # -> {"status": "ok"}
```

## Running the tests

From `backend/`:

```bash
python -m pytest -q
```

The suite runs fully locally on SQLite — no external services, no network
access, and no real credentials are required. A subset of live-wire tests is
env-gated and stays skipped unless you explicitly provide test-only
credentials via environment variables (see `backend/.env.example`).

Please make sure the full suite passes **except for two documented
pre-existing baseline failures** —
`test_step6_5::test_e_errors_are_application_level` and
`test_step6_7::test_reverse_audit_next_stage_not_smuggled` — both caused by
outdated inventory-freeze assertions, not product behavior: the frozen
error-class list is missing `DuplicateBotChannelError`, and the frozen
application-layer file list is missing `agent_identity.py`. As of the
B4.10 export audit (2026-10-16), the audited baseline on a clean export
copy was **2360 passed / 2 failed / 510 skipped / 0 errors**; these counts
describe the historical audited baseline and are **not a guarantee for your
environment or run**. Any failure outside this documented baseline —
including any new failure beyond these two — must be investigated as a
regression introduced by your change, not assumed to be part of the known
baseline. If your change touches the corresponding frozen inventories,
update the frozen lists accordingly. If your
change is architecture-related, note that the import-boundary rules are
enforced by machine-checked tests (`backend/tests/test_import_boundaries.py`)
— they will fail if layering rules are violated.

## Pull requests

1. Create a focused branch; one logical change per PR.
2. Add or update tests for any behavior change.
3. Keep the architecture honest: dependencies point inward (api → application
   → domain; persistence is the only SQLAlchemy layer; `repositories/` are
   ports that never import persistence). The boundary tests will tell you.
4. Never commit secrets: `.env` and `*.env` are ignored on purpose; TLS keys,
   htpasswd files, and rendered runtime configs must never be added.
5. Write commit messages that say what changed and why.
6. If you are adding a dependency, justify it in the PR description — the
   runtime dependency list is deliberately small and pinned.

## Documentation

- `README.md` is the primary (English) entry point — keep the quick start
  commands working and verified against the actual code.
- `docs/` contains the step-by-step design and decision ledgers (mostly
  Chinese); when a change contradicts a recorded contract, call it out in the
  PR rather than silently diverging.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0, the same as the project ([LICENSE](LICENSE)).
