"""sanctum onboard — trifecta cred-capture SEAM (Task 3).

When a captured device admin secret is written (the network-gear pairing gate,
:func:`store_device_secret`), the Keychain is the GUARANTEED tier (CLAUDE.md
secrets-trifecta). On a full haus — where the 1Password CLI service-account
token and SOPS are present — the secret is ALSO best-effort mirrored into the
trifecta: a 1P item is written/updated AND a ``providers.yaml`` ``sync_mirrors``
mapping is emitted so the daily drift-sync (`tools/secret-rotator/sync.py`)
manages real cross-tier propagation thereafter.

The military-grade contract here is fail-soft:

* On a stock friend-install (no ``op`` token / no ``sops``) the mirror is a clean
  NO-OP — keychain-only, no error, no block. Onboarding must NEVER hard-fail on
  the haus tooling being absent.
* Even when the haus IS present, the mirror is BEST-EFFORT: a failing ``op``
  write or a SOPS hiccup is swallowed — the guaranteed Keychain copy already
  drives the device.

Every test MOCKS the haus-detection + the real ``op``/SOPS calls. NO real ``op``
binary, ``sops`` binary, SSH, or 1Password account is ever touched — the
heavyweight cross-tier propagation is the daily drift-sync's job, deferred by
design (we only emit the mapping it consumes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

from sanctum_cli.commands import onboard

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ── _mirror_to_trifecta orchestration: haus-present vs absent ─────────


def test_mirror_no_op_when_haus_tooling_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``op`` token / no SOPS → keychain-only: NO 1P write, NO mapping emitted.

    The guaranteed tier (Keychain) already holds the secret; on a stock install
    the trifecta is simply not there, so the mirror is a clean no-op.
    """
    monkeypatch.setattr("sanctum_cli.commands.onboard._haus_trifecta_present", lambda: False)

    op_calls: list[Any] = []
    map_calls: list[Any] = []
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._op_write_item",
        lambda **k: op_calls.append(k),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._append_sync_mirror",
        lambda **k: map_calls.append(k),
    )

    onboard._mirror_to_trifecta(service="bell-hub-admin", account="admin", secret="s3cret")

    assert op_calls == []
    assert map_calls == []


def test_mirror_writes_op_item_and_mapping_when_haus_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haus present → 1P item written/updated AND a sync_mirrors mapping emitted.

    The mapping is what hands real cross-tier propagation to the daily drift-sync;
    onboarding does not itself push to SOPS/VM (that's deferred + heavyweight).
    """
    monkeypatch.setattr("sanctum_cli.commands.onboard._haus_trifecta_present", lambda: True)

    op_calls: list[dict[str, Any]] = []
    map_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._op_write_item",
        lambda **k: op_calls.append(k),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._append_sync_mirror",
        lambda **k: map_calls.append(k),
    )

    onboard._mirror_to_trifecta(service="bell-hub-admin", account="admin", secret="s3cret")

    # The 1P item is written with the captured secret value.
    assert len(op_calls) == 1
    assert op_calls[0]["value"] == "s3cret"
    assert op_calls[0]["title"]  # a non-empty 1P item title

    # A sync_mirrors mapping is emitted keyed off the keychain service, threading
    # the same op title + the keychain service as `kc`.
    assert len(map_calls) == 1
    m = map_calls[0]
    assert m["kc_service"] == "bell-hub-admin"
    assert m["op_title"] == op_calls[0]["title"]
    assert m["sops_key"]  # a non-empty SOPS top-level key
    assert m["logical_key"]  # a non-empty providers.yaml mapping key


def test_mirror_best_effort_op_failure_still_emits_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``op`` write is swallowed and does NOT abort the mapping emit.

    Best-effort: the daily drift-sync re-pushes the value from 1P later, but the
    mapping must still land so the key is under management. Neither sub-step may
    raise out of the mirror (store_device_secret swallows anyway, but the mirror
    should be internally fail-soft so a transient ``op`` error doesn't drop the
    mapping that hands the key to the drift-sync).
    """
    monkeypatch.setattr("sanctum_cli.commands.onboard._haus_trifecta_present", lambda: True)

    map_calls: list[Any] = []
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._op_write_item",
        lambda **k: (_ for _ in ()).throw(RuntimeError("op write failed")),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._append_sync_mirror",
        lambda **k: map_calls.append(k),
    )

    # Must NOT raise even though the op write blew up.
    onboard._mirror_to_trifecta(service="bell-hub-admin", account="admin", secret="s3cret")
    assert len(map_calls) == 1


# ── _haus_trifecta_present detection ─────────────────────────────────


def test_haus_trifecta_present_true_when_token_and_sops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both the op service-account token AND a `sops` binary present → True."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_xxxxxxxx")
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.shutil.which",
        lambda binary: "/opt/homebrew/bin/sops" if binary == "sops" else None,
    )
    assert onboard._haus_trifecta_present() is True


def test_haus_trifecta_absent_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No op service-account token → not a haus, regardless of sops presence."""
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.shutil.which",
        lambda binary: "/opt/homebrew/bin/sops",
    )
    assert onboard._haus_trifecta_present() is False


def test_haus_trifecta_absent_when_no_sops(monkeypatch: pytest.MonkeyPatch) -> None:
    """op token present but `sops` binary missing → not a (full) trifecta haus."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_xxxxxxxx")
    monkeypatch.setattr("sanctum_cli.commands.onboard.shutil.which", lambda binary: None)
    assert onboard._haus_trifecta_present() is False


