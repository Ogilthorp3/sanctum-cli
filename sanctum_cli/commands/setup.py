"""``sanctum setup`` — the Apple-way first-run Setup Assistant (local web wizard).

The **geeky door** is ``brew install … && sanctum onboard`` in a terminal. This is the
**front door**: a double-click / one-command GUI that walks *anyone* — a non-technical
family member included — through the whole first run, one calm step at a time, with a
live ✓ as each piece comes up. It is a thin, honest face over the *same functions the
CLI already uses*:

* **Read-only status** → the existing probe functions, called **in-process** (no
  subprocess, no PATH/version skew, no interactive-prompt hangs).
* **Mutations that need input** (name, OAuth creds, provider keys) → collected in the
  web form, then handed to the same seams ``onboard`` calls (verify-before-store kept).
* **Steps only a human can do** (Tailscale SSO, ``sudo …``, an OS toggle) → shown as the
  exact action + a deep-link, then the probe is re-polled and the ✓ flips when it's done.
  Never faked.

The server binds ``127.0.0.1`` only. The user types their own secrets into the local
form; this module hands them straight to the verify-before-store seams and never logs
or echoes them. Everything is stdlib (``http.server``) — no web framework is a dep.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rich.console import Console

from sanctum_cli import config, keychain
from sanctum_cli.commands.setup_page import PAGE

console = Console()

# ── constants ────────────────────────────────────────────────────────────────

_KC_ACCOUNT = "sanctum"
_KC_OAUTH_ID = "tailscale-oauth-client-id"
_KC_OAUTH_SECRET = "tailscale-oauth-secret"
_TS_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
_PEER = "berts-mbp"

_OAUTH_URL = "https://login.tailscale.com/admin/settings/oauth"
# The single literal Settings deep-link anchor in the codebase (sanctum-grant-tcc.sh).
_FDA_ANCHOR = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
_AUTOMATION_ANCHOR = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"

# node identity that holds the 13 haus TCC grants (self_test.probe_tcc_grants).
_TCC_CLIENT = "/usr/local/bin/node"
_TCC_REQUIRED = 13

_APP_PATH = Path.home() / "Applications" / "Sanctum Setup.app"

# The one shape a device identifier may take in devices.yaml (screen-time matches on it).
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


# ── read-only probes (called in-process) ──────────────────────────────────────


def _tier() -> str:
    """'haus' (the Mini, full stack) or 'basic' (a friend/family CLI install)."""
    try:
        from sanctum_cli.commands.self_test import _haus_tier_installed

        return "haus" if _haus_tier_installed() else "basic"
    except Exception:
        return "basic"


def _sanctum_bin() -> str:
    """The ``sanctum`` binary in the SAME venv as this interpreter (no PATH skew) —
    the editable copy that actually has this ``setup`` command."""
    sibling = Path(sys.executable).with_name("sanctum")
    if sibling.exists():
        return str(sibling)
    return shutil.which("sanctum") or "sanctum"


def _version() -> str:
    from sanctum_cli import __version__

    return __version__


def _instance_name() -> str | None:
    # instance_value never raises — safe on an unconfigured box.
    value = config.instance_value("instance.name", default=None)
    return str(value) if value else None


def _tailscale_installed() -> bool:
    return bool(shutil.which("tailscale")) or _TS_APP.exists()


def _oauth_stored() -> bool:
    return keychain.exists(_KC_ACCOUNT, _KC_OAUTH_ID) and keychain.exists(_KC_ACCOUNT, _KC_OAUTH_SECRET)


def _tcc_grant_count() -> int | None:
    """Count node's granted TCC rows (read-only). Returns None if TCC.db can't be
    read — usually because *this* process lacks Full Disk Access (the "need FDA to
    grant FDA" bootstrap), which the pane surfaces honestly rather than as a false ✗."""
    db = Path.home() / "Library/Application Support/com.apple.TCC/TCC.db"
    if not db.exists():
        return None
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT COUNT(*) FROM access WHERE client=? AND auth_value=2", (_TCC_CLIENT,)
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return None


def _restic_installed() -> bool:
    return bool(shutil.which("restic"))


def _backup_state() -> dict[str, Any]:
    """Cheap, never-raising summary of the configured backup surface.

    Reads through :func:`config.instance_value` (raw YAML walk, safe on an
    unconfigured box) rather than :func:`config.load` so a half-written
    instance.yaml still renders a truthful pane instead of a stack trace.
    Repo *pointers* only — never credentials (those live in the keychain)."""
    from sanctum_cli import recipes

    raw_recipes = config.instance_value("cli.recipes")
    known = set(recipes.BUILTINS) | (set(raw_recipes) if isinstance(raw_recipes, dict) else set())
    retention = config.instance_value("cli.cloud_backup.retention")
    if not isinstance(retention, dict):
        retention = {}
    repo = config.instance_value("cli.cloud_backup.primary.repo")
    secondary = config.instance_value("cli.cloud_backup.secondary.repo")
    default_recipe = config.instance_value("cli.default_recipe")
    return {
        "repo": str(repo) if repo else None,
        "secondary": str(secondary) if secondary else None,
        "recipes": sorted(known),
        "default_recipe": str(default_recipe) if default_recipe else None,
        "keep_daily": int(retention.get("keep_daily", 7)),
        "keep_weekly": int(retention.get("keep_weekly", 4)),
        "keep_monthly": int(retention.get("keep_monthly", 12)),
    }


def _devices_file() -> Path | None:
    """The devices.yaml screen-time reads (env override honored), or None when absent."""
    from sanctum_cli.commands import screen_time

    override = os.environ.get(screen_time.ENV_DEVICES_FILE)
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    for candidate in screen_time._DEVICES_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _family_members() -> list[dict[str, Any]]:
    """Never-raising read of the family block (members + personal devices)."""
    path = _devices_file()
    if path is None:
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("family"), dict):
        return []
    members: list[dict[str, Any]] = []
    for pid, person in data["family"].items():
        if not isinstance(person, dict):
            continue
        raw_devices = person.get("personal_devices")
        devices = (
            [
                {"name": str(d.get("name", "")), "mac": str(d.get("mac", ""))}
                for d in raw_devices
                if isinstance(d, dict)
            ]
            if isinstance(raw_devices, list)
            else []
        )
        members.append(
            {
                "id": str(pid),
                "name": str(person.get("name", pid)),
                "role": str(person.get("role", "")) or "child",
                "devices": devices,
            }
        )
    return members


