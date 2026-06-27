"""``net._out_of_band_reachable`` — the single-NAT cutover's recovery-path gate.

The cutover briefly drops the WAN, so before any mutation the orchestrator's start
precondition (:func:`sanctum_cli.devices.flip.gate_ok`) demands an out-of-band
recovery path. The gate is two-sided, not one:

* the **Mini jump host** — the out-of-band link the operator reaches the box over
  if the cutover strands the hub; and
* the **Firewalla** — the host that ACTUALLY PERFORMS the recovery re-lease. On a
  failed cutover (or an explicit ``--rollback``) the unwind disables DMZ and then
  fires the ``dhcp_release`` runner tag, which SSHes to the Firewalla
  (``sanctum_cli.net.system._fw_mutate_via_ssh`` → ``ssh pi@<gateway>``) to
  release + re-acquire the downstream WAN lease. If the Firewalla is unreachable,
  the recovery re-lease cannot run — so a gate that probed ONLY the Mini would
  green-light a cutover whose rollback can never bring the WAN back: the exact
  fail-to-DARK the gate exists to prevent.

These tests author their expectations from the REAL recovery transport (the
``dhcp_release`` op SSHes to the default gateway on port 22), not from the gate's
own assumption — and mock ONLY the genuinely external edge (``socket.create_connection``),
asserting on the (host, port) tuples the gate actually probes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.commands import net as net_cmd

if TYPE_CHECKING:
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
) -> None:
    """Point the gate at the recording socket probe + a fixed Firewalla gateway.

    The Firewalla recovery host is resolved the SAME way ``_build_runner`` resolves
    it for the ``dhcp_release`` op — :func:`net._firewalla_host` (``devices.firewalla.host``
    config override → parsed default gateway) — so the gate and the recovery
    transport target the identical box. We isolate from the machine's real
    instance.yaml (``SANCTUM_INSTANCE_FILE`` → an absent file, so the config override
    is unset and the gateway fallback is exercised), stub the gateway parse to a
    fixed IP, and stub the socket connect to the recorder; no real route/socket is used.

    The PRIMARY (FIX-3) Tailscale-on-box check is a real root-SSH round-trip, NOT a
    socket connect, so it is stubbed separately via ``interlock.tailscale_oob_live``
    (defaults to live so the LAN-side assertions below still exercise).
    """
    # FIX-b: the recovery host now reads config-first; isolate from the real
    # instance.yaml so this test exercises the gateway-fallback deterministically
    # regardless of whether the haus has pinned a tailnet box host.
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", "/nonexistent/sanctum-test-instance.yaml")
    monkeypatch.setattr(net_cmd.socket, "create_connection", probe.__call__)
    monkeypatch.setattr(
        net_cmd.detect, "parse_default_gateway", lambda _out: gateway
    )
    monkeypatch.setattr(net_cmd.system, "real_runner", lambda _tag: "")
    monkeypatch.setattr(
        net_cmd.interlock, "tailscale_oob_live", lambda **_kw: tailscale_live
    )


def test_gate_probes_both_the_mini_and_the_firewalla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate probes BOTH the Mini jump host AND the Firewalla recovery host.

    The recovery re-lease SSHes to the Firewalla; the gate must confirm THAT host
    is reachable (port 22), not only the Mini — otherwise it green-lights a cutover
    whose rollback can never re-lease the WAN.
    """
    probe = _SocketProbe()
    _patch(monkeypatch, probe, gateway="10.0.0.1")
    assert net_cmd._out_of_band_reachable() is True
    probed_hosts = {host for host, _port in probe.probes}
    # The Mini jump host (the out-of-band link) …
    assert net_cmd._OUT_OF_BAND_HOST in probed_hosts
    # … AND the Firewalla (the host that performs the recovery re-lease).
    assert "10.0.0.1" in probed_hosts
    # Both probed on the SSH port the recovery transport uses.
    assert all(port == 22 for _host, port in probe.probes)


def test_gate_refuses_when_firewalla_unreachable_even_if_mini_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firewalla down → the gate REFUSES, even with the Mini reachable.

    This is the core of the fix: a reachable Mini is NOT sufficient if the box that
    performs the recovery re-lease is unreachable. The gate must fail so the cutover
    refuses rather than strand the household with an un-runnable rollback.
    """
    probe = _SocketProbe(unreachable={"10.0.0.1"})  # Firewalla down, Mini up
    _patch(monkeypatch, probe, gateway="10.0.0.1")
    assert net_cmd._out_of_band_reachable() is False


def test_gate_refuses_when_mini_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mini down → the gate REFUSES (the original one-sided contract still holds)."""
    probe = _SocketProbe(unreachable={net_cmd._OUT_OF_BAND_HOST})
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

    With the Mini + Firewalla both reachable on the LAN but the Tailscale-on-box
    root-SSH round-trip NOT live, the gate must REFUSE — the LAN-bound hosts are
    exactly the ones that died with the LAN on 06-26, so without the tailnet safety
    net there is no channel proven to survive the cutover.
    """
    probe = _SocketProbe()  # both LAN hosts reachable
    _patch(monkeypatch, probe, gateway="10.0.0.1", tailscale_live=False)
    assert net_cmd._out_of_band_reachable() is False


def test_gate_requires_tailscale_then_both_lan_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three layers live (Tailscale + Mini + Firewalla) → the gate passes."""
    probe = _SocketProbe()
    _patch(monkeypatch, probe, gateway="10.0.0.1", tailscale_live=True)
    assert net_cmd._out_of_band_reachable() is True
