"""``sanctum ui`` — lightweight, zero-cloud local web dashboard for any Sanctum Mac.

Binds strictly to ``127.0.0.1`` (default port ``1119``), ships zero external
CDN/font/tracker dependencies, protects mutating endpoints with a per-session
CSRF token, and surfaces:
  - Live health of all 12 ``sanctum-rs`` Rust daemons
  - Live Castellan unified-memory headroom & resident models (``:2189/status``)
  - Live ``$0`` subscription seat proxies (Claude Max ``:3456``, Grok OAuth ``:4200``,
    Gemini AGY ``:6543``)
  - Disk & backup status snapshot
  - Interactive local Dragon / Jedi Council chat (proxied over local TLS CA to
    ``proxyd :4040``, falling back to Python Dragon ``:8787``)
  - One-click redacted bug-report copy
"""

from __future__ import annotations

import json
import secrets
import shutil
import ssl
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel

from sanctum_cli import __version__
from sanctum_cli.commands import bug_report as bug_report_cmd
from sanctum_cli.commands import dragon as dragon_cmd
from sanctum_cli.commands import haus_cmd
from sanctum_cli.commands import status as status_cmd
from sanctum_cli.mesh.metrics import detect_chip, detect_os, detect_ram_gb

console = Console()

DEFAULT_UI_PORT = 1119


def _fetch_castellan_status() -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=1.5) as client:
            r = client.get("http://127.0.0.1:2189/status")
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def collect_ui_snapshot() -> dict[str, Any]:
    """Collect a bounded, loopback-only JSON snapshot for the local Sanctum UI."""
    services = haus_cmd.probe_core_services()
    seats = haus_cmd.probe_subscription_seats()
    castellan = _fetch_castellan_status()
    disk = status_cmd.probe_disk()
    rust_state = haus_cmd.load_rust_state()

    up_count = sum(1 for v in services.values() if v)
    return {
        "cli_version": __version__,
        "rust_release": rust_state.get("release_tag")
        or ("operator-live" if haus_cmd.is_operator_haus() else "not-installed"),
        "chip": detect_chip(),
        "ram_gb": detect_ram_gb(),
        "os": detect_os(),
        "services": [{"name": k, "up": v} for k, v in services.items()],
        "services_up": up_count,
        "services_total": len(services),
        "seats": seats,
        "castellan": castellan,
        "disk": {
            "used_pct": disk.used_pct,
            "used_gb": disk.used_gb,
            "free_gb": disk.free_gb,
            "total_gb": disk.total_gb,
            "status": disk.status,
        },
        "dragon_python_up": dragon_cmd._dragon_health() is not None,
    }


def dispatch_local_chat(*, model: str, prompt: str) -> tuple[bool, str, str]:
    """Forward a prompt to local Rust proxyd (:4040 TLS) or Python Dragon (:8787)."""
    clean_prompt = prompt.strip()
    if not clean_prompt:
        return False, "Prompt cannot be empty.", "none"

    certs_dir = haus_cmd.sanctum_root() / "certs"
    ca_cert = certs_dir / "ca.crt"
    if haus_cmd.proxyd_health() and ca_cert.is_file():
        url = "https://127.0.0.1:4040/v1/chat/completions"
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca_cert))
        chosen_model = model.strip() or "council-mlx"
        via = f"proxyd :4040 ({chosen_model})"
    elif dragon_cmd._dragon_health() is not None:
        url = f"http://127.0.0.1:{dragon_cmd.DEFAULT_DRAGON_PORT}/v1/chat/completions"
        verify = True
        chosen_model = "sanctum-dragon"
        via = "dragon :8787"
    else:
        return (
            False,
            "No local engine running. Start with `sanctum haus start` (or `sanctum dragon start`).",
            "offline",
        )

    try:
        with httpx.Client(timeout=180.0, verify=verify) as client:
            r = client.post(
                url,
                headers={"Authorization": "Bearer sanctum-local"},
                json={
                    "model": chosen_model,
                    "max_tokens": 1024,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are the local Sanctum assistant. Be clear, truthful, and concise.",
                        },
                        {"role": "user", "content": clean_prompt},
                    ],
                },
            )
            r.raise_for_status()
            body = r.json()
            reply = str(body["choices"][0]["message"]["content"]).strip()
            return True, reply, via
    except Exception as exc:
        return False, f"Engine error ({via}): {exc}", via


