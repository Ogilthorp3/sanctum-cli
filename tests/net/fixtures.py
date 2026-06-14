from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Canned command outputs keyed by scenario, used by a FakeRunner in tests.
DOUBLE_NAT: dict[tuple[str, ...], str] = {
    ("traceroute",): " 1  10.0.0.1  1 ms\n 2  192.168.2.1  2 ms\n",
    ("route",): "       gateway: 10.0.0.1\n",
    ("ifconfig",): "en1: flags=8863 mtu 1500\n\tinet 192.168.2.10 netmask 0xffffff00\n",
    ("fw_wan_ip",): "192.168.2.10",
    ("fw_wan_mac",): "20:6d:31:51:67:82",
    ("public_ip",): "70.53.241.21",
}
SINGLE_NAT: dict[tuple[str, ...], str] = {
    ("traceroute",): " 1  10.0.0.1  1 ms\n 2  70.53.0.1  2 ms\n",
    ("route",): "       gateway: 10.0.0.1\n",
    ("ifconfig",): "en1: flags=8863 mtu 1500\n\tinet 70.53.241.21 netmask 0xfffffc00\n",
    ("fw_wan_ip",): "70.53.241.21",
    ("fw_wan_mac",): "20:6d:31:51:67:82",
    ("public_ip",): "70.53.241.21",
}
APIPA: dict[tuple[str, ...], str] = {
    ("traceroute",): " 1  10.0.0.1  1 ms\n 2  * * *\n",
    ("route",): "       gateway: 10.0.0.1\n",
    ("ifconfig",): "en1: flags=8863 mtu 1500\n\tinet 169.254.10.4 netmask 0xffff0000\n",
    ("fw_wan_ip",): "169.254.10.4",
    ("fw_wan_mac",): "20:6d:31:51:67:82",
    ("public_ip",): "",
}
CGNAT: dict[tuple[str, ...], str] = {
    ("traceroute",): " 1  10.0.0.1  1 ms\n 2  100.64.0.1  2 ms\n",
    ("route",): "       gateway: 10.0.0.1\n",
    ("ifconfig",): "en1: flags=8863 mtu 1500\n\tinet 100.96.0.5 netmask 0xffc00000\n",
    ("fw_wan_ip",): "100.96.0.5",
    ("fw_wan_mac",): "20:6d:31:51:67:82",
    ("public_ip",): "203.0.113.9",
}
NO_FIREWALLA: dict[tuple[str, ...], str] = {
    ("traceroute",): " 1  192.168.0.1  1 ms\n 2  70.53.0.1  2 ms\n",
    ("route",): "       gateway: 192.168.0.1\n",
    ("ifconfig",): "en0: flags=8863 mtu 1500\n\tinet 192.168.0.20 netmask 0xffffff00\n",
    ("fw_wan_ip",): "",
    ("fw_wan_mac",): "",
    ("public_ip",): "70.53.241.21",
}


class FakeRunner:
    """Maps a probe-kind tag to canned output (the production runner shells out)."""

    def __init__(self, table: dict[tuple[str, ...], str]) -> None:
        self._table = table

    def __call__(self, tag: tuple[str, ...]) -> str:
        return self._table.get(tag, "")


def fake_http(status: int, title: str) -> Callable[[str], tuple[int, str]]:
    def _probe(url: str) -> tuple[int, str]:
        return (status, title)

    return _probe
