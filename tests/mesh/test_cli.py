"""CLI tests for ``sanctum mesh`` — join / status / pull / seed (Task 7).

Every external boundary is an injected seam behind a module-level builder in
``sanctum_cli.commands.mesh``; these tests monkeypatch those builders with
deterministic fakes so NO live tailnet / tracker / VM is touched:

* ``_build_command_runner`` -> a fake that returns canned ``tailscale status``
  JSON (tailnet up/down, self address);
* ``_build_identity_store``  -> a real ``MeshIdentityStore`` bound to a
  ``FakeSigner`` and a tmp path (mint/load is exercised for real, the crypto is
  faked);
* ``_build_directory``       -> a recording ``FakeDirectory`` (register / peers /
  catalog / announce / find);
* ``_build_adopt_seams``     -> fakes for the verify -> eval -> sandbox -> promote
  pipeline.

The load-bearing property under test is HONEST-VERIFY: every ``joined ✓`` /
``registered ✓`` is derived from a real observed outcome (a live status read + a
real tracker ack), never assumed.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import mesh as mesh_cmd
from sanctum_cli.mesh.artifact import build_manifest
from sanctum_cli.mesh.identity import MeshIdentityStore
from sanctum_cli.mesh.types import ArtifactRef
from sanctum_cli.mesh.verify import SandboxResult

if TYPE_CHECKING:
    from pathlib import Path

    from sanctum_cli.mesh.identity import LoadedIdentity
    from sanctum_cli.mesh.types import ChampionManifest, MeshIdentity

runner = CliRunner()


# ─── fakes ───────────────────────────────────────────────────────────────


class FakeSigner:
    """Deterministic non-crypto stand-in for the ML-DSA signer (matches the
    identity/seed test fakes): a keypair shares ``token`` and a signature is
    ``sha256(token | message)`` so ``verify`` recomputes from the public token."""

    def __init__(self, token: str = "cli-test-token") -> None:
        self._token = token

    def generate(self) -> tuple[str, str]:
        return (f"fakepub:{self._token}", f"fakepriv:{self._token}")

    def sign(self, private_key: str, message: bytes) -> str:
        token = private_key.split(":", 1)[1]
        return "fakesig:" + hashlib.sha256(token.encode() + b"|" + message).hexdigest()

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        token = public_key.split(":", 1)[1]
        expected = "fakesig:" + hashlib.sha256(token.encode() + b"|" + message).hexdigest()
        return signature == expected


class FakeDirectory:
    """A recording mesh directory: register / peers / catalog / announce / find."""

    def __init__(
        self,
        *,
        peers: tuple[str, ...] = (),
        catalog: tuple[ChampionManifest, ...] = (),
        find_result: ArtifactRef | None = None,
        register_ack: bool = True,
    ) -> None:
        self._peers = list(peers)
        self._catalog = list(catalog)
        self._find_result = find_result
        self._register_ack = register_ack
        self.register_calls: list[tuple[MeshIdentity, str]] = []
        self.announce_calls: list[tuple[ChampionManifest, str]] = []
        self.find_calls: list[str] = []

    def register(self, identity: MeshIdentity, addr: str) -> bool:
        self.register_calls.append((identity, addr))
        return self._register_ack

    def peers(self) -> list[str]:
        return list(self._peers)

    def catalog(self) -> list[ChampionManifest]:
        return list(self._catalog)

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        self.announce_calls.append((manifest, addr))

    def find(self, content_hash: str) -> ArtifactRef | None:
        self.find_calls.append(content_hash)
        return self._find_result


class FakeVerifier:
    def __init__(self, *, hash_ok: bool = True, sig_ok: bool = True) -> None:
        self._hash_ok = hash_ok
        self._sig_ok = sig_ok

    def verify_hash(self, ref: ArtifactRef) -> bool:
        return self._hash_ok

    def verify_signature(self, ref: ArtifactRef) -> bool:
        return self._sig_ok


class FakeEval:
    def __init__(self, score: float) -> None:
        self._score = score

    def score(self, ref: ArtifactRef) -> float:
        return self._score


class FakeSandbox:
    def __init__(self, *, ok: bool = True, egress: bool = False, notes: str = "") -> None:
        self._ok = ok
        self._egress = egress
        self._notes = notes

    def probe(self, ref: ArtifactRef) -> SandboxResult:
        return SandboxResult(ok=self._ok, egress_attempted=self._egress, notes=self._notes)


class RecordingPromote:
    def __init__(self) -> None:
        self.calls: list[ArtifactRef] = []

    def __call__(self, ref: ArtifactRef) -> None:
        self.calls.append(ref)


# ─── helpers ─────────────────────────────────────────────────────────────


def _runner_up(addr: str = "100.64.0.5"):
    payload = json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": [addr]}})

    def run(argv: list[str]) -> str:
        return payload

    return run


def _runner_down():
    payload = json.dumps({"BackendState": "Stopped", "Self": {"TailscaleIPs": []}})

    def run(argv: list[str]) -> str:
        return payload

    return run


def _make_store(tmp_path: Path, token: str = "cli-test-token") -> MeshIdentityStore:
    return MeshIdentityStore(signer=FakeSigner(token), path=tmp_path / "id")


def _make_ref(tmp_path: Path) -> tuple[ArtifactRef, LoadedIdentity]:
    ident = _make_store(tmp_path).ensure(label="peer-haus")
    art = tmp_path / "champ"
    art.mkdir()
    (art / "adapters.safetensors").write_bytes(b"WEIGHTS")
    manifest = build_manifest(
        art, ident, base_model="qwen3.6-35b-a3b-4bit", eval_scores={"tiered": 0.9}
    )
    return ArtifactRef(content_hash=manifest.content_hash, seeders=["100.64.0.9"], manifest=manifest), ident


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run=None,
    store: MeshIdentityStore | None = None,
    directory: FakeDirectory | None = None,
    seams: object | None = None,
) -> None:
    if run is not None:
        monkeypatch.setattr(mesh_cmd, "_build_command_runner", lambda: run)
    if store is not None:
        monkeypatch.setattr(mesh_cmd, "_build_identity_store", lambda: store)
    if directory is not None:
        monkeypatch.setattr(mesh_cmd, "_build_directory", lambda: directory)
    if seams is not None:
        monkeypatch.setattr(mesh_cmd, "_build_adopt_seams", lambda: seams)


@pytest.fixture(autouse=True)
def _isolate_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config at a tmp instance.yaml so tests never read/write Bert's real one."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "instance.yaml"))


# ─── join ────────────────────────────────────────────────────────────────


def test_join_up_and_ack_reports_joined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(tmp_path)
    _peer_manifest_ref, _ = _make_ref(tmp_path)
    directory = FakeDirectory(
        peers=("100.64.0.9", "100.64.0.10"),
        catalog=(_peer_manifest_ref.manifest,),
        register_ack=True,
    )
    _install(monkeypatch, run=_runner_up("100.64.0.5"), store=store, directory=directory)

    result = runner.invoke(app, ["mesh", "join", "--label", "haus-alpha"])

    assert result.exit_code == 0, result.stdout
    # Honest joined ✓ derives from a real tailnet read AND a real tracker ack.
    assert "Joined the Sanctum mesh" in result.stdout
    assert "mldsa:" in result.stdout  # identity fingerprint
    # register was called exactly once with THIS node's tailnet addr.
    assert len(directory.register_calls) == 1
    assert directory.register_calls[0][1] == "100.64.0.5"
    # Peer + champion counts are the real directory answers.
    assert "Peers discovered: 2" in result.stdout
    assert "Champions available to pull: 1" in result.stdout
    # Identity was actually minted on disk.
    assert store.identity_file.exists()


def test_join_tailnet_down_does_not_claim_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path)
    directory = FakeDirectory(register_ack=True)
    _install(monkeypatch, run=_runner_down(), store=store, directory=directory)

    result = runner.invoke(app, ["mesh", "join", "--yes"])

    assert result.exit_code == 0, result.stdout
    # NEVER claim joined when the tailnet was not observed up.
    assert "Joined the Sanctum mesh" not in result.stdout
    assert "Tailnet not up" in result.stdout
    # And do NOT register a node that has no reachable tailnet address.
    assert directory.register_calls == []


def test_join_registration_declined_is_not_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path)
    directory = FakeDirectory(register_ack=False)  # tracker did NOT ack
    _install(monkeypatch, run=_runner_up(), store=store, directory=directory)

    result = runner.invoke(app, ["mesh", "join"])

    assert result.exit_code == 0, result.stdout
    assert len(directory.register_calls) == 1  # we tried
    assert "Joined the Sanctum mesh" not in result.stdout  # but did not ack


def test_join_persists_label_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanctum_cli import config

    store = _make_store(tmp_path)
    directory = FakeDirectory(register_ack=True)
    _install(monkeypatch, run=_runner_up(), store=store, directory=directory)

    result = runner.invoke(app, ["mesh", "join", "--label", "config-haus"])

    assert result.exit_code == 0, result.stdout
    assert config.instance_value("mesh.label") == "config-haus"


def test_join_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(tmp_path)
    directory = FakeDirectory(register_ack=True)
    _install(monkeypatch, run=_runner_up(), store=store, directory=directory)

    first = runner.invoke(app, ["mesh", "join", "--label", "haus-alpha"])
    second = runner.invoke(app, ["mesh", "join", "--label", "haus-alpha"])

    assert first.exit_code == 0 and second.exit_code == 0
    fp_first = _fingerprint_line(first.stdout)
    fp_second = _fingerprint_line(second.stdout)
    # Same identity both runs — join twice mints once (no re-mint).
    assert fp_first == fp_second


def _fingerprint_line(stdout: str) -> str:
    for line in stdout.splitlines():
        if "mldsa:" in line:
            return line[line.index("mldsa:") :]
    raise AssertionError(f"no fingerprint in output:\n{stdout}")


# ─── status ──────────────────────────────────────────────────────────────


def test_status_reports_identity_peers_and_champions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path)
    store.ensure(label="haus-alpha")  # pre-mint so status has an identity to show
    ref, _ = _make_ref(tmp_path)
    directory = FakeDirectory(
        peers=("100.64.0.9", "100.64.0.10", "100.64.0.11"),
        catalog=(ref.manifest,),
    )
    _install(monkeypatch, store=store, directory=directory)

    result = runner.invoke(app, ["mesh", "status"])

    assert result.exit_code == 0, result.stdout
    assert "mldsa:" in result.stdout
    assert "Peers: 3" in result.stdout
    assert "Champions available: 1" in result.stdout


def test_status_without_identity_prompts_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path)  # never minted
    directory = FakeDirectory()
    _install(monkeypatch, store=store, directory=directory)

    result = runner.invoke(app, ["mesh", "status"])

    assert result.exit_code == 0, result.stdout
    assert "No mesh identity" in result.stdout
    # A read-only status must NOT mint an identity as a side effect.
    assert not store.identity_file.exists()


# ─── pull ────────────────────────────────────────────────────────────────


def _seams(**kw: object) -> object:
    from sanctum_cli.commands.mesh import AdoptSeams

    verifier = kw.get("verifier") or FakeVerifier()
    eval_gate = kw.get("eval_gate") or FakeEval(0.9)
    sandbox = kw.get("sandbox") or FakeSandbox()
    promote = kw.get("promote") or RecordingPromote()
    return AdoptSeams(verifier=verifier, eval_gate=eval_gate, sandbox=sandbox, promote=promote)


def test_pull_promotes_when_all_gates_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, _ = _make_ref(tmp_path)
    directory = FakeDirectory(find_result=ref)
    promote = RecordingPromote()
    seams = _seams(eval_gate=FakeEval(0.95), promote=promote)
    _install(monkeypatch, directory=directory, seams=seams)

    result = runner.invoke(app, ["mesh", "pull", ref.content_hash])

    assert result.exit_code == 0, result.stdout
    assert "Adopted" in result.stdout
    assert directory.find_calls == [ref.content_hash]
    assert len(promote.calls) == 1  # champion was actually promoted


def test_pull_rejects_on_eval_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, _ = _make_ref(tmp_path)
    directory = FakeDirectory(find_result=ref)
    promote = RecordingPromote()
    seams = _seams(eval_gate=FakeEval(0.5), promote=promote)  # below the 0.881 baseline
    _install(monkeypatch, directory=directory, seams=seams)

    result = runner.invoke(app, ["mesh", "pull", ref.content_hash])

    assert result.exit_code != 0
    assert "Not adopted" in result.stdout
    assert "eval" in result.stdout  # the stage that rejected
    assert promote.calls == []  # local champion stays authoritative


def test_pull_rejects_on_sandbox_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, _ = _make_ref(tmp_path)
    directory = FakeDirectory(find_result=ref)
    promote = RecordingPromote()
    seams = _seams(sandbox=FakeSandbox(ok=True, egress=True, notes="blocked 8.8.8.8"), promote=promote)
    _install(monkeypatch, directory=directory, seams=seams)

    result = runner.invoke(app, ["mesh", "pull", ref.content_hash])

    assert result.exit_code != 0
    assert "sandbox" in result.stdout
    assert promote.calls == []


def test_pull_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = FakeDirectory(find_result=None)
    seams = _seams()
    _install(monkeypatch, directory=directory, seams=seams)

    result = runner.invoke(app, ["mesh", "pull", "sha256:deadbeef"])

    assert result.exit_code != 0
    assert "Not adopted" in result.stdout


# ─── seed ────────────────────────────────────────────────────────────────


def _champion(tmp_path: Path) -> Path:
    d = tmp_path / "mychamp"
    d.mkdir()
    (d / "adapters.safetensors").write_bytes(b"MY-WEIGHTS")
    (d / "adapter_config.json").write_bytes(b'{"r": 32}')
    return d


def test_seed_announces_when_beating_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path)
    directory = FakeDirectory()
    _install(monkeypatch, run=_runner_up("100.64.0.5"), store=store, directory=directory)
    champ = _champion(tmp_path)

    result = runner.invoke(
        app,
        ["mesh", "seed", str(champ), "--base-model", "qwen3.6-35b-a3b-4bit", "--score", "tiered=0.897"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Seeded" in result.stdout
    assert len(directory.announce_calls) == 1
    announced_manifest, announced_addr = directory.announce_calls[0]
    assert announced_addr == "100.64.0.5"
    assert announced_manifest.base_model == "qwen3.6-35b-a3b-4bit"


def test_seed_not_announced_below_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path)
    directory = FakeDirectory()
    _install(monkeypatch, run=_runner_up(), store=store, directory=directory)
    champ = _champion(tmp_path)

    result = runner.invoke(
        app,
        ["mesh", "seed", str(champ), "--base-model", "qwen", "--score", "tiered=0.5"],
    )

    assert result.exit_code != 0
    assert "Not seeded" in result.stdout
    assert directory.announce_calls == []  # a regressing champion is never advertised


# ─── registration ────────────────────────────────────────────────────────


def test_mesh_app_registered_with_all_subcommands() -> None:
    result = runner.invoke(app, ["mesh", "--help"])
    assert result.exit_code == 0, result.stdout
    for sub in ("join", "status", "pull", "seed"):
        assert sub in result.stdout


# ─── unit: pure helpers (honest-verify primitives) ───────────────────────


def test_read_tailnet_running_parses_addr() -> None:
    status = mesh_cmd.read_tailnet(_runner_up("100.64.0.7"))
    assert status.up is True
    assert status.self_addr == "100.64.0.7"


def test_read_tailnet_stopped_is_not_up() -> None:
    status = mesh_cmd.read_tailnet(_runner_down())
    assert status.up is False
    assert status.self_addr is None


def test_read_tailnet_garbage_is_not_up() -> None:
    status = mesh_cmd.read_tailnet(lambda argv: "not json")
    assert status.up is False
    assert status.self_addr is None


def test_fingerprint_is_stable_and_masks_key() -> None:
    fp = mesh_cmd.fingerprint("fakepub:abc")
    assert fp.startswith("mldsa:")
    assert "fakepub:abc" not in fp  # never leak the key material
    assert mesh_cmd.fingerprint("fakepub:abc") == fp  # deterministic
