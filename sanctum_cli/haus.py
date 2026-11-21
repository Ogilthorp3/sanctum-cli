"""Haus-only command gate — banner + cheap presence detection.

Several ``sanctum`` subcommands only mean something on a *full Sanctum haus*:
the Mini, the council proxyd (``:4040``), the mTLS Root CA, the bridge gateway,
the Firewalla, the screen-time ``devices.yaml``. They are not part of the public
beta. An outside tester who runs them today gets a fail-soft mystery (timeouts,
"no devices.yaml", empty council). This module turns that into one clean banner.

Design:

* :func:`haus_required` is called at the *top* of each haus-only command. If the
  required component is **not** present, it prints the banner and exits cleanly
  (``ExitCode.OK`` — this is "not for you", not an error). If it **is** present,
  the function returns and the command proceeds exactly as before.

* Detection is **cheap and safe**: filesystem + environment + (for the bridge)
  a non-blocking Keychain *existence* probe. It never opens a socket, so it can
  never hang, and it never reads a secret value into memory.

* It must NOT break the command for an operator who HAS the haus (a full haus). Every
  component has a present-signal that is true on a real haus and false on a bare
  beta box; an explicit env override (the same envs the commands already honour)
  also counts as present, so a staging/CI box that points the CLI at infra is
  treated as a haus.

* A declared satellite (``instance.role: satellite``) is not the hub, even though it
  has an mTLS CA of its own (for its local brain): the hub services (``council``,
  ``bridge``) read absent there unless :data:`SATELLITE_OVERRIDE_ENV` is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from sanctum_cli import config, keychain
from sanctum_cli.errors import ExitCode

#: The components a haus-only command can require. One per private dependency.
Component = Literal["council", "bridge", "screen-time", "launchagents"]

_err_console = Console(stderr=True)

# The mTLS Root CA is the cleanest haus fingerprint: present on every real
# Sanctum host (it signs proxyd + the bridge leaf), absent on a bare beta box.
# A satellite has one too, for its own local brain, hence the role check below.
_CA_CERT = Path("~/.sanctum/certs/ca.crt").expanduser()

#: The hub services. A declared satellite runs neither, and its own CA (or a
#: stray SANCTUM_PROXYD_* variable) proves nothing about them, so on a satellite
#: these read absent: the council commands (``council``, ``chat``, ``code``,
#: ``brainstorm``, ``endocrine``) and ``bridge`` print the banner instead of
#: running, and a bare ``sanctum`` in a terminal opens no council chamber.
_HUB_COMPONENTS: frozenset[Component] = frozenset({"council", "bridge"})

#: The explicit override for a satellite that can reach the hub's council and
#: bridge: a truthy value (``1``, ``true``, ``yes``, ``on``) lifts the satellite
#: rule, and the usual detection below applies. Any other value, or none, keeps it.
SATELLITE_OVERRIDE_ENV = "SANCTUM_HAUS_ON_SATELLITE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _devices_candidates() -> tuple[Path, Path]:
    """Screen-time source of truth — same candidates the screen-time/devices/schedule
    commands read, resolved per call (never frozen to the importing process's home)."""
    home = Path.home()
    return (
        home / ".sanctum/screen-time/devices.yaml",
        home / "Projects/sanctum-screen-time/devices.yaml",
    )


# Env overrides that, when set, mean the operator has explicitly pointed the CLI
# at real infra — treat as "haus present" without touching the filesystem.
_COUNCIL_ENVS = (
    "SANCTUM_PROXYD_URL",
    "SANCTUM_COUNCIL_URL",
    "SANCTUM_PROXYD_CA",
    "SANCTUM_PROXYD_INSECURE",
)
_BRIDGE_ENV = "SANCTUM_BRIDGE_URL"
_DEVICES_ENV = "SANCTUM_DEVICES_FILE"

# Bridge Keychain entries — existence (not value) is the present-signal.
_BRIDGE_KEYCHAIN_SERVICE = "sanctum-bridge-cf-access-client-id"
_BRIDGE_KEYCHAIN_ACCOUNT = "sanctum"


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _launchagents_present() -> bool:
    """True iff any ``haus.sanctum.*`` LaunchAgent plist is installed.

    Pure directory glob — no ``launchctl`` shell-out, so it cannot hang.
    """
    for base in (
        Path("~/Library/LaunchAgents").expanduser(),
        Path("/Library/LaunchAgents"),
    ):
        try:
            if any(base.glob("haus.sanctum.*.plist")):
                return True
        except OSError:
            continue
    return False


def _satellite_without_override() -> bool:
    """True on a declared satellite, unless :data:`SATELLITE_OVERRIDE_ENV` is truthy.

    The role comes from :func:`config.instance_role`, which never raises. It still
    reads ``role: satellite`` when another value in instance.yaml fails the schema
    or cannot be built (such as ``since: 2026-02-30``), and when the file holds an
    undefined alias or a second document; none of those lets the hub services
    through. A character PyYAML refuses (an escape code, say) is read as an
    unknown character, so one in a comment hides nothing (one where that character
    cannot stand, right after a quoted value say, is a syntax error). A role written
    before the first syntax error is read, even a plain value PyYAML ends at a tab or
    a ``: `` (``role: hub: x`` reads ``hub``); one after it, or only in a second
    document, is not, and neither is a role with a refused character in its value, its ``role``
    key or the ``instance`` key above it, nor the role in a file that is not
    UTF-8: that machine is gated like one that declares no role.
    """
    if os.environ.get(SATELLITE_OVERRIDE_ENV, "").strip().lower() in _TRUTHY:
        return False
    return config.instance_role() == "satellite"


def is_present(component: Component) -> bool:
    """Cheap, non-blocking check of whether *component*'s infra is here.

    Never opens a socket and never reads a Keychain *value* — for the bridge it
    asks the Keychain only whether an entry exists, which is a fast miss on a box
    that never provisioned the bridge. On a declared satellite the hub services
    (``council``, ``bridge``) are absent unless :data:`SATELLITE_OVERRIDE_ENV` is
    set; ``screen-time`` and ``launchagents`` keep their own on-disk signals (a
    satellite's devices.yaml and haus.sanctum.* agents are its own).
    """
    if component in _HUB_COMPONENTS and _satellite_without_override():
        return False
    if component == "council":
        return any(_env_set(e) for e in _COUNCIL_ENVS) or _CA_CERT.is_file()
    if component == "bridge":
        if _env_set(_BRIDGE_ENV) or _CA_CERT.is_file():
            return True
        return keychain.exists(_BRIDGE_KEYCHAIN_ACCOUNT, _BRIDGE_KEYCHAIN_SERVICE)
    if component == "screen-time":
        if _env_set(_DEVICES_ENV):
            return True
        return any(p.is_file() for p in _devices_candidates())
    if component == "launchagents":
        return _launchagents_present()
    return False  # pragma: no cover - exhaustive Literal


def _print_banner(component: Component) -> None:
    _err_console.print(
        "[bold yellow]This command needs a full Sanctum haus[/] "
        "(Mini + Firewalla + council) and isn't part of the beta."
    )
    _err_console.print(f"[dim]missing:[/] {component}")
    if component in _HUB_COMPONENTS and _satellite_without_override():
        _err_console.print(
            f"[dim]This machine is a satellite (instance.role: satellite) and the {component} "
            f"is the hub's. To use it from here anyway, set {SATELLITE_OVERRIDE_ENV}=1.[/]"
        )
    _err_console.print("See [underline]https://sanctum.run[/] for the full setup.")


def haus_required(component: Component) -> None:
    """Gate a haus-only command on *component* being present.

    Call this at the top of the command body. When the component is absent it
    prints a clean banner to stderr and raises ``typer.Exit(ExitCode.OK)`` — a
    deliberate, non-error exit (the command is simply not part of the beta), so
    scripts don't see a failure and the user never gets a traceback. When the
    component is present it returns and the command runs unchanged.
    """
    if is_present(component):
        return
    _print_banner(component)
    raise typer.Exit(code=int(ExitCode.OK))
