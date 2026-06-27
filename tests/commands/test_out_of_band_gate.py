"""``net._out_of_band_reachable`` — the single-NAT cutover's recovery-path gate.

The cutover briefly drops the WAN, so before any mutation the orchestrator's start
precondition (:func:`sanctum_cli.devices.flip.gate_ok`) demands an out-of-band
recovery path. The gate is three-layered, ALL of which must hold:

* the **LAN-independent Tailscale-on-box channel** (FIX-3) — the only one proven to
  survive the /1 collapse; checked first;
* the **Mini jump host** — the out-of-band link the operator reaches the box over if
  the cutover strands the hub. Config-first via :func:`net._out_of_band_host`
  (``devices.mini.host`` → the ``_OUT_OF_BAND_HOST`` LAN default), so an OFF-LAN
  operator on the Bell hub Wi-Fi probes the TAILNET Mini (which survives the
  collapse) instead of ``10.0.0.10`` (behind the Firewalla NAT, unreachable from the
  perch); and
* the **Firewalla** — the host that ACTUALLY PERFORMS the recovery re-lease. On a
  failed cutover (or an explicit ``--rollback``) the unwind disables DMZ and then
  fires the ``dhcp_release`` runner tag, which SSHes to the Firewalla
  (``sanctum_cli.net.system._fw_mutate_via_ssh`` → ``ssh pi@<host>``) to release +
  re-acquire the downstream WAN lease. Config-first via :func:`net._firewalla_host`.
  If the Firewalla is unreachable, the recovery re-lease cannot run — so a gate that
  probed ONLY the Mini would green-light a cutover whose rollback can never bring the
  WAN back: the exact fail-to-DARK the gate exists to prevent.

These tests author their expectations from the REAL recovery transport (the
``dhcp_release`` op SSHes to the box on port 22; the gate connects by bare TCP), read
BOTH the Mini and box hosts THROUGH THE CONSUMER (:func:`net._out_of_band_host` /
:func:`net._firewalla_host`) rather than from a hardcoded constant, and mock ONLY the
genuinely external edge (``socket.create_connection``), asserting on the (host, port)
tuples the gate actually probes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.commands import net as net_cmd

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _SocketProbe:
    """Records every ``socket.create_connection`` (host, port); scripts reachability.

    Stands in for the ONE external edge of the gate — a real TCP connect. Records
    each ``(host, port)`` the gate probes so a test can assert it checked BOTH the
    Mini and the Firewalla, and refuses (``OSError``) any address in
    ``unreachable`` so a test can model one host down.
    """

    def __init__(self, *, unreachable: set[str] | None = None) -> None:
        self.probes: list[tuple[str, int]] = []
        self._unreachable = unreachable or set()

    def __call__(self, address: tuple[str, int], timeout: float | None = None) -> object:
        host, port = address
        self.probes.append((host, port))
        if host in self._unreachable:
            msg = f"connection refused: {host}:{port}"
            raise OSError(msg)

        class _Conn:
            def close(self) -> None:
                return None

        return _Conn()


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    probe: _SocketProbe,
    *,
    gateway: str,
    tailscale_live: bool = True,
    instance_file: str = "/nonexistent/sanctum-test-instance.yaml",
) -> None:
    """Point the gate at the recording socket probe + a fixed Firewalla gateway.

    Both recovery hosts are resolved the SAME way the recovery transport resolves
    them — the Mini via :func:`net._out_of_band_host` (``devices.mini.host`` config
    override → the LAN default) and the Firewalla via :func:`net._firewalla_host`
    (``devices.firewalla.host`` config override → parsed default gateway) — so the
    gate and the recovery transport target the identical boxes.

    ``instance_file`` selects which instance.yaml the config resolvers read: an absent
    path (the default) exercises the SHIPPED no-config behavior (Mini → LAN default,
    Firewalla → parsed gateway); a real temp file lets a test pin a tailnet (off-LAN)
    or explicit-LAN config. We stub the gateway parse to a fixed IP and the socket
    connect to the recorder; no real route/socket is used.

    The PRIMARY (FIX-3) Tailscale-on-box check is a real root-SSH round-trip, NOT a
    socket connect, so it is stubbed separately via ``interlock.tailscale_oob_live``
    (defaults to live so the LAN/tailnet-side assertions below still exercise).
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", instance_file)
    monkeypatch.setattr(net_cmd.socket, "create_connection", probe.__call__)
    monkeypatch.setattr(net_cmd.detect, "parse_default_gateway", lambda _out: gateway)
    monkeypatch.setattr(net_cmd.system, "real_runner", lambda _tag: "")
    monkeypatch.setattr(
        net_cmd.interlock, "tailscale_oob_live", lambda **_kw: tailscale_live
    )


