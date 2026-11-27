"""Sovereign, opt-in Hive Pulse + lightweight version check.

Privacy contract (auditable at https://github.com/Ogilthorp3/sanctum-docs/tree/main/workers/hive-pulse):
  - Node heartbeats are STRICTLY OPT-IN (off until ``sanctum hive join`` or explicit
    opt-in during ``sanctum onboard``).
  - Sends only anonymous hardware/macro attributes: locally generated hex ``node_id``,
    Apple Silicon chip family, RAM GB, macOS version, 2-letter locale country code,
    CLI version, and active module slugs.
  - NEVER sends IP addresses (the Worker discards client IPs), hostnames, usernames,
    file paths, or prompt contents.
  - Instant erasure anytime via ``sanctum hive leave`` (``DELETE /pulse/:node_id``).
  - Lightweight daily version check (``GET /latest.json?v=<version>&os=macos``) sends
    ZERO unique identifier and is disabled cleanly via ``SANCTUM_NO_UPDATE_CHECK=1``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from sanctum_cli import __version__, config
from sanctum_cli.mesh.metrics import detect_chip, detect_country, detect_os, detect_ram_gb

DEFAULT_HIVE_ENDPOINT = "https://hive.sanctum.run"
ENV_HIVE_URL = "SANCTUM_HIVE_PULSE_URL"
ENV_HIVE_STATE = "SANCTUM_HIVE_PULSE_STATE"
ENV_NO_UPDATE_CHECK = "SANCTUM_NO_UPDATE_CHECK"

PULSE_TIMEOUT_S = 2.0
UPDATE_CHECK_TIMEOUT_S = 1.5
ANALYTICS_TIMEOUT_S = 6.0


def _state_path() -> Path:
    override = os.environ.get(ENV_HIVE_STATE, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".sanctum" / "state" / "hive-pulse.json"


def _endpoint() -> str:
    override = os.environ.get(ENV_HIVE_URL, "").strip().rstrip("/")
    return override or DEFAULT_HIVE_ENDPOINT


def _in_pytest_without_override() -> bool:
    """Prevent unrelated unit tests from hitting the real network."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get(ENV_HIVE_URL)


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _derive_or_create_node_id(state: dict[str, Any]) -> str:
    existing = state.get("node_id")
    if isinstance(existing, str) and len(existing) >= 16:
        return existing

    # Reuse existing anonymous mesh Ed25519 pubkey hash if present (zero hardware/PII derivation)
    pub_path = Path.home() / ".sanctum" / "mesh" / "identity" / "ed25519.pub"
    if pub_path.is_file():
        with contextlib.suppress(OSError):
            raw = pub_path.read_bytes().strip()
            if raw:
                return hashlib.sha256(b"sanctum-hive-pulse:" + raw).hexdigest()[:32]

    return secrets.token_hex(16)


def detect_active_modules() -> list[str]:
    """Return non-identifying active module slugs present on this host."""
    modules: list[str] = []
    if config.instance_path().is_file():
        with contextlib.suppress(Exception):
            cfg = config.load()
            if cfg.cli.cloud_backup and cfg.cli.cloud_backup.primary:
                modules.append("backup")
    # Check well-known local Sanctum capabilities without reading any secrets/paths
    sanctum_dir = Path.home() / ".sanctum"
    if (sanctum_dir / "sanctum-proxy" / "config.yaml").is_file():
        modules.append("proxyd")
    if (sanctum_dir / "bin" / "sanctum-mlx").is_file():
        modules.append("mlx")
    if (sanctum_dir / "state" / "rust-suite.json").is_file():
        modules.append("rust-suite")
    if (sanctum_dir / "dragon" / "manifest.json").is_file():
        modules.append("dragon")
    if (
        (sanctum_dir / "state" / "yoda-signal.json").is_file()
        or (sanctum_dir / "launchagents" / "live" / "haus.sanctum.signal-cli.plist").is_file()
    ):
        modules.append("yoda-signal")
    if (sanctum_dir / "services" / "home-assistant.yaml").is_file():
        modules.append("home-assistant")
    if (sanctum_dir / "memory").is_dir():
        modules.append("memory-vault")
    return sorted(set(modules))


def build_pulse_payload(
    *,
    node_id: str,
    event: str = "heartbeat",
    onboard_completed: bool = False,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "event": event,
        "onboard_completed": onboard_completed or event == "onboard",
        "chip": detect_chip(),
        "memory_gb": detect_ram_gb(),
        "os_version": detect_os(),
        "country": detect_country(),
        "cli_version": __version__,
        "modules": detect_active_modules(),
    }


