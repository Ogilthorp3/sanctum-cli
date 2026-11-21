"""Hive service principal (wave-1) — ownership checks for control-plane daemons.

The hub runs proxyd / force-flow / memory-vault as LaunchDaemons under a dedicated
local user ``sanctum`` so the control plane does not ride the operator's GUI
login session. Family Pass (CLI-only) installs and satellites (``instance.role:
satellite``) never create this user; they treat the probes as not-applicable.

Install (sudo, once per hub)::

    sanctum service-user install
    # → ~/.sanctum/scripts/service-user/install-on-new-hub.sh

Status / check (no sudo)::

    sanctum service-user status
    sanctum service-user check   # exit 1 on fail — self-test / CI
"""

from __future__ import annotations

import os
import pwd
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from sanctum_cli import config
from sanctum_cli.errors import ConfigError, SanctumError

WAVE1_LABELS: tuple[str, ...] = (
    "haus.sanctum.proxyd",
    "haus.sanctum.force-flow",
    "haus.sanctum.memory-vault",
)

# Process argv patterns (egrep-style) for each wave-1 unit.
_PROCESS_PATTERNS: dict[str, str] = {
    "haus.sanctum.proxyd": r"bin/proxyd$|/proxyd$",
    "haus.sanctum.force-flow": r"force_flow\.py",
    "haus.sanctum.memory-vault": r"sanctum-memory-vault",
}

INSTALL_SCRIPT = Path.home() / ".sanctum/scripts/service-user/install-on-new-hub.sh"
STATUS_SCRIPT = Path.home() / ".sanctum/scripts/service-user/status.sh"
DAEMON_DIR = Path("/Library/LaunchDaemons")
SERVICE_USERNAME = "sanctum"


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str = ""
    skip: bool = False


@dataclass
class Wave1Report:
    """Result of a wave-1 ownership / health check."""

    applicable: bool
    items: list[CheckItem] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        if not self.applicable:
            return True
        return all(i.ok or i.skip for i in self.items) and any(
            i.ok and not i.skip for i in self.items
        )

    @property
    def failed(self) -> list[CheckItem]:
        return [i for i in self.items if not i.ok and not i.skip]


def haus_tier_present() -> bool:
    """True when this Mac is a hub that should run the wave-1 daemons.

    A declared ``instance.role`` decides: a ``satellite`` never runs them (it can
    still carry hub-looking artifacts, such as model manifests for its local
    brain), and a ``hub`` always does. Without a role, True when haus-operator
    artifacts are on disk (not Family Pass only).
    """
    role = config.instance_role()
    if role == "satellite":
        return False
    if role == "hub":
        return True
    markers = [
        Path.home() / ".sanctum/sanctum-proxy",
        Path.home() / ".sanctum/manifests",
        DAEMON_DIR / "haus.sanctum.proxyd.plist",
    ]
    return any(p.exists() for p in markers)


def service_user_exists(name: str = SERVICE_USERNAME) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _plist_username(plist: Path) -> str | None:
    if not plist.is_file():
        return None
    try:
        # PlistBuddy is more reliable for Apple plists than ElementTree
        out = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Print :UserName", str(plist)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    # fallback: rough XML parse
    try:
        root = ET.parse(plist).getroot()
        # flat dict under <dict>  (ET Element truth-testing is deprecated → `is not None`)
        dict_el = root.find("dict")
        children = list(dict_el) if dict_el is not None else []
        for i, el in enumerate(children):
            if (
                el.tag == "key"
                and (el.text or "") == "UserName"
                and i + 1 < len(children)
                and children[i + 1].tag == "string"
            ):
                return children[i + 1].text
    except (ET.ParseError, OSError):
        return None
    return None


def _process_owner(pattern: str) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-Ao", "user,args"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    import re

    rx = re.compile(pattern)
    for line in out.stdout.splitlines():
        if "egrep" in line or "grep" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        user, args = parts[0], parts[1]
        if rx.search(args):
            return user
    return None


