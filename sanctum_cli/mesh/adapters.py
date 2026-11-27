"""Real adapters behind the mesh's injected Protocol seams.

The mesh core (identity / artifact / discovery / seed / verify) is pure
orchestration over :class:`typing.Protocol` seams so it can be unit-tested with
fakes. This module is the other side of those seams — the production
implementations that touch real crypto, the content store, the eval harness,
and the air-gapped sandbox VM:

* :class:`Ed25519Signer` — the crypto :class:`~sanctum_cli.mesh.identity.Signer`;
* :class:`LocalArtifactStore` — resolves an :class:`~sanctum_cli.mesh.types.ArtifactRef`
  to the local bytes to hash / eval / sandbox / promote;
* :class:`BoundManifestVerifier` — the hash + signature
  :class:`~sanctum_cli.mesh.verify.ManifestVerifier`;
* :class:`AutoresearchEvalGate` — the :class:`~sanctum_cli.mesh.verify.EvalGate`
  over the autoresearch eval harness;
* :class:`VmAirgapSandbox` — the :class:`~sanctum_cli.mesh.verify.Sandbox` that
  probes the adapter under network isolation;
* :func:`make_promote` — builds the ``promote`` seam ``adopt`` calls on success.

**Post-quantum note.** The Layer-1 signing target is ML-DSA-65 (post-quantum).
:class:`Ed25519Signer` is the *interim* real-crypto signer; ML-DSA-65 drops in
behind the same :class:`~sanctum_cli.mesh.identity.Signer` seam the moment
``liboqs-python`` (or another ML-DSA binding) is pinned — no caller changes.

**Never log key material.** Private keys and signatures are handled as opaque
hex strings and are never written to logs, reprs, or error messages.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from sanctum_cli.errors import LocalError
from sanctum_cli.mesh import artifact
from sanctum_cli.mesh.verify import SandboxResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sanctum_cli.mesh.artifact import VerifyFn
    from sanctum_cli.mesh.identity import Signer
    from sanctum_cli.mesh.types import ArtifactRef
    from sanctum_cli.mesh.verify import EvalGate, ManifestVerifier, Sandbox

__all__ = [
    "AutoresearchEvalGate",
    "BoundManifestVerifier",
    "CompositeSlhDsaEd25519Signer",
    "Ed25519Signer",
    "EvalRunner",
    "LocalArtifactStore",
    "SandboxProbe",
    "VmAirgapSandbox",
    "VmRunner",
    "make_promote",
    "mlx_eval_runner",
    "slh_dsa_available",
    "vm_airgap_runner",
]

_HASH_PREFIX = "sha256:"
# A real sha256 id after the prefix is stripped: exactly 64 lowercase hex chars.
# path_for enforces this so an untrusted peer manifest cannot smuggle path
# separators or `..` segments through content_hash (path-traversal defense).
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_EVAL_STDERR_TAIL = 500  # chars of harness stderr surfaced on a non-zero exit


# ─── crypto seam ─────────────────────────────────────────────────────────


class Ed25519Signer:
    """Interim real-crypto :class:`~sanctum_cli.mesh.identity.Signer` (Ed25519).

    Production Ed25519 via :mod:`cryptography`. Keys and signatures cross the
    seam as **hex strings**: raw 32-byte public/private keys
    (``Encoding.Raw`` / ``PrivateFormat.Raw`` / ``NoEncryption``) and the raw
    signature, all hex-encoded. :meth:`verify` returns ``False`` on an invalid
    signature or malformed input (bad hex, wrong-length key) — it never raises.

    Layer-1's signing target is ML-DSA-65 (post-quantum); this signer is the
    stand-in until an ML-DSA binding is pinned, at which point the PQ signer
    drops in behind this same seam with no caller changes.

    The signer is stateless and holds no key material — private keys live only
    for the duration of a :meth:`sign` call and are never logged.
    """

    def generate(self) -> tuple[str, str]:
        """Return a fresh ``(public_hex, private_hex)`` Ed25519 keypair."""
        private = ed25519.Ed25519PrivateKey.generate()
        private_hex = private.private_bytes(
            encoding=Encoding.Raw,
            format=PrivateFormat.Raw,
            encryption_algorithm=NoEncryption(),
        ).hex()
        public_hex = private.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        ).hex()
        return (public_hex, private_hex)

    def sign(self, private_key: str, message: bytes) -> str:
        """Return the hex detached signature over ``message`` using ``private_key``."""
        private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key))
        return private.sign(message).hex()

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        """Return whether ``signature`` verifies for ``message`` under ``public_key``.

        Returns ``False`` (never raises) on an invalid signature or malformed
        input — non-hex strings or a wrong-length key surface as a failed gate.
        """
        try:
            public = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
            public.verify(bytes.fromhex(signature), message)
        except (InvalidSignature, ValueError):
            return False
        return True


_SLH_COMPOSITE_PREFIX = "ed25519+slh-dsa-sha2-128s"
_SLH_DSA_ALG = "SLH-DSA-SHA2-128s"


def _find_pq_openssl() -> str | None:
    """Return an ``openssl`` executable path supporting NIST FIPS 205 ``SLH-DSA-SHA2-128s``."""
    candidates = [
        "/opt/homebrew/opt/openssl@4/bin/openssl",
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        "/opt/homebrew/bin/openssl",
        shutil.which("openssl") or "",
    ]
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen or not Path(cand).is_file():
            continue
        seen.add(cand)
        try:
            proc = subprocess.run(
                [cand, "list", "-signature-algorithms"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and _SLH_DSA_ALG in proc.stdout:
                return cand
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def slh_dsa_available() -> bool:
    """Return True when the host OpenSSL supports NIST FIPS 205 ``SLH-DSA-SHA2-128s``."""
    return _find_pq_openssl() is not None


def _write_mode_0600(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


class CompositeSlhDsaEd25519Signer:
    """Hash-based post-quantum composite :class:`~sanctum_cli.mesh.identity.Signer`.

    Combines **Ed25519** (ECC) + **NIST FIPS 205 ``SLH-DSA-SHA2-128s``** (stateless
    hash-based signatures backed solely by SHA-256). Both signatures are produced
    on :meth:`sign` and BOTH must verify strictly on :meth:`verify` — surviving
    S1 (RSA), S2 (ECC), S3 (Lattices), AND S4 (simultaneous collapse of RSA, ECC,
    and Lattices).

    Encoding (opaque strings across the ``Signer`` seam, zero key logging):
    ``"ed25519+slh-dsa-sha2-128s:<ed25519_hex>:<slh_dsa_hex>"``.
    """

    def __init__(self, openssl_bin: str | None = None) -> None:
        self._ed = Ed25519Signer()
        self._openssl = openssl_bin

    def _resolve_openssl(self) -> str:
        if self._openssl:
            return self._openssl
        found = _find_pq_openssl()
        if not found:
            msg = f"OpenSSL with {_SLH_DSA_ALG} (FIPS 205) support is required"
            raise LocalError(msg, fix="install OpenSSL 3.5+ / 4.x via Homebrew (`brew install openssl`)")
        self._openssl = found
        return found

    def generate(self) -> tuple[str, str]:
        """Generate a composite ``(public_key, private_key)`` pair (Ed25519 + SLH-DSA-SHA2-128s)."""
        openssl = self._resolve_openssl()
        ed_pub, ed_priv = self._ed.generate()
        with tempfile.TemporaryDirectory(prefix="sanctum-slh-") as td:
            td_path = Path(td)
            priv_der = td_path / "slh.priv.der"
            pub_der = td_path / "slh.pub.der"
            try:
                subprocess.run(
                    [
                        openssl,
                        "genpkey",
                        "-algorithm",
                        _SLH_DSA_ALG,
                        "-outform",
                        "DER",
                        "-out",
                        str(priv_der),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                priv_der.chmod(0o600)
                subprocess.run(
                    [
                        openssl,
                        "pkey",
                        "-inform",
                        "DER",
                        "-in",
                        str(priv_der),
                        "-pubout",
                        "-outform",
                        "DER",
                        "-out",
                        str(pub_der),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                slh_priv_hex = priv_der.read_bytes().hex()
                slh_pub_hex = pub_der.read_bytes().hex()
            except (OSError, subprocess.SubprocessError) as exc:
                msg = f"FIPS 205 {_SLH_DSA_ALG} key generation failed"
                raise LocalError(msg, fix="verify `openssl genpkey -algorithm SLH-DSA-SHA2-128s`") from exc
        return (
            f"{_SLH_COMPOSITE_PREFIX}:{ed_pub}:{slh_pub_hex}",
            f"{_SLH_COMPOSITE_PREFIX}:{ed_priv}:{slh_priv_hex}",
        )

    def sign(self, private_key: str, message: bytes) -> str:
        """Sign ``message`` with BOTH Ed25519 and FIPS 205 ``SLH-DSA-SHA2-128s``."""
        parts = private_key.split(":")
        if len(parts) != 3 or parts[0] != _SLH_COMPOSITE_PREFIX:
            raise ValueError("invalid composite SLH-DSA private key encoding")
        _, ed_priv, slh_priv_hex = parts
        openssl = self._resolve_openssl()
        ed_sig = self._ed.sign(ed_priv, message)
        with tempfile.TemporaryDirectory(prefix="sanctum-slh-") as td:
            td_path = Path(td)
            priv_der = td_path / "slh.priv.der"
            msg_file = td_path / "msg.bin"
            sig_file = td_path / "msg.sig"
            _write_mode_0600(priv_der, bytes.fromhex(slh_priv_hex))
            msg_file.write_bytes(message)
            try:
                subprocess.run(
                    [
                        openssl,
                        "pkeyutl",
                        "-sign",
                        "-keyform",
                        "DER",
                        "-inkey",
                        str(priv_der),
                        "-rawin",
                        "-in",
                        str(msg_file),
                        "-out",
                        str(sig_file),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                slh_sig_hex = sig_file.read_bytes().hex()
            except (OSError, subprocess.SubprocessError) as exc:
                msg = f"FIPS 205 {_SLH_DSA_ALG} signing failed"
                raise LocalError(msg, fix="verify OpenSSL FIPS 205 provider health") from exc
        return f"{_SLH_COMPOSITE_PREFIX}:{ed_sig}:{slh_sig_hex}"

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        """Strictly verify BOTH Ed25519 and FIPS 205 ``SLH-DSA-SHA2-128s`` signatures."""
        pub_parts = public_key.split(":")
        sig_parts = signature.split(":")
        if (
            len(pub_parts) != 3
            or len(sig_parts) != 3
            or pub_parts[0] != _SLH_COMPOSITE_PREFIX
            or sig_parts[0] != _SLH_COMPOSITE_PREFIX
        ):
            return False
        _, ed_pub, slh_pub_hex = pub_parts
        _, ed_sig, slh_sig_hex = sig_parts

        # 1. Classical Ed25519 half must verify strictly.
        if not self._ed.verify(ed_pub, message, ed_sig):
            return False

        # 2. Hash-based FIPS 205 SLH-DSA-SHA2-128s half must verify strictly.
        try:
            openssl = self._resolve_openssl()
            slh_pub_bytes = bytes.fromhex(slh_pub_hex)
            slh_sig_bytes = bytes.fromhex(slh_sig_hex)
        except (LocalError, ValueError):
            return False

        with tempfile.TemporaryDirectory(prefix="sanctum-slh-") as td:
            td_path = Path(td)
            pub_der = td_path / "slh.pub.der"
            msg_file = td_path / "msg.bin"
            sig_file = td_path / "msg.sig"
            pub_der.write_bytes(slh_pub_bytes)
            msg_file.write_bytes(message)
            sig_file.write_bytes(slh_sig_bytes)
            try:
                proc = subprocess.run(
                    [
                        openssl,
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-keyform",
                        "DER",
                        "-inkey",
                        str(pub_der),
                        "-rawin",
                        "-in",
                        str(msg_file),
                        "-sigfile",
                        str(sig_file),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return proc.returncode == 0


# ─── content store ───────────────────────────────────────────────────────


class LocalArtifactStore:
    """Resolve an :class:`~sanctum_cli.mesh.types.ArtifactRef` to local bytes.

    This is the single-box content store: the seeder and the puller are the same
    box, so the artifact bytes are already on disk. :meth:`path_for` maps a ref
    to ``base / <content hash>`` — the ``sha256:`` prefix is stripped and the
    remainder is **validated to be a real sha256 hex id** (``^[0-9a-f]{64}$``)
    before it is joined onto ``base``, so an untrusted peer's ``content_hash``
    cannot escape the store dir via path separators or ``..`` segments. It does
    **not** require the file to exist; callers (the hash gate, promote) check.

    The cross-box P2P byte-fetch over the tailnet is out of scope for Layer-1
    single-box. An ``HttpArtifactFetcher`` drops in behind the same
    :meth:`path_for` shape for the 2-box case: fetch-then-return-local-path.
    """

    __slots__ = ("_base",)

    def __init__(self, base: Path) -> None:
        self._base = base

    def path_for(self, ref: ArtifactRef) -> Path:
        """Return the local path for ``ref`` (existence not required).

        ``ref.content_hash`` comes from an untrusted peer manifest. After the
        ``sha256:`` prefix is stripped, the remainder must match a real sha256
        hex id (``^[0-9a-f]{64}$``); anything else — a wrong length, non-hex
        chars, or a ``../…`` traversal payload — raises
        :class:`~sanctum_cli.errors.LocalError` rather than resolving a path
        outside the store dir.
        """
        hex_id = ref.content_hash.removeprefix(_HASH_PREFIX)
        if not _SHA256_HEX.match(hex_id):
            msg = f"malformed content hash {ref.content_hash!r}: not a sha256 id"
            raise LocalError(
                msg,
                fix="a content hash must be 'sha256:' + 64 lowercase hex chars",
            )
        return self._base / hex_id


# ─── manifest verifier seam (hash + signature) ───────────────────────────


class BoundManifestVerifier:
    """The :class:`~sanctum_cli.mesh.verify.ManifestVerifier`, bound to a store.

    Splits content-integrity into its two attributable gates so the adopt
    pipeline can name which one rejected:

    * :meth:`verify_hash` — the local bytes reproduce ``ref.content_hash``
      (a missing artifact *or* a malformed/hostile content hash is a failed
      hash gate, not an error);
    * :meth:`verify_signature` — hash-independent, via
      :func:`sanctum_cli.mesh.artifact.verify_signature`.

    ``verify_fn`` is the crypto verify seam — pass :meth:`Ed25519Signer.verify`
    in real use, a fake in tests.
    """

    __slots__ = ("_store", "_verify_fn")

    def __init__(self, store: LocalArtifactStore, verify_fn: VerifyFn) -> None:
        self._store = store
        self._verify_fn = verify_fn

    def verify_hash(self, ref: ArtifactRef) -> bool:
        """Return whether the local bytes reproduce ``ref.content_hash``."""
        try:
            return artifact.content_hash(self._store.path_for(ref)) == ref.content_hash
        except FileNotFoundError:
            # No local artifact -> a failed hash gate, never an exception.
            return False
        except LocalError:
            # A malformed/hostile content hash (path_for rejected it) is a
            # clean FAILED hash gate, never a crash out of adopt().
            return False

    def verify_signature(self, ref: ArtifactRef) -> bool:
        """Return whether the manifest signature verifies under its producer key."""
        return artifact.verify_signature(ref.manifest, self._verify_fn)


# ─── eval gate seam ──────────────────────────────────────────────────────

EvalRunner = Callable[[Path, str], Mapping[str, float]]
"""``(adapter_path, base_model) -> per-metric scores`` — the eval seam's runner."""