def _hours_since(iso_ts: str | None, now: datetime) -> float:
    if not iso_ts:
        return 1e9
    try:
        parsed = datetime.fromisoformat(iso_ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return (now - parsed).total_seconds() / 3600.0
    except ValueError:
        return 1e9


def join_hive(
    *,
    event: str = "join",
    onboard_completed: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Opt into the anonymous Sanctum Hive counter and send a pulse immediately."""
    state = load_state()
    node_id = _derive_or_create_node_id(state)
    endpoint = _endpoint()
    payload = build_pulse_payload(
        node_id=node_id,
        event=event,
        onboard_completed=onboard_completed or bool(state.get("onboard_completed")),
    )
    now_iso = datetime.now(UTC).isoformat()

    state.update(
        {
            "enabled": True,
            "node_id": node_id,
            "endpoint": endpoint,
            "onboard_completed": bool(payload["onboard_completed"]),
        }
    )

    resp_data: dict[str, Any] = {"ok": False}
    try:
        with httpx.Client(timeout=PULSE_TIMEOUT_S, transport=transport) as client:
            r = client.post(f"{endpoint}/pulse", json=payload)
            if r.status_code == 200:
                resp_data = r.json()
                state["last_pulse_iso"] = now_iso
            else:
                resp_data = {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as exc:
        resp_data = {"ok": False, "error": str(exc)}

    save_state(state)
    return {**resp_data, "payload": payload, "endpoint": endpoint}


def leave_hive(*, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Opt out and erase this node's anonymous record from hive.sanctum.run."""
    state = load_state()
    node_id = state.get("node_id")
    endpoint = str(state.get("endpoint") or _endpoint()).rstrip("/")
    removed = False
    error: str | None = None

    if isinstance(node_id, str) and len(node_id) >= 16:
        try:
            with httpx.Client(timeout=PULSE_TIMEOUT_S, transport=transport) as client:
                r = client.delete(f"{endpoint}/pulse/{node_id}")
                if r.status_code == 200:
                    removed = bool(r.json().get("removed", False))
                else:
                    error = f"HTTP {r.status_code}"
        except Exception as exc:
            error = str(exc)

    state["enabled"] = False
    state["left_at_iso"] = datetime.now(UTC).isoformat()
    save_state(state)
    return {"ok": error is None, "removed": removed, "error": error, "endpoint": endpoint}


def maybe_heartbeat(
    event: str = "heartbeat",
    *,
    min_interval_hours: float = 24.0,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Best-effort opt-in heartbeat (max 1x/24h, strictly NO-OP if not opted in)."""
    if transport is None and _in_pytest_without_override():
        return False

    state = load_state()
    if not state.get("enabled"):
        return False

    now = datetime.now(UTC)
    if _hours_since(state.get("last_pulse_iso"), now) < min_interval_hours:
        return False

    node_id = _derive_or_create_node_id(state)
    endpoint = str(state.get("endpoint") or _endpoint()).rstrip("/")
    payload = build_pulse_payload(
        node_id=node_id,
        event=event,
        onboard_completed=bool(state.get("onboard_completed")),
    )
    try:
        with httpx.Client(timeout=PULSE_TIMEOUT_S, transport=transport) as client:
            r = client.post(f"{endpoint}/pulse", json=payload)
            if r.status_code == 200:
                state["node_id"] = node_id
                state["last_pulse_iso"] = now.isoformat()
                save_state(state)
                return True
    except Exception:
        return False
    return False


def _parse_semver(v: str) -> tuple[int, ...]:
    cleaned = v.strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for p in cleaned.split("."):
        if p.isdigit():
            parts.append(int(p))
        else:
            break
    return tuple(parts) if parts else (0,)


def is_newer_version(latest: str, current: str) -> bool:
    if not latest or not current or "+" in current:
        return False
    return _parse_semver(latest) > _parse_semver(current)


def maybe_check_update(
    *,
    current_version: str | None = None,
    min_interval_hours: float = 24.0,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Lightweight zero-ID daily update check (opt-out via SANCTUM_NO_UPDATE_CHECK=1)."""
    if os.environ.get(ENV_NO_UPDATE_CHECK, "").strip().lower() in ("1", "true", "yes"):
        return None
    if transport is None and _in_pytest_without_override():
        return None

    cur = current_version or __version__
    state = load_state()
    now = datetime.now(UTC)

    cached = state.get("cached_latest_version")
    if _hours_since(state.get("last_update_check_iso"), now) < min_interval_hours:
        if isinstance(cached, str) and is_newer_version(cached, cur):
            return cached
        return None

    endpoint = _endpoint()
    os_family = "macos" if sys.platform == "darwin" else platform.system().lower() or "unknown"
    try:
        with httpx.Client(timeout=UPDATE_CHECK_TIMEOUT_S, transport=transport) as client:
            r = client.get(
                f"{endpoint}/latest.json",
                params={"v": cur, "os": os_family},
            )
            if r.status_code == 200:
                data = r.json()
                latest = str(data.get("latest_version") or "").strip()
                dragon_tag = str(data.get("dragon_tag") or "").strip()
                rust_tag = str(data.get("rust_tag") or "").strip()
                if latest:
                    state["last_update_check_iso"] = now.isoformat()
                    state["cached_latest_version"] = latest
                    if dragon_tag:
                        state["cached_dragon_tag"] = dragon_tag
                    if rust_tag:
                        state["cached_rust_tag"] = rust_tag
                    with contextlib.suppress(OSError):
                        save_state(state)
                    if is_newer_version(latest, cur):
                        return latest
    except Exception:
        # Never delay or fail user commands when offline; backoff 1h on failure
        state["last_update_check_iso"] = (
            now - timedelta(hours=max(0.0, min_interval_hours - 1.0))
        ).isoformat()
        with contextlib.suppress(OSError):
            save_state(state)
    return None


def fetch_hive_analytics(*, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Fetch public aggregate Hive statistics from https://hive.sanctum.run/analytics."""
    endpoint = _endpoint()
    with httpx.Client(timeout=ANALYTICS_TIMEOUT_S, transport=transport) as client:
        r = client.get(f"{endpoint}/analytics")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}
