# Security Policy

## Supported versions

SteeringFlow is an early-stage release candidate (`v0.1.0-rc.x`). Only the
latest release candidate line is supported with security fixes.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

> **Note:** the project does not yet have a dedicated security email address
> or a CNA. Until one exists, the realistic reporting routes are:
>
> 1. **Preferred:** GitHub's *private vulnerability reporting* on this
>    repository (Security tab → "Report a vulnerability"), which keeps the
>    report and any discussion private between you and the maintainers.
> 2. If private vulnerability reporting is unavailable, open a **minimal
>    public issue that contains no technical details** (title only, e.g.
>    "security report: please provide a private channel") and ask maintainers
>    to follow up privately.

When reporting, please include (privately):

- A description of the issue and its impact
- Reproduction steps or a proof of concept
- Affected version / commit (the image or `VERSION` file value)

We will not publish vulnerability details until a fix or mitigation is
available. We ask you to do the same.

## Scope

In scope: the code and deployment assets in this repository — the backend API,
runtime, adapters, management API, and `_deploy/` deployment assets.

Out of scope: the hosted cloud service (if any), third-party services you
configure (WeCom, Tencent ACP, your mail provider), and issues that require a
compromised host or social engineering.

## Design notes relevant to security review

- All deployment credentials are injected via environment variables; compose
  files fail closed if required credentials are missing.
- `.env` files, TLS keys, and htpasswd files are git-ignored and must never be
  committed; the test suite includes guards against credential leakage.
- Channel/Connection credentials stored via the management API are write-only
  and redacted in all read responses.
- WeCom inbound webhooks are verified (SHA1 signature) and decrypted (AES)
  before reaching the runtime; the webhook surface is the only unauthenticated
  public endpoint, protected by protocol-level verification.