class AutoresearchEvalGate:
    """The :class:`~sanctum_cli.mesh.verify.EvalGate` over the eval harness.

    :meth:`score` runs the injected ``eval_runner`` on the resolved adapter path
    and picks the ``metric`` score (default ``"tiered"``); if the runner does not
    report that metric it falls back to the mean of what it did report, and an
    empty result scores ``0.0`` — a champion we cannot score does not clear any
    positive baseline. The real runner is :func:`mlx_eval_runner`; unit tests
    inject a fake.
    """

    __slots__ = ("_eval_runner", "_metric", "_store")

    def __init__(
        self,
        store: LocalArtifactStore,
        eval_runner: EvalRunner,
        *,
        metric: str = "tiered",
    ) -> None:
        self._store = store
        self._eval_runner = eval_runner
        self._metric = metric

    def score(self, ref: ArtifactRef) -> float:
        """Return the candidate's aggregate eval score."""
        scores = self._eval_runner(self._store.path_for(ref), ref.manifest.base_model)
        if not scores:
            return 0.0
        if self._metric in scores:
            return scores[self._metric]
        return sum(scores.values()) / len(scores)


def _parse_aggregate_score(output: str) -> float:
    """Read the ``"aggregate"`` field from the eval harness's JSON summary.

    Raises :class:`~sanctum_cli.errors.LocalError` if the output does not carry a
    numeric aggregate — an eval we cannot read is not a score we can trust.
    """
    try:
        payload = json.loads(output)
        return float(payload["aggregate"])
    except (ValueError, KeyError, TypeError) as exc:
        msg = "could not parse an aggregate score from the eval harness output"
        raise LocalError(
            msg,
            fix="check the JSON summary emitted by mlx-finetune/scripts/evaluate.py",
        ) from exc