def _http_status(url: str, timeout: float = 3.0, tls: bool = False) -> int:
    import ssl
    import urllib.error
    import urllib.request

    ctx = None
    if tls:
        ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def check_wave1(*, require_user: bool = True) -> Wave1Report:
    """Check wave-1 ownership and health.

    On a satellite or a CLI-only install (no haus markers), returns
    ``applicable=False``.
    """
    if not haus_tier_present():
        reason = (
            "satellite — the wave-1 hub daemons run on the hub, not here"
            if config.instance_role() == "satellite"
            else "CLI-only install — service principal not expected"
        )
        return Wave1Report(applicable=False, reason=reason)

    items: list[CheckItem] = []

    if require_user:
        if service_user_exists():
            items.append(CheckItem("sanctum user exists", True))
        else:
            items.append(
                CheckItem(
                    "sanctum user exists",
                    False,
                    "missing — run: sanctum service-user install",
                )
            )

    for label in WAVE1_LABELS:
        plist = DAEMON_DIR / f"{label}.plist"
        if not plist.is_file():
            items.append(
                CheckItem(f"plist {label}", False, "not installed in /Library/LaunchDaemons")
            )
            continue
        items.append(CheckItem(f"plist {label}", True, str(plist)))
        uname = _plist_username(plist)
        if uname == SERVICE_USERNAME:
            items.append(CheckItem(f"UserName={SERVICE_USERNAME}: {label}", True))
        else:
            items.append(
                CheckItem(
                    f"UserName={SERVICE_USERNAME}: {label}",
                    False,
                    f"got {uname!r}",
                )
            )

    for label, pat in _PROCESS_PATTERNS.items():
        owner = _process_owner(pat)
        short = label.removeprefix("haus.sanctum.")
        if owner is None:
            items.append(CheckItem(f"process {short}", False, "not running"))
        elif owner == SERVICE_USERNAME:
            items.append(CheckItem(f"process {short} owner", True, owner))
        else:
            items.append(
                CheckItem(
                    f"process {short} owner",
                    False,
                    f"got {owner!r}, expected {SERVICE_USERNAME}",
                )
            )

    # Health endpoints (any non-zero HTTP for TLS proxyd; 200 for force-flow)
    ff = _http_status("http://127.0.0.1:4077/health")
    if ff == 200:
        items.append(CheckItem("force-flow /health", True, "HTTP 200"))
    else:
        items.append(CheckItem("force-flow /health", False, f"HTTP {ff}"))

    # proxyd — any response means alive
    px = _http_status("https://127.0.0.1:4040/v1/models", tls=True)
    if px != 0:
        items.append(CheckItem("proxyd TLS", True, f"HTTP {px}"))
    else:
        items.append(CheckItem("proxyd TLS", False, "no response"))

    vault = _http_status("http://127.0.0.1:42069/")
    if vault != 0:
        items.append(CheckItem("memory-vault :42069", True, f"HTTP {vault}"))
    else:
        # vault may use different port on some hubs — skip hard fail if process ok
        items.append(
            CheckItem(
                "memory-vault :42069",
                False,
                "no response (instance.yaml port may differ)",
            )
        )

    return Wave1Report(applicable=True, items=items)


def install_script_path() -> Path:
    """Legacy path — still used for status when sanctum-config is present."""
    return INSTALL_SCRIPT


def operator_home() -> Path:
    """Home of the interactive operator (not root when under sudo)."""
    sudo_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    if sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def _package_asset(name: str) -> str:
    """Read a packaged asset (plist template) from the wheel."""
    try:
        from importlib.resources import files

        return files("sanctum_cli.data.service_user").joinpath(name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, TypeError, OSError):
        # editable / source tree fallback
        here = Path(__file__).resolve().parent / "data" / "service_user" / name
        return here.read_text(encoding="utf-8")


