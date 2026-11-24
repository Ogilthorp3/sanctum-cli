"""Tailnet identity for ``sanctum node``: which machine may answer for a hive name.

``tailscale status --json`` gives every machine two names. ``DNSName`` is the
MagicDNS name the control plane assigned, unique in the tailnet (a second machine
that asks for a taken name gets ``home-1``). ``HostName`` is whatever the machine
reports for itself. These tests pin that only the first identifies a machine, so a
compromised satellite that renames its host to the hub's name (``home``) does not
become the hub for ``node resolve``, ``vault-host``, ``route`` or
``export-council``; that this machine's own name stays its own; that two machines
claiming one name fail closed; and that a machine tagged ``tag:satellite`` never
answers for a node the roster says is not a satellite.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from typer.testing import CliRunner

from sanctum_cli import config
from sanctum_cli.cli import app
from sanctum_cli.commands import node as node_cmd
from sanctum_cli.hive.route import pick_node

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

SUFFIX = "tailnet-abc.ts.net"
MBP_IP = "100.64.0.11"
HOME_IP = "100.64.0.18"
STUDIO_IP = "100.64.0.32"

ROSTER_YAML = """\
nodes:
  home:
    type: hub
    tier: primary
    tailscale_name: home
    ssh_user: operator
    capabilities: [vault_authority, council]
  studio:
    type: satellite
    tier: edge
    tailscale_name: studio
    ssh_user: operator
    sync: {hub: home}
  mbp:
    type: mobile
    tier: edge
    tailscale_name: mbp
    ssh_user: operator
"""


def _nodes() -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = yaml.safe_load(ROSTER_YAML)["nodes"]
    return nodes


def _machine(
    stem: str,
    ip: str,
    *,
    host_name: str | None = None,
    online: bool = True,
    tags: tuple[str, ...] = (),
    sid: str | None = None,
    dns: str | None = None,
) -> dict[str, Any]:
    """One ``Self`` / ``Peer`` record shaped like ``tailscale status --json``."""
    rec: dict[str, Any] = {
        "ID": sid if sid is not None else f"n{stem.title()}CNTRL",
        "HostName": host_name if host_name is not None else stem,
        "DNSName": dns if dns is not None else f"{stem}.{SUFFIX}.",
        "OS": "macOS",
        "TailscaleIPs": [ip, "fd7a:115c:a1e0::1"],
        "Online": online,
    }
    if tags:
        rec["Tags"] = list(tags)
    return rec


def _status(self_node: dict[str, Any], *peers: dict[str, Any], suffix: str = SUFFIX) -> str:
    data: dict[str, Any] = {
        "Version": "1.90.0",
        "BackendState": "Running",
        "Self": self_node,
        "Peer": {f"nodekey:{i:04x}": p for i, p in enumerate(peers)},
    }
    if suffix:
        data["MagicDNSSuffix"] = suffix
    return json.dumps(data)


def _mbp() -> dict[str, Any]:
    return _machine("mbp", MBP_IP, tags=("tag:sanctum-admin",))


def _home(*, online: bool = True) -> dict[str, Any]:
    return _machine("home", HOME_IP, online=online, tags=("tag:sanctum-host",))


def _hostile_studio() -> dict[str, Any]:
    """A compromised satellite that set its OS host name to the hub's, ``home``.

    The control plane kept its MagicDNS name (``studio``); ``HostName`` is what the
    machine says.
    """
    return _machine("studio", STUDIO_IP, host_name="home", tags=("tag:satellite",))


def _squatter() -> dict[str, Any]:
    """A satellite holding the MagicDNS name ``home`` (the hub's machine was removed)."""
    return _machine("home", STUDIO_IP, tags=("tag:satellite",), sid="nSquatCNTRL")


@pytest.fixture(autouse=True)
def cli_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``tailscale ip`` fallback instead of running it."""
    calls: list[str] = []

    def _lookup(name: str) -> str | None:
        calls.append(name)
        return None

    monkeypatch.setattr(node_cmd, "_tailscale_ip_for_name", _lookup)
    return calls


@pytest.fixture
def haus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Point the node commands at :data:`ROSTER_YAML` and a status the test sets."""
    inst = tmp_path / "instance.yaml"
    inst.write_text(ROSTER_YAML, encoding="utf-8")
    monkeypatch.setattr(config, "instance_path", lambda: inst)
    monkeypatch.setattr(node_cmd, "read_node_id", lambda: "mbp")
    state: dict[str, Any] = {"status": ""}
    monkeypatch.setattr(node_cmd, "_tailscale_status_json", lambda: state["status"])
    return state


# ── the parser: MagicDNS stems only ──────────────────────────────────────────


def test_records_carry_tags_and_stable_id_and_hostname_is_never_a_key() -> None:
    ghost = _machine("ghost", "100.64.0.40", host_name="totally-home")
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), _home(), _hostile_studio(), ghost))
    assert set(peers) == {"mbp", "home", "studio", "ghost"}
    studio = peers["studio"]
    assert studio["ips"][0] == STUDIO_IP
    assert studio["tags"] == ["tag:satellite"]
    assert studio["stable_id"] == "nStudioCNTRL"
    assert studio["dns_name"] == f"studio.{SUFFIX}"
    assert studio["host_name"] == "home"  # kept for display, never a key
    assert studio["self"] is False
    assert peers["home"]["stable_id"] == "nHomeCNTRL"
    assert peers["mbp"]["self"] is True
    assert peers["mbp"]["tags"] == ["tag:sanctum-admin"]