def render_dashboard_html(*, csrf_token: str) -> str:
    """Return the self-contained obsidian+amber local dashboard HTML."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Sanctum Local Control</title>
<style>
  :root {{
    --bg: #0b0d13; --card: #131722; --border: rgba(212,149,46,0.26);
    --amber: #f3c26b; --green: #34d399; --red: #f87171; --text: #f3f4f6; --muted: #9ca3af;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.45;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem; }}
  header {{
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 1rem; padding-bottom: 1rem; border-bottom: 1px solid rgba(255,255,255,0.08);
  }}
  .brand {{ display: flex; align-items: center; gap: 0.7rem; font-weight: 700; font-size: 1.15rem; }}
  .diamond {{ color: var(--amber); font-size: 1.4rem; }}
  .pill {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.75rem;
    padding: 0.25rem 0.65rem; border-radius: 999px; border: 1px solid var(--border);
    background: rgba(212,149,46,0.12); color: var(--amber);
  }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 1rem; margin-top: 1.25rem; }}
  .card {{
    background: var(--card); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px;
    padding: 1.1rem 1.2rem;
  }}
  .card h2 {{
    margin: 0 0 0.75rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--amber); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  .svc-list {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.45rem; }}
  .svc {{
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(255,255,255,0.03); padding: 0.4rem 0.6rem; border-radius: 8px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.74rem;
  }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 0.4rem; }}
  .up {{ background: var(--green); box-shadow: 0 0 6px var(--green); }}
  .down {{ background: #4b5563; }}
  .bar {{ height: 10px; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden; margin: 0.4rem 0 0.75rem; }}
  .bar > span {{ display: block; height: 100%; background: linear-gradient(90deg, var(--green), var(--amber)); }}
  .muted {{ color: var(--muted); font-size: 0.82rem; }}
  .chat-box {{ grid-column: 1 / -1; }}
  .chat-row {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
  select, input, button {{
    background: #0d1018; color: var(--text); border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px; padding: 0.55rem 0.75rem; font-size: 0.88rem;
  }}
  input {{ flex: 1; min-width: 220px; }}
  button {{
    background: rgba(212,149,46,0.2); border-color: var(--border); color: var(--amber);
    cursor: pointer; font-weight: 600;
  }}
  button:hover {{ background: rgba(212,149,46,0.32); }}
  pre {{
    white-space: pre-wrap; background: #090b10; padding: 0.85rem; border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.07); font-size: 0.84rem; max-height: 280px; overflow: auto;
  }}
  .actions {{ display: flex; gap: 0.6rem; flex-wrap: wrap; margin-top: 0.8rem; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><span class="diamond">◆</span> <span>SANCTUM LOCAL CONTROL</span></div>
    <div id="hw-pill" class="pill">Loading…</div>
  </header>

  <div class="grid">
    <section class="card">
      <h2>Rust Engine Suite (<span id="svc-count">0/0</span>)</h2>
      <div id="svc-list" class="svc-list"></div>
    </section>

    <section class="card">
      <h2>Unified Memory &amp; Disk</h2>
      <div id="ram-text" class="muted">Probing Castellan (:2189)…</div>
      <div class="bar"><span id="ram-bar" style="width:0%"></span></div>
      <div id="disk-text" class="muted">Probing Disk…</div>
      <div class="bar"><span id="disk-bar" style="width:0%"></span></div>
      <div id="seats-text" class="muted"></div>
      <div class="actions">
        <button id="btn-bug" type="button">Copy Redacted Bug Report</button>
      </div>
    </section>

    <section class="card chat-box">
      <h2>Local Dragon / Council Chat (100% Loopback)</h2>
      <div class="chat-row">
        <select id="seat-select">
          <option value="council-mlx">Dragon Local (council-mlx)</option>
          <option value="council-code">Coder (council-code)</option>
          <option value="council-finance">Mundi / Grok Sub (council-finance)</option>
          <option value="council-max-thinking">Yoda / Claude Max (council-max-thinking)</option>
          <option value="gemini-31-pro">Windu / Gemini Sub (gemini-31-pro)</option>
        </select>
        <input id="chat-in" placeholder="Ask your local Sanctum anything…" />
        <button id="btn-send" type="button">Send</button>
      </div>
      <pre id="chat-out">Ready. Answers route through local proxyd (:4040 TLS) → sanctum-mlx (:1337 mTLS).</pre>
    </section>
  </div>
</div>
<script>
const CSRF = {json.dumps(csrf_token)};
async function refresh() {{
  try {{
    const r = await fetch('/api/snapshot');
    const d = await r.json();
    document.getElementById('hw-pill').textContent =
      `v${{d.cli_version}} · ${{d.rust_release}} · ${{d.chip}} · ${{d.ram_gb}} GB`;
    document.getElementById('svc-count').textContent = `${{d.services_up}}/${{d.services_total}} UP`;
    const list = document.getElementById('svc-list');
    list.innerHTML = d.services.map(s =>
      `<div class="svc"><span><span class="dot ${{s.up ? 'up' : 'down'}}"></span>${{s.name.split(' ')[0]}}</span><span>${{s.up ? 'UP' : 'off'}}</span></div>`
    ).join('');
    if (d.castellan && d.castellan.pool_mb) {{
      const pct = Math.min(100, Math.round((d.castellan.used_mb / d.castellan.pool_mb) * 100));
      document.getElementById('ram-text').textContent =
        `Castellan: ${{(d.castellan.used_mb/1024).toFixed(1)}} GB used / ${{(d.castellan.pool_mb/1024).toFixed(0)}} GB pool (${{(d.castellan.headroom_mb/1024).toFixed(1)}} GB headroom)`;
      document.getElementById('ram-bar').style.width = pct + '%';
    }} else {{
      document.getElementById('ram-text').textContent = `Host RAM: ${{d.ram_gb}} GB unified (Castellan offline)`;
    }}
    if (d.disk && d.disk.used_pct !== null) {{
      document.getElementById('disk-text').textContent =
        `Disk: ${{d.disk.used_gb}} GB / ${{d.disk.total_gb}} GB (${{d.disk.used_pct}}% · ${{d.disk.free_gb}} GB free)`;
      document.getElementById('disk-bar').style.width = d.disk.used_pct + '%';
    }}
    const seats = Object.entries(d.seats || {{}}).map(([k,v]) => `${{k}}:${{v.up ? 'UP' : 'off'}}`).join(' · ');
    document.getElementById('seats-text').textContent = `$0 Subscription Proxies: ${{seats}}`;
  }} catch (e) {{}}
}}
async function sendChat() {{
  const inp = document.getElementById('chat-in');
  const out = document.getElementById('chat-out');
  const msg = inp.value.trim();
  if (!msg) return;
  out.textContent = 'Thinking locally…';
  try {{
    const r = await fetch('/api/chat', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Sanctum-UI-Token': CSRF}},
      body: JSON.stringify({{model: document.getElementById('seat-select').value, prompt: msg}})
    }});
    const d = await r.json();
    out.textContent = d.ok ? `[${{d.via}}]\\n\\n${{d.reply}}` : `Error: ${{d.reply}}`;
  }} catch (e) {{ out.textContent = 'Request failed: ' + e; }}
}}
document.getElementById('btn-send').onclick = sendChat;
document.getElementById('chat-in').addEventListener('keydown', e => {{ if (e.key === 'Enter') sendChat(); }});
document.getElementById('btn-bug').onclick = async () => {{
  const r = await fetch('/api/bug-report', {{ headers: {{'X-Sanctum-UI-Token': CSRF}} }});
  const d = await r.json();
  if (navigator.clipboard && d.markdown) {{
    await navigator.clipboard.writeText(d.markdown);
    document.getElementById('btn-bug').textContent = '✓ Copied Redacted Report!';
    setTimeout(() => {{ document.getElementById('btn-bug').textContent = 'Copy Redacted Bug Report'; }}, 2500);
  }}
}};
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def create_ui_server(*, port: int = DEFAULT_UI_PORT) -> tuple[ThreadingHTTPServer, str]:
    """Create a loopback-only ThreadingHTTPServer for ``sanctum ui``."""
    csrf_token = secrets.token_hex(24)
    html_bytes = render_dashboard_html(csrf_token=csrf_token).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
            )
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = (json.dumps(payload) + "\n").encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", raw)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html_bytes)
                return
            if self.path == "/api/snapshot":
                self._send_json(HTTPStatus.OK, collect_ui_snapshot())
                return
            if self.path == "/api/bug-report":
                if self.headers.get("X-Sanctum-UI-Token") != csrf_token:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "invalid_csrf"})
                    return
                rep = bug_report_cmd.collect_redacted_report(run_self_test=False)
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "markdown": bug_report_cmd.render_markdown_report(rep)},
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/api/chat":
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                return
            if self.headers.get("X-Sanctum-UI-Token") != csrf_token:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "invalid_csrf"})
                return
            try:
                length = min(65536, int(self.headers.get("Content-Length") or "0"))
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
                model = str(data.get("model") or "council-mlx")
                prompt = str(data.get("prompt") or "")
            except Exception:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reply": "Invalid JSON"})
                return
            ok, reply, via = dispatch_local_chat(model=model, prompt=prompt)
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY, {"ok": ok, "reply": reply, "via": via})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server, csrf_token


def ui_command(
    port: Annotated[int, typer.Option("--port", "-p", help="Loopback port for local Sanctum UI.")] = DEFAULT_UI_PORT,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not auto-open the browser.")] = False,
) -> None:
    """Launch the local, zero-cloud Sanctum web dashboard (127.0.0.1 only)."""
    try:
        server, _tok = create_ui_server(port=port)
    except OSError as exc:
        console.print(f"[red]Cannot bind 127.0.0.1:{port}:[/] {exc}")
        raise typer.Exit(code=1) from exc

    bound_port = server.server_address[1]
    url = f"http://127.0.0.1:{bound_port}/"
    console.print(
        Panel.fit(
            f"[bold]Sanctum Local Web Dashboard[/]\n"
            f"URL:      [cyan]{url}[/]\n"
            f"Binding:  127.0.0.1 loopback only · zero external calls\n"
            f"Stop:     Ctrl-C",
            border_style="cyan",
        )
    )
    if not no_open and shutil.which("open"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Sanctum UI stopped.[/]")
    finally:
        server.server_close()