def _firewalla_bridge_url() -> str:
    from sanctum_cli.devices import firewalla

    return firewalla._bridge_url()


def _firewalla_token_stored() -> bool:
    """Presence only — the token value never leaves the resolver."""
    from sanctum_cli.devices import firewalla

    return firewalla._read_bridge_token() is not None


def _firewalla_paired() -> bool:
    return bool(config.instance_value("services.firewalla_bridge.enabled"))


def _firewalla_device() -> tuple[str, str]:
    """The recorded (device_ip, device_mac) pair, '' when unknown — for prefill."""
    ip = config.instance_value("services.firewalla_bridge.device_ip")
    mac = config.instance_value("services.firewalla_bridge.device_mac")
    return (str(ip) if ip else "", str(mac) if mac else "")


def _ha_green_url() -> str:
    from sanctum_cli.devices import ha_green

    return ha_green._ha_url()


def _ha_token_stored() -> bool:
    """Presence only — the token value never leaves the resolver."""
    from sanctum_cli.devices import ha_green

    return ha_green._read_ha_token() is not None


def _ha_paired() -> bool:
    return bool(config.instance_value("services.ha_green.enabled"))


def gather_state() -> dict[str, Any]:
    """A cheap, never-raising snapshot for ``GET /state``. Heavy/network probes
    (tailnet health, self-test) are fetched on demand via ``/probe/<id>``."""
    tier = _tier()
    name = _instance_name()
    steps: dict[str, dict[str, Any]] = {
        "cli": {"status": "ok", "detail": f"sanctum {_version()}"},
        "instance": (
            {"status": "ok", "detail": f"set up as “{name}”", "name": name}
            if name
            else {"status": "todo", "detail": "not named yet", "name": None}
        ),
        "tailscale_installed": (
            {"status": "ok", "detail": "Tailscale is installed"}
            if _tailscale_installed()
            else {"status": "todo", "detail": "Tailscale not found — install it below"}
        ),
        "oauth": (
            {"status": "ok", "detail": "credential stored in your keychain"}
            if _oauth_stored()
            else {"status": "todo", "detail": "no Tailscale credential yet"}
        ),
    }

    backup = _backup_state()
    restic = _restic_installed()
    if backup["repo"] and restic:
        bstep = {"status": "ok", "detail": f"restic → {backup['repo']}"}
    elif backup["repo"]:
        bstep = {"status": "attention", "detail": "destination set, but restic isn't installed"}
    elif restic:
        bstep = {"status": "todo", "detail": "restic is ready — pick a destination"}
    else:
        bstep = {"status": "todo", "detail": "no backups yet — install restic below"}
    steps["backup"] = {**backup, **bstep, "restic": restic}

    # Topology is a real (seconds-long) scan — always on-demand via /probe/network.
    steps["network"] = {
        "status": "unknown",
        "detail": "not scanned yet — run the scan to map your network",
    }

    if tier == "haus":
        members = _family_members()
        device_count = sum(len(m["devices"]) for m in members)
        steps["family"] = (
            {
                "status": "ok",
                "detail": f"{len(members)} member(s), {device_count} device(s) on file",
                "members": members,
            }
            if members
            else {"status": "todo", "detail": "no family on file yet", "members": []}
        )

        fw_ip, fw_mac = _firewalla_device()
        if _firewalla_paired() and _firewalla_token_stored():
            fwstep = {"status": "ok", "detail": "bridge paired — token on this Mac"}
        elif _firewalla_token_stored():
            fwstep = {"status": "attention", "detail": "token stored, but pairing not recorded yet"}
        else:
            fwstep = {"status": "todo", "detail": "not paired yet"}
        steps["firewalla"] = {
            **fwstep,
            "url": _firewalla_bridge_url(),
            "token_stored": _firewalla_token_stored(),
            "device_ip": fw_ip,
            "device_mac": fw_mac,
        }

        if _ha_paired() and _ha_token_stored():
            hastep = {"status": "ok", "detail": "connected — owner token on this Mac"}
        elif _ha_token_stored():
            hastep = {"status": "attention", "detail": "token stored, but pairing not recorded yet"}
        else:
            hastep = {"status": "todo", "detail": "not connected yet"}
        steps["ha"] = {
            **hastep,
            "url": _ha_green_url(),
            "token_stored": _ha_token_stored(),
        }

        count = _tcc_grant_count()
        if count is None:
            fda = {
                "status": "unknown",
                "detail": "can't read TCC — grant Full Disk Access to check",
                "anchor": _FDA_ANCHOR,
            }
        elif count >= _TCC_REQUIRED:
            fda = {"status": "ok", "detail": f"{count} grants in place", "anchor": _FDA_ANCHOR}
        else:
            fda = {
                "status": "attention",
                "detail": f"{count}/{_TCC_REQUIRED} grants — run the grant step",
                "anchor": _FDA_ANCHOR,
            }
        steps["fda"] = fda
        steps["automation"] = {
            "status": "unknown",
            "detail": "can't auto-detect — confirm once you've granted it",
            "anchor": _AUTOMATION_ANCHOR,
        }
    return {"tier": tier, "binary": _sanctum_bin(), "version": _version(), "steps": steps}


