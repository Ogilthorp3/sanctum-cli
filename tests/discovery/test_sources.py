from sanctum_cli.discovery.sources import arp_cache, router_clients, ssdp


def _runner(output: str):
    def run(argv: tuple[str, ...]) -> str:
        assert argv[0] == "arp"
        return output
    return run


def test_arp_cache_parses_ip_and_mac():
    out = (
        "? (10.0.0.1) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]\n"
        "? (10.0.0.5) at 11:22:33:44:55:66 on en0 ifscope [ethernet]\n"
        "? (10.0.0.9) at (incomplete) on en0 ifscope [ethernet]\n"
    )
    cands = {c.ip: c for c in arp_cache(_runner(out))}
    assert set(cands) == {"10.0.0.1", "10.0.0.5"}          # incomplete entry skipped
    assert cands["10.0.0.5"].mac == "11:22:33:44:55:66"
    assert "arp" in cands["10.0.0.5"].hints


def test_arp_cache_fails_open_on_runner_error():
    def boom(argv):
        raise OSError("no arp")
    assert list(arp_cache(boom)) == []


def test_ssdp_parses_location_ip_from_responses():
    responses = [
        "HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.5:5000/desc.xml\r\nST: upnp:rootdevice\r\n\r\n",
        "HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.1:80/igd.xml\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n",
        "garbage without a location",
    ]
    cands = {c.ip: c for c in ssdp(search=lambda: responses)}
    assert set(cands) == {"10.0.0.5", "10.0.0.1"}
    assert any(h.startswith("ssdp:") for h in cands["10.0.0.1"].hints)


def test_ssdp_fails_open():
    def boom():
        raise OSError("no socket")
    assert list(ssdp(search=boom)) == []


def test_router_clients_empty_without_lister():
    # No provider exposes a client table yet → empty, never raises.
    assert list(router_clients(lister=None)) == []