def test_a_machine_without_a_magicdns_name_gets_no_entry() -> None:
    nameless = _machine("x", STUDIO_IP, host_name="home", dns="")
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), nameless))
    assert set(peers) == {"mbp"}


def test_a_machine_from_another_tailnet_has_no_short_name_here() -> None:
    """A machine shared in from another tailnet keeps that tailnet's suffix."""
    shared = _machine("home", STUDIO_IP, dns="home.someone-else.ts.net.")
    assert "home" not in node_cmd.parse_tailscale_peer_ips(_status(_mbp(), shared))
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), _home(), shared))
    assert peers["home"]["ips"][0] == HOME_IP
    assert "ambiguous" not in peers["home"]


def test_without_magicdns_suffix_this_machines_own_name_gives_it() -> None:
    shared = _machine("home", STUDIO_IP, dns="home.someone-else.ts.net.")
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), _home(), shared, suffix=""))
    assert peers["home"]["ips"][0] == HOME_IP
    assert "ambiguous" not in peers["home"]


# ── resolve: a hostile HostName changes nothing ──────────────────────────────


@pytest.mark.parametrize("hostile_last", [True, False], ids=["hostile-last", "hostile-first"])
def test_a_hostname_copying_the_hubs_does_not_move_the_hub(hostile_last: bool) -> None:
    pair = [_home(), _hostile_studio()]
    peers = node_cmd.parse_tailscale_peer_ips(
        _status(_mbp(), *(pair if hostile_last else pair[::-1]))
    )
    r = node_cmd.resolve_node_address("home", _nodes(), peers)
    assert r["ok"] is True
    assert r["address"] == HOME_IP
    # The satellite still answers for its own name.
    studio = node_cmd.resolve_node_address("studio", _nodes(), peers)
    assert studio["ok"] is True
    assert studio["address"] == STUDIO_IP


def test_a_hostname_copying_the_hubs_cannot_stand_in_for_an_offline_hub(
    cli_lookups: list[str],
) -> None:
    peers = node_cmd.parse_tailscale_peer_ips(
        _status(_mbp(), _home(online=False), _hostile_studio())
    )
    r = node_cmd.resolve_node_address("home", _nodes(), peers)
    assert r["ok"] is False
    assert r["address"] is None
    assert r["online"] is False
    assert "offline" in r["error"]
    assert cli_lookups == []


def test_an_offline_peer_is_not_answered_with_the_rosters_lan_host(
    cli_lookups: list[str],
) -> None:
    """A roster ``host`` is a site LAN address; it must not answer for an offline peer.

    RL-04: with the hub offline, ``node resolve`` returned its row's 10.0.0.x host,
    which on another site's LAN is a different machine.
    """
    nodes = _nodes()
    nodes["home"]["host"] = "192.0.2.10"  # documentation range; the design-map address
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), _home(online=False)))
    r = node_cmd.resolve_node_address("home", nodes, peers)
    assert r["ok"] is False
    assert r["address"] is None
    assert r["online"] is False
    assert "offline" in r["error"]
    assert cli_lookups == []


def test_a_tailnet_named_node_the_tailnet_does_not_list_is_not_answered_with_its_lan_host() -> None:
    nodes = _nodes()
    nodes["home"]["host"] = "192.0.2.10"
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp()))  # no home at all
    r = node_cmd.resolve_node_address("home", nodes, peers)
    assert r["ok"] is False
    assert r["address"] is None
    assert "no resolvable address" in r["error"]


def test_a_node_without_a_tailnet_name_still_resolves_to_its_literal_host() -> None:
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp()))
    nodes = {"cabin": {"type": "satellite", "host": "192.0.2.40", "ssh_user": "operator"}}
    r = node_cmd.resolve_node_address("cabin", nodes, peers)
    assert (r["ok"], r["address"], r["source"]) == (True, "192.0.2.40", "host")


