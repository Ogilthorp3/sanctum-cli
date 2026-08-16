"""``sanctum deadman`` — off-box backup dead-man's-switch heartbeat.

On a successful backup or canary drill, ``sanctum deadman beat <check>`` writes
a heartbeat into the ``sanctum-backup-deadman`` GitHub repo's
``heartbeats.json`` via the **GitHub Contents API** (using the host's ``gh``
token — no git clone, no ssh agent, so it works from a background LaunchAgent).
A scheduled GitHub Action in that repo opens an issue — which emails the owner —
the moment a heartbeat goes overdue, and auto-closes it on recovery.

Why GitHub and not the Mac: every on-box guard (the backup's Force Flow alert,
the weekly canary) dies with the host/launchd/Force-Flow. The Action runs on
GitHub's infrastructure, so the *absence* of a fresh heartbeat is an alarm
nothing on the Mac can silence. No R2, no Cloudflare Worker, no extra secrets —
it reuses the ``gh`` auth the host already has.
"""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import time

from sanctum_cli import config
from sanctum_cli.errors import UserError

DEADMAN_REPO_NAME = "sanctum-backup-deadman"  # the bare repo name; owner is resolved


def _gh_login() -> str | None:
    """The authenticated GitHub login via ``gh api user``, or ``None``."""
    if not shutil.which("gh"):
        return None
    proc = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return None
    login = proc.stdout.strip()
    return login or None


def default_repo() -> str:
    """Deadman heartbeat repo — no baked-in personal account.

    Resolution order (discovery-first):
      1. instance.yaml ``vcs.deadman_repo`` (explicit operator config);
      2. ``<gh-login>/sanctum-backup-deadman`` from the authenticated ``gh`` user;
      3. raise :class:`UserError` — there is no personal fallback to ship.
    """
    configured = config.instance_value("vcs.deadman_repo", None)
    if configured:
        return str(configured)
    login = _gh_login()
    if login:
        return f"{login}/{DEADMAN_REPO_NAME}"
    msg = "could not resolve the deadman heartbeat repo"
    raise UserError(
        msg,
        fix="set vcs.deadman_repo in ~/.sanctum/instance.yaml (or run `gh auth login`)",
    )


DEFAULT_PATH = "heartbeats.json"
DEFAULT_MAX_HOURS: dict[str, int] = {"backup-fresh": 26, "restore-drill": 192}
GH_TIMEOUT_S = 30
MAX_RETRIES = 3
_AUTH_FIX = "run `gh auth status`; the token needs repo write access to the deadman repo"


def host_slug() -> str:
    """Short hostname (no domain), lowercased — the heartbeat key prefix."""
    return socket.gethostname().split(".", 1)[0].lower()


def merge_heartbeat(
    data: dict[str, object], key: str, *, ts: int, max_hours: int
) -> dict[str, object]:
    """Pure: return a copy of ``data`` with ``key`` set to {ts, max_hours}.

    Kept free of IO so it is trivially unit-testable.
    """
    out = dict(data)
    out[key] = {"ts": ts, "max_hours": max_hours}
    return out


def _gh_api(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )


def _last_line(text: str) -> str:
    return (text.strip().splitlines() or ["?"])[-1]


def _read_remote(repo: str, path: str) -> tuple[str | None, dict[str, object]]:
    """Return ``(sha, data)`` for the heartbeats file; ``(None, {})`` if absent."""
    get = _gh_api(f"repos/{repo}/contents/{path}")
    if get.returncode != 0:
        if "Not Found" in get.stderr or "404" in get.stderr:
            return None, {}
        raise UserError(f"deadman read failed: {_last_line(get.stderr)}", fix=_AUTH_FIX)
    meta = json.loads(get.stdout)
    raw = base64.b64decode(str(meta.get("content", ""))).decode("utf-8")
    parsed = json.loads(raw) if raw.strip() else {}
    return str(meta["sha"]), (parsed if isinstance(parsed, dict) else {})


def beat(
    check: str,
    *,
    max_hours: int | None = None,
    repo: str | None = None,
    path: str = DEFAULT_PATH,
) -> str:
    """Record a success heartbeat for ``check`` and push it off-box via ``gh``.

    Returns the heartbeat key (``<host>:<check>``). Raises on failure so a
    backup wrapper can surface it — a silently-dropped heartbeat would make the
    off-box Action false-alarm.
    """
    repo = repo or default_repo()
    mh = max_hours if max_hours is not None else DEFAULT_MAX_HOURS.get(check, 26)
    key = f"{host_slug()}:{check}"
    now = int(time.time())

    for _ in range(MAX_RETRIES):
        sha, data = _read_remote(repo, path)
        data = merge_heartbeat(data, key, ts=now, max_hours=mh)
        payload = base64.b64encode(
            (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        ).decode()
        put_args = [
            "-X",
            "PUT",
            f"repos/{repo}/contents/{path}",
            "-f",
            f"message=beat: {key}",
            "-f",
            f"content={payload}",
        ]
        if sha is not None:
            put_args += ["-f", f"sha={sha}"]
        put = _gh_api(*put_args)
        if put.returncode == 0:
            return key
        stderr = put.stderr.lower()
        if "does not match" in stderr or "409" in stderr or "conflict" in stderr:
            continue  # another host beat between our read and write — re-read + retry
        raise UserError(f"deadman write failed: {_last_line(put.stderr)}", fix=_AUTH_FIX)
    raise UserError("deadman write failed after repeated sha-conflict retries", fix=_AUTH_FIX)
