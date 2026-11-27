"""``sanctum bug-report`` — redacted one-command diagnostic report for beta feedback.

Collects OS/chip/RAM hardware specs, ``sanctum --version``, Rust engine service
health (``sanctum haus status``), and ``sanctum self-test`` summary, strips all
secrets / tokens / emails / phone numbers / home usernames / private IPs, copies
to the macOS clipboard, and opens a pre-filled GitHub issue URL.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import urllib.parse
import webbrowser
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel

from sanctum_cli import __version__
from sanctum_cli.commands import haus_cmd
from sanctum_cli.mesh.metrics import detect_chip, detect_os, detect_ram_gb

console = Console()

ISSUES_NEW_URL = "https://github.com/Ogilthorp3/sanctum-cli/issues/new"

# Patterns scrubbed unconditionally before any diagnostic leaves the machine.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b"), "<REDACTED_ANTHROPIC_KEY>"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{12,}\b"), "<REDACTED_OPENROUTER_KEY>"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "<REDACTED_API_KEY>"),
    (re.compile(r"\bxai-[A-Za-z0-9_\-]{16,}\b"), "<REDACTED_XAI_KEY>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}\b"), "<REDACTED_GH_TOKEN>"),
    (re.compile(r"\bAIzaSy[A-Za-z0-9_\-]{20,}\b"), "<REDACTED_GOOGLE_KEY>"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<REDACTED_TOKEN>"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?)[A-Za-z0-9._\-/+]{12,}"), r"\1<REDACTED>"),
    (re.compile(r"\b[a-f0-9]{48,64}\b", re.IGNORECASE), "<REDACTED_HEX>"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<REDACTED_EMAIL>"),
    (re.compile(r"(?<!\d)\+[1-9]\d{7,14}\b"), "<REDACTED_PHONE>"),
    (re.compile(r"/Users/[^/\s'\"`]+"), "/Users/<redacted>"),
    (re.compile(r"/home/[^/\s'\"`]+"), "/home/<redacted>"),
    # Private LAN & Tailnet IPs (preserve 127.0.0.1 loopback for diagnostic clarity)
    (re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"), "<REDACTED_IP>"),
    (re.compile(r"\b[a-zA-Z0-9\-]+\.tail[a-z0-9]+\.ts\.net\b"), "<REDACTED_TAILNET>"),
)


def redact_text(raw: str) -> str:
    """Scrub secrets, PII, usernames, and private network addresses from text."""
    out = raw
    for pattern, repl in _REDACTION_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _run_json_subcommand(args: list[str], *, timeout: int = 12) -> dict[str, Any] | None:
    sanctum_bin = shutil.which("sanctum")
    if not sanctum_bin:
        return None
    try:
        r = subprocess.run(
            [sanctum_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        txt = r.stdout.strip()
        start = txt.find("{")
        if start == -1:
            return None
        parsed = json.loads(txt[start:])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def collect_redacted_report(
    *,
    summary: str = "",
    run_self_test: bool = True,
) -> dict[str, Any]:
    """Assemble a zero-PII diagnostic snapshot suitable for GitHub issues."""
    rust_state = haus_cmd.load_rust_state()
    probes = haus_cmd.probe_core_services()
    self_test_data = _run_json_subcommand(["self-test", "--json"]) if run_self_test else None

    failed_probes: list[dict[str, str]] = []
    st_summary = "skipped"
    if isinstance(self_test_data, dict):
        st_summary = (
            f"tier={self_test_data.get('tier', '?')} "
            f"passed={self_test_data.get('passed', 0)} "
            f"failed={self_test_data.get('failed', 0)} "
            f"n/a={self_test_data.get('not_applicable', 0)}"
        )
        for p in self_test_data.get("probes") or []:
            if isinstance(p, dict) and not p.get("passed", True):
                failed_probes.append(
                    {
                        "name": str(p.get("name", "?")),
                        "detail": redact_text(str(p.get("detail", ""))),
                    }
                )

    report = {
        "cli_version": __version__,
        "rust_release": rust_state.get("release_tag")
        or ("operator-live" if haus_cmd.is_operator_haus() else "not-installed"),
        "os": detect_os(),
        "arch": platform.machine(),
        "chip": detect_chip(),
        "ram_gb": detect_ram_gb(),
        "services": {k: ("UP" if v else "offline") for k, v in probes.items()},
        "self_test": st_summary,
        "failed_probes": failed_probes,
        "summary": redact_text(summary.strip()),
    }
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    """Format the redacted diagnostic report as GitHub-ready Markdown."""
    lines = [
        "### What happened",
        report.get("summary") or "_Describe what you ran and what broke._",
        "",
        "### Environment (auto-redacted)",
        f"- **sanctum-cli:** `v{report['cli_version']}`",
        f"- **Rust suite:** `{report['rust_release']}`",
        f"- **Hardware:** `{report['chip']}` · `{report['ram_gb']} GB` · `{report['os']}` (`{report['arch']}`)",
        f"- **Self-test:** `{report['self_test']}`",
        "",
        "### Rust Services",
    ]
    for svc, st in (report.get("services") or {}).items():
        mark = "UP" if st == "UP" else "offline"
        lines.append(f"- `{svc}`: **{mark}**")

    failed = report.get("failed_probes") or []
    if failed:
        lines.extend(["", "### Failed Self-Test Probes"])
        for item in failed:
            lines.append(f"- **{item['name']}**: `{item['detail']}`")

    lines.extend(
        [
            "",
            "- [x] Generated via `sanctum bug-report` (secrets, tokens, emails, usernames, and private IPs redacted)",
        ]
    )
    return redact_text("\n".join(lines))


def build_issue_url(*, title: str, body: str) -> str:
    params = urllib.parse.urlencode(
        {
            "title": redact_text(title or "[beta] Bug report"),
            "body": body[:3500],
            "labels": "beta",
        }
    )
    return f"{ISSUES_NEW_URL}?{params}"


def bug_report_command(
    summary: Annotated[
        str,
        typer.Option("--summary", "-m", help="Short description of what broke."),
    ] = "",
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Print the report and URL without opening a browser."),
    ] = False,
    no_copy: Annotated[
        bool,
        typer.Option("--no-copy", help="Do not copy the Markdown report to the clipboard."),
    ] = False,
    skip_self_test: Annotated[
        bool,
        typer.Option("--skip-self-test", help="Skip running `sanctum self-test` (faster)."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the redacted diagnostic snapshot as JSON."),
    ] = False,
) -> None:
    """Generate a secret-redacted beta diagnostic report and open a GitHub issue."""
    report = collect_redacted_report(summary=summary, run_self_test=not skip_self_test)
    if json_output:
        print(json.dumps(report, indent=2))
        return

    md = render_markdown_report(report)
    url = build_issue_url(
        title=f"[beta v{__version__}] {summary[:70]}" if summary else f"[beta v{__version__}] Issue report",
        body=md,
    )

    copied = False
    if not no_copy and shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=md, text=True, check=True, timeout=3)
            copied = True
        except Exception:
            copied = False

    console.print(Panel(md, title="Sanctum Redacted Bug Report", border_style="cyan"))
    if copied:
        console.print("[green]✓[/] Copied redacted report to clipboard.")
    console.print(f"[dim]Issue URL:[/] {url[:140]}{'…' if len(url) > 140 else ''}")

    if not no_open:
        opened = webbrowser.open(url)
        if opened:
            console.print("[green]✓[/] Opened GitHub issue form in your browser.")
        else:
            console.print("[yellow]Could not open browser automatically — paste the URL above.[/]")
