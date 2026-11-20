"""Repo-wide regression gate ensuring no personal or private host literals leak."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKIP_SUFFIXES = frozenset({
    ".crt", ".key", ".pem", ".p12", ".der",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".pyc", ".pyo", ".so", ".dylib", ".whl", ".tar", ".gz", ".zip",
})
_SCAN_ROOTS = (
    "sanctum_cli", "deploy", "tests", "tools", "docs",
    "README.md", "pyproject.toml", "CHANGELOG.md", "SPEC.md", "SECURITY.md", ".github",
)

# Patterns are assembled via concatenation so this file never triggers itself.
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tailnet-id", re.compile("tail" + "7c6d11", re.I)),
    ("owner-mbp", re.compile("berts?-" + "(?:mbp|iphone|macbook)", re.I)),
    ("site-man" + "oir", re.compile(r"\b" + "man" + "oir" + r"\b", re.I)),
    ("site-cha" + "let", re.compile(r"\b" + "cha" + "let" + r"\b", re.I)),
    ("operator-home", re.compile("/Users/" + "be" + "rt(?:/|$|\\b)", re.I)),
    ("surname", re.compile(r"(?<!Bertrand\s)" + "nep" + "veu", re.I)),
    ("domain-org", re.compile("trip" + "tyq", re.I)),
    ("private-email", re.compile("charles" + "@", re.I)),
    ("operator-ssh", re.compile(r"\b" + "be" + "rt" + "@", re.I)),
    ("operator-name", re.compile(r"\b" + "be" + "rt" + r"\b", re.I)),
    ("captured-mac", re.compile(
        "(?:" + "d0:11:e5:" + "1c:88:59|"
        + "32:a6:f4:" + "de:54:cf|"
        + "20:6d:31:" + "51:67:82|"
        + "7a:87:ac:" + "cd:8e:a2|"
        + "20:f8:3b:" + "02:3a:c8|"
        + "84:2f:57:" + "02:be:ee|"
        + "de:48:45:" + "83:ae:0a)", re.I
    )),
    ("captured-isp-ip", re.compile(r"(?:\b70\.53\." + r"241\.21\b|\b24\.150\." + r"33\.\d{1,3}\b)")),
)

_CGNAT_IP_RE = re.compile(r"\b(100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b")
_CGNAT_CIDR_RE = re.compile(r"\b100\.64\.0\.0/10\b")


def _tracked_files() -> list[Path]:
    if (REPO_ROOT / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
        paths = [
            REPO_ROOT / rel
            for rel in proc.stdout.decode("utf-8").split("\0")
            if rel
        ]
        self_path = Path(__file__).resolve()
        if self_path not in paths:
            paths.append(self_path)
    else:
        paths = []
        for entry in _SCAN_ROOTS:
            target = REPO_ROOT / entry
            if target.is_file():
                paths.append(target)
            elif target.is_dir():
                paths.extend(p for p in target.rglob("*") if p.is_file())
    return [
        p for p in sorted(paths)
        if p.is_file()
        and p.suffix.lower() not in _SKIP_SUFFIXES
        and "__pycache__" not in p.parts
    ]


def _scan_text(text: str, rel: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pat in _FORBIDDEN_PATTERNS:
            if pat.search(line):
                hits.append(f"{rel}:{lineno}: forbidden {label}")
        stripped = _CGNAT_CIDR_RE.sub("", line)
        for m in _CGNAT_IP_RE.finditer(stripped):
            ip = m.group(1)
            if not ip.startswith("100.64.0."):
                hits.append(f"{rel}:{lineno}: non-placeholder CGNAT IP {ip}")
    return hits


def test_repo_has_no_personal_leaks() -> None:
    violations: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        violations.extend(_scan_text(text, rel))
    assert not violations, "Personal/private leaks detected:\n" + "\n".join(violations)


def test_leak_scanner_catches_patterns_and_allows_placeholders() -> None:
    sample_bad = "\n".join([
        "host=" + "man" + "oir." + "tail" + "7c6d11.ts.net",
        "path=/Users/" + "be" + "rt/Projects",
        "ip=100.68." + "36.16",
        "email=" + "charles" + "@example.org",
    ])
    hits = _scan_text(sample_bad, "synthetic.py")
    assert len(hits) >= 4

    sample_ok = "\n".join([
        "Copyright 2026 Bertrand " + "Nep" + "veu",
        "cidr = '100.64.0.0/10'",
        "peer = '100.64.0.5'",
        "dns = 'home.tailnet-abc.ts.net'",
    ])
    assert _scan_text(sample_ok, "clean.py") == []
