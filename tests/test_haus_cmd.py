"""Hermetic unit tests for ``sanctum haus`` (full sanctum-rs Rust engine suite)."""

from __future__ import annotations

import hashlib
import io
import plistlib
import stat
import subprocess
import tarfile
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from sanctum_cli.cli import app
from sanctum_cli.commands import haus_cmd
from sanctum_cli.commands import update as update_cmd
from sanctum_cli.commands import yoda as yoda_cmd

runner = CliRunner()


def _make_rust_tar_bytes(files: dict[str, bytes] | None = None) -> tuple[bytes, str]:
    payload = files or {
        "sanctum-mlx": b"#!/bin/sh\necho mlx\n",
        "proxyd": b"#!/bin/sh\necho proxyd\n",
        "sanctum-presidio": b"#!/bin/sh\necho presidio\n",
        "castellan": b"#!/bin/sh\necho castellan\n",
        "mlx.metallib": b"METALLIB_BYTES",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in payload.items():
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def test_mint_local_pki_and_tokens_permissions(tmp_path: Path) -> None:
    certs_dir = tmp_path / "certs"
    secrets_dir = tmp_path / "secrets"

    paths = haus_cmd.mint_local_pki(certs_dir)
    for k in ("ca_cert", "ca_key", "server_cert", "server_key", "client_cert", "client_key"):
        assert paths[k].is_file(), f"missing {k}"

    # Keys must be 0600
    for k in ("ca_key", "server_key", "client_key"):
        mode = stat.S_IMODE(paths[k].stat().st_mode)
        assert mode == 0o600, f"{k} mode={oct(mode)}"

    # Server cert must have SANs for localhost, 127.0.0.1, ::1 and be ECDSA P-384 (zero RSA)
    for cert_name in ("ca_cert", "server_cert", "client_cert"):
        cert_obj = x509.load_pem_x509_certificate(paths[cert_name].read_bytes())
        pub = cert_obj.public_key()
        assert isinstance(pub, ec.EllipticCurvePublicKey), f"{cert_name} must be ECDSA, not RSA"
        assert pub.curve.name == "secp384r1", f"{cert_name} curve={pub.curve.name}"

    srv = x509.load_pem_x509_certificate(paths["server_cert"].read_bytes())
    cli = x509.load_pem_x509_certificate(paths["client_cert"].read_bytes())
    assert (srv.not_valid_after_utc - srv.not_valid_before_utc).days <= 366
    assert (cli.not_valid_after_utc - cli.not_valid_before_utc).days <= 366
    san = srv.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addrs = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ip_addrs
    assert "::1" in ip_addrs

    # Idempotent: second call preserves exact certs
    before = paths["ca_cert"].read_bytes()
    haus_cmd.mint_local_pki(certs_dir, force=False)
    assert paths["ca_cert"].read_bytes() == before

    # Local IPC tokens created 0600
    haus_cmd.ensure_local_tokens(secrets_dir)
    for name in ("control-plane-ipc.token", "force-flow-notify.token", "proxyd-api-key"):
        p = secrets_dir / name
        assert p.is_file()
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert len(p.read_text(encoding="utf-8").strip()) == 64


def test_rust_suite_download_verify_extract_and_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SANCTUM_RUST_STATE", str(tmp_path / ".sanctum/state/rust-suite.json"))
    monkeypatch.setenv("SANCTUM_HIVE_PULSE_URL", "https://hive.test")

    tar_bytes, sha = _make_rust_tar_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/latest.json":
            return httpx.Response(
                200,
                json={
                    "latest_version": "1.0.3",
                    "rust_tag": "rust-v1.0.3",
                    "rust_url": "https://hive.test/rust-v1.0.3.tar.gz",
                    "rust_sha256": sha,
                },
            )
        if request.url.path == "/rust-v1.0.3.tar.gz":
            return httpx.Response(200, content=tar_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    bin_dir = tmp_path / ".sanctum" / "bin"

    # Fail-closed on SHA-256 mismatch
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        haus_cmd.download_and_install_rust_suite(
            "https://hive.test/rust-v1.0.3.tar.gz",
            "f" * 64,
            bin_dir,
            transport=transport,
        )

    # Valid SHA-256 installs binaries executable and metallib 0644
    got_sha, installed = haus_cmd.download_and_install_rust_suite(
        "https://hive.test/rust-v1.0.3.tar.gz",
        sha,
        bin_dir,
        transport=transport,
    )
    assert got_sha == sha
    assert "sanctum-mlx" in installed
    assert "mlx.metallib" in installed
    assert stat.S_IMODE((bin_dir / "sanctum-mlx").stat().st_mode) == 0o755
    assert stat.S_IMODE((bin_dir / "mlx.metallib").stat().st_mode) == 0o644

    # Simulate older installed tag and verify update_rust_suite_if_needed upgrades
    haus_cmd.save_rust_state({"release_tag": "rust-v1.0.0", "tar_sha256": "old"})
    monkeypatch.setattr(
        haus_cmd.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    ok_dry, msg_dry = haus_cmd.update_rust_suite_if_needed(dry_run=True, transport=transport)
    assert ok_dry
    assert "rust-v1.0.0 -> rust-v1.0.3" in msg_dry

    ok_up, msg_up = haus_cmd.update_rust_suite_if_needed(dry_run=False, transport=transport)
    assert ok_up, msg_up
    assert "rust-v1.0.3" in msg_up
    assert haus_cmd.load_rust_state()["tar_sha256"] == sha

    ok_again, msg_again = haus_cmd.update_rust_suite_if_needed(dry_run=False, transport=transport)
    assert ok_again
    assert "already up-to-date" in msg_again


def test_portable_configs_and_launchagents_ram_tiers(tmp_path: Path) -> None:
    certs_dir = tmp_path / ".sanctum" / "certs"
    proxy_cfg = tmp_path / ".sanctum" / "sanctum-proxy" / "config.yaml"
    cap_cfg = tmp_path / ".sanctum" / "castellan" / "capacity.yaml"
    inst_cfg = tmp_path / ".sanctum" / "instance.yaml"

    haus_cmd.write_portable_proxyd_config(
        config_path=proxy_cfg,
        certs_dir=certs_dir,
        api_model_name="Qwen3.8-27B-4bit",
    )
    parsed = yaml.safe_load(proxy_cfg.read_text(encoding="utf-8"))
    assert parsed["port"] == 4040
    assert parsed["presidio_scrub"] is True
    assert parsed["models"][0]["api_base"] == "https://127.0.0.1:1337"
    assert parsed["models"][0]["mtls"] is True
    assert parsed["model_aliases"]["council-brain"] == "council-mlx"
    assert parsed["model_aliases"]["sanctum-dragon"] == "council-mlx"

    haus_cmd.write_portable_castellan_capacity(cap_cfg, ram_gb=32)
    assert stat.S_IMODE(cap_cfg.stat().st_mode) == 0o644
    cap_parsed = yaml.safe_load(cap_cfg.read_text(encoding="utf-8"))
    assert cap_parsed["version"] == 1
    assert len(cap_parsed["hosts"]) == 1
    assert "sanctum-mlx" in cap_parsed["services"]

    mb_cfg = tmp_path / ".sanctum" / "msg-bus" / "config.yaml"
    haus_cmd.write_portable_msg_bus_config(mb_cfg)
    assert "quiet_hours" in yaml.safe_load(mb_cfg.read_text(encoding="utf-8"))

    haus_cmd.ensure_hub_instance_yaml(inst_cfg)
    inst_parsed = yaml.safe_load(inst_cfg.read_text(encoding="utf-8"))
    assert inst_parsed["instance"]["role"] == "hub"

    # Build LaunchAgent specs for 64GB M4 Pro (full hub parity: 2 cathedrals + ablation + prewarm + memory + pressure-valve + endocrine + r2d2)
    adapter_dir = tmp_path / "models" / "adapters" / "champ"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "adapters.safetensors").write_bytes(b"TENSORS")
    model_dir = tmp_path / "models" / "mlx" / "Qwen3.8-27B-4bit"
    coder_dir = tmp_path / "models" / "mlx" / "Qwen3.5-9B-4bit"
    (tmp_path / ".sanctum" / "bin").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sanctum" / "bin" / "refusal_dirs_38_v2.safetensors").write_bytes(b"ABL")
    prewarm_path, ablation_path = haus_cmd.ensure_hub_support_files(tmp_path / ".sanctum")
    assert prewarm_path.is_file()
    assert ablation_path is not None and ablation_path.is_file()
    assert (tmp_path / ".sanctum" / "memory" / "inbox" / "agy").is_dir()

    specs_64 = {
        s["Label"]: s
        for s in haus_cmd.build_launchagent_specs(
            home=tmp_path,
            model_dir=model_dir,
            adapter_dir=adapter_dir,
            ram_gb=64,
            coder_model_dir=coder_dir,
            ablation_path=ablation_path,
            prewarm_path=prewarm_path,
            sanctum_bin="/opt/homebrew/bin/sanctum",
        )
    }
    assert len(specs_64) == len(haus_cmd.CORE_LAUNCHAGENTS)
    assert "haus.sanctum.mlx-coder" in specs_64
    assert "haus.sanctum.memory-vault" in specs_64
    assert "haus.sanctum.pressure-valve" in specs_64
    assert "haus.sanctum.endocrine-gland" in specs_64
    assert "haus.sanctum.r2d2" in specs_64
    assert "--adapter-path" in specs_64["haus.sanctum.mlx"]["ProgramArguments"]
    assert "26000" in specs_64["haus.sanctum.mlx"]["ProgramArguments"]
    assert "SANCTUM_MLX_ABLATION_DIRS" in specs_64["haus.sanctum.mlx"]["EnvironmentVariables"]
    assert "SANCTUM_MLX_PREWARM_FILE" in specs_64["haus.sanctum.mlx"]["EnvironmentVariables"]
    assert specs_64["haus.sanctum.proxyd"]["WorkingDirectory"].endswith(".sanctum/sanctum-proxy")
    assert "SANCTUM_PROXY_CONFIG" in specs_64["haus.sanctum.proxyd"]["EnvironmentVariables"]
    assert "--capacity-file" in specs_64["haus.sanctum.castellan"]["ProgramArguments"]
    assert "CASTELLAN_STATE_DIR" in specs_64["haus.sanctum.castellan"]["EnvironmentVariables"]

    specs_16 = {
        s["Label"]: s
        for s in haus_cmd.build_launchagent_specs(
            home=tmp_path, model_dir=model_dir, adapter_dir=None, ram_gb=16
        )
    }
    assert "haus.sanctum.mlx-coder" not in specs_16
    assert "--adapter-path" not in specs_16["haus.sanctum.mlx"]["ProgramArguments"]
    assert "10000" in specs_16["haus.sanctum.mlx"]["ProgramArguments"]


def test_haus_install_operator_guard_and_yoda_ca_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(haus_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(haus_cmd.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(haus_cmd, "is_operator_haus", lambda: True)

    res = runner.invoke(app, ["haus", "install", "--dry-run"])
    assert res.exit_code == 0
    assert "operator haus" in res.output.lower()

    # Yoda LaunchAgent includes NODE_EXTRA_CA_CERTS pointing to ~/.sanctum/certs/ca.crt
    plist_path = yoda_cmd._write_launchagent(openclaw_bin="/opt/homebrew/bin/openclaw")
    pl = plistlib.loads(plist_path.read_bytes())
    assert pl["EnvironmentVariables"]["NODE_EXTRA_CA_CERTS"].endswith("/.sanctum/certs/ca.crt")
    assert "/.sanctum/bin:" in pl["EnvironmentVariables"]["PATH"]


def test_unified_update_includes_rust_suite_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_RUST_STATE", str(tmp_path / "rust-suite.json"))
    monkeypatch.setenv("SANCTUM_DRAGON_DIR", str(tmp_path / "dragon"))
    monkeypatch.setenv("SANCTUM_YODA_STATE", str(tmp_path / "yoda.json"))

    bin_dir = haus_cmd.sanctum_root() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "sanctum-mlx").write_bytes(b"stub")
    haus_cmd.save_rust_state({"release_tag": "rust-v1.0.0"})

    called: list[str] = []
    monkeypatch.setattr(update_cmd.shutil, "which", lambda n: f"/opt/homebrew/bin/{n}")
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(
        haus_cmd,
        "update_rust_suite_if_needed",
        lambda *, dry_run=False: (called.append(f"rust:{dry_run}"), (True, "upgraded"))[1],
    )

    res = runner.invoke(app, ["update", "--skip-self-test"])
    assert res.exit_code == 0, res.output
    assert called == ["rust:False"]
