"""``sanctum init`` — create, complete or update a minimal ``instance.yaml``.

The first-run blocker (v0.9.0): every primary command calls ``config.load()``,
which hard-raises ``ConfigError`` when ``~/.sanctum/instance.yaml`` is absent —
so a stranger on a brand-new Mac hit a wall, with a fix that told them to
hand-write YAML. ``sanctum init`` writes the smallest file the loader accepts
(``instance.name`` + ``instance.slug``) so the box is usable in one command.

Behaviour:
  - No file: prompts for the instance name with a hostname-derived default and
    derives the slug from it. ``--name`` skips the prompt; ``--yes`` accepts the
    default for a fully non-interactive run (CI, scripted installs). ``--role``
    writes ``instance.role``.
  - A file without an ``instance:`` block (a machine provisioned before ``init``
    existed, e.g. a second haus with only a ``router:`` block): the block is
    added with the same name selection and the rest of the file is kept.
  - A file with an ``instance:`` block: ``--role`` / ``--name`` update just those
    keys and print what changed. An existing slug never changes (it names
    buckets and URLs); a block without one gets a slug derived from its name
    (``--name``, else the name already there). With nothing to change the file
    is left alone: exit 0 when it loads, exit 5 with the schema error when the
    strict loader still rejects it.
  - An ``instance:`` that holds a single value or a list is refused up front,
    before any prompt, with the lines to write by hand (exit 1).
  - Every edit of an existing file goes through
    :func:`config.set_instance_fields`: comments, key order and line endings are
    kept, the result is re-parsed before it is written, and the original is
    saved to ``<file>.bak``. A file it cannot edit safely is left untouched and
    the lines to add by hand are printed (exit 1). An edited file that still
    fails the strict loader exits 5.
  - ``--force`` replaces the whole file; the old one is saved to ``<file>.bak``.
  - Honours ``$SANCTUM_INSTANCE_FILE`` via :func:`config.instance_path`.

Names, paths, keys and messages come from the user or their file, so every one
is printed as a :class:`rich.text.Text` span, never as Rich markup: no value can
inject a style or garble the line (a trailing backslash would escape a tag).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from sanctum_cli import config
from sanctum_cli.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

# soft_wrap keeps paths and hand-edit lines copy-pasteable; emoji=False prints a
# name such as "Haus :house:" exactly as typed.
console = Console(soft_wrap=True, emoji=False)
err_console = Console(stderr=True, soft_wrap=True, emoji=False)

_Part = str | tuple[str, str]  # plain text, or (text, style)

# The hand-edit lines' stand-ins when ``--name`` did not supply the real values.
_NAME_PLACEHOLDER = "<instance name>"
_SLUG_PLACEHOLDER = "<instance-slug>"


class Role(StrEnum):
    hub = "hub"
    satellite = "satellite"


def init_command(
    *,
    name: str | None = None,
    yes: bool = False,
    force: bool = False,
    role: Role | None = None,
) -> None:
    """Create ``instance.yaml``, or add / update its ``instance:`` block in place.

    ``cli.init_top`` owns the command's options and help text and calls this.
    """
    target = config.instance_path()
    role_value = role.value if role else None

    if force or not target.exists():
        _write_whole_file(target, name=name, yes=yes, role=role_value)
        return
    try:
        data = config.load_raw(target)
    except ConfigError as exc:
        _fail(exc)
    block = data.get("instance")
    if isinstance(block, dict) and block:
        _update_block(target, data, name=name, role=role_value)
        return
    _refuse_unusable_block(target, data, name=name, role=role_value)
    _add_block(target, name=name, yes=yes, role=role_value)


def _write_whole_file(target: Path, *, name: str | None, yes: bool, role: str | None) -> None:
    """No file yet, or ``--force``: write a fresh file holding only ``instance:``."""
    existed = target.exists()
    chosen = _choose_name(name, yes=yes)
    try:
        written = config.scaffold_instance(target, name=chosen, role=role)
        # Load back through the strict validator so we only ever claim success on
        # a file the rest of the CLI can actually read (contract at the boundary).
        cfg = config.load(written)
    except ConfigError as exc:
        _fail(exc)
    if existed:
        _say(
            ("✓", "green"),
            f" overwrote {written} ",
            (f"(previous file saved to {config.backup_path(written)})", "dim"),
        )
    else:
        _say(("✓", "green"), f" wrote {written}")
    _print_identity(cfg.instance.name, cfg.instance.slug, cfg.instance.role)
    console.print("[dim]next: [cyan]sanctum onboard --recipe family[/] to wire a backup[/]")


def _refuse_unusable_block(
    target: Path, data: dict[str, Any], *, name: str | None, role: str | None
) -> None:
    """Exit 1, before any prompt, when ``instance:`` holds a single value or a list.

    The editor will not rewrite that shape, so asking for a name first would only
    waste the answer. The hand-edit lines use ``--name`` when given, else
    placeholders.
    """
    cleaned = _valid_name(name.strip()) if name and name.strip() else None
    fields = {
        "name": cleaned or _NAME_PLACEHOLDER,
        "slug": config.slugify_name(cleaned) if cleaned else _SLUG_PLACEHOLDER,
    }
    if role:
        fields["role"] = role
    error = config.instance_shape_error(target, data, fields)
    if error is not None:
        _fail(error)


def _add_block(target: Path, *, name: str | None, yes: bool, role: str | None) -> None:
    """An existing file without an ``instance:`` block: add one, keep the rest."""
    chosen = _choose_name(name, yes=yes)
    slug = config.slugify_name(chosen)
    try:
        config.set_instance_fields(target, name=chosen, slug=slug, role=role)
    except ConfigError as exc:
        _fail(exc)
    _say(
        ("✓", "green"),
        f" added an instance: block to {target} ",
        (f"(other blocks kept; previous file saved to {config.backup_path(target)})", "dim"),
    )
    _print_identity(chosen, slug, role)
    _require_loadable(target, edited=True)


def _update_block(
    target: Path, data: dict[str, Any], *, name: str | None, role: str | None
) -> None:
    """An existing ``instance:`` block: change what ``--name`` / ``--role`` ask for.

    A block without a slug also gets one, derived from ``--name`` or else the
    name already there; an existing slug never changes.
    """
    block: dict[str, Any] = data["instance"]
    new_name = _valid_name(name.strip()) if name and name.strip() else None
    old_name = block.get("name")
    slug_source = new_name or (old_name.strip() if isinstance(old_name, str) else "")
    slug = config.slugify_name(slug_source) if block.get("slug") is None and slug_source else None
    if new_name is None and role is None and slug is None:
        _require_loadable(target, edited=False)
        _report_untouched(target, data)
        return
    try:
        changes = config.set_instance_fields(target, name=new_name, slug=slug, role=role)
    except ConfigError as exc:
        _fail(exc)
    if not changes:
        _require_loadable(target, edited=False)
        _say(("✓", "green"), f" {target} is already up to date — nothing to do")
        return
    _say(
        ("✓", "green"),
        f" updated {target} ",
        (f"(other blocks kept; previous file saved to {config.backup_path(target)})", "dim"),
    )
    for key, (old, new) in changes.items():
        _say(f"  {key}: {_show(old)} -> {_show(new)}")
    kept_slug = block.get("slug")
    if "name" in changes and kept_slug is not None:
        _say((f"  slug stays {_show(kept_slug)}: it names this haus's buckets and URLs", "dim"))
    _require_loadable(target, edited=True)


def _report_untouched(target: Path, data: dict[str, Any]) -> None:
    """No ``--name`` / ``--role`` on a file whose ``instance:`` block loads."""
    _say(("✓", "green"), f" {target} already exists — nothing to do")
    others = [str(key) for key in data if key != "instance"]
    if not others:
        console.print("[dim]re-run with --force to overwrite it[/]")
        return
    _say(
        (
            "to change it in place, re-run with --role <hub|satellite> and/or --name <name>, "
            f"or edit the instance: block in {target} by hand. --force would replace the "
            f"whole file and drop its other blocks ({', '.join(others)}).",
            "dim",
        )
    )


def _choose_name(name: str | None, *, yes: bool) -> str:
    """``--name``, else the hostname default under ``--yes``, else ask for it."""
    default_name, _ = config._default_identity()
    if name:
        return _valid_name(name.strip() or default_name)
    if yes:
        return default_name
    try:
        answer = Prompt.ask("instance name", default=default_name, console=console)
    except EOFError:
        console.print()  # end the unanswered prompt line
        _fail_with(
            "no instance name given and no terminal to ask for one",
            "pass --name <name>, or --yes to accept the default",
        )
    return _valid_name(answer.strip() or default_name)


def _valid_name(name: str) -> str:
    """Refuse a name that is not text (undecodable bytes in the argument)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        _fail_with("the instance name is not valid UTF-8 text", "retype it as plain text")
    return name