def mlx_eval_runner(
    adapter_path: Path,
    base_model: str,
    *,
    cases: Sequence[str] | None = None,
) -> dict[str, float]:
    """Interim real eval runner: shell out to the autoresearch eval harness.

    Builds the argv for ``~/Projects/mlx-finetune/scripts/evaluate.py`` (the
    tiered suite; ``cases`` restricts it to named cases for a fast smoke), runs
    it under the CLI's interpreter, and parses the aggregate into
    ``{"tiered": <float>}``.

    Live boundary — this reaches the real harness and is exercised only by the
    ``@pytest.mark.integration`` e2e drill, never by ``make check``.
    """
    harness = Path.home() / "Projects" / "mlx-finetune" / "scripts" / "evaluate.py"
    argv = [
        sys.executable,
        str(harness),
        "--adapter",
        str(adapter_path),
        "--base-model",
        base_model,
    ]
    if cases is not None:
        argv += ["--cases", ",".join(cases)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        # Fail with an attributable LocalError (not a raw CalledProcessError out
        # of the eval gate): surface a trimmed tail of the harness's stderr.
        tail = (proc.stderr or "").strip()[-_EVAL_STDERR_TAIL:]
        msg = f"eval harness exited non-zero ({proc.returncode}): {tail or '<no stderr>'}"
        raise LocalError(
            msg,
            fix="run ~/Projects/mlx-finetune/scripts/evaluate.py directly on the "
            "adapter to see the full error",
        )
    return {"tiered": _parse_aggregate_score(proc.stdout)}


# ─── air-gap sandbox seam ────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxProbe:
    """Raw result of a :class:`VmRunner` — the isolated run's observations.

    ``completed`` is whether the adapter loaded and answered the probe prompts;
    ``egress_attempted`` is the security verdict (did the isolated run try to
    reach the network); ``notes`` is free-form audit detail.
    """

    completed: bool
    egress_attempted: bool
    notes: str = ""


VmRunner = Callable[[Path], SandboxProbe]
"""``(adapter_path) -> SandboxProbe`` — the sandbox seam's isolated-run runner."""

_DEFAULT_PROBE_PROMPTS: tuple[str, ...] = (
    "Reply with the single word: ok",
    "Summarize in one sentence: the sky is blue.",
)
_SSH_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SSH_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_IPV6_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REMOTE_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


def _validate_ssh_host(host: str) -> tuple[str, str]:
    """Validate an SSH host (optional ``user@`` + hostname/IP) against option/shell injection.

    Returns ``(ssh_host, rsync_host)`` where IPv6 literals are bracketed for ``rsync``.
    """
    if not isinstance(host, str) or not host or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in host):
        raise LocalError(
            f"invalid sandbox host {host!r}",
            fix="set mesh.sandbox_host to a valid hostname or IP address",
        )
    if host.count("@") > 1:
        raise LocalError(
            f"invalid sandbox host {host!r}",
            fix="set mesh.sandbox_host to a valid hostname or IP address",
        )
    if "@" in host:
        user, bare = host.split("@", 1)
        if not user or user.startswith("-") or not _SSH_USER_RE.fullmatch(user):
            raise LocalError(
                f"invalid sandbox host {host!r}",
                fix="set mesh.sandbox_host to a valid hostname or IP address",
            )
    else:
        user, bare = "", host

    if not bare or bare.startswith("-") or ".." in bare or any(ch in bare for ch in (":", "/", "\\")):
        # Allow pure IPv6 literals via ipaddress below (which contain ':')
        try:
            addr = ipaddress.ip_address(bare)
        except ValueError as exc:
            raise LocalError(
                f"invalid sandbox host {host!r}",
                fix="set mesh.sandbox_host to a valid hostname or IP address",
            ) from exc
        if not isinstance(addr, ipaddress.IPv6Address):
            raise LocalError(
                f"invalid sandbox host {host!r}",
                fix="set mesh.sandbox_host to a valid hostname or IP address",
            )
        if addr.scope_id is not None or "%" in bare:
            scope_id = addr.scope_id
            if (
                not scope_id
                or scope_id.startswith("-")
                or ".." in scope_id
                or not _IPV6_SCOPE_ID_RE.fullmatch(scope_id)
            ):
                raise LocalError(
                    f"invalid sandbox host {host!r}",
                    fix="set mesh.sandbox_host to a valid hostname or IP address",
                )
        ssh_host = f"{user}@{bare}" if user else bare
        rsync_host = f"{user}@[{bare}]" if user else f"[{bare}]"
        return ssh_host, rsync_host

    if not _SSH_HOST_LABEL_RE.fullmatch(bare):
        raise LocalError(
            f"invalid sandbox host {host!r}",
            fix="set mesh.sandbox_host to a valid hostname or IP address",
        )
    normalized = f"{user}@{bare}" if user else bare
    return normalized, normalized


