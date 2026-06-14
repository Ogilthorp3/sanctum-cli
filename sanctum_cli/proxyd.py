"""Centralized proxyd transport policy — base URL + TLS verification.

Every proxyd touchpoint (``council``'s httpx client, ``self-test``'s urllib
probe) resolves its endpoint and its trust anchor *here* so the plaintext →
TLS cutover is a one-file change, not an N-callsite hunt. This is the "no
hardcoded endpoints" doctrine applied to the one service the CLI talks to over
the wire: discovery → env override → safe default, never a literal at the call
site.

Transport is **crypto-agnostic**. The client verifies the server's leaf chains
to ``~/.sanctum/certs/ca.crt`` and nothing more — it does not pin a key type,
curve, or KEM. The day proxyd's listener negotiates a hybrid ML-KEM group
(``X25519MLKEM768``) instead of classical X25519, this code is unchanged: the
PQC lives in the TLS handshake the server offers, below the verify boundary the
CLI cares about.

Resolution order (both ``base_url`` and ``verify``):

* ``base_url``  ← ``$SANCTUM_PROXYD_URL`` else ``https://127.0.0.1:4040``.
* ``verify``    ← ``$SANCTUM_PROXYD_INSECURE`` truthy → ``False`` (dev-only,
                  loud); else ``$SANCTUM_PROXYD_CA`` else ``~/.sanctum/certs/ca.crt``.

The default is **secure**: ``verify`` is a CA path, never ``False``. The
insecure escape hatch exists only for a developer poking a self-signed local
listener, and it must be set explicitly — there is no way to fall into it.

Cutover sequencing — during the transition proxyd still serves plaintext on
:4040 and TLS on :4041. To exercise the exact TLS+CA path *before* the :4040
flip::

    SANCTUM_PROXYD_URL=https://127.0.0.1:4041 sanctum self-test --only proxyd
    SANCTUM_PROXYD_URL=https://127.0.0.1:4041 sanctum council "ping"

When :4040 flips to TLS, the default ``https://127.0.0.1:4040`` is correct and
the override is dropped. If an ``https://`` URL ever lands on a plaintext
listener mid-cutover, the handshake fails fast with a TLS error
(``WRONG_VERSION_NUMBER`` / ``ConnectError``) rather than hanging — see
:func:`describe_transport_error` for the debuggable hint.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

#: Env override for the full proxyd base URL (scheme + host + port).
URL_ENV = "SANCTUM_PROXYD_URL"
#: Env override for the CA bundle used to verify the proxyd TLS leaf.
CA_ENV = "SANCTUM_PROXYD_CA"
#: Truthy env value disables verification entirely. Dev-only, explicit-only.
INSECURE_ENV = "SANCTUM_PROXYD_INSECURE"

#: TLS by default. When :4040 flips to TLS this is the standing endpoint; for
#: pre-flip TLS testing point ``SANCTUM_PROXYD_URL`` at the :4041 listener.
DEFAULT_URL = "https://127.0.0.1:4040"
#: The sanctum CA that signs the proxyd server leaf. Crypto-agnostic anchor.
DEFAULT_CA = Path("~/.sanctum/certs/ca.crt").expanduser()

_TRUTHY = {"1", "true", "yes", "on"}


def base_url() -> str:
    """The proxyd base URL — env override, else the TLS default. No trailing /."""
    return os.environ.get(URL_ENV, DEFAULT_URL).rstrip("/")


def _ca_path() -> str | None:
    """The CA bundle path: ``$SANCTUM_PROXYD_CA`` else the sanctum CA.

    ``None`` only when the insecure escape hatch is set — the caller then
    builds an unverified context.
    """
    if os.environ.get(INSECURE_ENV, "").strip().lower() in _TRUTHY:
        return None
    override = os.environ.get(CA_ENV, "").strip()
    return override if override else str(DEFAULT_CA)


def ssl_context() -> ssl.SSLContext:
    """The TLS client context — the single trust object both consumers share.

    Built from the sanctum CA (``cafile=``) so the proxyd leaf must chain to it.
    When the insecure escape hatch is set, returns a context with verification
    *off* (dev-only). This is **crypto-agnostic**: it constrains the trust
    anchor, not the cipher suite or key-exchange group, so a server that later
    negotiates a hybrid ML-KEM group verifies through it unchanged.

    Used by both httpx (``verify=<ctx>`` — accepted on 0.27 *and* 0.28+, where
    ``verify=<str>`` is deprecated) and the stdlib ``urllib`` probe
    (``urlopen(context=<ctx>)``), so the two paths trust identically.
    """
    cafile = _ca_path()
    if cafile is None:  # explicit insecure dev hatch
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=cafile)


def verify() -> ssl.SSLContext | bool:
    """The value to hand httpx ``verify=`` — an :class:`ssl.SSLContext`, or
    ``False`` only under the explicit insecure dev hatch.

    Returns a context rather than a path string so it works on httpx 0.27 and
    0.28+ alike (0.28 deprecates ``verify=<str>``), and so httpx and the
    stdlib probe trust through the *same* object.
    """
    if os.environ.get(INSECURE_ENV, "").strip().lower() in _TRUTHY:
        return False
    return ssl_context()


def is_https() -> bool:
    """True when the resolved base URL speaks TLS (drives the error hint)."""
    return base_url().lower().startswith("https://")


def describe_transport_error(exc: BaseException) -> str:
    """A debuggable one-liner for a proxyd transport failure.

    The classic cutover footgun is an ``https://`` URL hitting a still-plaintext
    listener: the TLS handshake aborts with ``WRONG_VERSION_NUMBER``. Surface
    that explicitly so the operator reaches for the port, not the cert.
    """
    text = str(exc)
    if is_https() and ("WRONG_VERSION_NUMBER" in text or "record layer" in text):
        return (
            f"{text} — this looks like an https:// request hitting a PLAINTEXT "
            f"listener. During cutover proxyd serves TLS on :4041 and plaintext "
            f"on :4040; point {URL_ENV} at the TLS port, or wait for the :4040 flip."
        )
    return text