def test_node_resolve_cli_reports_an_offline_peer_with_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance = tmp_path / "instance.yaml"
    nodes = _nodes()
    nodes["home"]["host"] = "192.0.2.10"
    instance.write_text(yaml.safe_dump({"nodes": nodes}), encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(instance))
    monkeypatch.setattr(node_cmd, "_tailscale_status_json", lambda: _status(_mbp(), _home(online=False)))
    result = runner.invoke(app, ["node", "resolve", "home"])
    assert result.exit_code != 0
    assert "192.0.2.10" not in result.output
    assert "offline" in result.output


def test_a_peer_with_this_machines_hostname_does_not_shadow_it() -> None:
    """On the hub itself, a peer whose HostName is the hub's name changes nothing."""
    peers = node_cmd.parse_tailscale_peer_ips(_status(_home(), _hostile_studio(), _mbp()))
    assert peers["home"]["self"] is True
    assert peers["home"]["ips"][0] == HOME_IP
    r = node_cmd.resolve_node_address("home", _nodes(), peers)
    assert r["address"] == HOME_IP


def test_this_machines_magicdns_name_stays_its_own() -> None:
    """Even a peer listed under this machine's own MagicDNS name cannot replace it."""
    twin = _machine("home", STUDIO_IP, tags=("tag:satellite",), sid="nTwinCNTRL")
    peers = node_cmd.parse_tailscale_peer_ips(_status(_home(), twin))
    assert peers["home"]["self"] is True
    assert peers["home"]["ips"][0] == HOME_IP
    assert "ambiguous" not in peers["home"]


# ── two machines, one name: fail closed, online or not ──────────────────────


@pytest.mark.parametrize(
    ("first_online", "second_online"),
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_two_machines_with_one_magicdns_name_fail_closed(
    first_online: bool, second_online: bool, cli_lookups: list[str]
) -> None:
    first = _machine("home", HOME_IP, online=first_online, sid="nFirstCNTRL")
    second = _machine("home", STUDIO_IP, online=second_online, sid="nSecondCNTRL")
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), first, second))
    assert peers["home"]["ambiguous"] is True
    assert peers["home"]["online"] is False
    assert peers["home"]["ips"] == []
    nodes = _nodes()
    nodes["home"]["host"] = "10.0.0.10"  # a literal LAN host is no way around it
    r = node_cmd.resolve_node_address("home", nodes, peers)
    assert r["ok"] is False
    assert r["address"] is None
    assert r["error"].startswith("ambiguous tailnet name 'home'")
    assert "nFirstCNTRL" in r["error"]
    assert "nSecondCNTRL" in r["error"]
    assert cli_lookups == []


# ── tag:satellite never answers for a node that is not a satellite ──────────


def test_a_satellite_cannot_answer_for_the_hub(cli_lookups: list[str]) -> None:
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), _squatter()))
    r = node_cmd.resolve_node_address("home", _nodes(), peers)
    assert r["ok"] is False
    assert r["address"] is None
    assert r["refused"] is True
    assert "tag:satellite" in r["error"]
    assert "nSquatCNTRL" in r["error"]
    assert "type: hub" in r["error"]
    assert cli_lookups == []


def test_a_satellite_still_answers_for_a_satellite() -> None:
    studio = _machine("studio", STUDIO_IP, tags=("tag:satellite",))
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), studio))
    r = node_cmd.resolve_node_address("studio", _nodes(), peers)
    assert r["ok"] is True
    assert r["address"] == STUDIO_IP


@pytest.mark.parametrize(
    ("roster", "reason"),
    [
        (
            {
                "home": {"tailscale_name": "home"},
                "studio": {"type": "satellite", "sync": {"hub": "home"}},
            },
            "studio syncs to it",
        ),
        ({"home": {"tailscale_name": "home", "capabilities": ["vault"]}}, "vault_authority"),
        ({"home": {"type": "mobile", "tailscale_name": "home"}}, "type: mobile"),
    ],
    ids=["untyped-sync-hub", "untyped-vault-authority", "mobile"],
)
def test_a_satellite_cannot_answer_for_a_node_that_is_not_one(
    roster: dict[str, dict[str, Any]], reason: str
) -> None:
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), _squatter()))
    r = node_cmd.resolve_node_address("home", roster, peers)
    assert r["ok"] is False
    assert reason in r["error"]


def test_a_satellite_may_answer_for_a_node_of_unknown_kind() -> None:
    """A row with no type and no hub role resolves as before."""
    lab = _machine("lab", STUDIO_IP, tags=("tag:satellite",))
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), lab))
    r = node_cmd.resolve_node_address("lab", {"lab": {"tailscale_name": "lab"}}, peers)
    assert r["ok"] is True
    assert r["address"] == STUDIO_IP


