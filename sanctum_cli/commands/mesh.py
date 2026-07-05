"""``sanctum mesh`` — join the open Sanctum mesh, then pull + seed champions.

Four subcommands, mirroring the ``sanctum net`` guided/reversible shape:

* ``join``   — ensure Tailscale-on-box is up, mint the mesh identity (if absent),
               register with the discovery tracker, and print an HONEST recap:
               ``joined ✓`` derives from a real ``tailscale status`` read AND a
               real tracker ack — never assumed.
* ``status`` — this node's identity fingerprint + N discovered peers + M
               champions available to pull.
* ``pull``   — find a champion by content hash and run the ``adopt`` pipeline
               (hash → signature → eval → sandbox → promote); a rejected champion
               leaves the local one authoritative.
* ``seed``   — sign + announce a local champion to the mesh, iff it beats the
               local baseline (the mirror of ``adopt``'s eval gate).

Every external dependency is an injected seam behind a module-level builder
(:func:`_build_command_runner` / :func:`_build_identity_store` /
:func:`_build_directory` / :func:`_build_adopt_seams`), so the whole CLI is
unit-tested with fakes and touches no live tailnet / tracker / VM. The real
ML-DSA signer, HTTP tracker, eval-gate, and VM-sandbox adapters are wired into
those builders in Task 8; until then the non-``CommandRunner`` builders raise a
clear :class:`~sanctum_cli.errors.LocalError` — the pure orchestration and
honest-verify proven here stay unchanged when the adapters land.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - Typer evaluates the annotation at decoration time
from typing import TYPE_CHECKING, Annotated, Protocol

import typer
import yaml
from rich.console import Console
from rich.markup import escape

from sanctum_cli import config
from sanctum_cli.errors import ExitCode, LocalError, SanctumError, UserError
from sanctum_cli.mesh.seed import seed as seed_local
from sanctum_cli.mesh.types import ArtifactKind, Verdict
from sanctum_cli.mesh.verify import adopt

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sanctum_cli.mesh.identity import LoadedIdentity, MeshIdentityStore
    from sanctum_cli.mesh.types import ArtifactRef, ChampionManifest, MeshIdentity
    from sanctum_cli.mesh.verify import EvalGate, ManifestVerifier, Sandbox

__all__ = [
    "AdoptSeams",
    "JoinReport",
    "MeshDirectory",
    "StatusReport",
    "TailnetStatus",
    "fingerprint",
    "join_mesh",
    "mesh_app",
    "mesh_status",
    "pull_champion",
    "read_tailnet",
    "seed_champion",
]

CommandRunner = Callable[[list[str]], str]
"""A thin subprocess seam (argv → stdout) so the tailscale reads are testable."""

_TS_BINARY = "tailscale"
_DEFAULT_BASELINE = 0.881
_DEFAULT_BASELINE_METRIC = "tiered"
_ADAPTERS_PENDING = (
    "mesh adapters are not wired yet (Task 8: ML-DSA signer / HTTP tracker / "
    "eval-gate / VM sandbox). The mesh orchestration is complete and unit-tested; "
    "the real network/crypto boundary lands with the adapters."
)

console = Console()
err_console = Console(stderr=True)
mesh_app = typer.Typer(help="Join the open Sanctum mesh; pull and seed council champions.")


# ─── injected seams ──────────────────────────────────────────────────────


class MeshDirectory(Protocol):
    """The discovery/tracker surface the ``sanctum mesh`` CLI consumes.

    A superset of the Task-4 :class:`~sanctum_cli.mesh.discovery.Discovery` shape
    (``announce`` / ``find`` / ``peers``) plus the two node-level operations the
    bootstrap needs: ``register`` (join advertises this node + its addr and returns
    the tracker's ack — the real outcome behind the "registered ✓") and ``catalog``
    (status enumerates the champions currently advertised). Task 8's HTTP-tracker
    adapter implements this; unit tests inject a recording fake.
    """

    def register(self, identity: MeshIdentity, addr: str) -> bool:
        """Advertise ``identity`` at ``addr``; return the tracker's ack."""
        ...

    def peers(self) -> list[str]:
        """Return the addresses of currently-known mesh peers."""
        ...

    def catalog(self) -> list[ChampionManifest]:
        """Return the champion manifests currently advertised on the mesh."""
        ...

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        """Advertise that ``addr`` seeds the artifact described by ``manifest``."""
        ...

    def find(self, content_hash: str) -> ArtifactRef | None:
        """Return the seeders + manifest for ``content_hash``, or ``None``."""
        ...


@dataclass(frozen=True)
class AdoptSeams:
    """The four seams :func:`pull_champion` threads into the ``adopt`` pipeline.

    Bundled so the ``pull`` command has a single builder/monkeypatch point. The
    real adapters (a hash+signature verifier bound to the downloaded artifact, the
    autoresearch eval gate, the VM air-gap sandbox, and the local-champion promote)
    are assembled in Task 8; unit tests inject fakes.
    """

    verifier: ManifestVerifier
    eval_gate: EvalGate
    sandbox: Sandbox
    promote: Callable[[ArtifactRef], None]


# ─── pure helpers (honest-verify primitives) ─────────────────────────────


@dataclass(frozen=True)
class TailnetStatus:
    """The slice of ``tailscale status`` the mesh needs: is it up + our addr."""

    up: bool
    self_addr: str | None


def read_tailnet(run: CommandRunner) -> TailnetStatus:
    """Read the live tailnet state behind an injected runner (a single shell-out).

    Parses ``tailscale status --json``: ``up`` is True only when the backend
    reports ``Running`` (logged-in AND connected), and ``self_addr`` is this
    node's first Tailscale IP. Any exec/parse failure degrades to
    ``TailnetStatus(up=False, self_addr=None)`` — the mesh never claims a tailnet
    it could not actually observe.
    """
    raw = run([_TS_BINARY, "status", "--json"])
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return TailnetStatus(up=False, self_addr=None)
    if not isinstance(data, dict):
        return TailnetStatus(up=False, self_addr=None)
    up = data.get("BackendState") == "Running"
    self_addr: str | None = None
    self_node = data.get("Self")
    if isinstance(self_node, dict):
        ips = self_node.get("TailscaleIPs")
        if isinstance(ips, list) and ips and isinstance(ips[0], str):
            self_addr = ips[0]
    return TailnetStatus(up=up, self_addr=self_addr)


def fingerprint(pubkey: str) -> str:
    """A short, stable, quotable id for a mesh public key.

    ``mldsa:<first-16-hex-of-sha256(pubkey)>`` — deterministic and safe to print
    (a digest of the public key, never the key material itself).
    """
    return "mldsa:" + hashlib.sha256(pubkey.encode("utf-8")).hexdigest()[:16]


# ─── reports (structured, honest outcomes) ───────────────────────────────


@dataclass(frozen=True)
class JoinReport:
    """The structured outcome of ``mesh join`` — every field a real observation."""

    tailnet_up: bool
    identity_fingerprint: str
    addr: str | None
    registered: bool
    peers: list[str]
    champions: list[str]

    @property
    def joined(self) -> bool:
        """Honest-verify: joined only when the tailnet is up AND the tracker ack'd."""
        return self.tailnet_up and self.registered


@dataclass(frozen=True)
class StatusReport:
    """The structured outcome of ``mesh status``."""

    identity_fingerprint: str | None
    peers: list[str]
    champions: list[str]


# ─── core orchestration (seams injected; no live boundary) ───────────────


def join_mesh(
    *,
    store: MeshIdentityStore,
    directory: MeshDirectory,
    run: CommandRunner,
    label: str,
) -> JoinReport:
    """Bring the node onto the mesh: tailnet check → mint identity → register.

    The identity is minted (once) via ``store.ensure``. Registration is attempted
    ONLY when the tailnet is really up AND we resolved a self address — a node with
    no reachable address has nothing meaningful to advertise, so we never fake a
    ``registered ✓``. Peer + champion counts are the directory's real answers.
    """
    status = read_tailnet(run)
    identity = store.ensure(label)
    addr = status.self_addr
    registered = False
    if status.up and addr is not None:
        registered = directory.register(identity.identity, addr)
    return JoinReport(
        tailnet_up=status.up,
        identity_fingerprint=fingerprint(identity.pubkey),
        addr=addr,
        registered=registered,
        peers=directory.peers(),
        champions=[m.content_hash for m in directory.catalog()],
    )


def mesh_status(*, store: MeshIdentityStore, directory: MeshDirectory, label: str) -> StatusReport:
    """Read-only snapshot: identity fingerprint (if minted) + peers + champions.

    Deliberately does NOT mint: the fingerprint is shown only when an identity
    already exists on disk, so ``status`` never has the side effect of joining.
    """
    fp: str | None = None
    if store.identity_file.exists():
        # ``ensure`` loads (never mints) when the file exists — the label is ignored.
        fp = fingerprint(store.ensure(label).pubkey)
    return StatusReport(
        identity_fingerprint=fp,
        peers=directory.peers(),
        champions=[m.content_hash for m in directory.catalog()],
    )


def pull_champion(
    content_hash: str,
    *,
    directory: MeshDirectory,
    seams: AdoptSeams,
    baseline: float,
) -> Verdict:
    """Find ``content_hash`` on the mesh and run the ``adopt`` trust pipeline.

    A discovery miss is its own verdict (``stage="discovery"``, not promoted) so a
    typo'd hash is distinguishable from a gate rejection; otherwise the located
    :class:`~sanctum_cli.mesh.types.ArtifactRef` is handed to
    :func:`~sanctum_cli.mesh.verify.adopt`, which keeps the local champion
    authoritative on any failure.
    """
    ref = directory.find(content_hash)
    if ref is None:
        return Verdict(
            promoted=False,
            reason=f"no seeder found for {content_hash}",
            stage="discovery",
        )
    return adopt(
        ref,
        verify_manifest=seams.verifier,
        eval_gate=seams.eval_gate,
        sandbox=seams.sandbox,
        promote=seams.promote,
        baseline=baseline,
    )


def seed_champion(
    champion_dir: Path,
    *,
    identity: LoadedIdentity,
    directory: MeshDirectory,
    addr: str,
    base_model: str,
    eval_scores: Mapping[str, float],
    baseline_scores: Mapping[str, float],
    kind: ArtifactKind = ArtifactKind.LORA_ADAPTER,
) -> ChampionManifest | None:
    """Sign + announce ``champion_dir`` iff it beats the local baseline.

    A thin CLI-side wrapper over :func:`sanctum_cli.mesh.seed.seed`; returns the
    signed manifest that was announced, or ``None`` when the champion did not clear
    the baseline (nothing announced).
    """
    return seed_local(
        champion_dir,
        identity=identity,
        discovery=directory,
        addr=addr,
        base_model=base_model,
        eval_scores=eval_scores,
        baseline_scores=baseline_scores,
        kind=kind,
    )


# ─── config-first resolution + persistence ───────────────────────────────


def _resolve_label() -> str:
    """The node's mesh label: ``mesh.label`` → instance slug → a safe default."""
    label = config.instance_value("mesh.label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    slug = config.instance_value("instance.slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    return "sanctum-haus"


def _resolve_baseline() -> float:
    """The adopt/seed regression bar: ``mesh.baseline`` → the shipped default."""
    raw = config.instance_value("mesh.baseline", _DEFAULT_BASELINE)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BASELINE


def _persist_mesh_config(label: str, addr: str | None) -> None:
    """Persist ``mesh.label`` (+ last ``mesh.addr``) into instance.yaml (best-effort).

    Config-first: a re-run of ``join``/``status`` reuses the stored label. A write
    failure never fails the join — the identity + tailnet are the load-bearing
    state; this is a convenience cache. Only the ``mesh`` block is touched, so the
    ``instance`` / ``cli`` config the schema validates is left intact.
    """
    path = config.instance_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    if not isinstance(raw, dict):
        return
    mesh_block = raw.get("mesh")
    if not isinstance(mesh_block, dict):
        mesh_block = {}
    mesh_block["label"] = label
    if addr:
        mesh_block["addr"] = addr
    raw["mesh"] = mesh_block
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    except OSError:
        return


# ─── builders — the Task-8 adapter boundary (monkeypatched in tests) ─────


def _real_run(argv: list[str], timeout: int = 8) -> str:
    """Default :data:`CommandRunner`: run ``argv``, return stdout, never raise."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    return proc.stdout


def _build_command_runner() -> CommandRunner:
    """The tailscale seam — a real subprocess reader (no adapter needed)."""
    return _real_run


def _build_identity_store() -> MeshIdentityStore:
    """The mesh identity store, bound to the real ML-DSA signer (Task 8)."""
    raise LocalError(_ADAPTERS_PENDING, fix="wire the mesh adapters (Task 8), then re-run")


def _build_directory() -> MeshDirectory:
    """The discovery/tracker client (Task 8's HTTP-tracker adapter)."""
    raise LocalError(_ADAPTERS_PENDING, fix="wire the mesh adapters (Task 8), then re-run")


def _build_adopt_seams() -> AdoptSeams:
    """The verify → eval → sandbox → promote seams for ``pull`` (Task 8)."""
    raise LocalError(_ADAPTERS_PENDING, fix="wire the mesh adapters (Task 8), then re-run")


# ─── printing (every ✓/✗ derived from a real outcome) ────────────────────


def _report(exc: SanctumError) -> None:
    err_console.print(f"[bold red]error:[/] {escape(exc.message)}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {escape(exc.fix)}")


def _print_join(report: JoinReport, *, yes: bool) -> None:
    if report.tailnet_up:
        where = f" ({escape(report.addr)})" if report.addr else ""
        console.print(f"[green]✓[/] Tailnet reachable{where}")
    else:
        console.print("[red]✗[/] Tailnet not up (no live 'tailscale status' == Running)")
    console.print(f"[green]✓[/] Mesh identity: {report.identity_fingerprint}")
    if report.registered:
        console.print("[green]✓[/] Registered with mesh discovery (tracker ack)")
    else:
        console.print("[yellow]•[/] Not registered (tailnet down or no tracker ack)")
    console.print(f"Peers discovered: {len(report.peers)}")
    console.print(f"Champions available to pull: {len(report.champions)}")

    if report.joined:
        console.print("\n[bold green]✓ Joined the Sanctum mesh.[/]")
    elif not report.tailnet_up:
        console.print("\n[yellow]Not joined yet.[/] Bring your tailnet up, then re-run.")
        if not yes:
            console.print(
                "  Run: [bold]tailscale up[/]   (your own free Tailscale account)"
            )
    else:
        console.print("\n[yellow]Not joined yet.[/] Discovery did not ack the registration.")


def _print_status(report: StatusReport) -> None:
    if report.identity_fingerprint is None:
        console.print("[yellow]•[/] No mesh identity yet — run: [bold]sanctum mesh join[/]")
    else:
        console.print(f"[green]✓[/] Mesh identity: {report.identity_fingerprint}")
    console.print(f"Peers: {len(report.peers)}")
    for peer in report.peers:
        console.print(f"  • {escape(peer)}")
    console.print(f"Champions available: {len(report.champions)}")
    for champ in report.champions:
        console.print(f"  • {escape(champ)}")


def _print_verdict(verdict: Verdict) -> None:
    if verdict.promoted:
        console.print(f"[bold green]✓ Adopted[/] — {escape(verdict.reason)}")
    else:
        console.print(
            f"[yellow]✗ Not adopted[/] (stage: {escape(verdict.stage)}) — {escape(verdict.reason)}"
        )
        console.print("[dim]Your current champion stays authoritative.[/]")


def _parse_scores(raw: list[str] | None) -> dict[str, float]:
    scores: dict[str, float] = {}
    for item in raw or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise UserError(
                f"bad --score {item!r}", fix="use name=value, e.g. --score tiered=0.897"
            )
        try:
            scores[name.strip()] = float(value)
        except ValueError as exc:
            raise UserError(
                f"non-numeric --score value in {item!r}", fix="the value must be a number"
            ) from exc
    if not scores:
        raise UserError(
            "no eval scores given", fix="pass at least one --score name=value (e.g. tiered=0.897)"
        )
    return scores


# ─── commands ────────────────────────────────────────────────────────────


@mesh_app.command("join", help="Join the open Sanctum mesh (tailnet + identity + discovery).")
def join_command(
    label: Annotated[
        str | None, typer.Option("--label", help="Human label for this node's mesh identity.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Non-interactive: skip prompts (onboard/scripts).")
    ] = False,
) -> None:
    try:
        run = _build_command_runner()
        store = _build_identity_store()
        directory = _build_directory()
        chosen_label = label.strip() if label and label.strip() else _resolve_label()
        report = join_mesh(store=store, directory=directory, run=run, label=chosen_label)
        _persist_mesh_config(chosen_label, report.addr)
        _print_join(report, yes=yes)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@mesh_app.command("status", help="Show this node's mesh identity, peers, and champions.")
def status_command() -> None:
    try:
        store = _build_identity_store()
        directory = _build_directory()
        report = mesh_status(store=store, directory=directory, label=_resolve_label())
        _print_status(report)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@mesh_app.command("pull", help="Pull a peer's champion by content hash and verify before adopting.")
def pull_command(
    content_hash: Annotated[str, typer.Argument(help="The 'sha256:…' content hash to pull.")],
) -> None:
    try:
        directory = _build_directory()
        seams = _build_adopt_seams()
        verdict = pull_champion(
            content_hash, directory=directory, seams=seams, baseline=_resolve_baseline()
        )
        _print_verdict(verdict)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    if not verdict.promoted:
        # A safe rejection (or a miss): the local champion is kept. Exit non-zero so
        # a scripted pull can tell "adopted" from "kept" — this is not an error frame.
        raise typer.Exit(code=int(ExitCode.USER_ERROR))


@mesh_app.command("seed", help="Sign + announce a local champion to the mesh (iff it beats baseline).")
def seed_command(
    champion_dir: Annotated[
        Path, typer.Argument(help="Path to the champion adapter dir (or file).")
    ],
    base_model: Annotated[str, typer.Option("--base-model", help="Base model id the adapter targets.")],
    score: Annotated[
        list[str] | None,
        typer.Option("--score", help="Eval score as name=value (repeatable)."),
    ] = None,
    addr: Annotated[
        str | None,
        typer.Option("--addr", help="Tailnet address peers pull from (default: this node's)."),
    ] = None,
) -> None:
    announced: ChampionManifest | None
    try:
        run = _build_command_runner()
        store = _build_identity_store()
        directory = _build_directory()
        identity = store.ensure(_resolve_label())
        eval_scores = _parse_scores(score)
        resolved_addr = addr or read_tailnet(run).self_addr
        if not resolved_addr:
            raise UserError(
                "no tailnet address for this node",
                fix="run 'sanctum mesh join' first, or pass --addr",
            )
        baseline_scores = {_DEFAULT_BASELINE_METRIC: _resolve_baseline()}
        announced = seed_champion(
            champion_dir,
            identity=identity,
            directory=directory,
            addr=resolved_addr,
            base_model=base_model,
            eval_scores=eval_scores,
            baseline_scores=baseline_scores,
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    if announced is None:
        console.print(
            f"[yellow]• Not seeded[/] — champion does not beat baseline "
            f"{{{_DEFAULT_BASELINE_METRIC}: {_resolve_baseline():.3f}}}."
        )
        raise typer.Exit(code=int(ExitCode.USER_ERROR))
    console.print(
        f"[green]✓[/] Seeded {escape(announced.content_hash)} to the mesh "
        f"(addr {escape(resolved_addr)})."
    )
