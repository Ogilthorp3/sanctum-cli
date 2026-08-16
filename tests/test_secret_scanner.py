"""Regression suite for secret_scanner — every pattern must catch its
canonical example, and clean files must NOT trigger any."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli import secret_scanner

if TYPE_CHECKING:
    from pathlib import Path


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


def test_clean_file_no_findings(tmp_path: Path) -> None:
    f = _write(tmp_path / "zshrc", "export PATH=/opt/homebrew/bin:$PATH\nalias ll='ls -la'\n")
    assert secret_scanner.scan_path(f) == []


def test_anthropic_key_caught(tmp_path: Path) -> None:
    body = "ANTHROPIC_API_KEY=sk-ant-api03-" + "x" * 80 + "\n"
    f = _write(tmp_path / "config.txt", body)
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "anthropic-api-key" for f in findings)


def test_openai_key_caught(tmp_path: Path) -> None:
    body = "OPENAI_API_KEY=sk-proj-" + "y" * 60 + "\n"
    f = _write(tmp_path / "config.txt", body)
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "openai-api-key" for f in findings)


def test_github_token_caught(tmp_path: Path) -> None:
    body = "token: ghp_" + "Z" * 40 + "\n"
    f = _write(tmp_path / "gh.yml", body)
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "github-token" for f in findings)


def test_google_oauth_secret_caught(tmp_path: Path) -> None:
    body = "client_secret = GOCSPX-" + "k" * 30 + "\n"
    f = _write(tmp_path / "rclone.conf", body)
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "google-oauth-client-secret" for f in findings)


def test_aws_access_key_caught(tmp_path: Path) -> None:
    body = "AWS_ACCESS_KEY_ID=AKIA" + "B" * 16 + "\n"
    f = _write(tmp_path / "env", body)
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "aws-access-key-id" for f in findings)


def test_ssh_private_key_caught(tmp_path: Path) -> None:
    body = "-----BEGIN OPENSSH PRIVATE KEY-----\njunk\n-----END OPENSSH PRIVATE KEY-----\n"
    f = _write(tmp_path / "id_test", body)
    findings = secret_scanner.scan_path(f)
    # Filename pattern (id_*) AND content pattern should both fire here.
    patterns = {f.pattern for f in findings}
    assert "ssh-private-key" in patterns


def test_filename_id_rsa_caught_even_empty(tmp_path: Path) -> None:
    f = _write(tmp_path / "id_rsa", "")  # empty content; filename alone trips
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "private-key-by-name" for f in findings)


def test_filename_env_caught(tmp_path: Path) -> None:
    f = _write(tmp_path / ".env", "PORT=8080\n")  # value harmless; filename trips
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "env-file" for f in findings)


def test_pem_file_filename_caught(tmp_path: Path) -> None:
    f = _write(tmp_path / "cert.pem", "-----BEGIN CERTIFICATE-----\n")
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "pem-file" for f in findings)


def test_rclone_conf_filename_caught(tmp_path: Path) -> None:
    f = _write(tmp_path / "rclone.conf", "[remote]\ntype = s3\n")
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "rclone-conf" for f in findings)


def test_b2_application_key_caught(tmp_path: Path) -> None:
    body = "B2_APP_KEY=K003" + "a" * 50 + "\n"
    f = _write(tmp_path / "b2", body)
    findings = secret_scanner.scan_path(f)
    assert any(f.pattern == "b2-application-key" for f in findings)


def test_snippet_truncates_long_secrets(tmp_path: Path) -> None:
    body = "key=sk-ant-api03-" + "z" * 200 + "\n"
    f = _write(tmp_path / "long.txt", body)
    findings = secret_scanner.scan_path(f)
    assert findings
    # Snippet should be redacted to keep us from echoing the whole secret in logs.
    for found in findings:
        assert "..." in found.snippet or len(found.snippet) <= 24


def test_scan_paths_aggregates(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.txt", "AKIA" + "C" * 16)
    b = _write(tmp_path / "b.txt", "ghp_" + "D" * 40)
    c = _write(tmp_path / "c.txt", "clean as a whistle")
    findings = secret_scanner.scan_paths([a, b, c])
    paths_with_findings = {f.path for f in findings}
    assert a in paths_with_findings
    assert b in paths_with_findings
    assert c not in paths_with_findings


def test_empty_file_is_silently_clean(tmp_path: Path) -> None:
    f = _write(tmp_path / "empty.txt", "")
    assert secret_scanner.scan_path(f) == []


def test_nonexistent_path_returns_empty(tmp_path: Path) -> None:
    assert secret_scanner.scan_path(tmp_path / "nope") == []