def _probe_tailnet() -> dict[str, Any]:
    """Live tailnet health — reuses the exact seams behind ``sanctum tailnet doctor``."""
    from sanctum_cli.commands import tailnet as tn
    from sanctum_cli.net.tailnet import build_tailnet_report

    report = build_tailnet_report(
        spine=tn._safe_probe(tn._probe_spine),
        peer=tn._safe_probe(lambda: tn._probe_peer(_PEER)),
        cred=tn._safe_probe(tn._probe_cred),
        drift=tn._safe_probe(tn._probe_drift),
        trifecta=tn._safe_probe(tn._probe_trifecta),
    )
    return {
        "overall": report.overall,
        "rows": [{"label": r.label, "status": r.status.name, "detail": r.detail} for r in report.rows],
    }


def _probe_verify() -> dict[str, Any]:
    """Final health roll-up via ``sanctum self-test --json`` (same-venv binary)."""
    proc = subprocess.run(
        [_sanctum_bin(), "self-test", "--json"], capture_output=True, text=True, check=False
    )
    with contextlib.suppress(Exception):
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
    return {"tier": _tier(), "error": "could not parse self-test output", "raw": proc.stdout[-400:]}


def _probe_network() -> dict[str, Any]:
    """One-shot read-only topology scan — the exact seams behind ``sanctum net
    check`` (NAT class, gateway, ISP), plus the tailnet spine (is this node on
    Tailscale, and under which MagicDNS suffix)."""
    from sanctum_cli.commands import net as net_cmd
    from sanctum_cli.commands import tailnet as tn
    from sanctum_cli.net import detect

    report = detect.detect(
        runner=net_cmd._build_runner(),
        http=net_cmd._build_http(),
        firewalla_present=net_cmd._firewalla_present(),
    )
    spine = tn._safe_probe(tn._probe_spine)
    return {
        "nat": report.nat.value,
        "gateway": report.gateway_ip,
        "isp": report.isp,
        "public_ip": report.public_ip,
        "firewalla": report.firewalla_present,
        "reason": report.reason,
        "tailnet": {
            "on": bool(spine and spine.on_tailnet),
            "suffix": spine.suffix if spine else "",
        },
    }


