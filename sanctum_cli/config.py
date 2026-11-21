"""Configuration schema for sanctum-cli.

The CLI reads ``~/.sanctum/instance.yaml`` (or ``$SANCTUM_INSTANCE_FILE``)
and pulls out the ``cli:`` block. The block is optional — a fresh
install runs with sensible defaults; users add a ``cli:`` block when
they want to customize routing, providers, or telemetry.

Validation is via pydantic v2 with ``model_config = ConfigDict(extra="forbid")``
so typos in keys fail loudly with a precise pointer.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml.constructor import SafeConstructor
from yaml.reader import Reader
from yaml.scanner import ScannerError

from sanctum_cli.errors import ConfigError

DEFAULT_INSTANCE_FILE = Path("~/.sanctum/instance.yaml").expanduser()
ENV_INSTANCE_FILE = "SANCTUM_INSTANCE_FILE"


# ─── Model fragments ────────────────────────────────────────────────


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeychainRef(StrictModel):
    """Pointer to a Keychain generic-password entry."""

    service: str
    account: str


class ClaudeProvider(StrictModel):
    """Claude routing config.

    ``via=proxy`` (default) talks to a local OpenAI-compatible proxy that
    shells out to the ``claude`` CLI — billing happens against the user's
    Max subscription, not the API. ``via=direct`` uses the official
    Anthropic SDK and bills against the API key in Keychain.
    """

    via: Literal["proxy", "direct"] = "proxy"
    # claude-max-proxy (Max subscription, OpenAI-compat) listens on :3456 and
    # advertises the un-suffixed id `claude-opus-4` via /v1/models. The old
    # anthropic-proxy on :2001 was retired (see instance.yaml services).
    endpoint: str = "http://127.0.0.1:3456"
    model: str = "claude-opus-4"
    keychain: KeychainRef = Field(
        default_factory=lambda: KeychainRef(service="anthropic-api-key", account="sanctum")
    )
    timeout_s: int = Field(default=300, ge=1, le=900)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_tokens: int = Field(default=4096, ge=1, le=200_000)


class GeminiProvider(StrictModel):
    """Gemini routing config.

    ``via=direct`` (default) uses the ``google-genai`` SDK against Google AI
    Studio and bills the API key in Keychain. ``via=proxy`` talks to a local
    OpenAI-compatible Gemini subscription bridge (``sanctum-agy-proxy`` on
    ``proxy_url``, default ``http://127.0.0.1:6543``) with zero per-token API
    charges.
    """

    via: Literal["direct", "proxy"] = "direct"
    endpoint: str = "https://generativelanguage.googleapis.com"
    proxy_url: str = "http://127.0.0.1:6543"
    model: str = "gemini-2.5-pro"
    keychain: KeychainRef = Field(
        default_factory=lambda: KeychainRef(service="google-ai-api-key", account="sanctum")
    )
    timeout_s: int = Field(default=120, ge=1, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)


class MlxLocalProvider(StrictModel):
    endpoint: str = "http://127.0.0.1:8900"
    model: str = "council-secure"
    timeout_s: int = Field(default=60, ge=1, le=600)
    always_available: bool = True


class Providers(StrictModel):
    claude: ClaudeProvider = Field(default_factory=ClaudeProvider)
    gemini: GeminiProvider = Field(default_factory=GeminiProvider)
    mlx_local: MlxLocalProvider = Field(default_factory=MlxLocalProvider)


# ─── Routing ────────────────────────────────────────────────────────


class RoutingRule(StrictModel):
    """A single routing rule — `when` matched, dispatch to `then`."""

    when: dict[str, Any]
    then: Literal["claude", "gemini", "mlx_local"]


class Routing(StrictModel):
    rules: list[RoutingRule] = Field(default_factory=list)
    fallback: Literal["claude", "gemini", "mlx_local"] = "claude"


# ─── Telemetry ──────────────────────────────────────────────────────


class Telemetry(StrictModel):
    enabled: bool = True
    path: Path = Path("~/.sanctum/telemetry/cli.jsonl")
    redact_prompts: bool = True
    aggregate_window_days: int = Field(default=7, ge=1, le=365)

    @field_validator("path", mode="before")
    @classmethod
    def expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v


# ─── Cloud backup pointers ──────────────────────────────────────────


class CloudBackupRepo(StrictModel):
    kind: Literal["restic", "borg", "kopia"] = "restic"
    repo: str
    keychain: KeychainRef


class CloudBackupRetention(StrictModel):
    keep_daily: int = Field(default=7, ge=1)
    keep_weekly: int = Field(default=4, ge=0)
    keep_monthly: int = Field(default=12, ge=0)


class CloudBackup(StrictModel):
    primary: CloudBackupRepo | None = None
    secondary: CloudBackupRepo | None = None
    retention: CloudBackupRetention = Field(default_factory=CloudBackupRetention)


# ─── UI ──────────────────────────────────────────────────────────────


class UISettings(StrictModel):
    color: Literal["auto", "always", "never"] = "auto"
    progress: Literal["auto", "rich", "none"] = "auto"
    json_default: bool = False


# ─── Module overrides ────────────────────────────────────────────────


class ModuleConfig(StrictModel):
    """Per-module operator overrides.

    Only ``enabled`` for now (YAGNI). Keyed by module name under
    ``cli.modules`` in instance.yaml, e.g.::

        cli:
          modules:
            backup:
              enabled: false
    """

    enabled: bool = True


# ─── Top-level CLI section ──────────────────────────────────────────


class Recipe(StrictModel):
    """A backup recipe — a named bundle of sources/excludes for an audience.

    Recipes solve the ``what do I back up?`` question. ``family`` covers the
    crucial-but-small data a typical household cares about (documents,
    secrets) and is sized to fit the R2 free tier with dedup. ``operator``
    is the Sanctum-host configuration recipe used by the original bash
    script. Users can override built-ins or add new ones via ``instance.yaml``.
    """

    description: str = ""
    sources: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    target: Literal["primary", "secondary"] = "primary"
    auto_exclude_icloud_photos: bool = True


class CliConfig(StrictModel):
    """The ``cli:`` block in instance.yaml."""

    default_provider: Literal["claude", "gemini", "mlx_local"] = "claude"
    routing: Routing = Field(default_factory=Routing)
    providers: Providers = Field(default_factory=Providers)
    telemetry: Telemetry = Field(default_factory=Telemetry)
    cloud_backup: CloudBackup | None = None
    ui: UISettings = Field(default_factory=UISettings)
    recipes: dict[str, Recipe] = Field(default_factory=dict)
    default_recipe: str | None = None
    modules: dict[str, ModuleConfig] = Field(default_factory=dict)


class InstanceMetadata(StrictModel):
    """Subset of ``instance:`` block we actually read."""

    name: str
    slug: str
    timezone: str | None = None
    # ``satellite`` marks a contained remote site (a second haus) that runs its own
    # local brain but none of the hub services; ``self-test`` grades it on the
    # satellite profile. Absent = infer the tier from what is on disk.
    role: Literal["hub", "satellite"] | None = None


class Config(StrictModel):
    """Composite of instance metadata + cli section.

    Other top-level keys in instance.yaml (services, paths, network, …)
    are deliberately not modeled here — they belong to other tools.
    """

    instance: InstanceMetadata
    cli: CliConfig = Field(default_factory=CliConfig)

    @field_validator("cli", mode="before")
    @classmethod
    def empty_cli_block(cls, v: object) -> object:
        """``cli:`` with no value (or ``~``) is a block with no keys: every setting at its default.

        A template whose ``cli:`` keys are all commented out reads this way, and the
        ``cloud setup`` wizards fill such a block.
        """
        return CliConfig() if v is None else v


# ─── Loader ──────────────────────────────────────────────────────────


def instance_path() -> Path:
    """``$SANCTUM_INSTANCE_FILE`` with ``~`` expanded, else ``~/.sanctum/instance.yaml``.

    A ``~user`` that names no user on this machine (or a ``~`` with no home to
    expand to) is kept as the literal path instead of raising: the role and value
    readers call this while the CLI is still importing, where an exception would
    crash every command.
    """
    override = os.environ.get(ENV_INSTANCE_FILE)
    if not override:
        return DEFAULT_INSTANCE_FILE
    try:
        return Path(override).expanduser()
    except RuntimeError:  # pathlib: "Could not determine home directory."
        return Path(override)


# What ``yaml.safe_load`` raises for a malformed document. Syntax errors are
# ``YAMLError``, but PyYAML's constructors raise plain Python errors for a value
# its tag cannot hold: an impossible date such as 2026-02-30 (ValueError),
# ``!!int ""`` (IndexError), ``!!bool maybe`` (KeyError), ``!!timestamp x``
# (AttributeError). Runaway nesting exhausts the recursion limit.
_YAML_LOAD_ERRORS: tuple[type[Exception], ...] = (
    yaml.YAMLError,
    ValueError,
    TypeError,
    IndexError,
    KeyError,
    AttributeError,
    OverflowError,
    RecursionError,
)
# Reading the file adds OSError; a file that is not UTF-8 (UnicodeDecodeError)
# is already covered as a ValueError.
_READ_AND_LOAD_ERRORS: tuple[type[Exception], ...] = (OSError, *_YAML_LOAD_ERRORS)


def instance_value(dotted_key: str, default: object = None, path: Path | None = None) -> Any:
    """Read an arbitrary dotted key from instance.yaml.

    For per-setup blocks that :class:`Config` deliberately does not model
    (``vcs.*``, ``services.*``, ``notifications.*``) but that need to stop being
    hardcoded — e.g. the GitHub owner, the deadman repo, the bridge URL. Returns
    ``default`` on any read/parse miss (unreadable, not UTF-8, bad YAML, a value
    PyYAML cannot build such as an impossible date) or absent key, so callers
    keep a safe fallback and never hard-fail on a fresh box. It never raises,
    whatever ``$SANCTUM_INSTANCE_FILE`` holds (see :func:`instance_path`):
    ``council`` calls it while the CLI is still importing, where an exception
    would crash every command with a traceback.
    """
    target = path or instance_path()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except _READ_AND_LOAD_ERRORS:
        return default
    cur: Any = raw
    for key in dotted_key.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


InstanceRole = Literal["hub", "satellite"]
_ROLES: dict[str, InstanceRole] = {"hub": "hub", "satellite": "satellite"}
_STR_TAG = "tag:yaml.org,2002:str"
_MAP_TAG = "tag:yaml.org,2002:map"


def instance_role(path: Path | None = None) -> InstanceRole | None:
    """The declared ``instance.role``: ``"hub"``, ``"satellite"`` or ``None``.

    The single role detector: the haus gate, self-test, doctor, service-user and
    setup all ask it rather than reading the key themselves. It never raises. The
    raw YAML is read, not the schema, so a schema violation elsewhere does not
    matter. A character PyYAML refuses (a control character such as an escape
    code or a BEL, over which PyYAML refuses the whole text) is read as an unknown
    character, U+FFFD, which is never part of ``hub``, ``satellite``, ``role`` or
    ``instance``: one in a comment hides nothing, and one inside a key or value
    breaks that key or value; where U+FFFD cannot stand (inside a tag, an anchor or
    alias name or a block scalar's header, or right after a quoted value, a flow
    collection or a key's colon) it is a syntax error like any other, so a role
    after it is not read. When another value cannot be built at all (an
    impossible date such as ``since: 2026-02-30``, ``!!bool maybe``), the role is
    read off the composed node tree instead, which builds no values
    (:func:`_composed_role`). When the file does not even compose (a syntax error,
    such as a tab indent; an undefined alias; a second document), it is read off
    the parser's events (:func:`_parsed_role`): a role written before the first
    syntax error is read; one after it, or only in a second document, is not. A
    missing, unreadable or non-UTF-8 file, a missing key or any value other than
    exactly ``hub`` or ``satellite`` all mean ``None``, and callers then infer the
    tier from what is on disk.
    """
    target = path or instance_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):  # missing, unreadable, or not UTF-8
        return None
    text = Reader.NON_PRINTABLE.sub("\ufffd", text)  # each refused character reads as unknown
    role: object
    try:
        data = yaml.safe_load(text)
    except _YAML_LOAD_ERRORS:
        role = _composed_role(text)
    else:
        block = data.get("instance") if isinstance(data, dict) else None
        role = block.get("role") if isinstance(block, dict) else None
    return _ROLES.get(role) if isinstance(role, str) else None


def _composed_role(text: str) -> object:
    """``instance.role`` as ``safe_load`` would read it, for a file it cannot build.

    The document is only composed (parsed into nodes; no value is built), so a
    value PyYAML cannot build anywhere in it is never looked at. The role is
    returned only when it is a string scalar; anything else is None. A document
    that does not even compose is read off the parser's events instead
    (:func:`_parsed_role`). Never raises.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except _YAML_LOAD_ERRORS:
        return _parsed_role(text)
    role = _composed_value(_composed_value(root, "instance"), "role")
    if isinstance(role, yaml.ScalarNode) and role.tag == _STR_TAG:
        return role.value
    return None


def _composed_value(node: yaml.Node | None, key: str) -> yaml.Node | None:
    """The node ``safe_load`` would build as ``node[key]``, found without building anything.

    PyYAML's own merge step runs first (it only rearranges nodes), so a key
    brought in by ``<<`` resolves as ``safe_load`` resolves it; a malformed
    merge is passed over and the explicit keys still count. Then, as in
    ``safe_load``, the last pair with the key wins. None when ``node`` is not a
    plain mapping (tagged ``tag:yaml.org,2002:map``: a ``!!set`` is built as a
    set, not a dict) or lacks the key.
    """
    if not isinstance(node, yaml.MappingNode) or node.tag != _MAP_TAG:
        return None
    with contextlib.suppress(*_YAML_LOAD_ERRORS):
        SafeConstructor().flatten_mapping(node)
    pairs = _pairs_for(node, key)
    return pairs[-1][1] if pairs else None


# The tags a parser event can carry on a scalar read as text, and on a mapping read
# as a plain block of keys: none (resolved from the value), ``!``, or the standard tag.
_TEXT_EVENT_TAGS = (None, "!", _STR_TAG)
_MAP_EVENT_TAGS = (None, "!", _MAP_TAG)
_UNDECIDED = object()  # the events read so far stop before the node that decides the role


@dataclass(slots=True)
class _OpenCollection:
    """A collection whose events are being read by :func:`_role_off_events`."""

    block: str | None  # "root" or "instance" for those blocks of keys; None when not read
    key_next: bool = True  # in a block of keys: whether its next node is a key
    key: str | None = None  # the current key, when it is text


def _parsed_role(text: str) -> object:
    """``instance.role`` read off the parser's events, for a file that does not even compose.

    Only what the parser reads before the first error counts, and only the first
    document: a role written before the first syntax error is read; one after it,
    or only in a second document, is not. The role is the scalar given as the
    value of the last ``role`` key in the block of keys that is the value of the
    last top-level ``instance`` key, as the parser reads that scalar: a value the
    error cuts off (an unclosed quote, say) is not a role, but a plain value ends
    where PyYAML ends it, before the error: at a tab or a ``: ``, and inside braces
    also at ``,``, ``?``, ``[``, ``]``, ``{`` or ``}``. So ``role: satellite<TAB># note``,
    ``role: hub<TAB>x`` and ``role: hub: x`` read as the text before the tab or
    colon, and ``{role: hub]`` reads ``hub``. Those keys and the role must be untagged, ``!`` or ``!!str``
    scalars and both blocks plain mappings; aliases and ``<<`` merges are not
    followed here. The parser reads a little ahead, so an error can stop it just
    before the node that decides the role; the text before the error is then read
    again (:func:`_error_start`), which settles that node. ``text`` holds no
    character PyYAML refuses: :func:`instance_role` has read each as an unknown
    character (U+FFFD). Never raises.
    """
    while True:
        role, cut = _role_off_events(text)
        if cut is None or cut >= len(text):
            return None if role is _UNDECIDED else role
        text = text[:cut]


def _role_off_events(text: str) -> tuple[object, int | None]:
    """The role read off ``text``'s events, and where to cut ``text`` to read it again.

    The role is ``_UNDECIDED`` while the next node would decide it. When an error
    stops the read there, the cut is where the error starts (:func:`_error_start`),
    so that the text before the error is read again; else None. ``text`` holds no
    character PyYAML refuses: :func:`instance_role` has read each as an unknown
    character (U+FFFD).
    """
    stack: list[_OpenCollection] = []
    role: object = _UNDECIDED  # until the document's root node starts
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.DocumentEndEvent):
                break  # a second document is what compose refused: it is not read
            if isinstance(event, yaml.CollectionEndEvent):
                stack.pop()
            elif isinstance(event, yaml.NodeEvent):
                role, block = _node_started(stack, event, role)
                if isinstance(event, yaml.CollectionStartEvent):
                    plain = (
                        isinstance(event, yaml.MappingStartEvent) and event.tag in _MAP_EVENT_TAGS
                    )
                    stack.append(_OpenCollection(block if plain else None))
    except _YAML_LOAD_ERRORS as exc:
        if role is _UNDECIDED:
            return role, _error_start(exc)
    return role, None


def _node_started(
    stack: list[_OpenCollection], event: yaml.NodeEvent, role: object
) -> tuple[object, str | None]:
    """The role once ``event`` starts a node, and which block of keys that node would be."""
    if not stack:
        return None, "root"  # no role unless the root is a block of keys with an instance key
    parent = stack[-1]
    if parent.block is None:
        return role, None
    text = (
        event.value
        if isinstance(event, yaml.ScalarEvent) and event.tag in _TEXT_EVENT_TAGS
        else None
    )
    if parent.key_next:
        parent.key, parent.key_next = text, False
        if (parent.block, text) in (("root", "instance"), ("instance", "role")):
            return _UNDECIDED, None  # its value replaces whatever came before
        return role, None
    parent.key_next = True
    if (parent.block, parent.key) == ("root", "instance"):
        return None, "instance"
    if (parent.block, parent.key) == ("instance", "role"):
        return text, None
    return role, None


def _error_start(error: Exception) -> int | None:
    """Where YAML ``error`` starts in the text it was read from; None when it has no place.

    For a scanner error with a context mark, that is the start of the token being
    read (the opening quote of an unclosed string, say), not where the scanner
    gave up; for any other error, its problem mark, where the character or token
    it could not take is.
    """
    if not isinstance(error, yaml.MarkedYAMLError) or error.problem_mark is None:
        return None
    index = error.problem_mark.index
    if isinstance(error, ScannerError) and error.context_mark is not None:
        index = min(index, error.context_mark.index)
    return index


def load(path: Path | None = None) -> Config:
    """Read and validate the instance config.

    Raises ``ConfigError`` for any read or schema failure with a precise
    pointer to the offending key: a missing or unreadable file, one that is not
    UTF-8, invalid YAML, a value PyYAML cannot build (an impossible date such as
    2026-02-30, ``!!bool maybe``), a top level that is not a mapping, or a
    schema violation. Never a traceback.
    """
    target = path or instance_path()
    if not target.exists():
        msg = f"instance config not found: {target}"
        raise ConfigError(
            msg,
            fix=(
                f"create {target} with at minimum:\n"
                "  instance:\n    name: My Sanctum\n    slug: my-sanctum\n"
            ),
        )

    raw = load_raw(target)
    relevant = {k: raw[k] for k in ("instance", "cli") if k in raw}
    try:
        return Config.model_validate(relevant)
    except ValidationError as exc:
        msg = _format_validation_error(exc, target)
        raise ConfigError(msg) from exc


def _format_validation_error(exc: ValidationError, target: Path) -> str:
    lines = [f"schema violation in {target}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


# ─── First-run scaffolding ───────────────────────────────────────────


def _default_identity() -> tuple[str, str]:
    """Derive a friendly ``(name, slug)`` from the hostname for first-run setup."""
    host = (socket.gethostname() or "sanctum").split(".")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "sanctum"
    return f"{host} Sanctum", slug


def slugify_name(name: str) -> str:
    """Bucket/url-safe slug from a free-text instance name.

    Lowercased, non-alphanumerics collapsed to ``-``, trimmed; falls back to
    ``sanctum`` when the input has no usable characters. Mirrors the hostname
    slug rule in :func:`_default_identity` so a typed name and a derived name
    produce slugs of the same shape.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "sanctum"


def _instance_block(name: str | None, role: str | None) -> dict[str, str]:
    cleaned = name.strip() if name else ""
    if cleaned:
        chosen_name, slug = cleaned, slugify_name(cleaned)
    else:
        chosen_name, slug = _default_identity()
    block = {"name": chosen_name, "slug": slug}
    if role:
        block["role"] = role
    return block


def scaffold_instance(
    path: Path | None = None, *, name: str | None = None, role: str | None = None
) -> Path:
    """Write a minimal, valid ``instance.yaml`` when the user has none.

    Backs the ``sanctum init`` command and :func:`ensure` so a brand-new machine
    can run the CLI without hand-writing YAML. ``name`` overrides the
    hostname-derived default; the slug is always derived from the chosen name so
    the two stay consistent. ``role`` (``hub`` / ``satellite``) is written only
    when given. Returns the path written. Always overwrites the target — callers
    that must not clobber check existence first (``init`` does). An existing file
    (``init --force``) is replaced atomically with its mode kept, after its bytes
    are saved to :func:`backup_path`.
    """
    target = path or instance_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        {"instance": _instance_block(name, role)},
        sort_keys=False,
        allow_unicode=True,
    )
    if target.exists():
        _replace_file(target, rendered.encode("utf-8"), backup=_read_bytes(target))
    else:
        target.write_text(rendered, encoding="utf-8")
    return target


# ─── Editing an existing instance.yaml ───────────────────────────────
#
# ``sanctum init`` adds or updates the ``instance:`` block of a file that also
# holds other blocks (``router:``, ``cli:``) and the operator's comments. A
# load/dump round-trip would drop every comment and reflow the file, so the
# editor splices text at the positions PyYAML reports for each node, re-parses
# the result, and writes nothing unless only the requested keys changed.

_UTF8_BOM = b"\xef\xbb\xbf"
_BREAK_CHARS = "\r\n\x85\u2028\u2029"  # every line break PyYAML recognises
_LINE_BREAK = re.compile("\r\n|[\r\n\x85\u2028\u2029]")
_FIRST_NEWLINE = re.compile("\r\n|\r|\n")
_COLON = re.compile(r"[ \t]*:")
_BLANK_OR_COMMENT = re.compile(r"[ \t]*(?:#.*)?")
_EMPTY_VALUE = re.compile(r"~|null|Null|NULL|\{[ \t]*\}")
_NULL_TAG = "tag:yaml.org,2002:null"
_MERGE_TAG = "tag:yaml.org,2002:merge"

_Edit = tuple[int, int, str]  # replace text[start:end] with the string


def backup_path(path: Path) -> Path:
    """Where an edit of ``path`` keeps the original bytes: ``<file>.bak`` beside it."""
    return path.with_name(path.name + ".bak")


def load_raw(path: Path | None = None) -> dict[str, Any]:
    """Read ``instance.yaml`` as a plain mapping, without schema validation.

    ``{}`` for an empty or comments-only file. Raises ``ConfigError`` (never a
    traceback) when the file cannot be read, is not UTF-8, is not valid YAML or
    is not a mapping at the top level.
    """
    target = path or instance_path()
    return _parse_mapping(target, _read_for_edit(target)[2]) or {}


def load_raw_if_present(path: Path | None = None) -> dict[str, Any]:
    """:func:`load_raw`, but ``{}`` when the file does not exist yet.

    For a read-modify-write that may create the file. A file that exists but
    cannot be read or built still raises ``ConfigError``, so it is reported
    instead of being treated as empty and rewritten with only the new keys.
    """
    target = path or instance_path()
    return load_raw(target) if target.exists() else {}


def read_yaml(path: Path) -> Any:
    """Parse another YAML file the CLI reads and rewrites, such as screen-time's devices.yaml.

    The same reading as :func:`load_raw`, with the top level left for the caller
    to check: ``None`` for an empty file, and ``ConfigError`` (never a
    traceback) when the file cannot be read, is not UTF-8, is not valid YAML or
    holds a value PyYAML cannot build, naming the file, line and value.
    """
    return _parse_yaml(path, _read_for_edit(path)[2])


def set_instance_fields(
    path: Path | None = None,
    *,
    name: str | None = None,
    slug: str | None = None,
    role: str | None = None,
) -> dict[str, tuple[Any, str]]:
    """Set ``instance.name`` / ``slug`` / ``role`` in an existing file, keeping the rest.

    The one way ``sanctum init`` edits a file that already exists:

    - no ``instance`` key: a block is inserted at the top of the document, after
      any ``%`` directives and a leading ``---``. Needs ``name`` and ``slug``.
    - ``instance:`` empty, ``~`` or ``{}`` (a template whose keys are commented
      out): the keys go right under it. Needs ``name`` and ``slug``.
    - ``instance:`` block mapping: an existing ``name:`` / ``role:`` value is
      replaced in place (a trailing comment stays); a missing key is inserted
      right under ``instance:`` at the block's indentation. An existing slug is
      never changed: it names this haus's buckets and URLs.

    Everything else stays byte for byte: comments, key order, a UTF-8 BOM and the
    line ending (CRLF, CR or LF), which inserted lines reuse. Values are YAML
    quoted wherever needed, so any name reads back exactly. The new text must
    parse to the original with only the requested keys changed; then it is
    written atomically with the file's mode kept and the original bytes saved to
    :func:`backup_path`. Anything else (an unreadable or non-UTF-8 file, invalid
    YAML, a flow-style or scalar ``instance:``) raises ``ConfigError``, with the
    exact lines to add by hand where that helps, and leaves the file untouched.

    Returns ``{field: (old, new)}`` for the fields that changed; ``{}`` (and no
    write) when the file already holds the requested values.
    """
    target = path or instance_path()
    wanted = {
        key: value
        for key, value in (("name", name), ("slug", slug), ("role", role))
        if value is not None
    }
    if not wanted:
        return {}
    try:
        InstanceMetadata.model_validate({"name": "-", "slug": "-", **wanted})
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, target)) from exc

    raw, bom, text = _read_for_edit(target)
    data = _parse_mapping(target, text)
    shape_error = instance_shape_error(target, data, wanted)
    if shape_error is not None:
        raise shape_error
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    first_break = _FIRST_NEWLINE.search(text)
    newline = first_break.group() if first_break else "\n"
    current = data.get("instance") if data else None

    expected: dict[str, Any]
    if isinstance(current, dict) and current:
        changes = _changed_fields(target, current, wanted)
        if not changes:
            return {}
        fields = {key: new for key, (_, new) in changes.items()}
        expected = {**current, **fields}
        whole_block = False
        edits = _plan_update(target, text, root, fields, newline)
    else:  # no instance: key yet, or an empty / null / {} one
        if "name" not in wanted or "slug" not in wanted:
            msg = f"{target} has no instance: block yet; adding one needs a name and a slug"
            raise ValueError(msg)
        changes = {key: (None, value) for key, value in wanted.items()}
        fields = expected = dict(wanted)
        whole_block = True
        plan = _plan_fill if data is not None and "instance" in data else _plan_new_block
        edits = plan(target, text, root, fields, newline)

    new_text = _apply_edits(text, edits)
    if not _reads_back(new_text, data, expected):
        reason = "the edited text did not read back as intended"
        raise _cannot_edit(target, reason, fields, whole_block=whole_block)
    payload = (_UTF8_BOM if bom else b"") + new_text.encode("utf-8")
    _replace_file(target, payload, backup=raw)
    return changes


def instance_shape_error(
    path: Path, data: dict[str, Any] | None, fields: dict[str, str]
) -> ConfigError | None:
    """The editor's refusal when ``instance:`` holds a single value or a list, else None.

    :func:`set_instance_fields` raises it; ``sanctum init`` asks first, so it can
    refuse before prompting for a name. ``fields`` fill the lines to add by hand.
    """
    current = data.get("instance") if data else None
    if current is None or isinstance(current, dict):
        return None
    kind = "a list" if isinstance(current, list) else "a single value"
    reason = f"its instance: entry is {kind}, not a block of keys"
    return _cannot_edit(path, reason, fields, whole_block=True)


def _changed_fields(
    path: Path, current: dict[str, Any], wanted: dict[str, str]
) -> dict[str, tuple[Any, str]]:
    """``{field: (old, new)}`` for the requested values the block does not hold yet."""
    changes: dict[str, tuple[Any, str]] = {}
    for key, value in wanted.items():
        old = current.get(key)
        if key in current and _same(old, value):
            continue
        if key == "slug" and old is not None:
            msg = (
                f"refusing to change instance.slug in {path} from {old!r} to {value!r}: "
                "the slug names this haus's backup buckets and URLs"
            )
            raise ConfigError(msg, fix="keep the slug; only instance.name (display) can change")
        changes[key] = (old, value)
    return changes


def _plan_new_block(
    path: Path, text: str, root: yaml.Node | None, fields: dict[str, str], newline: str
) -> list[_Edit]:
    """Insert a whole ``instance:`` block at the top of the document."""
    block_root = root if isinstance(root, yaml.MappingNode) and not root.flow_style else None
    if root is not None and block_root is None and not _is_empty_node(root):
        reason = "its top level is not an indented block of keys"
        raise _cannot_edit(path, reason, fields, whole_block=True)
    pos = _document_insert_pos(text)
    if pos is None:
        raise _cannot_edit(path, "its content starts on the '---' line", fields, whole_block=True)
    indent = block_root.start_mark.column if block_root is not None else 0
    lines = ["instance:", *_field_lines(fields, " " * _indent_step(root))]
    lines = [" " * indent + line for line in lines]
    return [(pos, pos, _insertion(text, pos, lines, newline))]


def _plan_fill(
    path: Path, text: str, root: yaml.Node | None, fields: dict[str, str], newline: str
) -> list[_Edit]:
    """Put the keys under an ``instance:`` whose value is empty, ``~``/``null`` or ``{}``."""
    key, value = _single_pair(path, root, fields, whole_block=True)
    colon = _colon_end(text, key)
    start, end = value.start_mark.index, value.end_mark.index
    line = key.start_mark.line
    token = text[start:end]
    if (
        colon is None
        or start < colon
        or value.start_mark.line != line
        or value.end_mark.line != line
        or text[colon:start].strip(" \t")
        or (token and not _EMPTY_VALUE.fullmatch(token))
        or not _BLANK_OR_COMMENT.fullmatch(_rest_of_line(text, end))
    ):
        reason = "its instance: line has a form this editor does not rewrite"
        raise _cannot_edit(path, reason, fields, whole_block=True)
    eol = _line_end(text, end)
    indent = " " * (key.start_mark.column + _indent_step(root))
    edits: list[_Edit] = [(eol, eol, _insertion(text, eol, _field_lines(fields, indent), newline))]
    if token:  # drop the explicit ``~`` / ``null`` / ``{}`` so the keys can nest under it
        edits.append((colon, end, ""))
    return edits


def _plan_update(
    path: Path, text: str, root: yaml.Node | None, fields: dict[str, str], newline: str
) -> list[_Edit]:
    """Replace or insert keys in an ``instance:`` block mapping."""
    key, block = _single_pair(path, root, fields, whole_block=False)
    colon = _colon_end(text, key)
    if (
        colon is None
        or not _BLANK_OR_COMMENT.fullmatch(_rest_of_line(text, colon))
        or not isinstance(block, yaml.MappingNode)
        or block.flow_style
        or not block.value
        or block.start_mark.index < colon
    ):
        reason = "its instance: entry is not an indented block of keys (flow style, tag or alias)"
        raise _cannot_edit(path, reason, fields, whole_block=False)
    edits: list[_Edit] = []
    missing: dict[str, str] = {}
    for field, value in fields.items():
        pairs = _pairs_for(block, field)
        if not pairs:
            missing[field] = value
        elif len(pairs) > 1:
            reason = f"its instance: block has more than one {field}: key"
            raise _cannot_edit(path, reason, fields, whole_block=False)
        else:
            edits.append(_value_edit(path, text, pairs[0], value, fields))
    if missing:
        eol = _line_end(text, colon)
        indent = " " * block.value[0][0].start_mark.column
        edits.append((eol, eol, _insertion(text, eol, _field_lines(missing, indent), newline)))
    return edits


def _value_edit(
    path: Path,
    text: str,
    pair: tuple[yaml.Node, yaml.Node],
    value: str,
    fields: dict[str, str],
) -> _Edit:
    """Replace one single-line scalar value in place; a trailing comment stays."""
    key, node = pair
    colon = _colon_end(text, key)
    start, end = node.start_mark.index, node.end_mark.index
    line = key.start_mark.line
    if (
        colon is None
        or not isinstance(node, yaml.ScalarNode)
        or node.style not in (None, "'", '"')
        or start < colon
        or node.start_mark.line != line
        or node.end_mark.line != line
        or text[colon:start].strip(" \t")
        or text[start:end].startswith(("&", "!"))
    ):
        reason = f"its {key.value}: value spans lines or carries an anchor, tag or alias"
        raise _cannot_edit(path, reason, fields, whole_block=False)
    if start == end:  # ``role:`` with no value yet
        return (colon, colon, " " + _yaml_scalar(value))
    return (start, end, _yaml_scalar(value))


def _single_pair(
    path: Path, root: yaml.Node | None, fields: dict[str, str], *, whole_block: bool
) -> tuple[yaml.Node, yaml.Node]:
    """The one literal ``instance`` key of the document and its value node."""
    pairs = _pairs_for(root, "instance") if isinstance(root, yaml.MappingNode) else []
    if len(pairs) != 1:
        reason = (
            "it has more than one instance: key"
            if pairs
            else "its instance: entry is not written out as a plain key"
        )
        raise _cannot_edit(path, reason, fields, whole_block=whole_block)
    return pairs[0]


def _pairs_for(mapping: yaml.MappingNode, key: str) -> list[tuple[yaml.Node, yaml.Node]]:
    return [
        (k, v)
        for k, v in mapping.value
        if isinstance(k, yaml.ScalarNode) and k.tag == _STR_TAG and k.value == key
    ]


def _is_empty_node(node: yaml.Node) -> bool:
    """An empty document (``---`` and nothing else) composes to a zero-width null."""
    return (
        isinstance(node, yaml.ScalarNode)
        and node.tag == _NULL_TAG
        and node.start_mark.index == node.end_mark.index
    )


def _colon_end(text: str, key: yaml.Node) -> int | None:
    """Index just past the ``:`` that follows ``key`` on the same line, else None."""
    if key.start_mark.line != key.end_mark.line:
        return None
    found = _COLON.match(text, key.end_mark.index)
    return found.end() if found else None


def _indent_step(root: yaml.Node | None) -> int:
    """The file's indentation step, read off its first nested block mapping (else 2)."""
    if isinstance(root, yaml.MappingNode):
        for key, value in root.value:
            if (
                isinstance(value, yaml.MappingNode)
                and not value.flow_style
                and value.value
                and value.start_mark.index > key.end_mark.index
            ):
                step = int(value.value[0][0].start_mark.column - key.start_mark.column)
                if step > 0:
                    return step
    return 2


def _document_insert_pos(text: str) -> int | None:
    """Where a new top-level block goes.

    Under a leading ``---`` (which follows any ``%`` directives) when there is
    one, else the very top. None when content shares the ``---`` line.
    """
    marker: yaml.Token | None = None
    for token in yaml.scan(text, Loader=yaml.SafeLoader):
        if isinstance(token, yaml.StreamStartToken | yaml.DirectiveToken):
            continue
        if marker is None and isinstance(token, yaml.DocumentStartToken):
            marker = token
            continue
        if marker is None:
            return 0
        if token.start_mark.line == marker.end_mark.line and not isinstance(
            token, yaml.StreamEndToken
        ):
            return None
        return _line_end(text, marker.end_mark.index)
    return 0  # pragma: no cover - every token stream ends with StreamEndToken


def _line_end(text: str, pos: int) -> int:
    """Index just past the line break that ends the line holding ``pos``."""
    found = _LINE_BREAK.search(text, pos)
    return found.end() if found else len(text)


def _rest_of_line(text: str, pos: int) -> str:
    found = _LINE_BREAK.search(text, pos)
    return text[pos : found.start() if found else len(text)]


def _insertion(text: str, pos: int, lines: list[str], newline: str) -> str:
    """``lines`` as text to insert at ``pos``: a line start, or the end of the text."""
    if pos == len(text) and text and text[-1] not in _BREAK_CHARS:
        return newline + newline.join(lines)  # the last line had no break; keep it so
    return "".join(line + newline for line in lines)


def _apply_edits(text: str, edits: list[_Edit]) -> str:
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _field_lines(fields: dict[str, str], indent: str) -> list[str]:
    return [f"{indent}{key}: {_yaml_scalar(value)}" for key, value in fields.items()]


def _yaml_scalar(value: str) -> str:
    """``value`` as a one-line YAML scalar that loads back as exactly ``value``.

    PyYAML quotes where the plain form would change meaning (``yes``, ``123``,
    ``a: b``, ``#``, a leading ``*``/``&``, edge spaces). A value it would fold
    over several lines (a line break, NEL, LS/PS) is forced to the escaped
    double-quoted form.
    """
    for style, prefix in ((None, "v: "), ('"', '"v": ')):
        dumped = yaml.safe_dump(
            {"v": value}, default_style=style, allow_unicode=True, width=float("inf")
        )
        body = dumped.removeprefix(prefix).removesuffix("\n")
        if dumped.startswith(prefix) and not _LINE_BREAK.search(body):
            loaded = yaml.safe_load(prefix + body)
            if isinstance(loaded, dict) and loaded.get("v") == value:
                return body
    msg = f"cannot write {value!r} as a single-line YAML value"  # pragma: no cover
    raise ConfigError(msg)  # pragma: no cover


def _cannot_edit(
    path: Path, reason: str, fields: dict[str, str], *, whole_block: bool
) -> ConfigError:
    """The refusal for a file the editor will not touch, with the lines to add by hand."""
    if whole_block:
        how = f"edit {path} by hand so its top-level instance: entry reads:"
        lines = ["instance:", *_field_lines(fields, "  ")]
    else:
        how = f"set these keys in the instance: entry of {path} by hand:"
        lines = _field_lines(fields, "")
    fix = "\n".join([how, *("    " + line for line in lines)])
    return ConfigError(f"cannot safely edit {path}: {reason}; the file was not changed", fix=fix)


def _reads_back(new_text: str, old: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    """True when ``new_text`` parses to ``old`` with only ``instance`` now ``expected``."""
    try:
        new = yaml.safe_load(new_text)
        if not isinstance(new, dict) or "instance" not in new:
            return False
        rest_old = {k: v for k, v in (old or {}).items() if k != "instance"}
        rest_new = {k: v for k, v in new.items() if k != "instance"}
        return _same(new["instance"], expected) and _same(rest_new, rest_old)
    except _YAML_LOAD_ERRORS:
        return False


def _same(a: object, b: object, memo: dict[tuple[int, int], bool] | None = None) -> bool:
    """Structural equality that tells ``True`` from ``1`` and lets NaN equal itself.

    Each pair of containers is compared once (``memo``, keyed by identity). An
    alias is one shared object, so a file that doubles an alias level after level
    (``b: [*a, *a]``, ``c: [*b, *b]``, ...) is checked in linear time, not
    exponential.
    """
    if memo is None:
        memo = {}
    if isinstance(a, dict) and isinstance(b, dict):
        key = (id(a), id(b))
        if key not in memo:
            memo[key] = a.keys() == b.keys() and all(_same(a[k], b[k], memo) for k in a)
        return memo[key]
    if isinstance(a, list) and isinstance(b, list):
        key = (id(a), id(b))
        if key not in memo:
            memo[key] = len(a) == len(b) and all(
                _same(x, y, memo) for x, y in zip(a, b, strict=True)
            )
        return memo[key]
    if type(a) is not type(b):
        return False
    return bool(a == b) or (a != a and b != b)  # NaN is the one value unequal to itself


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        msg = f"cannot read {path}: {exc.strerror or exc}"
        raise ConfigError(msg, fix=f"make it a regular file you can read (ls -l {path})") from exc


def _read_for_edit(path: Path) -> tuple[bytes, bool, str]:
    """``(raw bytes, had a UTF-8 BOM, text without the BOM)``."""
    raw = _read_bytes(path)
    bom = raw.startswith(_UTF8_BOM)
    try:
        text = raw[len(_UTF8_BOM) if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError as exc:
        offset = exc.start + (len(_UTF8_BOM) if bom else 0)
        msg = f"{path} is not UTF-8 text (undecodable byte at offset {offset})"
        raise ConfigError(msg, fix="re-save it as UTF-8, then re-run") from exc
    return raw, bom, text


def _parse_yaml(path: Path, text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except _YAML_LOAD_ERRORS as exc:
        raise _yaml_load_error(path, text, exc) from exc


def _parse_mapping(path: Path, text: str) -> dict[str, Any] | None:
    data = _parse_yaml(path, text)
    if data is None or isinstance(data, dict):
        return data
    msg = f"top-level YAML must be a mapping in {path}, got {type(data).__name__}"
    raise ConfigError(msg, fix="its top level must be keys such as instance: and router:")


# What a scalar had to be for PyYAML to build it, by its resolved tag.
_TAG_KINDS = {
    "tag:yaml.org,2002:timestamp": "date",
    "tag:yaml.org,2002:int": "whole number",
    "tag:yaml.org,2002:float": "number",
    "tag:yaml.org,2002:bool": "true/false value",
    "tag:yaml.org,2002:binary": "base64 value",
}


def _yaml_load_error(path: Path, text: str, exc: Exception) -> ConfigError:
    """``exc`` raised by ``yaml.safe_load(text)`` as a ConfigError with a fix hint.

    A value PyYAML parses but cannot build (an impossible date, ``!!int ""``)
    raises a plain Python error that names neither the file nor the line, so the
    culprit is looked up and quoted as written.
    """
    if isinstance(exc, yaml.YAMLError):
        msg = f"YAML parse error in {path}: {exc}"
        return ConfigError(msg, fix="fix the YAML syntax error noted above, then re-run")
    if isinstance(exc, RecursionError):
        msg = f"cannot read {path}: its YAML is nested too deeply"
        return ConfigError(msg, fix=f"flatten it: {path.name} needs only a few levels of keys")
    found = _unbuildable_scalar(text)
    if found is None:
        msg = f"cannot read {path}: PyYAML could not build one of its values ({type(exc).__name__})"
        return ConfigError(msg, fix="check its dates, numbers and !!tags, then re-run")
    node, error = found
    line = node.start_mark.line + 1
    written = " ".join(text[node.start_mark.index : node.end_mark.index].split())
    if len(written) > 60:
        written = written[:57] + "..."
    kind = _TAG_KINDS.get(node.tag, f"{node.tag} value")
    # The reason is the reported value's own error: safe_load can stop at a later
    # bad value first, since it builds nested blocks after their parents' scalars.
    why = f" ({error})" if isinstance(error, ValueError | OverflowError) and str(error) else ""
    msg = f"cannot read {path}: {written} on line {line} is not a valid {kind}{why}"
    if written.startswith("!"):
        fix = f"correct the value on line {line} or drop its tag, then re-run"
    else:
        fix = f"correct the value on line {line}, or quote it to keep it as text, then re-run"
    return ConfigError(msg, fix=fix)


def _unbuildable_scalar(text: str) -> tuple[yaml.ScalarNode, Exception] | None:
    """The first scalar PyYAML parses but cannot build, in document order, and its error.

    Only a plain Python error (an impossible date, ``!!int ""``) counts. A merge
    key (``<<``) is skipped, since PyYAML builds it only as part of its mapping,
    and so is any scalar that fails on its own with a ``yaml.YAMLError``: neither
    is what made ``safe_load`` raise a plain Python error.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except _YAML_LOAD_ERRORS:
        return None
    builder = SafeConstructor()
    stack: list[yaml.Node] = [root] if root is not None else []
    seen: set[int] = set()  # an alias can point back at its own ancestor
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            if node.tag == _MERGE_TAG:
                continue
            try:
                builder.construct_object(node)  # type: ignore[no-untyped-call]
            except yaml.YAMLError:
                continue
            except _YAML_LOAD_ERRORS as error:
                return node, error
        elif isinstance(node, yaml.MappingNode):
            stack.extend(item for pair in reversed(node.value) for item in reversed(pair))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(reversed(node.value))
    return None


def _replace_file(
    target: Path, payload: bytes, *, backup: bytes, backup_to: Path | None = None
) -> None:
    """Atomically replace ``target`` with ``payload``, first saving ``backup`` beside it.

    The backup goes to ``backup_to`` (default :func:`backup_path`), with the
    file's own mode. The file's mode (and owner, where allowed) is kept. A
    symlinked ``target`` is followed, so the link itself survives. Nothing is
    changed on failure.
    """
    real = target.resolve()
    try:
        info = real.stat()
    except OSError as exc:
        msg = f"cannot read {target}: {exc.strerror or exc}"
        raise ConfigError(msg) from exc
    if not os.access(real, os.W_OK):
        msg = f"{target} is read-only; the file was not changed"
        raise ConfigError(msg, fix=f"make it writable (chmod u+w {target}), then re-run")
    mode = info.st_mode & 0o7777
    owner = (info.st_uid, info.st_gid)
    try:
        _atomic_write(backup_to or backup_path(target), backup, mode, owner)
        _atomic_write(real, payload, mode, owner)
    except OSError as exc:
        msg = f"could not write {target}: {exc.strerror or exc}; the file was not changed"
        raise ConfigError(msg, fix=f"check that {real.parent} is writable and has space") from exc


def _atomic_write(dest: Path, payload: bytes, mode: int, owner: tuple[int, int]) -> None:
    """Write ``payload`` to a temp file beside ``dest``, then rename it over ``dest``."""
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            with contextlib.suppress(OSError):  # only root may give a file to someone else
                os.fchown(handle.fileno(), *owner)
            os.fsync(handle.fileno())
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ─── The last step of each cloud setup wizard ────────────────────────


def set_cloud_backup_slot(
    path: Path, slot: str, *, repo: str, keychain_service: str, keychain_account: str
) -> Path:
    """Point ``cli.cloud_backup.<slot>`` of ``path`` at a restic repo, keeping a backup.

    Shared by the R2, B2 and Google Drive wizards. The file is read as
    :func:`load_raw` reads it, so one that cannot be read or built raises
    ``ConfigError`` naming the file (and the line). ``cli`` and
    ``cli.cloud_backup`` must each be a block of keys (``{}`` included) or absent,
    and a key with no value (or ``~``) counts as absent; anything else, ``[]``,
    ``''``, ``false`` and ``0`` included, raises ``ConfigError`` with the entry to
    add by hand. The slot is replaced, the default retention added when there is
    none, and every other key kept; comments are not, since the file is written
    back from its parsed YAML. The original bytes are saved to
    ``<file>.bak.<UTC stamp>``, then the file is replaced atomically, keeping its
    mode and owner and following a symlink (:func:`_replace_file`). Nothing is
    written when the file is refused. Returns the backup's path.
    """
    raw, _bom, text = _read_for_edit(path)
    data = _parse_mapping(path, text) or {}
    entry = {
        "kind": "restic",
        "repo": repo,
        "keychain": {"service": keychain_service, "account": keychain_account},
    }
    wanted = {"cli": {"cloud_backup": {slot: entry}}}
    cli = _block_of_keys(data, "cli", path, wanted)
    cloud_backup = _block_of_keys(cli, "cli.cloud_backup", path, wanted)
    cloud_backup[slot] = entry
    cloud_backup.setdefault("retention", CloudBackupRetention().model_dump())
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    payload = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    _replace_file(path, payload.encode("utf-8"), backup=raw, backup_to=backup)
    return backup


def _block_of_keys(
    parent: dict[str, Any], dotted: str, path: Path, wanted: dict[str, Any]
) -> dict[str, Any]:
    """The block of keys at the last part of ``dotted`` in ``parent``, created when absent."""
    key = dotted.rsplit(".", 1)[-1]
    block = parent.get(key)
    if block is None:
        block = parent[key] = {}
    if isinstance(block, dict):
        return block
    msg = f"{dotted} in {path} is not a block of keys; the file was not changed"
    how = f"edit {path} by hand so that {dotted} is a block of keys, like this:"
    lines = yaml.safe_dump(wanted, sort_keys=False, default_flow_style=False).splitlines()
    raise ConfigError(msg, fix="\n".join([how, *("    " + line for line in lines)]))


def ensure(path: Path | None = None) -> Config:
    """Load the instance config, scaffolding a minimal one if it is absent.

    :func:`load` hard-raises when the file is missing — correct for most
    commands, but fatal on a fresh Mac's first ``onboard``. ``ensure`` creates a
    minimal stub first so first-run works, then loads normally.
    """
    target = path or instance_path()
    if not target.exists():
        scaffold_instance(target)
    return load(target)
