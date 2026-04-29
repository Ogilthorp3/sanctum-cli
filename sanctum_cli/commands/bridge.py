"""``sanctum bridge`` — talk to the Sanctum Bridge from the terminal.

Subcommands:

    sanctum bridge health                         liveness check
    sanctum bridge whoami                         show effective config
    sanctum bridge manifest [--json]              list modules + actions
    sanctum bridge folder <path>                  look up a SP folder
    sanctum bridge upload <file> <folder> ...     upload a file to SP

Credentials come from the Keychain (services
``sanctum-bridge-cf-access-client-{id,secret}`` and
``sanctum-bridge-hmac-sharepoint``). The base URL defaults to
``https://bridge.nepveu.name`` and can be overridden with
``SANCTUM_BRIDGE_URL`` for staging.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli.bridge_client import (
    BridgeClient,
    BridgeCreds,
    base_url_from_env,
    encode_file,
)
from sanctum_cli.errors import UserError

console = Console()
err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _client(module: str = "sharepoint") -> BridgeClient:
    return BridgeClient(
        BridgeCreds.from_keychain(),
        base_url=base_url_from_env(),
        module=module,
    )


def _parse_kv_list(items: list[str] | None) -> dict[str, str]:
    """Convert ``["k=v", "x=y"]`` into ``{"k":"v", "x":"y"}``."""
    if not items:
        return {}
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            msg = f"--metadata expects key=value, got {raw!r}"
            raise UserError(msg, fix="pass each pair as --metadata k=v")
        k, _, v = raw.partition("=")
        k = k.strip()
        if not k:
            msg = f"--metadata key is empty in {raw!r}"
            raise UserError(msg)
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Command implementations
# --------------------------------------------------------------------------- #

def health_command(json_output: bool = False) -> None:
    with _client() as c:
        data = c.health()
    if json_output:
        console.print_json(data=data)
        return
    modules = ", ".join(data.get("modules", [])) or "(none)"
    console.print(
        f"[green]ok[/green]  v{data.get('version', '?')}  modules: {modules}  "
        f"@ {c.base_url}"
    )


def whoami_command() -> None:
    creds = BridgeCreds.from_keychain()
    base = base_url_from_env()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("base URL", base)
    table.add_row("CF Access client id", creds.cf_access_id)
    table.add_row(
        "CF Access secret",
        f"{creds.cf_access_secret[:8]}…{creds.cf_access_secret[-4:]} (redacted)",
    )
    table.add_row(
        "HMAC secret",
        f"{creds.hmac_secret[:8]}…{creds.hmac_secret[-4:]} (redacted)",
    )
    console.print(table)


def manifest_command(json_output: bool = False) -> None:
    with _client() as c:
        data = c.manifest()
    if json_output:
        console.print_json(data=data)
        return
    for mod in data.get("modules", []):
        console.print(f"[bold]{mod['name']}[/bold]  v{mod['version']}")
        actions = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        actions.add_column("action")
        actions.add_column("HTTP")
        actions.add_column("summary", overflow="fold")
        for a in mod.get("actions", []):
            actions.add_row(
                f"{mod['name']}.{a['name']}",
                f"{a['method']} /{mod['name']}{a['path']}",
                a.get("summary", ""),
            )
        console.print(actions)


def folder_command(path: str, json_output: bool = False) -> None:
    with _client() as c:
        data = c.folder(path)
    if json_output:
        console.print_json(data=data)
        return
    console.print(f"[bold]{path}[/bold]")
    console.print(f"  drive_item_id : {data['drive_item_id']}")
    console.print(f"  child_count   : {data['child_count']}")
    console.print(f"  last_modified : {data['last_modified']}")
    console.print(f"  web_url       : [link]{data['web_url']}[/link]")


def upload_command(
    file: Path,
    folder: str,
    *,
    if_exists: str = "version",
    doc_type: str | None = None,
    metadata: list[str] | None = None,
    no_create_folders: bool = False,
    json_output: bool = False,
) -> None:
    if if_exists not in {"version", "overwrite", "rename", "fail"}:
        msg = f"--if-exists must be one of version|overwrite|rename|fail (got {if_exists!r})"
        raise UserError(msg)

    filename, b64 = encode_file(file)
    meta = _parse_kv_list(metadata)
    if doc_type:
        meta.setdefault("doc_type", doc_type)

    with _client() as c:
        result = c.upload(
            folder_path=folder,
            filename=filename,
            content_b64=b64,
            metadata=meta,
            create_folder_if_missing=not no_create_folders,
            if_exists=if_exists,
        )

    if json_output:
        console.print_json(data=result)
        return
    console.print(f"[green]uploaded[/green]  {filename} → {folder}/")
    console.print(f"  web_url       : [link]{result['web_url']}[/link]")
    if result.get("version") is not None:
        console.print(f"  version       : {result['version']}")
    console.print(f"  drive_item_id : {result['drive_item_id']}")
    console.print(f"  etag          : {result['etag']}")
    if not result.get("metadata_applied", True):
        err_console.print(
            "[yellow]warning[/yellow]: metadata patch was rejected — "
            "the SharePoint library probably has no column for one of the "
            "supplied keys. The file is uploaded; only the metadata is missing."
        )
