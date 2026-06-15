"""The BLOODSTREAM — the broadcast + read side of the endocrine system.

The gland computes the :class:`~sanctum_cli.endocrine.gland.Panel`; the
bloodstream carries it. Two transport surfaces, both resolved through the
No-Hardcoded-Endpoints doctrine (env override > discovery default), never a
literal at a call site:

  • chitti samskara   — the ambient-state heartbeat agents already read. The
                        gland appends one ``hormone-panel`` record per tick via
                        ``POST {chitti}/action`` (the SAME write contract the
                        launchd-health-sentinel uses). This is the slow,
                        broadcast, timeseries channel (chitti records it).
  • a queryable file  — the live panel is mirrored to a tiny JSON state file so
                        any reader (a council seat, the CLI, a test) can read
                        the current disposition WITHOUT standing up an HTTP
                        listener. The daemon writes it every tick; readers
                        treat its absence as "gland down → neutral" (the
                        off-by-default fail-soft).

The creative-mode lever is ALSO a file here (``creative-mode.json``): the CLI
writes it (operator dosing), the gland reads it as a real input signal. A
file, not a flag, because creative mode is a STATE the gland SUSTAINS (slow
decay), not a per-prompt toggle — and a file survives a daemon restart.

Reads are fail-soft to NEUTRAL: a missing/garbage panel file means the gland
isn't running or hasn't published, and the receptor's neutral path leaves every
seat byte-identical to today. There is no read path that can fabricate a
non-neutral disposition.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any

from .gland import Panel, samskara_record

# ── endpoints (env > default; the daemon copy reads instance.yaml ports) ──
# These mirror the live SoT (instance.yaml services.chitti.port=2188,
# services.force_flow.port=4077). They are resolved through env so the daemon,
# tests, and a relocated host all override without a code edit — the literal
# here is the documented default, not a hidden hardcode.
CHITTI_ENV = "CHITTI_BASE_URL"
FORCE_FLOW_ENV = "FORCE_FLOW_URL"
DEFAULT_CHITTI = "http://127.0.0.1:2188"
DEFAULT_FORCE_FLOW = "http://127.0.0.1:4077"

# ── on-disk bloodstream (the listener-free query surface) ──
# Daemon-side state lives under ~/.sanctum per the secret-rotator pattern (NOT
# the OneDrive-synced repo). Overridable for tests via env.
STATE_DIR_ENV = "SANCTUM_ENDOCRINE_DIR"
_DEFAULT_STATE_DIR = Path("~/.sanctum/state/endocrine").expanduser()
PANEL_FILENAME = "panel.json"
CREATIVE_FILENAME = "creative-mode.json"
# Persistent opt-OUT marker: `sanctum endocrine off` writes it, `on` removes it.
# Distinct from the per-invocation SANCTUM_ENDOCRINE=0 env (which still wins) —
# this is the durable in-product switch so an operator doesn't have to remember
# to export the env every shell.
OPTOUT_FILENAME = "subscribed-off.json"


def chitti_base() -> str:
    return os.environ.get(CHITTI_ENV, DEFAULT_CHITTI).rstrip("/")


def force_flow_base() -> str:
    return os.environ.get(FORCE_FLOW_ENV, DEFAULT_FORCE_FLOW).rstrip("/")


def state_dir() -> Path:
    raw = os.environ.get(STATE_DIR_ENV)
    return Path(raw).expanduser() if raw else _DEFAULT_STATE_DIR


def _ensure_state_dir() -> Path:
    """Make the state dir at 0700 (owner-only). The endocrine lever + panel are
    operator state — the creative-mode lever in particular is an UNauthenticated
    input signal the gland trusts, so it must not be world-writable. 0700 keeps
    the directory owner-only; the files inside are written 0600 (mirroring
    telemetry.py's deliberate-0600 pattern)."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Best-effort tighten: idempotent, and pulls a pre-existing looser dir down.
    with suppress(OSError):
        d.chmod(0o700)
    return d


def _write_private_atomic(path: Path, data: str) -> None:
    """Atomic write with a 0600 final file (and 0600 temp, so the secret-ish
    payload is never momentarily world-readable between create and rename)."""
    tmp = path.with_name(path.name + ".tmp")
    # os.open with the mode arg is the only way to create AT 0600 (a plain
    # write_text honours umask, which is typically 0644). Mirror telemetry.py.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    # A pre-existing temp from an older run may carry looser perms — force 0600.
    with suppress(OSError):
        tmp.chmod(0o600)
    tmp.replace(path)


def panel_path() -> Path:
    return state_dir() / PANEL_FILENAME


def creative_path() -> Path:
    return state_dir() / CREATIVE_FILENAME


def optout_path() -> Path:
    return state_dir() / OPTOUT_FILENAME


# ───────────────────────────── publish (gland → bloodstream) ──────────────


def publish_panel_file(panel: Panel) -> None:
    """Mirror the live panel to the query file (atomic write).

    Readers (seats, CLI, tests) poll this instead of an HTTP endpoint — no
    listener to crash, no port to allocate. Atomic via write-temp-then-rename
    so a reader never sees a half-written panel. Written 0600 in a 0700 dir."""
    _ensure_state_dir()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "panel": panel.to_wire(),
    }
    _write_private_atomic(panel_path(), json.dumps(rec))