def _validate_remote_dir(remote_dir: str) -> str:
    """Validate ``remote_dir`` is a safe absolute POSIX path with no traversal or metacharacters."""
    if (
        not isinstance(remote_dir, str)
        or not _REMOTE_DIR_RE.fullmatch(remote_dir)
        or "//" in remote_dir
        or ".." in PurePosixPath(remote_dir).parts
    ):
        raise LocalError(
            f"invalid sandbox remote_dir {remote_dir!r}",
            fix="use a safe absolute POSIX path without '..' or whitespace",
        )
    return remote_dir.rstrip("/") or "/"


class VmAirgapSandbox:
    """The :class:`~sanctum_cli.mesh.verify.Sandbox` over an air-gapped VM.

    :meth:`probe` runs the injected ``vm_runner`` on the resolved adapter path
    and maps its :class:`SandboxProbe` onto the pipeline's
    :class:`~sanctum_cli.mesh.verify.SandboxResult` (``ok=completed``). The real
    runner is :func:`vm_airgap_runner`; unit tests inject a fake.
    """

    __slots__ = ("_store", "_vm_runner")

    def __init__(self, store: LocalArtifactStore, vm_runner: VmRunner) -> None:
        self._store = store
        self._vm_runner = vm_runner

    def probe(self, ref: ArtifactRef) -> SandboxResult:
        """Load + exercise the adapter in isolation and report what happened."""
        probe = self._vm_runner(self._store.path_for(ref))
        return SandboxResult(
            ok=probe.completed,
            egress_attempted=probe.egress_attempted,
            notes=probe.notes,
        )


