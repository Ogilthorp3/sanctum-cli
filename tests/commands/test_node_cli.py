"""``sanctum node`` CLI surface — productized satellite onboarding.

Drives scan / bootstrap-script / adopt end-to-end through Typer's ``CliRunner``
while faking every impure seam — the local runner (``tailscale status`` /
``arp -an`` / ``ssh-keygen``), the TCP probe, and the SSH runner — so no live
network, no keygen, and no SSH is ever touched.

The contract the commands MUST honor:

* ``scan`` is READ-ONLY: it never opens an SSH session, only sockets.
* ``bootstrap-script`` embeds the console's automation pubkey (minting the pair
  on first run via the seam) and preserves the field-validated script semantics
  exactly — remote-login on, guarded authorized_keys append with 700/600,
  headless pmset/systemsetup hardening, sudoers ONLY behind ``--sudo-nopasswd``.
* ``adopt`` is honest-verify: every scorecard ✓ comes from a scripted "wire"
  answer; a failing required layer (L1-L4) registers NOTHING and exits non-zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import node as node_cmd

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

# A shaped-like-the-real-thing automation pubkey (blob is fake but base64-ish).
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeBlobForNodeTests sanctum-automation-console-2026-07-18"


# ── pure parsers ──────────────────────────────────────────────────────────────


TAILSCALE_STATUS = """\
100.101.102.103 chalet-mini          bert@        macOS   -
100.101.102.104 attic-mini           bert@        macOS   offline
100.101.102.105 berts-iphone         bert@        iOS     active; direct, tx 1 rx 1
# Health check:
#     - some advisory line
"""


def test_parse_tailscale_status_filters_macos_and_marks_offline() -> None:
    """Only macOS peers survive; the offline one is kept but marked offline."""
    peers = node_cmd.parse_tailscale_status(TAILSCALE_STATUS)
    assert [(p.name, p.online) for p in peers] == [
        ("chalet-mini", True),
        ("attic-mini", False),
    ]
    assert all(p.source == "tailnet" for p in peers)
    # Ports are NOT probed by the parser (the caller owns the bounded pass).
    assert all(p.ssh_open is None and p.screen_open is None for p in peers)


ARP_AN = """\
? (10.0.0.3) at a4:83:e7:11:22:33 on en0 ifscope [ethernet]
? (10.0.0.77) at (incomplete) on en0 ifscope [ethernet]
? (10.0.0.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
? (10.0.0.9) at de:ad:be:ef:0:9 on en0 ifscope [ethernet]
? (10.0.0.3) at a4:83:e7:11:22:33 on en0 ifscope [ethernet]
"""  # ip-allow: parser fixture for arp -an junk-filtering; addresses are inert test data


def test_parse_arp_hosts_skips_junk_and_dedupes() -> None:
    """Incomplete, broadcast, and multicast rows drop; survivors are deduped."""
    assert node_cmd.parse_arp_hosts(ARP_AN) == ["10.0.0.3", "10.0.0.9"]  # ip-allow: asserts the fixture above


def test_parse_pmset_custom_last_section_wins() -> None:
    """Section headers drop out; a two-section (laptop) read keeps the last values."""
    text = (
        "Battery Power:\n sleep\t10\n womp\t0\n"
        "AC Power:\n sleep\t0\n disksleep\t0\n powernap\t0\n womp\t1\n"
    )
    values = node_cmd.parse_pmset_custom(text)
    assert values["sleep"] == "0"
    assert values["womp"] == "1"
    assert values["disksleep"] == "0"


# ── bootstrap-script: the field-validated semantics are the contract ──────────


def test_render_bootstrap_script_field_semantics() -> None:
    """The generated script preserves the chalet-validated procedure exactly."""
    script = node_cmd.render_bootstrap_script(PUBKEY, sudo_nopasswd=False)
    # a. remote login on
    assert "sudo systemsetup -setremotelogin on" in script
    # b. idempotent authorized_keys append: guard BEFORE append, 700/600 perms.
    assert 'chmod 700 "$HOME/.ssh"' in script
    assert 'chmod 600 "$HOME/.ssh/authorized_keys"' in script
    guard = 'grep -qF "$PUBKEY" "$HOME/.ssh/authorized_keys" || printf'
    assert guard in script
    # c. headless reliability, verbatim.
    assert "sudo systemsetup -setrestartpowerfailure on" in script
    assert "sudo systemsetup -setcomputersleep Never" in script
    assert "sudo pmset -a sleep 0 disksleep 0 powernap 0 womp 1" in script
    # The console's pubkey is embedded for the append.
    assert f"PUBKEY='{PUBKEY}'" in script
    # d. NO sudoers content without --sudo-nopasswd.
    assert "sudoers.d" not in script
    assert "NOPASSWD" not in script
    # One-run script hygiene.
    assert script.startswith("#!/bin/bash")
    assert "set -euo pipefail" in script


def test_render_bootstrap_script_sudo_nopasswd_block() -> None:
    """--sudo-nopasswd adds the sudoers drop-in with the remove-on-invalid guard."""
    script = node_cmd.render_bootstrap_script(PUBKEY, sudo_nopasswd=True)
    assert "/etc/sudoers.d/sanctum-automation" in script
    assert "NOPASSWD: ALL" in script
    assert "sudo chmod 0440 /etc/sudoers.d/sanctum-automation" in script
    # visudo validates the drop-in; an invalid entry is REMOVED (never brick sudo).
    assert "sudo visudo -c -f /etc/sudoers.d/sanctum-automation" in script
    assert "sudo rm -f /etc/sudoers.d/sanctum-automation" in script


def test_render_bootstrap_script_refuses_quoted_pubkey() -> None:
    """A quote in the pubkey would break the single-quoted embed — refuse loudly."""
    from sanctum_cli.errors import LocalError

    with pytest.raises(LocalError):
        node_cmd.render_bootstrap_script("ssh-ed25519 AAAA it's-corrupt", sudo_nopasswd=False)


def test_bootstrap_script_stdout_is_byte_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default output is the raw script on stdout (pipe/redirect clean)."""
    monkeypatch.setattr(node_cmd, "_ensure_automation_pubkey", lambda: PUBKEY)
    result = runner.invoke(app, ["node", "bootstrap-script"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("#!/bin/bash")
    assert PUBKEY in result.stdout


def test_bootstrap_script_out_writes_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--out writes the script 0755 and prints the hand-off guidance."""
    monkeypatch.setattr(node_cmd, "_ensure_automation_pubkey", lambda: PUBKEY)
    target = tmp_path / "bootstrap.sh"
    result = runner.invoke(app, ["node", "bootstrap-script", "--out", str(target)])
    assert result.exit_code == 0, result.stdout
    assert target.read_text(encoding="utf-8").startswith("#!/bin/bash")
    assert target.stat().st_mode & 0o111  # executable
    assert "adopt" in result.stdout  # the next-step hint


def test_bootstrap_script_mints_key_pair_on_first_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``.pub`` on disk → the keygen seam fires once and its key is embedded."""
    priv = tmp_path / "sanctum_automation"
    monkeypatch.setattr(node_cmd, "_automation_key_path", lambda: priv)
    calls: list[Path] = []

    def fake_keygen(path: Path) -> None:
        calls.append(path)
        path.with_suffix(".pub").write_text(PUBKEY + "\n", encoding="utf-8")

    monkeypatch.setattr(node_cmd, "_generate_automation_key", fake_keygen)
    result = runner.invoke(app, ["node", "bootstrap-script"])
    assert result.exit_code == 0, result.stdout
    assert calls == [priv]
    assert PUBKEY in result.stdout
    # Second run: the pair exists, the seam must NOT fire again.
    result = runner.invoke(app, ["node", "bootstrap-script"])
    assert result.exit_code == 0
    assert calls == [priv]


# ── scan: read-only discovery over faked seams ────────────────────────────────


def _wire_scan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_ports: set[tuple[str, int]],
    tailscale: str | None = TAILSCALE_STATUS,
    arp: str = ARP_AN,
) -> list[list[str]]:
    """Point ``node scan`` at scripted seams; return the local-runner call log."""
    calls: list[list[str]] = []

    def fake_run_local(argv: list[str], *, timeout: int = 15) -> tuple[int, str]:
        calls.append(argv)
        if "status" in argv:
            return (0, tailscale or "")
        if argv[0] == "arp":
            return (0, arp)
        raise AssertionError(f"unexpected local command: {argv}")

    monkeypatch.setattr(node_cmd, "_run_local", fake_run_local)
    monkeypatch.setattr(
        node_cmd, "_tailscale_bin", lambda: None if tailscale is None else "tailscale"
    )
    monkeypatch.setattr(
        node_cmd,
        "_probe_tcp",
        lambda host, port, timeout=0.0: (host, port) in open_ports,
    )
    # READ-ONLY contract: scan must never open an SSH session.
    def no_ssh(*_a: object, **_k: object) -> tuple[int, str]:
        raise AssertionError("node scan opened an SSH session — it must be read-only")

    monkeypatch.setattr(node_cmd, "_run_ssh", no_ssh)
    return calls


def test_node_scan_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSH-open → adoptable now; closed → needs bootstrap; offline peer says so."""
    _wire_scan(
        monkeypatch,
        open_ports={
            ("100.101.102.103", 22),  # the online tailnet Mac answers SSH
            ("10.0.0.9", 5900),  # ip-allow: fixture address from ARP_AN — screen-sharing only
        },
    )
    result = runner.invoke(app, ["node", "scan"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "chalet-mini" in out
    assert "adoptable now" in out
    assert "needs bootstrap" in out
    assert "offline" in out  # the attic-mini peer is not mislabeled


def test_node_scan_offline_peer_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An offline tailnet peer gets no socket probe (it cannot answer)."""
    probed: list[tuple[str, int]] = []

    _wire_scan(monkeypatch, open_ports=set())

    def recording_probe(host: str, port: int, timeout: float = 0.0) -> bool:
        probed.append((host, port))
        return False

    monkeypatch.setattr(node_cmd, "_probe_tcp", recording_probe)
    result = runner.invoke(app, ["node", "scan"])
    assert result.exit_code == 0, result.stdout
    assert ("100.101.102.104", 22) not in probed  # the offline peer


def test_node_scan_without_tailscale_still_lists_lan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tailscale binary → the tailnet source is skipped with a note, LAN still scans."""
    _wire_scan(monkeypatch, open_ports=set(), tailscale=None)
    result = runner.invoke(app, ["node", "scan"])
    assert result.exit_code == 0, result.stdout
    assert "no tailscale binary" in result.stdout
    assert "10.0.0.3" in result.stdout  # ip-allow: fixture address from ARP_AN


def test_node_scan_nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty tailnet + empty ARP cache → a calm note, exit 0."""
    _wire_scan(monkeypatch, open_ports=set(), tailscale="", arp="")
    result = runner.invoke(app, ["node", "scan"])
    assert result.exit_code == 0, result.stdout
    assert "nothing discovered" in result.stdout


# ── adopt: honest-verify scorecard over a scripted wire ───────────────────────


HEALTHY_PMSET = " System-wide power settings:\n sleep\t0\n disksleep\t0\n powernap\t0\n womp\t1\n"


class ScriptedSsh:
    """Answers each remote command from a substring→(rc, stdout) script.

    Raises on an unscripted command so a probe the test didn't anticipate
    surfaces as a loud failure, not a silent (255, "").
    """

    def __init__(self, script: dict[str, tuple[int, str]]) -> None:
        self.script = dict(script)
        self.calls: list[str] = []

    def __call__(
        self, host: str, user: str, command: str, *, timeout: int = 0
    ) -> tuple[int, str]:
        self.calls.append(command)
        for needle, answer in self.script.items():
            if needle in command:
                return answer
        raise AssertionError(f"unscripted ssh command: {command!r}")


def _healthy_script(user: str = "bert") -> dict[str, tuple[int, str]]:
    """The wire answers of a satellite whose bootstrap ran cleanly."""
    return {
        "whoami": (0, f"{user}\n"),
        "hostname -s": (0, "chalet-mini|15.5|arm64"),
        "stat -f%Lp": (0, "700\n600\nkey-present\n"),
        "pmset -g custom": (0, HEALTHY_PMSET),
        "sudo -n true": (1, ""),  # no passwordless sudo — the default bootstrap
    }


def _wire_adopt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh: ScriptedSsh,
    *,
    reachable: bool = True,
) -> Path:
    """Wire adopt's seams: key pair on disk, instance.yaml in tmp, scripted wire."""
    priv = tmp_path / "sanctum_automation"
    priv.write_text("private", encoding="utf-8")
    priv.with_suffix(".pub").write_text(PUBKEY + "\n", encoding="utf-8")
    monkeypatch.setattr(node_cmd, "_automation_key_path", lambda: priv)
    monkeypatch.setattr(node_cmd, "_probe_tcp", lambda *_a, **_k: reachable)
    monkeypatch.setattr(node_cmd, "_run_ssh", ssh)
    instance = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(instance))
    return instance


