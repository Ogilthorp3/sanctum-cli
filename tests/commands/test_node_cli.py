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

from sanctum_cli import config
from sanctum_cli.cli import app
from sanctum_cli.commands import node as node_cmd
from sanctum_cli.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

# A shaped-like-the-real-thing automation pubkey (44-char valid base64 blob).
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeBlobForNodeTest sanctum-automation-console-2026-07-18"


# ── pure parsers ──────────────────────────────────────────────────────────────


TAILSCALE_STATUS = """\
100.64.0.13 studio-mini          operator@    macOS   -
100.64.0.14 edge-mini            operator@    macOS   offline
100.64.0.15 alice-phone          operator@    iOS     active; direct, tx 1 rx 1
# Health check:
#     - some advisory line
"""


def test_parse_tailscale_status_filters_macos_and_marks_offline() -> None:
    """Only macOS peers survive; the offline one is kept but marked offline."""
    peers = node_cmd.parse_tailscale_status(TAILSCALE_STATUS)
    assert [(p.name, p.online) for p in peers] == [
        ("studio-mini", True),
        ("edge-mini", False),
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
    """The generated script preserves the field-validated procedure exactly."""
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
    """--sudo-nopasswd adds a scoped, mktemp/visudo-validated sudoers drop-in."""
    script = node_cmd.render_bootstrap_script(PUBKEY, sudo_nopasswd=True)
    assert "/etc/sudoers.d/sanctum-automation" in script
    assert "NOPASSWD: ALL" not in script
    for expected in (
        "/usr/bin/true",
        "/usr/sbin/systemsetup",
        "/usr/bin/pmset",
        "/usr/sbin/networksetup",
        "/usr/sbin/ipconfig",
    ):
        assert expected in script
    assert "sudo mktemp /etc/sudoers.d/.sanctum-automation.XXXXXX" in script
    assert 'sudo chmod 0440 "$TMP_SUDO"' in script
    # visudo validates the staged drop-in before atomic mv into /etc/sudoers.d/.
    assert 'sudo visudo -c -f "$TMP_SUDO"' in script
    assert 'sudo rm -f "$TMP_SUDO"' in script
    assert 'sudo mv -f "$TMP_SUDO" /etc/sudoers.d/sanctum-automation' in script


def test_render_bootstrap_script_refuses_quoted_pubkey() -> None:
    """A quote in the pubkey would break the single-quoted embed — refuse loudly."""
    from sanctum_cli.errors import LocalError

    with pytest.raises(LocalError):
        node_cmd.render_bootstrap_script("ssh-ed25519 AAAA it's-corrupt", sudo_nopasswd=False)


def test_bootstrap_script_stdout_is_byte_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--format script` output is the raw script on stdout (pipe/redirect clean)."""
    monkeypatch.setattr(node_cmd, "_ensure_automation_pubkey", lambda: PUBKEY)
    result = runner.invoke(app, ["node", "bootstrap-script", "--format", "script"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("#!/bin/bash")
    assert PUBKEY in result.stdout


def test_bootstrap_script_out_writes_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--format script --out` writes the script 0755 and prints the hand-off guidance."""
    monkeypatch.setattr(node_cmd, "_ensure_automation_pubkey", lambda: PUBKEY)
    target = tmp_path / "bootstrap.sh"
    result = runner.invoke(
        app, ["node", "bootstrap-script", "--format", "script", "--out", str(target)]
    )
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
    result = runner.invoke(app, ["node", "bootstrap-script", "--format", "script"])
    assert result.exit_code == 0, result.stdout
    assert calls == [priv]
    assert PUBKEY in result.stdout
    # Second run: the pair exists, the seam must NOT fire again.
    result = runner.invoke(app, ["node", "bootstrap-script", "--format", "script"])
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
            ("100.64.0.13", 22),  # the online tailnet Mac answers SSH
            ("10.0.0.9", 5900),  # ip-allow: fixture address from ARP_AN — screen-sharing only
        },
    )
    result = runner.invoke(app, ["node", "scan"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "studio-mini" in out
    assert "adoptable now" in out
    assert "needs bootstrap" in out
    assert "offline" in out  # the edge-mini peer is not mislabeled


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
    assert ("100.64.0.14", 22) not in probed  # the offline peer


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


def _healthy_script(user: str = "operator") -> dict[str, tuple[int, str]]:
    """The wire answers of a satellite whose bootstrap ran cleanly."""
    return {
        "whoami": (0, f"{user}\n"),
        "hostname -s": (0, "studio-mini|15.5|arm64"),
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
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
    )
    assert result.exit_code == 0, result.stdout
    assert "ADOPTED" in result.stdout
    # The registration is REAL: read the file, not the command's own prose.
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    node = data["nodes"]["studio-mini"]
    assert node["host"] == "sat.example"
    assert node["ssh_user"] == "operator"
    assert node.get("user") is None  # legacy field no longer written
    assert node["type"] == "satellite"
    assert node["tier"] == "edge"
    assert node["adopted"]  # dated
    # The key-hygiene probe grepped for OUR blob, not just any key.
    assert any("AAAAC3NzaC1lZDI1NTE5AAAAIFakeBlobForNodeTest" in c for c in ssh.calls)


def test_node_adopt_fails_l6_on_an_unreadable_instance_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scorecard names the bad value; the file is not rewritten; nothing is adopted."""
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(_healthy_script()))
    before = "instance:\n  name: Box\n  slug: box\nsince: 2026-02-30\n"
    instance.write_text(before, encoding="utf-8")
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    out = " ".join(result.stdout.split())
    assert "2026-02-30 on line 4 is not a valid date" in out
    assert "NOT ADOPTED" in out
    assert instance.read_text(encoding="utf-8") == before