def test_a_satellite_tag_written_as_one_string_still_counts() -> None:
    peers = {"home": {"online": True, "ips": [STUDIO_IP], "tags": "tag:satellite"}}
    r = node_cmd.resolve_node_address("home", _nodes(), peers)
    assert r["ok"] is False
    assert r["refused"] is True


@pytest.mark.parametrize(("stem", "host"), [("10", "10.0.0.50"), ("010", "010.0.0.50")])
def test_an_ip_host_is_not_looked_up_as_a_tailnet_name(stem: str, host: str) -> None:
    """A machine named ``10`` does not answer for every row whose host is 10.x."""
    ten = _machine(stem, STUDIO_IP, tags=("tag:satellite",))
    peers = node_cmd.parse_tailscale_peer_ips(_status(_mbp(), ten))
    roster = {"printer": {"host": host, "ssh_user": "pi"}}
    r = node_cmd.resolve_node_address("printer", roster, peers)
    assert r["ok"] is True
    assert r["address"] == host
    assert r["source"] == "host"
    online = pick_node(
        "satellite", {"printer": {"host": host, "capabilities": ["satellite"]}}, peer_ips=peers
    )
    assert online["online"]["printer"] is None


# ── the commands that act on the answer ──────────────────────────────────────


def test_node_commands_ignore_a_hostile_hostname(haus: dict[str, Any], tmp_path: Path) -> None:
    haus["status"] = _status(_mbp(), _home(), _hostile_studio())

    res = runner.invoke(app, ["node", "resolve", "home"])
    assert res.exit_code == 0, res.output
    assert HOME_IP in res.stdout
    assert STUDIO_IP not in res.stdout

    vault = runner.invoke(app, ["node", "vault-host"])
    assert vault.exit_code == 0, vault.output
    assert f"hop operator@{HOME_IP}" in vault.stdout

    route = runner.invoke(app, ["node", "route", "vault", "--prefer", "primary"])
    assert route.exit_code == 0, route.output
    assert f"address {HOME_IP}" in route.stdout

    council = tmp_path / "nodes.json"
    council.write_text(
        json.dumps(
            {"nodes": {"hub": {"host": "100.64.0.99"}, "satellite": {"host": "100.64.0.98"}}}
        ),
        encoding="utf-8",
    )
    out = runner.invoke(app, ["node", "export-council", "--from", str(council)])
    assert out.exit_code == 0, out.output
    doc = json.loads(out.stdout)
    assert doc["nodes"]["hub"]["host"] == HOME_IP
    assert doc["nodes"]["satellite"]["host"] == STUDIO_IP


def test_node_resolve_reports_an_ambiguous_name(
    haus: dict[str, Any], cli_lookups: list[str]
) -> None:
    first = _machine("home", HOME_IP, tags=("tag:sanctum-host",), sid="nFirstCNTRL")
    second = _machine("home", STUDIO_IP, online=False, sid="nSecondCNTRL")
    haus["status"] = _status(_mbp(), first, second)
    res = runner.invoke(app, ["node", "resolve", "home"])
    assert res.exit_code == 3
    assert "ambiguous tailnet name 'home'" in res.stderr
    assert HOME_IP not in res.output
    assert STUDIO_IP not in res.output
    assert cli_lookups == []


def test_node_commands_refuse_a_satellite_for_the_hub_and_say_so(
    haus: dict[str, Any], tmp_path: Path
) -> None:
    studio = _machine("studio", "100.64.0.33", tags=("tag:satellite",))
    haus["status"] = _status(_mbp(), _squatter(), studio)

    res = runner.invoke(app, ["node", "resolve", "home"])
    assert res.exit_code == 3
    assert "tag:satellite" in res.stderr
    assert STUDIO_IP not in res.output

    vault = runner.invoke(app, ["node", "vault-host"])
    assert vault.exit_code == 1
    assert "tag:satellite" in vault.stdout
    assert STUDIO_IP not in vault.output

    # route answers which node; it is the address line that is refused.
    route = runner.invoke(app, ["node", "route", "vault", "--prefer", "primary"])
    assert route.exit_code == 0, route.output
    assert "tag:satellite" in route.stdout
    assert STUDIO_IP not in route.output

    council = tmp_path / "nodes.json"
    council.write_text(json.dumps({"nodes": {"hub": {"host": "100.64.0.99"}}}), encoding="utf-8")
    out = runner.invoke(app, ["node", "export-council", "--from", str(council)])
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["nodes"]["hub"]["host"] == "100.64.0.99"
    assert "tag:satellite" in out.stderr