def _probe_firewalla() -> dict[str, Any]:
    """Authenticated read-only bridge probe with the STORED token (never echoed).

    The same fail-closed classifier onboarding trusts
    (:func:`screen_time.validate_firewalla_pairing`); a candidate token typed in
    the form is verified by the *save action* instead, so an unsaved secret never
    rides a GET."""
    from sanctum_cli.commands import screen_time
    from sanctum_cli.devices import firewalla

    token = firewalla._read_bridge_token()
    if token is None:
        return {
            "state": "no_token",
            "ok": False,
            "detail": "no bridge token on this Mac yet — pair below",
        }
    result = screen_time.validate_firewalla_pairing(firewalla._bridge_url(), token)
    return {"state": result.state, "ok": result.ok, "detail": result.detail}


def _probe_ha() -> dict[str, Any]:
    """Live HA Green reachability — read-only, the onboard ha-green gate's seams.
    Reports presence of the token, never its value."""
    from sanctum_cli.devices import ha_green

    host, port = ha_green._url_host_port()
    reachable = ha_green.lan_reachable()
    running = ha_green.api_running() if reachable else False
    return {
        "host": host,
        "port": port,
        "reachable": reachable,
        "api_running": running,
        "version": ha_green.ha_version() if running else None,
        "tailnet_node": ha_green.tailscale_node_present(),
        "token_stored": _ha_token_stored(),
    }


def probe(pid: str) -> dict[str, Any]:
    if pid == "tailnet":
        return _probe_tailnet()
    if pid == "verify":
        return _probe_verify()
    if pid == "network":
        return _probe_network()
    if pid == "firewalla":
        return _probe_firewalla()
    if pid == "ha":
        return _probe_ha()
    # otherwise refresh a single cheap step by id
    steps = gather_state()["steps"]
    found = steps.get(pid) if isinstance(steps, dict) else None
    return found if isinstance(found, dict) else {"status": "unknown", "detail": "no such probe"}


# ── actions (mutations — reuse the exact onboard/tailnet seams) ────────────────


