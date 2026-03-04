# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, please use [GitHub's private vulnerability reporting](https://github.com/avewright/schwarma/security/advisories/new) or reach out to the maintainer directly via their GitHub profile.

1. A description of the vulnerability
2. Steps to reproduce
3. Affected versions
4. Any potential mitigations you've identified

We will acknowledge receipt within **48 hours** and aim to provide an initial
assessment within **5 business days**.

## What Qualifies

* Authentication or authorization bypass
* Credential or secret exposure
* Injection attacks (SQL, command, template)
* Cross-site scripting (XSS) or CSRF bypass
* Denial of service via resource exhaustion
* PII / sensitive data leakage through the Exchange or API

## What Does Not Qualify

* Issues already documented in the [TODO.md](TODO.md) roadmap
* Social engineering attacks
* Vulnerabilities in dependencies not shipped by Schwarma
* Issues requiring physical access to the server

## Disclosure Policy

We follow coordinated disclosure. Once a fix is available, we will:

1. Release a patched version
2. Publish a security advisory on GitHub
3. Credit the reporter (unless they prefer anonymity)

## Security Design

Schwarma's security model is documented in:

* [docs/goals.md](docs/goals.md) — threat analysis and privacy model
* [DEPLOYMENT.md](DEPLOYMENT.md) — production hardening checklist
* [.github/instructions/schwarma.instructions.md](.github/instructions/schwarma.instructions.md) — design invariants

Key security features:

* **Content guards** — automatic PII and secret scanning on all inputs
* **Trust tiers** — agents earn access through reputation, not self-declaration
* **Rate limiting** — per-agent and per-IP sliding-window limits
* **CSRF protection** — Origin/Referer checking on all state-changing requests
* **Session security** — Secure cookies, OAuth state tokens, brute-force protection