def test_node_adopt_happy_path_registers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cleanly bootstrapped satellite passes L1-L6 and lands in instance.yaml."""
    ssh = ScriptedSsh(_healthy_script())
    instance = _wire_adopt(monkeypatch, tmp_path, ssh)
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code == 0, result.stdout
    assert "ADOPTED" in result.stdout
    # The registration is REAL: read the file, not the command's own prose.
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    node = data["nodes"]["chalet-mini"]
    assert node["host"] == "sat.example"
    assert node["user"] == "bert"
    assert node["adopted"]  # dated
    # The key-hygiene probe grepped for OUR blob, not just any key.
    assert any("AAAAC3NzaC1lZDI1NTE5AAAAIFakeBlobForNodeTests" in c for c in ssh.calls)


def test_node_adopt_name_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--name wins over the hostname-derived default."""
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(_healthy_script()))
    result = runner.invoke(
        app,
        ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0", "--name", "chalet"],
    )
    assert result.exit_code == 0, result.stdout
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    assert "chalet" in data["nodes"]
    assert "chalet-mini" not in data["nodes"]


def test_node_adopt_auth_failure_registers_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """L2 FAIL (key rejected) → L3-L6 SKIP, nothing written, exit non-zero."""
    script = _healthy_script()
    script["whoami"] = (255, "")
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(script))
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code != 0
    assert "NOT ADOPTED" in result.stdout
    assert not instance.exists()  # zero writes on a failed adoption