def _act_identity(name: str) -> dict[str, Any]:
    """Set instance.name/slug via a block-preserving read-modify-write.

    NOT ``scaffold_instance``/``init`` — those rewrite the whole file to just the
    ``instance`` block, which would clobber a configured box's ``cli``/``notifications``
    blocks. Mirrors ``onboard.set_instance_identity``'s RMW + ``.bak`` contract."""
    import yaml

    name = name.strip()
    if not name:
        return {"ok": False, "detail": "please enter a name"}
    target = config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        with contextlib.suppress(Exception):
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    inst = data.get("instance")
    if not isinstance(inst, dict):
        inst = data["instance"] = {}
    inst["name"] = name
    inst["slug"] = config.slugify_name(name)
    if target.exists():
        with contextlib.suppress(Exception):
            (target.parent / (target.name + ".bak")).write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    try:
        config.load(target)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "detail": f"saved, but the file no longer validates: {exc}"}
    return {"ok": True, "name": name, "detail": f"named “{name}”"}


def _act_creds(client_id: str, client_secret: str) -> dict[str, Any]:
    """Verify BOTH OAuth scopes against the live API, then store — the exact
    verify-before-store path of ``tailnet creds``, minus the terminal prompt.
    Nothing is stored unless both scopes pass."""
    from sanctum_cli.commands import tailnet as tn

    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        return {"ok": False, "detail": "both the Client ID and secret are required"}
    token = tn._mint_oauth_token(client_id, client_secret)
    if token is None:
        return {
            "ok": False,
            "detail": "couldn't mint a token — re-check the id/secret (a stray space is the usual culprit)",
        }
    acl_code, _ = tn._api_request(
        "GET", f"tailnet/{tn._TAILNET}/acl", token, accept="application/hujson"
    )
    dev_code, _ = tn._api_request("GET", f"tailnet/{tn._TAILNET}/devices", token)
    missing: list[str] = []
    if acl_code != 200:
        missing.append("ACL (write)")
    if dev_code != 200:
        missing.append("Devices Core (write)")
    if missing:
        return {
            "ok": False,
            "detail": (
                f"the OAuth client is missing scope(s): {', '.join(missing)}. "
                f"Add them at {_OAUTH_URL}, then retry — nothing was stored."
            ),
        }
    tn._store_creds(client_id, client_secret)
    return {"ok": True, "detail": "both scopes confirmed ✓ — stored in your keychain."}


