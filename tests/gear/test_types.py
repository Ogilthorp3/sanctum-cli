from sanctum_cli.gear.types import Candidate, DiscoveredDevice, HausInventory


def test_candidate_merges_hints_and_dedupes_by_ip():
    a = Candidate(ip="10.0.0.5", mac="aa:bb", hostname=None, hints=frozenset({"ssdp:rootdevice"}))
    b = Candidate(ip="10.0.0.5", mac=None, hostname="orbi", hints=frozenset({"arp"}))
    merged = a.merge(b)
    assert merged.ip == "10.0.0.5"
    assert merged.mac == "aa:bb"           # first non-None wins
    assert merged.hostname == "orbi"
    assert merged.hints == frozenset({"ssdp:rootdevice", "arp"})


def test_inventory_counts_recognized_and_unrecognized():
    dev = DiscoveredDevice(kind="orbi", brand="orbi", ip="10.0.0.5", name="Orbi", score=1.0)
    inv = HausInventory(devices=[dev], unrecognized_count=3)
    assert [d.kind for d in inv.devices] == ["orbi"]
    assert inv.unrecognized_count == 3
    assert inv.recognized_count == 1