def test_haus_trifecta_absent_when_token_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank/whitespace token is treated as absent (env set to '')."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "   ")
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.shutil.which",
        lambda binary: "/opt/homebrew/bin/sops",
    )
    assert onboard._haus_trifecta_present() is False


# ── _append_sync_mirror: pure YAML read-modify-write ─────────────────


def test_append_sync_mirror_creates_section_when_absent(tmp_path: Path) -> None:
    """A providers.yaml with no sync_mirrors gets the section + the new entry."""
    pf = tmp_path / "providers.yaml"
    pf.write_text("secrets:\n  some_key:\n    op: Foo\n", encoding="utf-8")

    onboard._append_sync_mirror(
        logical_key="hub_admin_password",
        op_title="Sanctum - Hub Admin Password",
        sops_key="hub_admin_password",
        kc_service="bell-hub-admin",
        path=pf,
    )

    data = yaml.safe_load(pf.read_text(encoding="utf-8"))
    entry = data["sync_mirrors"]["hub_admin_password"]
    assert entry["op"] == "Sanctum - Hub Admin Password"
    assert entry["sops"] == "hub_admin_password"
    assert entry["kc"] == "bell-hub-admin"
    # Sibling section preserved.
    assert data["secrets"]["some_key"]["op"] == "Foo"
    # .bak written.
    assert (tmp_path / "providers.yaml.bak").exists()


def test_append_sync_mirror_merges_with_existing_entries(tmp_path: Path) -> None:
    """A new mapping is added without clobbering existing sync_mirrors entries."""
    pf = tmp_path / "providers.yaml"
    pf.write_text(
        "sync_mirrors:\n"
        "  anthropic_api_key:\n"
        "    op: Manoir - Anthropic API Key\n"
        "    sops: anthropic_api_key\n"
        "    kc: anthropic-api-key\n",
        encoding="utf-8",
    )

    onboard._append_sync_mirror(
        logical_key="orbi_admin_password",
        op_title="Sanctum - Orbi Admin Password",
        sops_key="orbi_admin_password",
        kc_service="orbi-admin",
        path=pf,
    )

    data = yaml.safe_load(pf.read_text(encoding="utf-8"))
    # Existing entry untouched.
    assert data["sync_mirrors"]["anthropic_api_key"]["op"] == "Manoir - Anthropic API Key"
    # New entry present.
    assert data["sync_mirrors"]["orbi_admin_password"]["kc"] == "orbi-admin"


def test_append_sync_mirror_idempotent_update(tmp_path: Path) -> None:
    """Re-pairing the same kind updates the existing mapping in place (no duplicate)."""
    pf = tmp_path / "providers.yaml"
    pf.write_text("sync_mirrors: {}\n", encoding="utf-8")

    for title in ("Sanctum - Hub Admin Password", "Sanctum - Hub Admin Password v2"):
        onboard._append_sync_mirror(
            logical_key="hub_admin_password",
            op_title=title,
            sops_key="hub_admin_password",
            kc_service="bell-hub-admin",
            path=pf,
        )

    data = yaml.safe_load(pf.read_text(encoding="utf-8"))
    sm = data["sync_mirrors"]
    # Exactly one entry under the logical key — the second call updated, not appended.
    assert list(sm.keys()) == ["hub_admin_password"]
    assert sm["hub_admin_password"]["op"] == "Sanctum - Hub Admin Password v2"


def test_append_sync_mirror_creates_file_when_missing(tmp_path: Path) -> None:
    """No providers.yaml yet → it is created with the sync_mirrors section."""
    pf = tmp_path / "nested" / "providers.yaml"
    onboard._append_sync_mirror(
        logical_key="hub_admin_password",
        op_title="Sanctum - Hub Admin Password",
        sops_key="hub_admin_password",
        kc_service="bell-hub-admin",
        path=pf,
    )
    data = yaml.safe_load(pf.read_text(encoding="utf-8"))
    assert data["sync_mirrors"]["hub_admin_password"]["kc"] == "bell-hub-admin"


# ── trifecta key derivation (service → logical/op/sops names) ─────────


def test_trifecta_names_derive_deterministically_from_service() -> None:
    """The logical/op/sops names are a pure, stable function of the keychain service."""
    names = onboard._trifecta_names_for("bell-hub-admin")
    assert names.logical_key  # non-empty
    assert names.sops_key
    assert names.op_title
    # Pure function — same input, same output.
    assert onboard._trifecta_names_for("bell-hub-admin") == names
    # Different service → different logical/sops key (no collision).
    other = onboard._trifecta_names_for("orbi-admin")
    assert other.logical_key != names.logical_key
    assert other.sops_key != names.sops_key


# ── store_device_secret end-to-end: keychain guaranteed, mirror wired ──


def test_store_device_secret_invokes_mirror_after_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """store_device_secret writes the Keychain (guaranteed) THEN calls the mirror.

    The mirror runs only after the guaranteed tier lands, and a mirror failure is
    swallowed (the keychain copy is enough to drive the device).
    """
    order: list[str] = []

    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._keychain_write",
        lambda *, account, service, value: order.append("keychain"),
    )

    def fake_mirror(*, service: str, account: str, secret: str) -> None:
        order.append("mirror")

    monkeypatch.setattr("sanctum_cli.commands.onboard._mirror_to_trifecta", fake_mirror)

    onboard.store_device_secret(service="bell-hub-admin", account="admin", secret="s3cret")
    assert order == ["keychain", "mirror"]