def _act_apply() -> dict[str, Any]:
    """Push the ACL via the real ``sanctum tailnet apply --apply`` (same-venv binary,
    non-interactive) so the GUI runs the exact shipped command, no logic drift."""
    proc = subprocess.run(
        [_sanctum_bin(), "tailnet", "apply", "--apply"], capture_output=True, text=True, check=False
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    lines = [line for line in out.splitlines() if line.strip()]
    detail = "\n".join(lines)[-800:] if lines else ("applied" if proc.returncode == 0 else "apply failed")
    return {"ok": proc.returncode == 0, "detail": detail}


def _act_provider(kind: str, key: str) -> dict[str, Any]:
    """Store an API key, health-probe it, and persist routing on success — or revoke
    on rejection. Reuses onboard's fail-closed provider seams verbatim."""
    from sanctum_cli.commands import onboard

    kind = kind.strip().lower()
    key = key.strip()
    if kind not in ("claude", "gemini"):
        return {"ok": False, "detail": "unknown provider"}
    if not key:
        return {"ok": False, "detail": "enter an API key, or skip this step"}
    service, account = onboard._CLAUDE_KEYCHAIN if kind == "claude" else onboard._GEMINI_KEYCHAIN
    onboard.store_device_secret(service=service, account=account, secret=key)
    if kind == "claude":
        cfg = onboard._config_with_provider_overrides(
            claude={"via": "direct", "endpoint": "https://api.anthropic.com"}
        )
        health = onboard._provider_health("claude", cfg)
    else:
        cfg = onboard._config_with_provider_overrides(gemini={"model": "gemini-2.5-pro"})
        health = onboard._provider_health("gemini", cfg)
    if not health.ok:
        onboard._revoke_device_secret(service=service, account=account)
        return {
            "ok": False,
            "detail": f"the key was rejected ({health.detail or 'auth failed'}) — nothing stored.",
        }
    if kind == "claude":
        onboard.set_provider_config(
            claude={"via": "direct", "endpoint": "https://api.anthropic.com"},
            default_provider="claude",
        )
    else:
        onboard.set_provider_config(gemini={"model": "gemini-2.5-pro"}, default_provider="gemini")
    return {"ok": True, "detail": f"{kind.title()} connected ✓"}


def _act_backup(payload: dict[str, Any]) -> dict[str, Any]:
    """Write the backup *policy* (default recipe + retention) via the same
    block-preserving read-modify-write contract as :func:`_act_identity`.

    Destination credentials are deliberately NOT collected here — the ``sanctum
    cloud setup`` backend wizards own verify-before-store for those, so the pane
    hands off to the exact shipped command instead of growing a second cred path."""
    import yaml

    from sanctum_cli import recipes

    raw_recipes = config.instance_value("cli.recipes")
    known = sorted(
        set(recipes.BUILTINS) | (set(raw_recipes) if isinstance(raw_recipes, dict) else set())
    )
    recipe = str(payload.get("default_recipe", "")).strip()
    if recipe and recipe not in known:
        return {"ok": False, "detail": f"unknown recipe {recipe!r} — pick one of: {', '.join(known)}"}
    try:
        keep = {
            "keep_daily": int(payload.get("keep_daily", 7)),
            "keep_weekly": int(payload.get("keep_weekly", 4)),
            "keep_monthly": int(payload.get("keep_monthly", 12)),
        }
    except (TypeError, ValueError):
        return {"ok": False, "detail": "retention values must be whole numbers"}
    if keep["keep_daily"] < 1 or keep["keep_weekly"] < 0 or keep["keep_monthly"] < 0:
        return {"ok": False, "detail": "retention: keep at least 1 daily; weekly/monthly can be 0"}

    target = config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        with contextlib.suppress(Exception):
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    cli = data.get("cli")
    if not isinstance(cli, dict):
        cli = data["cli"] = {}
    if recipe:
        cli["default_recipe"] = recipe
    cloud = cli.get("cloud_backup")
    if not isinstance(cloud, dict):
        cloud = cli["cloud_backup"] = {}
    cloud["retention"] = keep
    if target.exists():
        with contextlib.suppress(Exception):
            (target.parent / (target.name + ".bak")).write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    # Only a *named* box can fully validate (Config requires the instance block);
    # before naming, the write above is still well-formed YAML the loader will pick up.
    if isinstance(data.get("instance"), dict):
        try:
            config.load(target)
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "detail": f"saved, but the file no longer validates: {exc}"}
    saved = f"policy saved — keep {keep['keep_daily']}d/{keep['keep_weekly']}w/{keep['keep_monthly']}m"
    return {"ok": True, "detail": (saved + (f", recipe “{recipe}”" if recipe else ""))}