def test_node_adopt_name_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--name wins over the hostname-derived default."""
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(_healthy_script()))
    result = runner.invoke(
        app,
        ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0", "--name", "studio"],
    )
    assert result.exit_code == 0, result.stdout
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    assert "studio" in data["nodes"]
    assert "studio-mini" not in data["nodes"]


def test_node_adopt_auth_failure_registers_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """L2 FAIL (key rejected) → L3-L6 SKIP, nothing written, exit non-zero."""
    script = _healthy_script()
    script["whoami"] = (255, "")
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(script))
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
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
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
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
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
    )
    assert result.exit_code == 0, result.stdout
    assert "with warnings" in result.stdout
    assert "sleep=10" in result.stdout  # the drift is named, with the wanted value
    data = yaml.safe_load(instance.read_text(encoding="utf-8"))
    assert "studio-mini" in data["nodes"]


def test_node_adopt_key_hygiene_failure_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Our key absent from authorized_keys → L4 FAIL → no registration."""
    script = _healthy_script()
    script["stat -f%Lp"] = (0, "700\n600\nkey-absent\n")
    instance = _wire_adopt(monkeypatch, tmp_path, ScriptedSsh(script))
    result = runner.invoke(
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
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
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
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
        app, ["node", "adopt", "sat.example", "--user", "operator", "--wait", "0"]
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
    node_cmd.register_node("studio", host="sat.example", user="operator", path=target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["instance"]["slug"] == "test-sanctum"  # sibling preserved
    assert data["nodes"]["older"]["host"] == "old.example"  # existing node preserved
    studio = data["nodes"]["studio"]
    assert studio["host"] == "sat.example"
    assert studio["ssh_user"] == "operator"
    assert studio["type"] == "satellite"
    assert studio["tier"] == "edge"
    assert "tailscale_name" not in studio  # sat.example is a FQDN, not a TS stem
    assert (tmp_path / "instance.yaml.bak").exists()


def test_register_node_sets_tailscale_name_for_simple_host(tmp_path: Path) -> None:
    """MagicDNS stems become tailscale_name so resolve can find them."""
    target = tmp_path / "instance.yaml"
    node_cmd.register_node("studio", host="studio", user="operator", path=target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["nodes"]["studio"]["tailscale_name"] == "studio"
    assert data["nodes"]["studio"]["ssh_user"] == "operator"


def test_ssh_user_of_accepts_legacy_user_key() -> None:
    assert node_cmd.ssh_user_of({"user": "operator"}) == "operator"
    assert node_cmd.ssh_user_of({"ssh_user": "neo", "user": "old"}) == "neo"


def test_looks_like_tailscale_name() -> None:
    assert node_cmd.looks_like_tailscale_name("home") is True
    assert node_cmd.looks_like_tailscale_name("studio") is True
    assert node_cmd.looks_like_tailscale_name("10.0.0.10") is False
    assert node_cmd.looks_like_tailscale_name("sat.example") is False


def test_parse_tailscale_peer_ips_maps_stems() -> None:
    raw = """
    {
      "Self": {"HostName": "alice-mbp", "DNSName": "alice-mbp.tailnet-abc.ts.net.",
               "TailscaleIPs": ["100.64.0.11"]},
      "Peer": {
        "x": {"HostName": "home", "DNSName": "home.tailnet-abc.ts.net.",
              "Online": true, "TailscaleIPs": ["100.64.0.18"]},
        "y": {"HostName": "studio", "DNSName": "studio.tailnet-abc.ts.net.",
              "Online": false, "TailscaleIPs": ["100.64.0.17"]}
      }
    }
    """
    peers = node_cmd.parse_tailscale_peer_ips(raw)
    assert peers["home"]["online"] is True
    assert peers["home"]["ips"][0] == "100.64.0.18"
    assert peers["studio"]["online"] is False


def test_resolve_node_address_prefers_online_tailscale() -> None:
    nodes = {
        "studio": {
            "type": "satellite",
            "tailscale_name": "studio",
            "host": "studio",
            "ssh_user": "operator",
        }
    }
    peers = {"studio": {"online": True, "ips": ["100.64.0.17"]}}
    r = node_cmd.resolve_node_address("studio", nodes, peers)
    assert r["ok"] is True
    assert r["address"] == "100.64.0.17"
    assert r["source"].startswith("tailscale:")


def test_resolve_node_address_offline_fail_closed() -> None:
    nodes = {"studio": {"tailscale_name": "studio", "ssh_user": "operator"}}
    peers = {"studio": {"online": False, "ips": ["100.64.0.17"]}}
    r = node_cmd.resolve_node_address("studio", nodes, peers)
    assert r["ok"] is False
    assert r["online"] is False


def test_resolve_node_address_lan_host_fallback() -> None:
    nodes = {"printer": {"host": "10.0.0.50", "ssh_user": "pi"}}
    r = node_cmd.resolve_node_address("printer", nodes, {})
    assert r["ok"] is True
    assert r["address"] == "10.0.0.50"
    assert r["source"] == "host"


def test_node_list_and_get(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "nodes:\n"
        "  home:\n"
        "    type: hub\n"
        "    tier: primary\n"
        "    site: home\n"
        "    ssh_user: operator\n"
        "    tailscale_name: home\n"
        "  studio:\n"
        "    type: satellite\n"
        "    tier: edge\n"
        "    user: legacy\n"
        "    host: studio\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    monkeypatch.setattr(config, "instance_path", lambda: inst)
    result = runner.invoke(app, ["node", "list"])
    assert result.exit_code == 0, result.stdout
    assert "home" in result.stdout and "hub" in result.stdout
    assert "studio" in result.stdout
    got = runner.invoke(app, ["node", "get", "home"])
    assert got.exit_code == 0
    assert "primary" in got.stdout


def test_node_list_reports_an_impossible_date_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One clean error and fix (exit 5), never a traceback, for a value PyYAML cannot build."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("nodes:\n  home:\n    type: hub\nsince: 2026-02-30\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    result = runner.invoke(app, ["node", "list"])
    assert result.exit_code == 5
    assert isinstance(result.exception, SystemExit)
    assert result.stderr.splitlines() == [
        f"error: cannot read {inst}: 2026-02-30 on line 4 is not a valid date "
        "(day is out of range for month)",
        "fix: correct the value on line 4, or quote it to keep it as text, then re-run",
    ]


def test_node_names_notes_an_unreadable_instance_yaml_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live roster is optional: the cheat sheet prints, then a note says why no roster."""
    inst = tmp_path / "[site]" / "instance.yaml"  # a bracketed path: the note is text, not markup
    inst.parent.mkdir()
    inst.write_text("nodes:\n  home:\n    type: hub\nsince: 2026-02-30\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    result = runner.invoke(app, ["node", "names"])
    assert result.exit_code == 0, result.output
    assert "Hive naming" in result.stdout
    assert "    aliases: [alice-mbp]   # until Tailscale machine renamed" in result.stdout
    assert "Live roster stems" not in result.stdout
    assert result.stdout.splitlines()[-1] == (
        f"live roster not shown: cannot read {inst}: 2026-02-30 on line 4 is not a valid date "
        "(day is out of range for month)"
    )
    assert result.stderr == ""


def test_node_names_prints_the_live_roster_as_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        'nodes:\n  home:\n    type: hub\n    aliases: [old-home]\n  "[bold]x":\n    type: satellite\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    result = runner.invoke(app, ["node", "names"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-3:] == [
        "Live roster stems",
        "  [bold]x → [bold]x",
        "  home → home  aliases=['old-home']",
    ]


@pytest.mark.parametrize("aliases", ["5", "true", "old-home", "{old: home}"])
def test_node_names_treats_aliases_that_are_not_a_list_as_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, aliases: str
) -> None:
    """A number crashed the command; a single name was listed one letter per alias."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(f"nodes:\n  home:\n    type: hub\n    aliases: {aliases}\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    result = runner.invoke(app, ["node", "names"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-2:] == ["Live roster stems", "  home → home"]


@pytest.mark.parametrize("argv", [["node", "ensure-hub"], ["node", "declare-peer", "montreal"]])
def test_roster_writes_leave_an_unreadable_instance_yaml_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    inst = tmp_path / "instance.yaml"
    before = "instance:\n  name: Box\n  slug: box\nflag: !!bool maybe\n"
    inst.write_text(before, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    result = runner.invoke(app, argv)
    assert result.exit_code == 5
    assert isinstance(result.exception, SystemExit)
    assert "!!bool maybe on line 4 is not a valid true/false value" in result.stderr
    assert inst.read_text(encoding="utf-8") == before
    assert not (tmp_path / "instance.yaml.bak").exists()


def test_register_node_refuses_to_rewrite_a_file_with_a_list_at_the_top(tmp_path: Path) -> None:
    """It used to replace such a file with just the new ``nodes:`` block."""
    target = tmp_path / "instance.yaml"
    target.write_text("- keep\n- me\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="top-level YAML must be a mapping"):
        node_cmd.register_node("studio", host="studio", user="operator", path=target)
    assert target.read_text(encoding="utf-8") == "- keep\n- me\n"


def test_node_whoami(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "nodes:\n  home:\n    type: hub\n    tier: primary\n    site: home\n    ssh_user: operator\n",
        encoding="utf-8",
    )
    nid = tmp_path / ".node_id"
    nid.write_text("home\n", encoding="utf-8")
    monkeypatch.setattr(config, "instance_path", lambda: inst)
    monkeypatch.setattr(node_cmd, "_node_id_path", lambda: nid)
    result = runner.invoke(app, ["node", "whoami"])
    assert result.exit_code == 0, result.stdout
    assert "home" in result.stdout
    assert "primary" in result.stdout


def test_node_route_and_offers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "nodes:\n"
        "  home:\n"
        "    type: hub\n"
        "    tier: primary\n"
        "    tailscale_name: home\n"
        "    capabilities: [vault_authority, inference]\n"
        "  studio:\n"
        "    type: satellite\n"
        "    tier: edge\n"
        "    tailscale_name: studio\n"
        "    capabilities: [local_inference]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "instance_path", lambda: inst)
    monkeypatch.setattr(node_cmd, "read_node_id", lambda: "studio")
    monkeypatch.setattr(
        node_cmd,
        "_tailscale_status_json",
        lambda: (
            '{"Self":{},"Peer":{'
            '"a":{"HostName":"home","DNSName":"home.t.ts.net.","Online":true,'
            '"TailscaleIPs":["100.64.0.21"]},'
            '"b":{"HostName":"studio","DNSName":"studio.t.ts.net.","Online":true,'
            '"TailscaleIPs":["100.64.0.22"]}}}'
        ),
    )
    offers = runner.invoke(app, ["node", "offers"])
    assert offers.exit_code == 0
    assert "vault_authority" in offers.stdout
    can = runner.invoke(app, ["node", "can", "vault"])
    assert can.exit_code == 0 and "home" in can.stdout
    route = runner.invoke(app, ["node", "route", "local_inference", "--prefer", "local"])
    assert route.exit_code == 0, route.stdout
    assert "studio" in route.stdout
    val = runner.invoke(app, ["node", "validate"])
    assert val.exit_code == 0 and "roster ok" in val.stdout


def test_node_resolve_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "nodes:\n  home:\n    type: hub\n    tailscale_name: home\n    ssh_user: operator\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "instance_path", lambda: inst)
    monkeypatch.setattr(
        node_cmd,
        "_tailscale_status_json",
        lambda: (
            '{"Self":{},"Peer":{"a":{"HostName":"home",'
            '"DNSName":"home.tailnet-abc.ts.net.","Online":true,'
            '"TailscaleIPs":["100.64.0.18"]}}}'
        ),
    )
    result = runner.invoke(app, ["node", "resolve", "home"])
    assert result.exit_code == 0, result.stdout
    assert "100.64.0.18" in result.stdout
    assert "operator@100.64.0.18" in result.stdout


# ── pairing code: stable, key-bound, 6 digits ─────────────────────────────────

_OTHER_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDifferentBlobHereXY sanctum-automation-console-2026-07-18"


def test_pairing_code_is_six_digits() -> None:
    code = node_cmd.pairing_code(PUBKEY)
    assert len(code) == 6
    assert code.isdigit()


def test_pairing_code_is_stable_for_a_key() -> None:
    """Same key → same code (so the console can show it and a UI can verify it)."""
    assert node_cmd.pairing_code(PUBKEY) == node_cmd.pairing_code(PUBKEY)


def test_pairing_code_differs_across_keys() -> None:
    assert node_cmd.pairing_code(PUBKEY) != node_cmd.pairing_code(_OTHER_PUBKEY)


# ── postinstall script: the pkg's root-run bootstrap ──────────────────────────

import re as _re  # noqa: E402 — test-local helper import

_DOTTED_QUAD = _re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def test_render_postinstall_field_semantics() -> None:
    """The pkg postinstall validates $CU/$HD, rejects symlinks, and drops privileges via sudo -u."""
    code = node_cmd.pairing_code(PUBKEY)
    script = node_cmd.render_postinstall(PUBKEY, sudo_nopasswd=False, code=code)
    # Runs as root via Installer — validates non-root console user and $CU-owned home.
    assert "stat -f%Su /dev/console" in script
    assert 'dscl . -read /Users/"$CU" NFSHomeDirectory' in script
    assert 'stat -f%Su "$HD"' in script
    assert '[ -L "$HD/.ssh" ] || [ -L "$HD/.ssh/authorized_keys" ]' in script
    assert 'sudo -u "$CU" -- /bin/sh -c' in script
    assert 'chown "$CU"' not in script
    # The bootstrap steps, verbatim (no sudo — already root).
    assert "systemsetup -setremotelogin on" in script
    assert "pmset -a sleep 0 disksleep 0 powernap 0 womp 1" in script
    assert "systemsetup -setrestartpowerfailure on" in script
    # The console's pubkey is embedded; the pairing code is stamped.
    assert f"KEY='{PUBKEY}'" in script
    assert f"# sanctum-pairing: {code}" in script
    # NO sudoers content without the flag; and NEVER a hardcoded IP.
    assert "sudoers.d" not in script
    assert "NOPASSWD" not in script
    assert not _DOTTED_QUAD.search(script), "postinstall must embed no IP literal"


def test_render_postinstall_sudo_nopasswd_block() -> None:
    """--sudo-nopasswd adds a scoped, mktemp/visudo-validated sudoers drop-in."""
    script = node_cmd.render_postinstall(PUBKEY, sudo_nopasswd=True, code="000000")
    assert "/etc/sudoers.d/sanctum-automation" in script
    assert "NOPASSWD: ALL" not in script
    for expected in (
        "/usr/bin/true",
        "/usr/sbin/systemsetup",
        "/usr/bin/pmset",
        "/usr/sbin/networksetup",
        "/usr/sbin/ipconfig",
    ):
        assert expected in script
    assert "mktemp /etc/sudoers.d/.sanctum-automation.XXXXXX" in script
    assert 'visudo -c -f "$TMP_SUDO"' in script
    assert 'rm -f "$TMP_SUDO"' in script
    assert 'mv -f "$TMP_SUDO" /etc/sudoers.d/sanctum-automation' in script


def test_render_postinstall_refuses_quoted_pubkey() -> None:
    from sanctum_cli.errors import LocalError

    with pytest.raises(LocalError):
        node_cmd.render_postinstall("ssh-ed25519 AAAA it's-corrupt", sudo_nopasswd=False, code="000000")


# ── build_pkg: pkgbuild / productsign argv behind the injectable seam ─────────


class _RecordingPkgBuilder:
    """Records each pkg-tool argv and returns success, building NO real package."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return (0, "")


def test_build_pkg_unsigned_calls_pkgbuild_only(tmp_path: Path) -> None:
    """No signing identity → one pkgbuild call with the exact argv, no productsign."""
    builder = _RecordingPkgBuilder()
    scripts_dir = tmp_path / "scripts"
    out_pkg = tmp_path / "Sanctum-Node-Setup.pkg"
    result = node_cmd.build_pkg(
        "#!/bin/bash\nexit 0\n",
        out_pkg,
        version="2026.07.18",
        pkg_builder=builder,
        find_identity=lambda: None,
        stage_scripts=lambda _s: scripts_dir,
    )
    assert builder.calls == [
        [
            "pkgbuild",
            "--nopayload",
            "--scripts",
            str(scripts_dir),
            "--identifier",
            "haus.sanctum.node-setup",
            "--version",
            "2026.07.18",
            str(out_pkg),
        ]
    ]
    assert result.signed is False
    assert result.identity is None
    assert result.path == out_pkg


def test_build_pkg_signed_invokes_productsign_when_identity_present(tmp_path: Path) -> None:
    """A Developer ID Installer identity → pkgbuild(unsigned) then productsign(out)."""
    builder = _RecordingPkgBuilder()
    scripts_dir = tmp_path / "scripts"
    out_pkg = tmp_path / "Sanctum-Node-Setup.pkg"
    identity = "Developer ID Installer: Operator (TEAMID123)"
    result = node_cmd.build_pkg(
        "#!/bin/bash\nexit 0\n",
        out_pkg,
        version="2026.07.18",
        pkg_builder=builder,
        find_identity=lambda: identity,
        stage_scripts=lambda _s: scripts_dir,
    )
    unsigned = out_pkg.with_suffix(".unsigned.pkg")
    assert builder.calls == [
        [
            "pkgbuild",
            "--nopayload",
            "--scripts",
            str(scripts_dir),
            "--identifier",
            "haus.sanctum.node-setup",
            "--version",
            "2026.07.18",
            str(unsigned),
        ],
        ["productsign", "--sign", identity, str(unsigned), str(out_pkg)],
    ]
    assert result.signed is True
    assert result.identity == identity


# ── bootstrap-script --format pkg: the mom-friendly default ───────────────────


def _wire_pkg(
    monkeypatch: pytest.MonkeyPatch, *, identity: str | None
) -> tuple[list[list[str]], list[Path]]:
    """Point the pkg path at recording seams — no real pkgbuild/security/open/keygen."""
    monkeypatch.setattr(node_cmd, "_ensure_automation_pubkey", lambda: PUBKEY)
    argv_log: list[list[str]] = []

    def fake_pkg_tool(argv: list[str]) -> tuple[int, str]:
        argv_log.append(argv)
        return (0, "")

    def fake_stage(postinstall: str) -> Path:
        # Assert the postinstall content flows through, but write nothing real.
        assert "systemsetup -setremotelogin on" in postinstall
        from pathlib import Path as RuntimePath

        return RuntimePath("/tmp/fake-scripts")  # ip-allow: inert fake path, never created

    revealed: list[Path] = []
    monkeypatch.setattr(node_cmd, "_run_pkg_tool", fake_pkg_tool)
    monkeypatch.setattr(node_cmd, "_find_installer_identity", lambda: identity)
    monkeypatch.setattr(node_cmd, "_stage_postinstall", fake_stage)
    monkeypatch.setattr(node_cmd, "_reveal_in_finder", revealed.append)
    return argv_log, revealed


def test_bootstrap_pkg_is_default_and_calls_pkgbuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bare `bootstrap-script` (default pkg) builds via the seam with the expected argv."""
    argv_log, revealed = _wire_pkg(monkeypatch, identity=None)
    out_pkg = tmp_path / "Node.pkg"
    result = runner.invoke(app, ["node", "bootstrap-script", "--out", str(out_pkg)])
    assert result.exit_code == 0, result.stdout
    assert argv_log == [
        [
            "pkgbuild",
            "--nopayload",
            "--scripts",
            "/tmp/fake-scripts",  # ip-allow: inert fake path from the seam
            "--identifier",
            "haus.sanctum.node-setup",
            "--version",
            node_cmd._pkg_version(),
            str(out_pkg),
        ]
    ]
    # Mom-friendly output: the pairing code + unsigned note + reveal-in-Finder.
    assert node_cmd.pairing_code(PUBKEY) in result.stdout
    assert "unsigned" in result.stdout
    assert revealed == [out_pkg]


def test_bootstrap_pkg_signs_when_identity_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A signing identity → productsign is invoked and the output says 'signed'."""
    identity = "Developer ID Installer: Operator (TEAMID123)"
    argv_log, _revealed = _wire_pkg(monkeypatch, identity=identity)
    out_pkg = tmp_path / "Node.pkg"
    result = runner.invoke(app, ["node", "bootstrap-script", "--out", str(out_pkg)])
    assert result.exit_code == 0, result.stdout
    assert any(a[0] == "productsign" for a in argv_log)
    assert "signed" in result.stdout


def test_bootstrap_script_format_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--format script` still emits the byte-clean developer script (unchanged)."""
    monkeypatch.setattr(node_cmd, "_ensure_automation_pubkey", lambda: PUBKEY)
    # A pkg seam that would fail loudly if `--format script` wrongly built a pkg.
    monkeypatch.setattr(
        node_cmd, "_run_pkg_tool", lambda _a: (_ for _ in ()).throw(AssertionError("built a pkg!"))
    )
    result = runner.invoke(app, ["node", "bootstrap-script", "--format", "script"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("#!/bin/bash")
    assert PUBKEY in result.stdout


def test_render_postinstall_refuses_unsafe_cu_and_symlink_hd(tmp_path: Path) -> None:
    """Execute the $CU/$HD validation + symlink checks in bash against mocked commands."""
    import os
    import subprocess

    script = node_cmd.render_postinstall(PUBKEY, sudo_nopasswd=False, code="123456")
    # Extract lines from '# Resolve' through the 'KEY=' line and test in a sandboxed PATH.
    head = "\n".join(script.splitlines()[:35])
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "dscl").write_text(f"#!/bin/sh\necho 'NFSHomeDirectory: {tmp_path}/hd'\n")
    (fake_bin / "dscl").chmod(0o755)
    (fake_bin / "id").write_text("#!/bin/sh\necho 501\n")
    (fake_bin / "id").chmod(0o755)

    # Case 1: stat reports root as console user -> exit 1
    (fake_bin / "stat").write_text("#!/bin/sh\necho root\n")
    (fake_bin / "stat").chmod(0o755)
    (fake_bin / "ls").write_text("#!/bin/sh\necho 'crw------- 1 root wheel'\n")
    (fake_bin / "ls").chmod(0o755)
    res_root = subprocess.run(
        ["/bin/bash", "-c", head],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_root.returncode == 1
    assert "refusing" in res_root.stderr

    # Case 2: real non-root user + symlinked .ssh -> exit 1
    hd = tmp_path / "hd"
    hd.mkdir()
    (tmp_path / "target").mkdir()
    (hd / ".ssh").symlink_to(tmp_path / "target")
    (fake_bin / "stat").write_text("#!/bin/sh\necho alice\n")
    res_sym = subprocess.run(
        ["/bin/bash", "-c", head],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_sym.returncode == 1
    assert "refusing symlink" in res_sym.stderr


def test_generate_automation_key_uses_keychain_and_askpass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    priv = tmp_path / "ssh" / "sanctum_automation"
    stored: list[str] = []
    seen: dict[str, object] = {}

    monkeypatch.setattr(node_cmd, "_store_automation_passphrase", stored.append)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["env"] = dict(kwargs["env"])  # type: ignore[arg-type]
        seen["stdin"] = kwargs.get("stdin")
        priv.write_text("PRIVATE", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(node_cmd.subprocess, "run", fake_run)
    node_cmd._generate_automation_key(priv)

    assert len(stored) == 1 and len(stored[0]) >= 20
    argv = seen["argv"]
    assert "-N" not in argv  # type: ignore[operator]
    assert stored[0] not in argv  # type: ignore[operator]
    env = seen["env"]
    assert env["SSH_ASKPASS_REQUIRE"] == "force"  # type: ignore[index]
    assert "SSH_ASKPASS" in env  # type: ignore[operator]
    assert seen["stdin"] == subprocess.DEVNULL
    assert (priv.stat().st_mode & 0o777) == 0o600


def test_run_ssh_uses_askpass_when_passphrase_present_else_batchmode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    monkeypatch.setattr(node_cmd, "_automation_key_path", lambda: tmp_path / "k")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        calls.append((list(argv), dict(env) if isinstance(env, dict) else None))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(node_cmd.subprocess, "run", fake_run)

    monkeypatch.setattr(node_cmd, "_read_automation_passphrase", lambda: "pass-123")
    rc1, out1 = node_cmd._run_ssh("host", "u", "echo ok")
    assert (rc1, out1) == (0, "ok\n")
    assert "BatchMode=yes" not in calls[0][0]
    assert calls[0][1] is not None and calls[0][1]["SSH_ASKPASS_REQUIRE"] == "force"

    monkeypatch.setattr(node_cmd, "_read_automation_passphrase", lambda: None)
    rc2, _ = node_cmd._run_ssh("host", "u", "echo ok")
    assert rc2 == 0
    assert "BatchMode=yes" in calls[1][0]


def test_probe_key_hygiene_rejects_malformed_blob_and_uses_double_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh_calls: list[str] = []
    monkeypatch.setattr(
        node_cmd,
        "_run_ssh",
        lambda _h, _u, cmd: ssh_calls.append(cmd) or (0, "700\n600\nkey-present\n"),
    )

    hostile_keys = (
        "",
        "   ",
        "ssh-ed25519 ';id;'",
        "ssh-ed25519 -e",
        "ssh-ed25519 $(whoami)",
        "-f /etc/passwd",
        "ssh-ed25519\nrm -rf /",
        "ssh-ed25519 AA AA",
        "AAA\nrm -rf /",
        "ssh-ed25519 AAAA\nrm -rf /",
        "AAA\nBBB",
        "'-e'",
        f"{PUBKEY}\x00extra",
        "ssh-ed25519 AAAAAAAAAAAAAAA",  # <16 chars and invalid base64 len
        "ssh-ed25519 AAAAAAAAAAAAAAAAA",  # >=16 chars but invalid base64 len (17 % 4 == 1)
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeBlobForNodeTest\r",
    )
    for bad_key in hostile_keys:
        res = node_cmd._probe_key_hygiene("h", "u", bad_key)
        assert res.status == node_cmd.LayerStatus.FAIL, f"expected FAIL for {bad_key!r}"
    assert ssh_calls == []

    ok = node_cmd._probe_key_hygiene("h", "u", PUBKEY)
    assert ok.status == node_cmd.LayerStatus.PASS
    assert len(ssh_calls) == 1
    assert "grep -qF -- " in ssh_calls[0]


def test_validate_pubkey_rejects_nul_byte() -> None:
    from sanctum_cli.errors import LocalError

    nul_in_comment = f"{PUBKEY}\x00extra"
    with pytest.raises(LocalError):
        node_cmd._validate_pubkey(nul_in_comment)
    with pytest.raises(LocalError):
        node_cmd.render_bootstrap_script(nul_in_comment, sudo_nopasswd=False)
    with pytest.raises(LocalError):
        node_cmd.render_postinstall(nul_in_comment, sudo_nopasswd=False, code="000000")


def test_node_source_has_no_personal_leaks() -> None:
    from pathlib import Path

    src = Path(node_cmd.__file__).read_text(encoding="utf-8")
    for leak in (
        "man" + "oir",
        "cha" + "let",
        "berts-" + "mbp",
        "tail" + "7c6d11",
        "/Users/" + "be" + "rt",
    ):
        assert leak not in src
