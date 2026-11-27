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
import html
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


def seal_dir() -> Path:
    override = os.environ.get(ENV_HIVE_STATE, "").strip()
    if override:
        return Path(override).expanduser().parent / "seals"
    return Path.home() / ".sanctum" / "seals"


def seal_path() -> Path:
    return seal_dir() / "sovereign-seal.json"


def seal_svg_path() -> Path:
    return seal_dir() / "sovereign-seal.svg"


def load_seal() -> dict[str, Any] | None:
    path = seal_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("tier") else None
    except (OSError, json.JSONDecodeError):
        return None


def render_seal_svg(
    seal: dict[str, Any],
    *,
    chip: str = "",
    ram_gb: int = 0,
    country: str = "",
) -> str:
    """Render a zero-dependency Obsidian & Gold Sovereign Seal SVG locally."""
    title = html.escape(str(seal.get("title") or "Sovereign Node"))
    badge = html.escape(str(seal.get("short_badge") or "SANCTUM HIVE"))
    motto = html.escape(str(seal.get("motto") or "Local silicon, zero cloud leash."))
    minted = html.escape(str(seal.get("minted_at") or "")[:10] or "2026")
    pub_hash = html.escape(str(seal.get("public_hash") or "0000000000000000")[:16].upper())
    tier = str(seal.get("tier") or "sovereign")
    accent = "#f5c451" if tier in ("seed-1", "founding-circle", "seed-0") else "#67e8f9"
    hw_parts = [p for p in [chip, f"{ram_gb} GB" if ram_gb else "", country if country and country != "ZZ" else ""] if p]
    hw_line = html.escape(" · ".join(hw_parts) or "Apple Silicon Sovereign Node")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="640" height="360" role="img" '
        f'aria-label="Sanctum Sovereign Seal — {title}">\n'
        "  <defs>\n"
        '    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">\n'
        '      <stop offset="0%" stop-color="#0b0d14"/>\n'
        '      <stop offset="100%" stop-color="#151926"/>\n'
        "    </linearGradient>\n"
        '    <linearGradient id="gold" x1="0" y1="0" x2="1" y2="1">\n'
        f'      <stop offset="0%" stop-color="{accent}"/>\n'
        '      <stop offset="100%" stop-color="#d4952e"/>\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        '  <rect width="640" height="360" rx="18" fill="url(#bg)"/>\n'
        '  <rect x="14" y="14" width="612" height="332" rx="12" fill="none" stroke="url(#gold)" stroke-width="1.5" opacity="0.72"/>\n'
        '  <rect x="22" y="22" width="596" height="316" rx="8" fill="none" stroke="#ffffff" stroke-width="0.6" opacity="0.14"/>\n'
        '  <g transform="translate(320 90)">\n'
        '    <polygon points="0,-38 30,0 0,38 -30,0" fill="none" stroke="url(#gold)" stroke-width="2.5"/>\n'
        '    <polygon points="0,-20 16,0 0,20 -16,0" fill="url(#gold)" opacity="0.9"/>\n'
        "  </g>\n"
        f'  <text x="320" y="156" text-anchor="middle" fill="{accent}" font-family="IBM Plex Mono,ui-monospace,SFMono-Regular,Menlo,monospace" font-size="13" font-weight="600" letter-spacing="3.5">{badge} · SANCTUM HIVE</text>\n'
        f'  <text x="320" y="194" text-anchor="middle" fill="#f9fafb" font-family="Bricolage Grotesque,-apple-system,BlinkMacSystemFont,sans-serif" font-size="25" font-weight="700">{title}</text>\n'
        f'  <text x="320" y="224" text-anchor="middle" fill="#9ca3af" font-family="Instrument Sans,-apple-system,BlinkMacSystemFont,sans-serif" font-size="13">{motto}</text>\n'
        '  <line x1="88" y1="252" x2="552" y2="252" stroke="#ffffff" stroke-opacity="0.12" stroke-width="1"/>\n'
        f'  <text x="320" y="282" text-anchor="middle" fill="#e5e7eb" font-family="IBM Plex Mono,ui-monospace,SFMono-Regular,Menlo,monospace" font-size="12">{hw_line}</text>\n'
        f'  <text x="320" y="308" text-anchor="middle" fill="#9ca3af" font-family="IBM Plex Mono,ui-monospace,SFMono-Regular,Menlo,monospace" font-size="11">MINTED {minted}  ·  SEAL HASH {pub_hash}  ·  ZERO PII</text>\n'
        '  <text x="320" y="330" text-anchor="middle" fill="#6b7280" font-family="IBM Plex Mono,ui-monospace,SFMono-Regular,Menlo,monospace" font-size="10" letter-spacing="2">LOCAL SILICON · ZERO CLOUD LEASH · SANCTUM.RUN</text>\n'
        "</svg>\n"
    )


def save_seal(
    seal: dict[str, Any],
    *,
    chip: str = "",
    ram_gb: int = 0,
    country: str = "",
) -> tuple[Path, Path]:
    """Persist the Sovereign Seal JSON (0600) and local SVG crest atomically."""
    sdir = seal_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    enriched = dict(seal)
    if chip:
        enriched["chip"] = chip
    if ram_gb:
        enriched["ram_gb"] = ram_gb
    if country:
        enriched["country"] = country

    jpath = seal_path()
    tmp = jpath.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(enriched, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(jpath)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise

    svg = render_seal_svg(
        enriched,
        chip=str(enriched.get("chip") or ""),
        ram_gb=int(enriched.get("ram_gb") or 0),
        country=str(enriched.get("country") or ""),
    )
    spath = seal_svg_path()
    spath.write_text(svg, encoding="utf-8")
    return jpath, spath


def verify_seal_remote(
    claim_code: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Verify any Sovereign Seal claim code via GET /verify-seal (zero PII)."""
    endpoint = _endpoint()
    with httpx.Client(timeout=ANALYTICS_TIMEOUT_S, transport=transport) as client:
        r = client.get(f"{endpoint}/verify-seal", params={"code": claim_code.strip().upper()})
        data = r.json()
        return data if isinstance(data, dict) else {"ok": False, "valid": False}


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
                seal = resp_data.get("seal")
                if isinstance(seal, dict) and seal.get("tier"):
                    with contextlib.suppress(OSError):
                        jpath, spath = save_seal(
                            seal,
                            chip=str(payload.get("chip") or ""),
                            ram_gb=int(payload.get("memory_gb") or 0),
                            country=str(payload.get("country") or ""),
                        )
                        resp_data["seal_path"] = str(jpath)
                        resp_data["seal_svg_path"] = str(spath)
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
                with contextlib.suppress(Exception):
                    seal = r.json().get("seal")
                    if isinstance(seal, dict) and seal.get("tier"):
                        save_seal(
                            seal,
                            chip=str(payload.get("chip") or ""),
                            ram_gb=int(payload.get("memory_gb") or 0),
                            country=str(payload.get("country") or ""),
                        )
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