def _act_family(payload: dict[str, Any]) -> dict[str, Any]:
    """Read-modify-write the ``family`` block of devices.yaml (screen-time's SoT).

    Updates names / roles / personal devices; PRESERVES every unmodeled key —
    per-member curfew/wake/enforce_personal, per-device ``enforce`` (matched by
    MAC), ``shared_devices``, and any sibling top-level blocks. Members absent
    from the form are kept, never silently deleted (removal stays a deliberate
    devices.yaml edit)."""
    import yaml

    from sanctum_cli.commands import screen_time

    raw_members = payload.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        return {"ok": False, "detail": "add at least one family member"}
    parsed: list[tuple[str, str, str, list[dict[str, str]]]] = []
    seen: set[str] = set()
    for member in raw_members:
        if not isinstance(member, dict):
            return {"ok": False, "detail": "malformed member entry"}
        name = str(member.get("name", "")).strip()
        if not name:
            return {"ok": False, "detail": "every member needs a name"}
        pid = str(member.get("id", "")).strip() or config.slugify_name(name)
        if pid in seen:
            return {"ok": False, "detail": f"“{name}” appears twice — give each person one card"}
        seen.add(pid)
        role = str(member.get("role", "")).strip() or "child"
        devices: list[dict[str, str]] = []
        for dev in member.get("devices") or []:
            if not isinstance(dev, dict):
                continue
            mac = str(dev.get("mac", "")).strip().upper()
            dev_name = str(dev.get("name", "")).strip()
            if not mac and not dev_name:
                continue  # an untouched blank row
            if not _MAC_RE.match(mac):
                shown = mac or "(empty)"
                return {
                    "ok": False,
                    "detail": f"{dev_name or name}: {shown} isn't a MAC like AA:BB:CC:DD:EE:FF",
                }
            devices.append({"name": dev_name or mac, "mac": mac})
        parsed.append((pid, name, role, devices))

    override = os.environ.get(screen_time.ENV_DEVICES_FILE)
    target = _devices_file() or (
        Path(override).expanduser() if override else screen_time._DEVICES_CANDIDATES[0]
    )
    data: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            return {"ok": False, "detail": f"couldn't parse {target.name}: {exc}"}
        if isinstance(loaded, dict):
            data = loaded
    family = data.get("family")
    if not isinstance(family, dict):
        family = data["family"] = {}
    for pid, name, role, devices in parsed:
        person = family.get(pid)
        if not isinstance(person, dict):
            person = family[pid] = {}
        person["name"] = name
        person["role"] = role
        old = person.get("personal_devices")
        old_by_mac = (
            {str(d.get("mac", "")).upper(): d for d in old if isinstance(d, dict)}
            if isinstance(old, list)
            else {}
        )
        person["personal_devices"] = [
            {**old_by_mac.get(d["mac"], {}), "name": d["name"], "mac": d["mac"]} for d in devices
        ]
    if target.exists():
        with contextlib.suppress(Exception):
            (target.parent / (target.name + ".bak")).write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    total = sum(len(d) for _, _, _, d in parsed)
    return {"ok": True, "detail": f"saved {len(parsed)} member(s), {total} device(s)"}