def _write_instance(tmp_path: Path, body: str) -> str:
    """Write an instance.yaml with ``body`` and return its path (for ``_patch``)."""
    target = tmp_path / "instance.yaml"
    target.write_text(body, encoding="utf-8")
    return str(target)


# ── shipped-default (no config) behavior — the general-purpose tool is unchanged ──


def test_gate_probes_both_the_mini_and_the_firewalla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate probes BOTH the Mini jump host AND the Firewalla recovery host.

    The recovery re-lease SSHes to the Firewalla; the gate must confirm THAT host
    is reachable (port 22), not only the Mini — otherwise it green-lights a cutover
    whose rollback can never re-lease the WAN. Hosts read through the consumers.
    """
    probe = _SocketProbe()
    _patch(monkeypatch, probe, gateway="10.0.0.1")
    assert net_cmd._out_of_band_reachable() is True
    probed_hosts = {host for host, _port in probe.probes}
    # The Mini jump host (the out-of-band link), resolved through the consumer …
    assert net_cmd._out_of_band_host() in probed_hosts
    # … AND the Firewalla (the host that performs the recovery re-lease).
    assert net_cmd._firewalla_host() in probed_hosts
    # No config pinned → the shipped LAN default is what the Mini leg resolves to.
    assert net_cmd._out_of_band_host() == net_cmd._OUT_OF_BAND_HOST
    # Both probed on the SSH port the recovery transport uses.
    assert all(port == 22 for _host, port in probe.probes)


def test_gate_refuses_when_firewalla_unreachable_even_if_mini_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewalla down → the gate REFUSES, even with the Mini reachable.

    A reachable Mini is NOT sufficient if the box that performs the recovery re-lease
    is unreachable. The gate must fail so the cutover refuses rather than strand the
    household with an un-runnable rollback.
    """
    probe = _SocketProbe(unreachable={"10.0.0.1"})  # Firewalla down, Mini up
    _patch(monkeypatch, probe, gateway="10.0.0.1")
    assert net_cmd._out_of_band_reachable() is False


def test_gate_refuses_when_mini_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mini down → the gate REFUSES (the out-of-band link contract still holds).

    The Mini host is read THROUGH THE CONSUMER (``net._out_of_band_host``) under the
    same isolated no-config env the gate sees, so the test models 'the resolved Mini'
    down rather than a hardcoded literal.
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", "/nonexistent/sanctum-test-instance.yaml")
    mini = net_cmd._out_of_band_host()  # == _OUT_OF_BAND_HOST under no-config
    probe = _SocketProbe(unreachable={mini})
    _patch(monkeypatch, probe, gateway="10.0.0.1")
    assert net_cmd._out_of_band_reachable() is False


def test_gate_refuses_when_no_firewalla_gateway_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No default gateway resolved → the gate REFUSES (cannot confirm the recovery host).

    If the default-gateway probe yields nothing there is no Firewalla address to
    confirm the recovery re-lease can reach — the absence of a recovery host is the
    absence of a recovery path, so fail-closed rather than guess reachable.
    """
    probe = _SocketProbe()
    _patch(monkeypatch, probe, gateway="")  # no gateway parsed
    assert net_cmd._out_of_band_reachable() is False


def test_gate_refuses_when_tailscale_oob_not_live_even_if_lan_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX-3: the LAN-independent Tailscale channel is the PRIMARY gate.

    With the Mini + Firewalla both reachable but the Tailscale-on-box root-SSH
    round-trip NOT live, the gate must REFUSE — the LAN-bound hosts are exactly the
    ones that died with the LAN on 06-26, so without the tailnet safety net there is
    no channel proven to survive the cutover.
    """
    probe = _SocketProbe()  # both hosts reachable
    _patch(monkeypatch, probe, gateway="10.0.0.1", tailscale_live=False)
    assert net_cmd._out_of_band_reachable() is False