def broadcast_to_chitti(panel: Panel, *, timeout: float = 5.0) -> bool:
    """Append the panel timeseries to chitti's samskara journal.

    Uses the SAME write contract as launchd-health-sentinel
    (``POST {chitti}/action`` with the samskara record shape) so the existing
    readers parse it. Returns True on success; a failure is non-fatal (the
    file mirror is the durable channel) — the gland keeps stepping.
    """
    rec = samskara_record(panel)
    body = json.dumps(rec).encode()
    req = urllib.request.Request(
        f"{chitti_base()}/action",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


# ───────────────────────────── read (bloodstream → receptor) ──────────────


def read_panel() -> Panel | None:
    """Read the live panel off the bloodstream. Fail-soft to None.

    None means "gland down / not published" — the receptor's None path is
    OFF-BY-DEFAULT (changes nothing). A garbage file is also None (never a
    fabricated disposition); a well-formed file with missing keys is repaired
    to neutral per :meth:`Panel.from_wire`."""
    try:
        raw = panel_path().read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        doc = json.loads(raw)
        wire = doc.get("panel", doc)  # tolerate both {"panel":{...}} and bare
        if not isinstance(wire, dict):
            return None
        return Panel.from_wire(wire)
    except (ValueError, TypeError):
        return None


# ───────────────────────────── creative-mode lever ────────────────────────


def read_creative_mode() -> bool:
    """Is the council dosed into creative mode? The gland reads this as a
    REAL input signal. Defaults False (the resting, conservative state)."""
    try:
        doc = json.loads(creative_path().read_text())
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return False
    if not isinstance(doc, dict):
        return False
    until = doc.get("until_epoch")
    if isinstance(until, (int, float)) and until > 0:
        # auto-expiring dose: creative mode lapses back to baseline on its own,
        # so a forgotten dose can never leave the council permanently hot.
        return time.time() < until
    return bool(doc.get("creative", False))


def set_creative_mode(on: bool, *, ttl_seconds: int | None = None) -> dict[str, Any]:
    """Operator dose: turn creative mode on (optionally with a TTL) or off.

    Writing the lever is all the CLI does; the gland picks it up on its next
    tick and SLOWLY elevates dopamine / lowers cortisol (a sustained STATE, not
    a flag). Returns the written record.

    The lever is an UNauthenticated input the gland trusts, so it is written
    0600 in a 0700 dir — a world-writable lever would let any local user dose
    the council. (The auth boundary itself is roadmapped; this closes the
    file-permission half.)"""
    _ensure_state_dir()
    rec: dict[str, Any] = {
        "creative": bool(on),
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if on and ttl_seconds:
        rec["until_epoch"] = int(time.time()) + int(ttl_seconds)
    _write_private_atomic(creative_path(), json.dumps(rec))
    return rec


# ───────────────────────────── persistent opt-out switch ──────────────────


def read_optout() -> bool:
    """Is the durable opt-out marker present? (`sanctum endocrine off` wrote it.)

    A missing/garbage marker means NOT opted out (the default is subscribed,
    per the 2026-06-15 ON-by-default directive). The per-invocation env
    SANCTUM_ENDOCRINE=0 still wins over this; it is checked at the call site."""
    try:
        doc = json.loads(optout_path().read_text())
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return False
    return bool(doc.get("optout", False)) if isinstance(doc, dict) else False


def set_optout(optout: bool) -> None:
    """Persist the in-product switch. `off` writes optout=True; `on` removes the
    marker entirely (back to the subscribed default)."""
    if optout:
        _ensure_state_dir()
        rec = {"optout": True, "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _write_private_atomic(optout_path(), json.dumps(rec))
    else:
        with suppress(FileNotFoundError, OSError):
            optout_path().unlink()