def _act_firewalla(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify-before-store bridge pairing — the exact fail-closed contract of the
    onboard firewalla-pairing gate, minus the terminal prompts. A blank token
    re-verifies the one already on this Mac; nothing persists unless the
    authenticated probe answers."""
    from sanctum_cli.commands import onboard, screen_time
    from sanctum_cli.devices import firewalla

    url = str(payload.get("url", "")).strip() or firewalla._bridge_url()
    token = str(payload.get("token", "")).strip() or (firewalla._read_bridge_token() or "")
    if not token:
        return {"ok": False, "detail": "paste the bridge token — none is stored on this Mac yet"}
    result = screen_time.validate_firewalla_pairing(url, token)
    if not result.ok:
        return {"ok": False, "detail": f"not paired — {result.detail}. Nothing was stored."}
    onboard.set_firewalla_bridge(
        token=token,
        device_ip=str(payload.get("device_ip", "")).strip(),
        device_mac=str(payload.get("device_mac", "")).strip(),
        port=onboard._port_from_url(url),
    )
    return {"ok": True, "detail": f"paired ✓ — {result.detail}"}


def _act_ha(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify-before-store HA Green pairing — mirrors the onboard ha-green gate:
    persist ONLY after the live "API running." marker answers for the candidate
    (or already-stored) token. A blank token means "re-verify the stored one"."""
    from sanctum_cli.commands import onboard
    from sanctum_cli.devices import ha_green

    url = str(payload.get("url", "")).strip() or ha_green._ha_url()
    token = str(payload.get("token", "")).strip()
    if not token and ha_green._read_ha_token() is None:
        return {"ok": False, "detail": "paste the long-lived owner token — none is stored yet"}
    if not ha_green.api_running(url=url, token=token or None):
        return {
            "ok": False,
            "detail": (
                "Home Assistant didn't answer “API running.” at that address with this "
                "token — nothing was stored"
            ),
        }
    host, port = ha_green._url_host_port(url)
    onboard.set_ha_green(
        token=token or None,
        host=host,
        port=port,
        device_mac=onboard._HA_GREEN_MAC,
        tailnet_node=ha_green._TAILNET_NODE,
    )
    version = ha_green.ha_version(url=url, token=token or None)
    suffix = f" {version}" if version else ""
    return {"ok": True, "detail": f"verified ✓ — Home Assistant{suffix} is running"}


def _os_open(url: str) -> None:
    """Open any URL scheme through macOS ``open`` (handles https AND the
    ``x-apple.systempreferences:`` Settings deep-links a browser would refuse)."""
    with contextlib.suppress(Exception):
        subprocess.run(["open", url], check=False)


def do_action(step: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = f"{step}.{action}"
    if key == "identity.save":
        return _act_identity(str(payload.get("name", "")))
    if key == "tailscale.creds":
        return _act_creds(str(payload.get("client_id", "")), str(payload.get("client_secret", "")))
    if key == "tailscale.apply":
        return _act_apply()
    if key == "provider.save":
        return _act_provider(str(payload.get("kind", "")), str(payload.get("key", "")))
    if key == "backup.save":
        return _act_backup(payload)
    if key == "family.save":
        return _act_family(payload)
    if key == "firewalla.save":
        return _act_firewalla(payload)
    if key == "ha.save":
        return _act_ha(payload)
    if key == "open.url":
        _os_open(str(payload.get("url", "")))
        return {"ok": True}
    return {"ok": False, "detail": f"unknown action: {key}"}


# ── server ─────────────────────────────────────────────────────────────────────


class _Session:
    def __init__(self) -> None:
        self.shutdown = threading.Event()


def _make_handler(session: _Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/?"):
                self._write_html(PAGE)
                return
            if self.path == "/state":
                self._write_json(gather_state())
                return
            if self.path.startswith("/probe/"):
                self._write_json(probe(self.path[len("/probe/") :]))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/action":
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    data = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    data = {}
                self._write_json(
                    do_action(
                        str(data.get("step", "")),
                        str(data.get("action", "")),
                        data.get("payload") or {},
                    )
                )
                return
            if self.path == "/done":
                self._write_json({"ok": True})
                session.shutdown.set()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, *args: Any) -> None:  # noqa: ARG002 - silence access logging
            return

    return Handler


def _find_port(preferred: int) -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", preferred))
        return int(sock.getsockname()[1])


def _open_window(url: str) -> None:
    """Prefer a chromeless Chrome app-window (installer feel); fall back to the
    default browser tab. Both are best-effort — the URL is always printed too."""
    if Path("/Applications/Google Chrome.app").exists():
        with contextlib.suppress(Exception):
            subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}"])
            return
    with contextlib.suppress(Exception):
        webbrowser.open(url)


# ── .app bundle (locally built ⇒ no quarantine ⇒ no Gatekeeper block) ───────────

_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Sanctum Setup</string>
    <key>CFBundleDisplayName</key><string>Sanctum Setup</string>
    <key>CFBundleExecutable</key><string>sanctum-setup</string>
    <key>CFBundleIdentifier</key><string>ai.sanctum.setup</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
"""


def _install_app() -> None:
    macos = _APP_PATH / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    launcher = macos / "sanctum-setup"
    launcher.write_text(f"#!/bin/bash\nexec {shlex.quote(_sanctum_bin())} setup\n", encoding="utf-8")
    launcher.chmod(0o755)
    (_APP_PATH / "Contents" / "Info.plist").write_text(_INFO_PLIST, encoding="utf-8")
    console.print(f"[green]✓[/] built [bold]{_APP_PATH}[/]")
    console.print("[dim]Open Finder → Applications and double-click “Sanctum Setup” to run it anytime.[/]")


# ── command entrypoint ─────────────────────────────────────────────────────────


def setup_command(
    *,
    no_open: bool = False,
    port: int = 0,
    install_app: bool = False,
) -> None:
    """Serve the first-run Setup Assistant on ``127.0.0.1`` and open it in a window."""
    if install_app:
        _install_app()
        return

    session = _Session()
    chosen = _find_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", chosen), _make_handler(session))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{chosen}/"

    console.print("\n[bold]Sanctum Setup[/] is open in your browser.")
    console.print(f"[dim]{url}   ·   press Ctrl-C here to close[/]\n")
    if not no_open:
        _open_window(url)

    try:
        session.shutdown.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    console.print("[green]✓[/] Setup closed.")