def vm_airgap_runner(
    adapter_path: Path,
    *,
    host: str,
    remote_dir: str = "/tmp/sanctum-mesh-sandbox",
    probe_prompts: Sequence[str] | None = None,
    timeout: float = 120.0,
) -> SandboxProbe:
    """Interim real air-gap sandbox: ship + probe the adapter on an isolated VM.

    Mechanism: rsync the adapter directory to ``host``, then — under
    ``unshare -n`` (no network namespace) — load it and run a couple of probe
    prompts via a remote ``sanctum-mesh-probe`` helper that watches for any
    egress attempt. The helper reports ``{completed, egress_attempted, notes}``
    as JSON, which maps to a :class:`SandboxProbe`.

    Live boundary — the real path REQUIRES the air-gapped VM to be up. It is
    exercised by the 2-box acceptance test, NOT by ``make check``.
    """
    ssh_host, rsync_host = _validate_ssh_host(host)
    clean_remote_dir = _validate_remote_dir(remote_dir)
    raw_adapter = str(adapter_path)
    if any(ch in raw_adapter for ch in ("\x00", "\r", "\n")):
        raise LocalError(
            f"invalid adapter path {raw_adapter!r}",
            fix="adapter path must not contain control characters",
        )
    local_src = f"{Path(adapter_path).resolve()}/"
    prompts = list(probe_prompts) if probe_prompts is not None else list(_DEFAULT_PROBE_PROMPTS)
    request = json.dumps({"adapter_dir": clean_remote_dir, "prompts": prompts})
    remote_cmd = shlex.join(["unshare", "-n", "sanctum-mesh-probe", "--json", request])
    try:
        subprocess.run(
            ["rsync", "-a", "--delete", "--", local_src, f"{rsync_host}:{clean_remote_dir}/"],
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", ssh_host, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        report = json.loads(proc.stdout)
        return SandboxProbe(
            completed=bool(report["completed"]),
            egress_attempted=bool(report["egress_attempted"]),
            notes=str(report.get("notes", "")),
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        # A down/unreachable VM, a timeout, a missing helper, or a garbled probe
        # report must fail CLOSED as a clean LocalError — never a raw traceback,
        # and never a silent "clean, no egress". The sandbox gate then rejects;
        # the local champion stays authoritative. (Mirrors mlx_eval_runner's exit handling.)
        raise LocalError(
            f"air-gapped sandbox probe failed on {host}: {exc}",
            fix="ensure mesh.sandbox_host is a reachable VM with the sanctum-mesh-probe "
            "helper and a working 'unshare -n'; run the probe there by hand to see the error",
        ) from exc


# ─── promote seam ────────────────────────────────────────────────────────


def make_promote(
    store: LocalArtifactStore,
    *,
    record: Callable[[ArtifactRef], None] | None = None,
) -> Callable[[ArtifactRef], None]:
    """Build the ``promote`` seam ``adopt`` calls on success.

    The returned callable marks the adopted artifact as the local champion. It
    first verifies the artifact's bytes are present — raising
    :class:`~sanctum_cli.errors.LocalError` otherwise, because you cannot promote
    bytes you do not have — then invokes the optional ``record`` hook. The CLI
    wiring passes a ``record`` that persists ``mesh.champion`` into
    ``instance.yaml``; unit tests pass a spy.
    """

    def _promote(ref: ArtifactRef) -> None:
        path = store.path_for(ref)
        if not path.exists():
            msg = f"cannot promote {ref.content_hash}: no local bytes at {path}"
            raise LocalError(msg, fix="fetch the artifact before promoting it")
        if record is not None:
            record(ref)

    return _promote


def _static_conformance() -> tuple[Signer, Signer, ManifestVerifier, EvalGate, Sandbox]:
    """Type-only guard (never called): fail ``mypy --strict`` if an adapter's
    signature drifts from the injected seam it must satisfy."""
    store = LocalArtifactStore(Path())
    signer = Ed25519Signer()
    pq_signer = CompositeSlhDsaEd25519Signer()
    return (
        signer,
        pq_signer,
        BoundManifestVerifier(store, pq_signer.verify),
        AutoresearchEvalGate(store, mlx_eval_runner),
        VmAirgapSandbox(store, lambda adapter: vm_airgap_runner(adapter, host="vm")),
    )