def test_node_adopt_wrong_user_fails_honestly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Landing as a DIFFERENT user than expected is an L2 failure, not a pass."""
    script = _healthy_script()
    script["whoami"] = (0, "somebodyelse\n")
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(script))
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code != 0
    assert not instance.exists()


def test_node_adopt_headless_drift_warns_but_adopts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """L5 divergence (sleep enabled) WARNs with the fix — but the node still adopts."""
    script = _healthy_script()
    script["pmset -g custom"] = (0, " sleep\t10\n disksleep\t0\n powernap\t0\n womp\t1\n")
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(script))
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code == 0, result.stdout
    assert "with warnings" in result.stdout
    assert "sleep=10" in result.stdout  # the drift is named, with the wanted value
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    assert "chalet-mini" in data["nodes"]


def test_node_adopt_key_hygiene_failure_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Our key absent from authorized_keys → L4 FAIL → no registration."""
    script = _healthy_script()
    script["stat -f%Lp"] = (0, "700\n600\nkey-absent\n")
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(script))
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code != 0
    assert "authorized_keys" in result.stdout
    assert not instance.exists()


def test_node_adopt_unreachable_points_at_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """:22 never answers within --wait → non-zero with the bootstrap hint."""
    _wire_adopt(monkeypatch, tmp_path, ScriptedSsh({}), reachable=False)
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code != 0
    combined = result.stdout + result.stderr
    assert "bootstrap" in combined