def test_gate_passes_with_explicit_lan_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All three layers live, hosts pinned to explicit LAN config → the gate passes.

    Reads the Mini + box hosts FROM CONFIG (a real instance.yaml), proving the gate
    consumes ``devices.mini.host`` / ``devices.firewalla.host`` rather than a constant.
    """
    inst = _write_instance(
        tmp_path,
        "devices:\n"
        "  firewalla:\n    host: 10.0.0.1\n"
        "  mini:\n    host: bert@10.0.0.10\n",
    )
    probe = _SocketProbe()
    _patch(monkeypatch, probe, gateway="10.0.0.1", instance_file=inst)
    assert net_cmd._out_of_band_host() == "10.0.0.10"
    assert net_cmd._firewalla_host() == "10.0.0.1"
    assert net_cmd._out_of_band_reachable() is True


# ── off-LAN (tailnet-pinned) behavior — the FIX-b keystone scenario ──


def test_out_of_band_host_strips_user_prefix_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``devices.mini.host: bert@100.107.112.118`` → the TCP probe target is the bare IP.

    The gate connects by TCP, not SSH, so the ``user@`` login prefix the armor deploy
    needs MUST be stripped for the reachability probe.
    """
    inst = _write_instance(
        tmp_path, "devices:\n  mini:\n    host: bert@100.107.112.118\n"
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", inst)
    assert net_cmd._out_of_band_host() == "100.107.112.118"


def test_off_lan_gate_probes_tailnet_not_lan_mini(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Off-LAN config pinned → the gate probes the TAILNET Mini + box, never 10.0.0.10.

    The literal FIX-b keystone: with ``devices.mini.host`` / ``devices.firewalla.host``
    pinned to the tailnet, the recovery gate must address the tailnet coordinates the
    off-LAN perch can actually reach — and must NOT probe the LAN ``10.0.0.10`` that
    sits behind the Firewalla NAT.
    """
    inst = _write_instance(
        tmp_path,
        "devices:\n"
        "  firewalla:\n    host: 100.68.36.16\n    ssh_user: pi\n"
        "  mini:\n    host: bert@100.107.112.118\n",
    )
    probe = _SocketProbe()
    _patch(monkeypatch, probe, gateway="10.0.0.1", instance_file=inst)
    assert net_cmd._out_of_band_reachable() is True
    probed_hosts = {host for host, _port in probe.probes}
    assert "100.107.112.118" in probed_hosts  # tailnet Mini
    assert "100.68.36.16" in probed_hosts  # tailnet box
    assert "10.0.0.10" not in probed_hosts  # the LAN Mini is NEVER probed off-LAN


def test_off_lan_gate_passes_when_lan_mini_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The off-LAN reality: 10.0.0.10 DOWN but tailnet up → the gate PASSES.

    From the Bell hub Wi-Fi the LAN Mini (10.0.0.10) is unreachable behind the
    Firewalla NAT. Before FIX-b's gate leg, the fail-closed AND-chain refused here.
    With the Mini leg config-driven to the tailnet, a reachable tailnet Mini + box
    (and a live Tailscale-on-box channel) authorize the cutover even though 10.0.0.10
    is dark — which is the whole point of running off-LAN.
    """
    inst = _write_instance(
        tmp_path,
        "devices:\n"
        "  firewalla:\n    host: 100.68.36.16\n"
        "  mini:\n    host: bert@100.107.112.118\n",
    )
    # Model the perch: the LAN Mini is unreachable; tailnet Mini + box are up.
    probe = _SocketProbe(unreachable={"10.0.0.10"})
    _patch(monkeypatch, probe, gateway="10.0.0.1", instance_file=inst)
    assert net_cmd._out_of_band_reachable() is True
    assert ("10.0.0.10", 22) not in probe.probes


def test_off_lan_gate_still_refuses_when_tailnet_box_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Zero-masking holds off-LAN: tailnet box down → the gate STILL refuses.

    Retargeting to the tailnet must not weaken the Firewalla leg — if the box that
    performs the recovery re-lease is unreachable over the tailnet, there is still no
    runnable rollback, so the gate fails-closed exactly as it does on the LAN.
    """
    inst = _write_instance(
        tmp_path,
        "devices:\n"
        "  firewalla:\n    host: 100.68.36.16\n"
        "  mini:\n    host: bert@100.107.112.118\n",
    )
    probe = _SocketProbe(unreachable={"100.68.36.16"})  # tailnet box down
    _patch(monkeypatch, probe, gateway="10.0.0.1", instance_file=inst)
    assert net_cmd._out_of_band_reachable() is False