def materialize_assets(op_home: Path | None = None) -> Path:
    """Write packaged plists into ``~/.sanctum/launchdaemons`` (expanded).

    Idempotent. Does not require root. Returns the launchdaemons directory.
    """
    home = op_home or operator_home()
    dest = home / ".sanctum" / "launchdaemons"
    dest.mkdir(parents=True, exist_ok=True)
    scripts = home / ".sanctum" / "scripts" / "service-user"
    scripts.mkdir(parents=True, exist_ok=True)
    # marker so operators know CLI owns the greenfield path
    (scripts / "README.md").write_text(
        "# Service user (wave-1)\n\n"
        "Installed by `sanctum service-user install` (packaged assets).\n"
        "Canonical command: `sanctum service-user status|check|install`.\n",
        encoding="utf-8",
    )
    for name in (
        "haus.sanctum.proxyd.plist",
        "haus.sanctum.force-flow.plist",
        "haus.sanctum.memory-vault.plist",
    ):
        text = _package_asset(name).replace("@OPERATOR_HOME@", str(home))
        (dest / name).write_text(text, encoding="utf-8")
    return dest


def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=120,
    )


def _ensure_group_and_user() -> None:
    """Create group+user ``sanctum`` if missing. Must run as root."""
    if os.geteuid() != 0:
        raise PermissionError("root required")

    # group
    if _run(["dscl", ".", "-read", f"/Groups/{SERVICE_USERNAME}"]).returncode != 0:
        # pick free gid >= 502
        gid = 502
        existing = _run(["dscl", ".", "-list", "/Groups", "PrimaryGroupID"])
        used = {line.split()[-1] for line in existing.stdout.splitlines() if line.strip()}
        while str(gid) in used:
            gid += 1
        _run(["dscl", ".", "-create", f"/Groups/{SERVICE_USERNAME}"], check=True)
        _run(
            ["dscl", ".", "-create", f"/Groups/{SERVICE_USERNAME}", "PrimaryGroupID", str(gid)],
            check=True,
        )
        _run(
            [
                "dscl",
                ".",
                "-create",
                f"/Groups/{SERVICE_USERNAME}",
                "RealName",
                "Sanctum hive service",
            ],
            check=True,
        )

    # user
    if not service_user_exists():
        uid = 502
        existing_u = _run(["dscl", ".", "-list", "/Users", "UniqueID"])
        used_u = {line.split()[-1] for line in existing_u.stdout.splitlines() if line.strip()}
        while str(uid) in used_u:
            uid += 1
        # try sysadminctl first
        rc = _run(
            [
                "sysadminctl",
                "-addUser",
                SERVICE_USERNAME,
                "-fullName",
                "Sanctum Service",
                "-UID",
                str(uid),
                "-shell",
                "/usr/bin/false",
                "-home",
                f"/Users/{SERVICE_USERNAME}",
                "-password",
                "-",
            ]
        )
        if rc.returncode != 0 or not service_user_exists():
            gid = pwd.getpwnam(SERVICE_USERNAME).pw_gid if service_user_exists() else 502
            try:
                gid = int(
                    _run(
                        ["dscl", ".", "-read", f"/Groups/{SERVICE_USERNAME}", "PrimaryGroupID"]
                    ).stdout.split()[-1]
                )
            except (IndexError, ValueError):
                gid = 502
            home = f"/Users/{SERVICE_USERNAME}"
            _run(["dscl", ".", "-create", f"/Users/{SERVICE_USERNAME}"], check=True)
            _run(
                [
                    "dscl",
                    ".",
                    "-create",
                    f"/Users/{SERVICE_USERNAME}",
                    "UserShell",
                    "/usr/bin/false",
                ],
                check=True,
            )
            _run(
                [
                    "dscl",
                    ".",
                    "-create",
                    f"/Users/{SERVICE_USERNAME}",
                    "RealName",
                    "Sanctum Service",
                ],
                check=True,
            )
            _run(
                ["dscl", ".", "-create", f"/Users/{SERVICE_USERNAME}", "UniqueID", str(uid)],
                check=True,
            )
            _run(
                [
                    "dscl",
                    ".",
                    "-create",
                    f"/Users/{SERVICE_USERNAME}",
                    "PrimaryGroupID",
                    str(gid),
                ],
                check=True,
            )
            _run(
                [
                    "dscl",
                    ".",
                    "-create",
                    f"/Users/{SERVICE_USERNAME}",
                    "NFSHomeDirectory",
                    home,
                ],
                check=True,
            )
            _run(
                ["dscl", ".", "-create", f"/Users/{SERVICE_USERNAME}", "Password", "*"],
                check=False,
            )
            Path(home).mkdir(parents=True, exist_ok=True)
            _run(["createhomedir", "-c", "-u", SERVICE_USERNAME], check=False)

    # membership: sanctum + operator (SUDO_USER)
    op = os.environ.get("SUDO_USER") or ""
    for member in (SERVICE_USERNAME, op):
        if member:
            _run(
                ["dseditgroup", "-o", "edit", "-a", member, "-t", "user", SERVICE_USERNAME],
                check=False,
            )

    home_dir = Path(f"/Users/{SERVICE_USERNAME}")
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "logs").mkdir(parents=True, exist_ok=True)
    (home_dir / "run").mkdir(parents=True, exist_ok=True)
    try:
        pw = pwd.getpwnam(SERVICE_USERNAME)
        os.chown(home_dir, pw.pw_uid, pw.pw_gid)
        os.chown(home_dir / "logs", pw.pw_uid, pw.pw_gid)
        os.chown(home_dir / "run", pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


_SERVICE_SECRET_ALLOWLIST: tuple[str, ...] = (
    "gemini-api-key",
    "openrouter-api-key",
    "anthropic-api-key",
    "sonos-bridge.token",
    "ha-token",
)


def _acl_operator_tree(op_home: Path) -> None:
    """Least-privilege group share of non-secret operator .sanctum/.openclaw trees."""
    sanctum = op_home / ".sanctum"
    sanctum.mkdir(parents=True, exist_ok=True)
    # Non-recursive execute/traverse on operator home + .openclaw parent
    _run(["chmod", "+a", f"group:{SERVICE_USERNAME} allow execute", str(op_home)], check=False)
    openclaw = op_home / ".openclaw"
    openclaw.mkdir(parents=True, exist_ok=True)
    (openclaw / "logs").mkdir(parents=True, exist_ok=True)
    _run(["chmod", "+a", f"group:{SERVICE_USERNAME} allow execute", str(openclaw)], check=False)
    if not (openclaw / "logs").is_symlink():
        _run(["chgrp", "-R", SERVICE_USERNAME, str(openclaw / "logs")], check=False)
        _run(["chmod", "-R", "g+rwX", str(openclaw / "logs")], check=False)

    # Non-recursive group share on ~/.sanctum root + safe config metadata files
    _run(["chgrp", SERVICE_USERNAME, str(sanctum)], check=False)
    _run(["chmod", "g+rx", str(sanctum)], check=False)
    for fname in ("endpoints.env", "endpoints.json", ".node_id", "instance.yaml"):
        cfg_file = sanctum / fname
        if cfg_file.is_file() and not cfg_file.is_symlink():
            _run(["chgrp", SERVICE_USERNAME, str(cfg_file)], check=False)
            _run(["chmod", "g+r", str(cfg_file)], check=False)

    # Recursive share ONLY on safe non-secret service subtrees
    for sub in ("bin", "sanctum-proxy", "memory-vault", "lib"):
        p = sanctum / sub
        if p.is_dir() and not p.is_symlink():
            _run(["chgrp", "-R", SERVICE_USERNAME, str(p)], check=False)
            _run(["chmod", "-R", "g+rX", str(p)], check=False)
    for sub in ("state", "logs", "memory", "force-flow", "metrics"):
        p = sanctum / sub
        if p.is_dir() and not p.is_symlink():
            _run(["chgrp", "-R", SERVICE_USERNAME, str(p)], check=False)
            _run(["chmod", "-R", "g+rwX", str(p)], check=False)

    # Non-recursive allowlist for non-CA-key TLS certs
    certs = sanctum / "certs"
    if certs.is_dir() and not certs.is_symlink():
        _run(["chgrp", SERVICE_USERNAME, str(certs)], check=False)
        _run(["chmod", "g+rx", str(certs)], check=False)
        clients = certs / "clients"
        if clients.is_dir() and not clients.is_symlink():
            _run(["chgrp", SERVICE_USERNAME, str(clients)], check=False)
            _run(["chmod", "g+rx", str(clients)], check=False)
        for rel in ("ca.crt", "clients/canary.crt", "clients/canary-pkcs8.key"):
            cf = certs / rel
            if cf.is_file() and not cf.is_symlink():
                _run(["chgrp", SERVICE_USERNAME, str(cf)], check=False)
                _run(["chmod", "0640", str(cf)], check=False)

    # Traverse-only (no -R, no g+r) on secrets/ and credentials/
    for secret_dir_name in ("secrets", "credentials"):
        sd = sanctum / secret_dir_name
        if sd.exists():
            real_sd = sd.resolve()
            if real_sd.is_dir():
                _run(
                    ["chmod", "+a", f"group:{SERVICE_USERNAME} allow search", str(real_sd)],
                    check=False,
                )
    secrets_dir = sanctum / "secrets"
    if secrets_dir.exists():
        real_secrets = secrets_dir.resolve()
        for token_name in _SERVICE_SECRET_ALLOWLIST:
            tf = real_secrets / token_name
            if tf.is_file() and not tf.is_symlink():
                _run(["chgrp", SERVICE_USERNAME, str(tf)], check=False)
                _run(["chmod", "0640", str(tf)], check=False)


def _install_plists(op_home: Path) -> None:
    dest = Path("/Library/LaunchDaemons")
    op_uid = None
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            op_uid = pwd.getpwnam(sudo_user).pw_uid
        except KeyError:
            op_uid = None

    for name in (
        "haus.sanctum.proxyd.plist",
        "haus.sanctum.force-flow.plist",
        "haus.sanctum.memory-vault.plist",
    ):
        label = name.removesuffix(".plist")
        text = _package_asset(name).replace("@OPERATOR_HOME@", str(op_home))
        target = dest / name
        # backup once
        pre = dest / f"{name}.pre-wave1"
        if target.is_file() and not pre.is_file():
            pre.write_bytes(target.read_bytes())
        target.write_text(text, encoding="utf-8")
        target.chmod(0o644)
        _run(["chown", "root:wheel", str(target)], check=False)

        # bootout old system + gui
        _run(["launchctl", "bootout", f"system/{label}"], check=False)
        if op_uid is not None:
            _run(["launchctl", "bootout", f"gui/{op_uid}/{label}"], check=False)
        _run(["launchctl", "bootstrap", "system", str(target)], check=False)
        _run(["launchctl", "enable", f"system/{label}"], check=False)
        _run(["launchctl", "kickstart", "-k", f"system/{label}"], check=False)

    # disable operator-side agents that would double-run
    agents = op_home / "Library" / "LaunchAgents"
    for label in ("haus.sanctum.force-flow", "haus.sanctum.memory-vault"):
        agent = agents / f"{label}.plist"
        disabled = agents / f"{label}.plist.disabled-wave1"
        if agent.is_file() and not disabled.is_file():
            agent.rename(disabled)
            if op_uid is not None:
                _run(["launchctl", "bootout", f"gui/{op_uid}/{label}"], check=False)


def _role_file() -> Path:
    """The instance.yaml that decides the role: the operator's, also under sudo.

    ``$SANCTUM_INSTANCE_FILE`` when set. As root it usually is not (sudo resets
    the environment, and HOME may be root's), so the operator's own
    ``~/.sanctum/instance.yaml``.
    """
    if os.environ.get(config.ENV_INSTANCE_FILE) or os.geteuid() != 0:
        return config.instance_path()
    return operator_home() / ".sanctum" / "instance.yaml"


def refuse_on_a_satellite() -> None:
    """Raise ``ConfigError`` on a declared satellite: the service principal is hub-only.

    Fails closed. An existing instance.yaml must load (:func:`config.load_raw`
    raises ``ConfigError`` for one it cannot read, such as an impossible date
    anywhere in it), so nothing is installed from a file that does not load,
    whatever its role. The role then comes from :func:`config.instance_role`,
    the single role detector. Only a missing file, or a role other than
    ``satellite``, passes.
    """
    target = _role_file()
    if not target.exists():
        return
    config.load_raw(target)  # fail closed: a file that does not load raises ConfigError
    if config.instance_role(target) != "satellite":
        return
    msg = (
        "the hive service principal (user sanctum + the wave-1 proxyd / force-flow / "
        "memory-vault LaunchDaemons) is hub-only, and this machine is a satellite "
        f"(instance.role: satellite in {target})"
    )
    raise ConfigError(
        msg,
        fix="run it on the hub; if this machine is the hub, set instance.role: hub "
        "(sanctum init --role hub) and re-run",
    )


def install_wave1_as_root() -> int:
    """Full greenfield wave-1 install. Must run as root. Returns 0 on success.

    Refuses on a declared satellite (``ConfigError``) before writing anything.
    """
    refuse_on_a_satellite()
    if os.geteuid() != 0:
        raise PermissionError("install_wave1_as_root requires root")
    op_home = operator_home()
    materialize_assets(op_home)
    _ensure_group_and_user()
    _acl_operator_tree(op_home)
    _install_plists(op_home)
    return 0


def run_install(*, dry_run: bool = False) -> int:
    """Greenfield install — no pre-synced sanctum-config required.

    Materializes packaged plists, creates user ``sanctum``, installs
    LaunchDaemons, fixes ACLs. Prompts for admin via ``sudo`` when not root.
    Refuses on a declared satellite (``ConfigError``, see
    :func:`refuse_on_a_satellite`) before it writes or asks for anything.

    Returns the subprocess / install exit code.
    """
    refuse_on_a_satellite()
    if dry_run:
        # Validate packaged assets load cleanly without writing anything to disk.
        for name in (
            "haus.sanctum.proxyd.plist",
            "haus.sanctum.force-flow.plist",
            "haus.sanctum.memory-vault.plist",
        ):
            _ = _package_asset(name)
        return 0

    if os.geteuid() == 0:
        return install_wave1_as_root()

    # Re-enter as root with isolated Python (-I) and sanitized sudo env (no -E),
    # through main(), so a refusal there is one clean error, not a traceback.
    import sys

    cmd = ["sudo", "--", sys.executable, "-I", "-m", "sanctum_cli.service_user", "install"]
    return subprocess.call(cmd)


def main() -> None:
    """``python -m sanctum_cli.service_user install`` (usually under sudo).

    A refusal (a satellite, an unreadable instance.yaml) prints ``error:`` /
    ``fix:`` on stderr and exits with the error's code (5), never a traceback.
    """
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "install":
        try:
            rc = install_wave1_as_root() if os.geteuid() == 0 else run_install()
        except SanctumError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            if exc.fix:
                print(f"fix: {exc.fix}", file=sys.stderr)
            raise SystemExit(int(exc.exit_code)) from exc
        raise SystemExit(rc)
    raise SystemExit("usage: python -m sanctum_cli.service_user install")


if __name__ == "__main__":
    main()
