from sanctum_cli.devices.base import NetContext
from sanctum_cli.gear.scan import discover_haus
from sanctum_cli.gear.types import Candidate


class _FakeProvider:
    def __init__(self, kind, brand, match_ips):
        self.kind = kind
        self.brand = brand
        self._match = set(match_ips)
    def detect(self, net):                      # instance detect for the fake registry
        return 1.0 if net.gateway_ip in self._match else 0.0


def _fingerprint(providers):
    def fp(ip, *, runner):
        for p in providers:
            if p.detect(NetContext(gateway_ip=ip, runner=runner)) > 0:
                return (p.kind, p.brand, 1.0)
        return None
    return fp


def test_discover_recognizes_matching_candidate_counts_the_rest():
    hub = _FakeProvider("hub", "sagemcom", {"192.168.2.1"})
    orbi = _FakeProvider("orbi", "orbi", {"10.0.0.5"})
    net = NetContext(gateway_ip="192.168.2.1", runner=lambda a: "")
    inv = discover_haus(
        net,
        allow_active=True,
        sources=[lambda: [Candidate(ip="10.0.0.5"), Candidate(ip="10.0.0.9")]],
        fingerprint=_fingerprint([hub, orbi]),
    )
    kinds = {d.kind: d.ip for d in inv.devices}
    assert kinds == {"hub": "192.168.2.1", "orbi": "10.0.0.5"}   # gateway + candidate
    assert inv.unrecognized_count == 1                            # 10.0.0.9 unrecognized


def test_discover_passive_only_skips_candidate_fingerprint_but_still_probes_gateway():
    hub = _FakeProvider("hub", "sagemcom", {"192.168.2.1"})
    orbi = _FakeProvider("orbi", "orbi", {"10.0.0.5"})
    calls = []
    def fp(ip, *, runner):
        calls.append(ip)
        for p in (hub, orbi):
            if p.detect(NetContext(gateway_ip=ip, runner=runner)) > 0:
                return (p.kind, p.brand, 1.0)
        return None
    net = NetContext(gateway_ip="192.168.2.1", runner=lambda a: "")
    inv = discover_haus(net, allow_active=False,
                        sources=[lambda: [Candidate(ip="10.0.0.5")]], fingerprint=fp)
    assert calls == ["192.168.2.1"]                 # gateway probed; candidate NOT (no consent)
    assert {d.kind for d in inv.devices} == {"hub"}
    assert inv.unrecognized_count == 1              # the un-probed candidate counts as unknown


def test_discover_fails_open_on_source_error():
    net = NetContext(gateway_ip=None, runner=lambda a: "")
    def boom():
        raise OSError("nope")
    inv = discover_haus(net, allow_active=True, sources=[boom],
                        fingerprint=lambda ip, *, runner: None)
    assert inv.devices == [] and inv.unrecognized_count == 0
