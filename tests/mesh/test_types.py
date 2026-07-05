from sanctum_cli.mesh.types import (
    ArtifactKind,
    ArtifactRef,
    ChampionManifest,
    MeshIdentity,
    Verdict,
)


def _sample_manifest() -> ChampionManifest:
    return ChampionManifest(
        content_hash="sha256:abc",
        kind=ArtifactKind.LORA_ADAPTER,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        size_bytes=42_000_000,
        producer_pubkey="mldsa:PUB",
        signature="sig:XYZ",
    )


def test_manifest_roundtrip() -> None:
    m = _sample_manifest()
    assert ChampionManifest.from_dict(m.to_dict()) == m
    assert m.kind is ArtifactKind.LORA_ADAPTER


def test_manifest_to_dict_is_json_native() -> None:
    d = _sample_manifest().to_dict()
    # kind must serialize to its string value, not a live enum instance.
    assert d["kind"] == "lora_adapter"
    assert type(d["kind"]) is str
    assert d["eval_scores"] == {"tiered": 0.897}


def test_manifest_to_dict_copies_scores() -> None:
    m = _sample_manifest()
    d = m.to_dict()
    d["eval_scores"]["tiered"] = 0.0
    # Mutating the exported dict must not corrupt the frozen manifest.
    assert m.eval_scores["tiered"] == 0.897


def test_from_dict_accepts_full_weights_kind() -> None:
    m = _sample_manifest()
    d = m.to_dict()
    d["kind"] = "full_weights"
    assert ChampionManifest.from_dict(d).kind is ArtifactKind.FULL_WEIGHTS


def test_artifact_kind_values() -> None:
    assert ArtifactKind.LORA_ADAPTER.value == "lora_adapter"
    assert ArtifactKind.FULL_WEIGHTS.value == "full_weights"


def test_mesh_identity_constructs_and_is_frozen() -> None:
    ident = MeshIdentity(pubkey="mldsa:PUB", label="haus-x", created="2026-07-05T00:00:00Z")
    assert ident.pubkey == "mldsa:PUB"
    assert ident.label == "haus-x"
    assert ident == MeshIdentity(pubkey="mldsa:PUB", label="haus-x", created="2026-07-05T00:00:00Z")


def test_artifact_ref_carries_seeders_and_manifest() -> None:
    m = _sample_manifest()
    ref = ArtifactRef(content_hash="sha256:abc", seeders=["100.64.0.1"], manifest=m)
    assert ref.content_hash == m.content_hash
    assert ref.seeders == ["100.64.0.1"]
    assert ref.manifest is m


def test_verdict_fields() -> None:
    v = Verdict(promoted=True, reason="all gates passed", stage="promote")
    assert v.promoted is True
    assert v.reason == "all gates passed"
    assert v.stage == "promote"
    assert v == Verdict(promoted=True, reason="all gates passed", stage="promote")
