"""Regex-based secret scanner used as a pre-flight guard before pushing to
GitHub Tier 0. Conservative by design — false positives are acceptable;
false negatives are not.

Strategy: curated set of well-known credential prefixes (AWS, Anthropic,
OpenAI, GitHub, Google OAuth, SSH private keys) + filename heuristics
(``.env*``, ``id_*``, ``*.pem``, ``*.key``, ``*.p12``). Any match = refuse.

This is **not** a substitute for tools like ``gitleaks``. It catches the
top-tier mistakes; for paranoia-level coverage, run gitleaks too. The
goal here is to make the common case safe by default for the Sanctum
productization audience.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Patterns that strongly indicate a credential. Each entry is (label, regex).
CONTENT_PATTERNS: list[tuple[str, re.Pattern[bytes]]] = [
    ("aws-access-key-id", re.compile(rb"AKIA[0-9A-Z]{16}")),
    (
        "aws-secret-access-key-env",
        re.compile(rb"AWS_SECRET_ACCESS_KEY\s*=\s*['\"]?[A-Za-z0-9/+]{40}"),
    ),
    ("anthropic-api-key", re.compile(rb"sk-ant-(?:api03-)?[A-Za-z0-9_\-]{40,}")),
    ("openai-api-key", re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_\-]{40,}")),
    ("github-token", re.compile(rb"gh[pousr]_[A-Za-z0-9_]{36,}")),
    ("google-oauth-client-secret", re.compile(rb"GOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("google-api-key", re.compile(rb"AIza[0-9A-Za-z_\-]{35}")),
    ("slack-token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}")),
    (
        "private-key-block",
        re.compile(
            rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
            rb".*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
    ),
    (
        "ssh-private-key",
        re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"),
    ),
    ("pgp-private-key", re.compile(rb"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    (
        "jwt-with-likely-secret",
        re.compile(rb"eyJ[A-Za-z0-9_\-]{20,}\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    ),
    ("b2-application-key", re.compile(rb"K00[0-9][A-Za-z0-9+/_-]{42,}")),
    (
        "r2-secret-access-key-context",
        re.compile(rb"r2[_-]?secret[_-]?access[_-]?key\s*[:=]\s*['\"]?[0-9a-f]{64}", re.IGNORECASE),
    ),
]

# Filenames that are categorically risky regardless of contents.
FILENAME_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("env-file", re.compile(r"^\.env(\.[^/]*)?$|/\.env(\.[^/]*)?$")),
    ("private-key-by-name", re.compile(r"(?:^|/)id_(?:rsa|ed25519|ecdsa|dsa)$")),
    ("pem-file", re.compile(r"\.pem$")),
    ("key-file", re.compile(r"\.key$")),
    ("p12-file", re.compile(r"\.p12$|\.pfx$")),
    ("netrc", re.compile(r"(?:^|/)\.netrc$")),
    ("aws-credentials", re.compile(r"(?:^|/)\.aws/credentials$")),
    ("rclone-conf", re.compile(r"(?:^|/)rclone\.conf$")),
    ("kubeconfig", re.compile(r"(?:^|/)\.kube/config$")),
]

MAX_SCAN_BYTES = 2 * 1024 * 1024  # never read more than 2 MB per file


@dataclass(frozen=True, slots=True)
class Finding:
    """One secret-scanner hit. ``location`` is either ``filename`` or a
    line range from the file's content."""

    path: Path
    pattern: str
    location: str
    snippet: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.path} · {self.pattern} ({self.location}): {self.snippet}"


def scan_path(path: Path) -> list[Finding]:
    """Scan one file (regular files only). Returns matches; empty if clean."""
    findings: list[Finding] = []
    posix = str(path)
    for label, name_regex in FILENAME_PATTERNS:
        if name_regex.search(posix):
            findings.append(
                Finding(path=path, pattern=label, location="filename", snippet=path.name)
            )
    if not path.is_file():
        return findings
    try:
        size = path.stat().st_size
    except OSError:
        return findings
    if size == 0:
        return findings
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_SCAN_BYTES)
    except OSError:
        return findings
    for label, content_regex in CONTENT_PATTERNS:
        match = content_regex.search(data)
        if match is None:
            continue
        # Compute line number + a tight snippet
        before = data[: match.start()].count(b"\n") + 1
        snippet = match.group(0)
        # Trim and redact most of the credential — keep ~12 chars of context
        if len(snippet) > 24:
            snippet = snippet[:12] + b"..." + snippet[-4:]
        findings.append(
            Finding(
                path=path,
                pattern=label,
                location=f"line {before}",
                snippet=snippet.decode("utf-8", errors="replace"),
            )
        )
    return findings


def scan_paths(paths: list[Path]) -> list[Finding]:
    """Scan a flat list of files. Caller is responsible for path expansion."""
    out: list[Finding] = []
    for p in paths:
        out.extend(scan_path(p))
    return out
