# Security Policy

`sanctum-cli` manages cloud-backup credentials, Keychain secrets, and the local
provider/proxy stack on the machines it runs on. We take vulnerability reports
seriously and appreciate responsible disclosure.

## Reporting a Vulnerability

**Do not open a public GitHub issue for a security problem.** Public issues are
visible to everyone and can put users at risk before a fix ships.

Instead, report privately through either:

- **GitHub Security Advisories** — the preferred channel. Open a draft advisory
  at <https://github.com/ogilthorp3/sanctum-cli/security/advisories/new>. This
  keeps the report private and lets us collaborate on a fix and a CVE.
- **Email** — `security@sanctum.run`. Encrypt with our public key if the report
  contains sensitive details.

Please include:

- The version (`sanctum --version`) and platform (macOS version, arch).
- A clear description of the issue and its impact.
- Reproduction steps or a proof of concept, if you have one.
- Any suggested remediation.

## What to Expect

- **Acknowledgement** within 3 business days.
- **Triage + initial assessment** within 7 business days, including whether we
  consider it in scope and a rough severity.
- **Fix + coordinated disclosure** — we aim to ship a fix and publish an advisory
  within 90 days, sooner for actively-exploited issues. We will credit you in the
  advisory unless you ask us not to.

## Scope

In scope:

- The `sanctum-cli` Python package and the `sanctum` binary in this repository.
- Credential handling (Keychain reads/writes, backup keys, bridge/CF Access
  tokens), the secret scanner, and the cloud-backup wizards.
- The local provider/proxy transport (TLS verification, endpoint resolution).

Out of scope:

- Vulnerabilities in third-party dependencies — please report those upstream,
  though we welcome a heads-up so we can pin or patch.
- Issues that require an attacker to already have local root / Keychain unlock on
  the user's machine, or physical access.
- The author's private Sanctum haus infrastructure (the Mini, Firewalla, council,
  bridge). The CLI's haus-only commands are not part of the public beta.
- Social engineering, denial of service against the author's own hosts, and
  findings that only affect unsupported configurations.

## Supported Versions

Security fixes target the latest released version. Older versions are
best-effort; please upgrade with `sanctum update` (or `brew upgrade sanctum-cli`)
before reporting.

| Version | Supported |
|---|---|
| 0.10.x | ✅ |
| < 0.10 | ❌ (upgrade) |
