from sanctum_cli.discovery.sources import arp_cache


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