def test_node_adopt_missing_key_pair_names_the_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No automation pair on the console → refuse with the bootstrap-script fix."""
    monkeypatch.setattr(node_cmd, "_automation_key_path", lambda: tmp_path / "absent")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "instance.yaml"))
    result = runner.invoke(app, ["node", "adopt", "sat.example", "--wait", "0"])
    assert result.exit_code != 0
    combined = result.stdout + result.stderr
    assert "bootstrap-script" in combined


def test_node_adopt_sudo_available_verifies_power_failure_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With passwordless sudo the restart-on-power-failure read is verified too."""
    script = _healthy_script()
    script["sudo -n true"] = (0, "")
    script["getrestartpowerfailure"] = (0, "Restart After Power Failure: On\n")
    ssh = ScriptedSsh(script)
    _wire_adopt(monkeypatch, tmp_path, ssh)
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "bert", "--wait", "0"]
    )
    assert result.exit_code == 0, result.stdout
    assert "unverified" not in result.stdout
    assert any("getrestartpowerfailure" in c for c in ssh.calls)


# ── register_node: read-modify-write preserves siblings ───────────────────────


def test_register_node_preserves_existing_blocks(tmp_path: Path) -> None:
    """Registering never clobbers other instance.yaml blocks; .bak is written."""
    target = tmp_path / "instance.yaml"
    target.write_text(
        "instance:\n  name: Test Sanctum\n  slug: test-sanctum\nnodes:\n  older:\n    host: old.example\n",
        encoding="utf-8",
    )
    node_cmd.register_node("chalet", host="sat.example", user="bert", path=target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["instance"]["slug"] == "test-sanctum"  # sibling preserved
    assert data["nodes"]["older"]["host"] == "old.example"  # existing node preserved
    assert data["nodes"]["chalet"]["host"] == "sat.example"
    assert (tmp_path / "instance.yaml.bak").exists()