def _require_loadable(target: Path, *, edited: bool) -> None:
    """Exit 5 with the strict loader's reason when the file does not load."""
    try:
        config.load(target)
    except ConfigError as exc:
        lead = (
            f" the instance: block is in place, but {target} does not load yet:"
            if edited
            else f" {target} does not load:"
        )
        err_console.print(Text.assemble(("!", "yellow"), lead))
        _fail(exc, code=int(exc.exit_code))


def _print_identity(name: str, slug: str, role: str | None) -> None:
    role_part = f", role={role}" if role else ""
    _say("  instance: ", (name, "bold"), f" (slug={slug}{role_part})")


def _fail(exc: ConfigError, *, code: int = 1) -> NoReturn:
    _fail_with(exc.message, exc.fix, code=code, cause=exc)


def _fail_with(
    message: str, fix: str | None = None, *, code: int = 1, cause: BaseException | None = None
) -> NoReturn:
    err_console.print(Text.assemble(("✗", "red"), f" {message}"))
    if fix:
        err_console.print(Text.assemble(("fix:", "dim"), f" {fix}"))
    raise typer.Exit(code) from cause


def _say(*parts: _Part) -> None:
    """Print one line built from plain text and ``(text, style)`` spans; no markup."""
    console.print(Text.assemble(*parts))


def _show(value: object) -> str:
    return "(unset)" if value is None else str(value)
