"""The Setup Assistant's single, self-contained web page.

One module-level ``PAGE`` string: inline CSS + JS, **no external assets** (works
offline, nothing to fetch). It talks to the local server via ``GET /state``,
``GET /probe/<id>``, and ``POST /action`` — the contract defined in ``setup.py``.
Apple-installer feel: a left step-rail, one calm pane at a time, live ✓ that flips
when a real probe passes, and honest hand-offs for the steps only a human can do.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Sanctum Setup</title>
<style>
  :root {
    --bg: #f5f5f7; --card: #ffffff; --ink: #1d1d1f; --muted: #6e6e73;
    --line: #e3e3e6; --accent: #0071e3; --accent-ink: #ffffff;
    --ok: #34c759; --warn: #ff9f0a; --down: #ff3b30; --unk: #b0b0b5;
    --shadow: 0 1px 3px rgba(0,0,0,.06), 0 8px 30px rgba(0,0,0,.05);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1d1d1f; --card: #2c2c2e; --ink: #f5f5f7; --muted: #a1a1a6;
      --line: #3a3a3c; --accent: #0a84ff; --shadow: 0 1px 3px rgba(0,0,0,.4);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, "SF Pro Text", system-ui, "Helvetica Neue", sans-serif;
    -webkit-font-smoothing: antialiased; font-size: 15px; line-height: 1.5;
  }
  .app { display: grid; grid-template-columns: 248px 1fr; min-height: 100vh; }
  @media (max-width: 720px) { .app { grid-template-columns: 1fr; } .rail { display: none; } }

  .rail { border-right: 1px solid var(--line); padding: 34px 18px; }
  .brand { display:flex; align-items:center; gap:9px; font-weight:600; margin: 4px 8px 26px; }
  .brand .mark { width:22px; height:22px; border-radius:6px;
    background: linear-gradient(135deg, var(--accent), #64d2ff); }
  .step { display:flex; align-items:center; gap:10px; padding:9px 10px; border-radius:9px;
    cursor:pointer; color:var(--muted); }
  .step:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .step.active { background: color-mix(in srgb, var(--accent) 14%, transparent); color:var(--ink); }
  .step .num { width:22px; height:22px; border-radius:50%; border:1.5px solid var(--line);
    display:grid; place-items:center; font-size:12px; flex:0 0 auto; }
  .step.active .num { border-color: var(--accent); color: var(--accent); }
  .step.done .num { background: var(--ok); border-color: var(--ok); color:#fff; }
  .step .lbl { font-size: 14px; font-weight: 500; }

  .content { display:flex; flex-direction:column; min-height:100vh; }
  .pane { flex:1; padding: 56px 48px; max-width: 680px; width:100%; margin: 0 auto; }
  @media (max-width: 720px) { .pane { padding: 34px 22px; } }
  h1 { font-size: 30px; line-height:1.15; letter-spacing:-.02em; margin: 0 0 10px; font-weight: 600; }
  h2 { font-size: 21px; letter-spacing:-.01em; margin: 0 0 6px; font-weight: 600; }
  .lede { font-size: 17px; color: var(--muted); margin: 0 0 28px; }
  p { margin: 0 0 14px; }
  .muted { color: var(--muted); }
  .hero { font-size:52px; margin-bottom: 8px; }

  .card { background: var(--card); border:1px solid var(--line); border-radius:14px;
    padding: 18px 20px; margin: 14px 0; box-shadow: var(--shadow); }
  .card h3 { margin:0 0 4px; font-size:16px; font-weight:600; }
  .card .sub { color: var(--muted); font-size:14px; margin: 0; }
  .row { display:flex; align-items:center; gap:12px; }
  .row .grow { flex:1; }

  .dot { width:10px; height:10px; border-radius:50%; background:var(--unk); flex:0 0 auto; display:inline-block; }
  .dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); }
  .dot.down, .dot.todo { background: var(--down); } .dot.unk { background: var(--unk); }
  .pill { font-size:12px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:2px 10px; }

  label { display:block; font-size:13px; font-weight:600; margin: 12px 0 5px; }
  input[type=text], input[type=password], input[type=number], select {
    width:100%; padding:11px 13px; border:1px solid var(--line); border-radius:10px;
    background: var(--bg); color: var(--ink); font-size:15px; font-family:inherit; }
  input:focus, select:focus { outline:none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 25%, transparent); }

  button { font-family:inherit; font-size:14px; font-weight:600; border-radius:10px;
    padding:9px 16px; border:1px solid transparent; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .btn { background: var(--accent); color: var(--accent-ink); }
  .btn.sec { background: transparent; color: var(--accent); border-color: var(--line); }
  .btn.ghost { background: transparent; color: var(--muted); border-color: transparent; }

  .cmd { display:flex; align-items:center; gap:10px; background: var(--bg);
    border:1px solid var(--line); border-radius:10px; padding: 8px 8px 8px 13px; margin: 8px 0; }
  .cmd code { font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size:13px; flex:1;
    white-space: pre-wrap; word-break: break-all; }
  .copy { background: var(--card); color: var(--ink); border:1px solid var(--line); padding:5px 11px; }

  .result { font-size:13.5px; margin-top:10px; padding:10px 13px; border-radius:10px; white-space:pre-wrap;
    font-family: "SF Mono", ui-monospace, Menlo, monospace; display:none; }
  .result.show { display:block; }
  .result.good { background: color-mix(in srgb, var(--ok) 14%, transparent); }
  .result.bad  { background: color-mix(in srgb, var(--down) 14%, transparent); }

  .tnrow { display:flex; align-items:center; gap:11px; padding:8px 0; border-bottom:1px solid var(--line); }
  .tnrow:last-child { border-bottom:none; }
  .tnrow .l { font-weight:600; width:130px; flex:0 0 auto; }
  .tnrow .d { color:var(--muted); font-size:13.5px; }

  .footer { border-top:1px solid var(--line); padding: 16px 48px; display:flex; justify-content:space-between;
    background: var(--card); }
  @media (max-width: 720px){ .footer{ padding:14px 22px; } }
  .spin { display:inline-block; width:13px; height:13px; border:2px solid currentColor; border-right-color:transparent;
    border-radius:50%; animation: sp .7s linear infinite; vertical-align:-2px; }
  @keyframes sp { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand"><span class="mark"></span> Sanctum</div>
    <div id="rail"></div>
  </aside>
  <div class="content">
    <div class="pane" id="pane"></div>
    <div class="footer">
      <button class="btn ghost" id="back">Back</button>
      <button class="btn" id="next">Continue</button>
    </div>
  </div>
</div>
<script>
const $  = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
let STATE=null, PANES=[], cur=0;
const local = { autoOK:false, claude:false, gemini:false, netScanned:false, family:null };

async function getState(){ STATE = await (await fetch('/state')).json(); }
async function probe(id){ return (await fetch('/probe/'+id)).json(); }
async function act(step, action, payload={}){
  return (await fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({step,action,payload})})).json();
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }
function dotClass(s){ s=String(s).toLowerCase();
  return ({ok:'ok',operational:'ok',green:'ok',attention:'warn',todo:'todo',down:'down',
           degraded:'down',failed:'down',unknown:'unk'})[s] || 'unk'; }
function cmd(c){ return `<div class="cmd"><code>${esc(c)}</code><button class="copy" data-c="${esc(c)}">Copy</button></div>`; }
function wireCopy(root=document){ $$('.copy',root).forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText(b.dataset.c);}catch(e){}
  const t=b.textContent; b.textContent='Copied'; setTimeout(()=>b.textContent=t,1200); }); }
async function run(btn, fn){ const t=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  try{ return await fn(); } finally { btn.disabled=false; btn.innerHTML=t; } }
function showResult(el, ok, text){ el.className='result show '+(ok?'good':'bad'); el.textContent=text; }

function buildPanes(){
  const haus = STATE.tier==='haus';
  PANES = [
    {id:'welcome',   title:'Welcome'},
    {id:'preflight', title:'Get started'},
    {id:'name',      title:'Name it'},
    {id:'tailscale', title:'Connect'},
    {id:'perms',     title:'Permissions'},
    {id:'backup',    title:'Back up'},
  ];
  if(haus) PANES.push({id:'family', title:'Family'});
  PANES.push({id:'network', title:'Network'});
  if(haus) PANES.push({id:'firewalla', title:'Firewalla'},
                      {id:'ha',        title:'Home Assistant'});
  PANES.push({id:'ai',   title:'AI (optional)'},
             {id:'done', title:'Finish'});
}
function railDot(id){
  const s = STATE.steps || {};
  if(id==='name') return s.instance ? dotClass(s.instance.status) : 'unk';
  if(id==='tailscale') return s.oauth ? dotClass(s.oauth.status) : 'unk';
  if(id==='perms'){ if(STATE.tier!=='haus') return 'ok'; return s.fda ? dotClass(s.fda.status):'unk'; }
  if(id==='backup') return s.backup ? dotClass(s.backup.status) : 'unk';
  if(id==='family') return s.family ? dotClass(s.family.status) : 'unk';
  if(id==='network') return local.netScanned ? 'ok' : 'unk';
  if(id==='firewalla') return s.firewalla ? dotClass(s.firewalla.status) : 'unk';
  if(id==='ha') return s.ha ? dotClass(s.ha.status) : 'unk';
  if(id==='ai') return (local.claude||local.gemini) ? 'ok' : 'unk';
  return '';
}

function render(){
  $('#rail').innerHTML = PANES.map((p,i)=>{
    const d = railDot(p.id);
    const badge = (i<cur) ? '✓' : (i+1);
    const dotHtml = d ? `<span class="dot ${d}" style="margin-left:auto"></span>` : '';
    return `<div class="step ${i===cur?'active':''} ${i<cur?'done':''}" data-i="${i}">
      <span class="num">${badge}</span><span class="lbl">${p.title}</span>${dotHtml}</div>`;
  }).join('');
  $$('#rail .step').forEach(el=>el.onclick=()=>{ cur=+el.dataset.i; render(); });

  const p = PANES[cur];
  $('#pane').innerHTML = HTML[p.id]();
  (WIRE[p.id]||(()=>{}))();
  wireCopy($('#pane'));
  $('#back').style.visibility = cur===0 ? 'hidden' : 'visible';
  $('#next').textContent = (cur===PANES.length-1) ? 'Finish' : 'Continue';
}

$('#back').onclick = ()=>{ if(cur>0){cur--;render();} };
$('#next').onclick = async ()=>{
  if(cur===PANES.length-1){ await act('','' ); await fetch('/done',{method:'POST'});
    $('#pane').innerHTML = `<div class="hero">✓</div><h1>All set.</h1>
      <p class="lede">Sanctum is ready. You can close this window.</p>`;
    $('.footer').style.display='none'; return; }
  cur++; render();
};

// ── panes ──────────────────────────────────────────────────────────────────
const HTML = {
  welcome: ()=>`
    <div class="hero">🏛️</div>
    <h1>Welcome to Sanctum</h1>
    <p class="lede">A calm, private network for your home. Let's set it up together —
       about five minutes, one step at a time. You can stop and come back anytime.</p>
    <div class="card"><div class="row"><span class="dot ok"></span>
      <span class="grow">No terminal required. Every button here just runs Sanctum for you.</span></div></div>`,

  preflight: ()=>{
    const s=STATE.steps;
    const tierLine = STATE.tier==='haus'
      ? 'This Mac is set up as a full Sanctum hub.'
      : 'This is a personal Sanctum install — light and simple.';
    return `<h1>Before we begin</h1>
      <p class="lede">${tierLine}</p>
      <div class="card"><div class="row"><span class="dot ok"></span><span class="grow">${esc(s.cli.detail)}</span><span class="pill">${STATE.tier}</span></div></div>
      <div class="card"><div class="row"><span class="dot ${dotClass(s.instance.status)}"></span>
        <span class="grow">${esc(s.instance.detail)}</span></div></div>
      <p class="muted">We'll name your Sanctum, connect it to your private network, sort out
      permissions, and finish with a quick health check.</p>`;
  },

  name: ()=>{
    const n = (STATE.steps.instance.name)||'';
    return `<h1>Name your Sanctum</h1>
      <p class="lede">A friendly name for this machine — you'll see it in messages and on your dashboard.</p>
      <label for="nm">Name</label>
      <input type="text" id="nm" placeholder="e.g. Home Sanctum" value="${esc(n)}" />
      <div class="row" style="margin-top:16px"><button class="btn" id="save">Save</button></div>
      <div class="result" id="nmres"></div>`;
  },

  tailscale: ()=>{
    const s=STATE.steps;
    const installed = s.tailscale_installed.status==='ok';
    const credOK = s.oauth.status==='ok';
    return `<h1>Connect your private network</h1>
      <p class="lede">Sanctum reaches your devices over Tailscale — an encrypted private network only you can see.</p>

      <div class="card">
        <div class="row"><span class="dot ${dotClass(s.tailscale_installed.status)}"></span>
          <div class="grow"><h3>1 · Install Tailscale</h3><p class="sub">${esc(s.tailscale_installed.detail)}</p></div></div>
        ${installed?'':cmd('brew install tailscale')+`<div class="row"><button class="btn sec" id="rcheck">Re-check</button></div>`}
      </div>

      <div class="card">
        <h3>2 · Connect this Mac</h3>
        <p class="sub">Run this once and sign in with your Tailscale account (it opens your browser).</p>
        ${cmd('sudo tailscale up')}
      </div>

      <div class="card">
        <div class="row"><span class="dot ${dotClass(s.oauth.status)}"></span>
          <div class="grow"><h3>3 · Let Sanctum manage the network</h3>
          <p class="sub">${credOK?'Credential stored — you can skip ahead.':'Create a one-time access key so Sanctum can apply your network policy.'}</p></div></div>
        <div class="row" style="margin:6px 0"><button class="btn sec" id="opents">Open Tailscale settings</button>
          <span class="muted" style="font-size:13px">→ Generate OAuth client · tick <b>ACL&nbsp;write</b> + <b>Devices&nbsp;write</b></span></div>
        <label for="cid">Client ID</label><input type="text" id="cid" placeholder="k123ABC..." />
        <label for="csec">Client secret</label><input type="password" id="csec" placeholder="tskey-client-..." />
        <div class="row" style="margin-top:14px"><button class="btn" id="verify">Verify &amp; store</button></div>
        <div class="result" id="credres"></div>
      </div>

      <div class="card">
        <h3>4 · Apply your network policy</h3>
        <p class="sub">Pushes the access rules (this is what lets you reach your Mac from anywhere).</p>
        <div class="row"><button class="btn" id="apply">Apply policy</button>
          <button class="btn sec" id="check">Check network</button></div>
        <div class="result" id="applyres"></div>
        <div id="tnrows" style="margin-top:12px"></div>
      </div>`;
  },

  perms: ()=> STATE.tier==='haus' ? HTML._permsHaus() : HTML._permsBasic(),

  _permsBasic: ()=>`
    <h1>Permissions</h1>
    <p class="lede">Good news — a personal install needs almost nothing from macOS.</p>
    <div class="card"><div class="row"><span class="dot ok"></span>
      <div class="grow"><h3>Keychain</h3><p class="sub">Sanctum stores keys in your login keychain. Nothing to do — just keep it unlocked as usual.</p></div></div></div>
    <div class="card"><h3>Deeper network self-healing <span class="pill">optional</span></h3>
      <p class="sub">If you want Sanctum to auto-repair your connection in the background, run this once (it'll ask for your password):</p>
      ${''}<div id="nhcmd"></div></div>`,

  _permsHaus: ()=>{
    const s=STATE.steps;
    return `<h1>Permissions</h1>
      <p class="lede">A hub needs a couple of macOS permissions. I'll show you exactly where — and check them off as you go.</p>

      <div class="card">
        <div class="row"><span class="dot ${dotClass(s.fda.status)}"></span>
          <div class="grow"><h3>Full Disk Access</h3><p class="sub">${esc(s.fda.detail)}</p></div></div>
        <div class="row" style="margin-top:8px"><button class="btn sec" id="fdaopen">Open Settings</button>
          <button class="btn sec" id="fdarecheck">Re-check</button></div>
      </div>

      <div class="card">
        <h3>Background permissions</h3>
        <p class="sub">Grants the long tail of file/media access Sanctum's services need. Run once, then re-check.</p>
        ${cmd('bash ~/.sanctum/scripts/sanctum-grant-tcc.sh')}
      </div>

      <div class="card">
        <div class="row"><span class="dot ${local.autoOK?'ok':'unk'}" id="autodot"></span>
          <div class="grow"><h3>Automation</h3><p class="sub">Lets Sanctum drive Messages / Calendar. macOS can't report this one, so tick it once you've allowed it.</p></div></div>
        <div class="row" style="margin-top:8px"><button class="btn sec" id="autoopen">Open Settings</button>
          <button class="btn sec" id="autoack">I've allowed it</button></div>
      </div>`;
  },

  ai: ()=>`
    <h1>Connect an AI provider</h1>
    <p class="lede">Optional. Sanctum always has a private local model — add a cloud key only if you want one.</p>
    <div class="card">
      <h3>Claude</h3><p class="sub">Paste an Anthropic API key. It's verified, then stored in your keychain.</p>
      <input type="password" id="claudekey" placeholder="sk-ant-..." />
      <div class="row" style="margin-top:12px"><button class="btn" id="claudesave">Connect Claude</button></div>
      <div class="result" id="clauderes"></div>
    </div>
    <div class="card">
      <h3>Gemini</h3><p class="sub">Paste a Google AI key (optional).</p>
      <input type="password" id="geminikey" placeholder="AIza..." />
      <div class="row" style="margin-top:12px"><button class="btn" id="geminisave">Connect Gemini</button></div>
      <div class="result" id="geminires"></div>
    </div>
    <p class="muted">No key? That's fine — press Continue to use the local model.</p>`,

  backup: ()=>{
    const b=STATE.steps.backup;
    const dest = b.repo
      ? `<div class="row"><span class="dot ok"></span><div class="grow"><h3>Destination</h3>
           <p class="sub">${esc(b.repo)}${b.secondary?(' · second copy: '+esc(b.secondary)):''}</p></div></div>`
      : `<h3>Destination</h3>
         <p class="sub">No cloud destination yet. Run this in Terminal — it walks you through a free,
         egress-free bucket and stores the key safely:</p>`+cmd('sanctum cloud setup --backend r2')+
        `<div class="row"><button class="btn sec" id="bkrecheck">Re-check</button></div>`;
    const opts = (b.recipes||[]).map(r=>
      `<option value="${esc(r)}" ${r===b.default_recipe?'selected':''}>${esc(r)}</option>`).join('');
    return `<h1>Back up what matters</h1>
      <p class="lede">A small, crucial slice — documents and settings — encrypted and deduplicated with restic.</p>
      <div class="card">
        <div class="row"><span class="dot ${b.restic?'ok':'todo'}"></span>
          <div class="grow"><h3>restic</h3><p class="sub">${b.restic?'installed':'not found — install it, then re-check'}</p></div></div>
        ${b.restic?'':cmd('brew install restic')+`<div class="row"><button class="btn sec" id="bkrestic">Re-check</button></div>`}
      </div>
      <div class="card">${dest}</div>
      <div class="card"><h3>Policy</h3>
        <label for="bkrecipe">What to back up</label>
        <select id="bkrecipe">${opts}</select>
        <div class="row" style="margin-top:6px">
          <div class="grow"><label for="bkd">Daily kept</label><input type="number" id="bkd" min="1" value="${b.keep_daily}"/></div>
          <div class="grow"><label for="bkw">Weekly</label><input type="number" id="bkw" min="0" value="${b.keep_weekly}"/></div>
          <div class="grow"><label for="bkm">Monthly</label><input type="number" id="bkm" min="0" value="${b.keep_monthly}"/></div>
        </div>
        <div class="row" style="margin-top:14px"><button class="btn" id="bksave">Save policy</button></div>
        <div class="result" id="bkres"></div>
      </div>`;
  },

  family: ()=>`
    <h1>Your family</h1>
    <p class="lede">Who's in the haus, and which devices are theirs — this is who Sanctum protects.
       Curfews and limits come later; this pane never deletes anyone.</p>
    <div id="members"></div>
    <div class="row" style="margin-top:12px"><button class="btn sec" id="addmember">Add a person</button>
      <button class="btn" id="famsave">Save family</button></div>
    <div class="result" id="famres"></div>
    <p class="muted" style="margin-top:10px">A device's MAC address is on its Wi-Fi settings screen.
       To remove someone, edit devices.yaml by hand — deliberately.</p>`,

  network: ()=>`
    <h1>Your network</h1>
    <p class="lede">A quick, read-only scan: who hands out addresses, whether you're behind
       double-NAT, and if this Mac is on your private Tailscale network.</p>
    <div class="row"><button class="btn" id="netscan">Scan my network</button></div>
    <div id="netrows" style="margin-top:14px"></div>
    <div class="card"><h3>Want to improve it?</h3>
      <p class="sub">These run in Terminal, explain every change first, and are reversible:</p>
      ${cmd('sanctum net check')}${cmd('sanctum net optimize')}</div>`,

  firewalla: ()=>{
    const s=STATE.steps.firewalla;
    return `<h1>Pair your Firewalla</h1>
      <p class="lede">The bridge lets Sanctum see the network and enforce screen-time.
         The token is verified live, then stays on this Mac.</p>
      <div class="card">
        <div class="row"><span class="dot ${dotClass(s.status)}"></span>
          <div class="grow"><h3>Bridge</h3><p class="sub">${esc(s.detail)}</p></div></div>
        <div class="row" style="margin-top:8px"><button class="btn sec" id="fwtest">Test connection</button></div>
        <div class="result" id="fwproberes"></div>
      </div>
      <div class="card"><h3>Pair</h3>
        <label for="fwurl">Bridge URL</label>
        <input type="text" id="fwurl" value="${esc(s.url||'')}" />
        <label for="fwtoken">Bridge token ${s.token_stored?'<span class="pill">stored — leave blank to reuse</span>':''}</label>
        <input type="password" id="fwtoken" placeholder="${s.token_stored?'(using the stored token)':'paste the bridge token'}" />
        <div class="row">
          <div class="grow"><label for="fwip">Firewalla IP</label>
            <input type="text" id="fwip" value="${esc(s.device_ip||'')}" placeholder="the LAN gateway (optional)"/></div>
          <div class="grow"><label for="fwmac">Firewalla MAC</label>
            <input type="text" id="fwmac" value="${esc(s.device_mac||'')}" placeholder="optional"/></div>
        </div>
        <div class="row" style="margin-top:14px"><button class="btn" id="fwsave">Verify &amp; pair</button></div>
        <div class="result" id="fwres"></div>
      </div>
      <p class="muted">No Firewalla? Press Continue — curfews stay off until one is paired.</p>`;
  },

  ha: ()=>{
    const s=STATE.steps.ha;
    return `<h1>Home Assistant</h1>
      <p class="lede">If a Home Assistant Green runs your home, connect it — verified with an
         owner token that stays on this Mac.</p>
      <div class="card">
        <div class="row"><span class="dot ${dotClass(s.status)}"></span>
          <div class="grow"><h3>HA Green</h3><p class="sub">${esc(s.detail)}</p></div></div>
        <div class="row" style="margin-top:8px"><button class="btn sec" id="hacheck">Check</button></div>
        <div id="harows" style="margin-top:10px"></div>
      </div>
      <div class="card"><h3>Connect</h3>
        <label for="haurl">URL</label>
        <input type="text" id="haurl" value="${esc(s.url||'')}" />
        <label for="hatoken">Owner token ${s.token_stored?'<span class="pill">stored — leave blank to reuse</span>':''}</label>
        <input type="password" id="hatoken" placeholder="${s.token_stored?'(using the stored token)':'a long-lived access token'}" />
        <div class="row" style="margin-top:14px"><button class="btn" id="hasave">Verify &amp; connect</button></div>
        <div class="result" id="hares"></div>
      </div>
      <p class="muted">No Home Assistant? Press Continue — this step is optional.</p>`;
  },

  done: ()=>`
    <h1>One last check</h1>
    <p class="lede">Let's make sure everything's healthy before you go.</p>
    <div class="row"><button class="btn" id="verify">Run health check</button></div>
    <div id="verres" style="margin-top:16px"></div>`,
};

// ── wiring ───────────────────────────────────────────────────────────────────
const WIRE = {
  name: ()=>{
    $('#save').onclick = (e)=> run(e.target, async ()=>{
      const r = await act('identity','save',{name:$('#nm').value});
      showResult($('#nmres'), r.ok, r.ok ? ('✓ '+(r.detail||'saved')) : ('✗ '+(r.detail||'failed')));
      if(r.ok){ await getState(); }
    });
  },
  tailscale: ()=>{
    const rc=$('#rcheck'); if(rc) rc.onclick=(e)=>run(e.target, async ()=>{ await getState(); render(); });
    $('#opents').onclick=(e)=>run(e.target, ()=>act('open','url',{url:'https://login.tailscale.com/admin/settings/oauth'}));
    $('#verify').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('tailscale','creds',{client_id:$('#cid').value, client_secret:$('#csec').value});
      showResult($('#credres'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||''));
      if(r.ok){ await getState(); }
    });
    $('#apply').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('tailscale','apply');
      showResult($('#applyres'), r.ok, (r.ok?'✓ Applied\\n':'✗ ')+(r.detail||''));
    });
    $('#check').onclick=(e)=>run(e.target, async ()=>{
      const r=await probe('tailnet');
      $('#tnrows').innerHTML = `<div class="pill" style="margin-bottom:6px">Tailnet — ${esc(r.overall||'?')}</div>`+
        (r.rows||[]).map(x=>`<div class="tnrow"><span class="dot ${dotClass(x.status)}"></span>
          <span class="l">${esc(x.label)}</span><span class="d grow">${esc(x.detail)}</span></div>`).join('');
    });
  },
  perms: ()=>{
    if(STATE.tier==='haus'){
      $('#fdaopen').onclick=(e)=>run(e.target, ()=>act('open','url',{url:STATE.steps.fda.anchor}));
      $('#fdarecheck').onclick=(e)=>run(e.target, async ()=>{ await getState(); render(); });
      $('#autoopen').onclick=(e)=>run(e.target, ()=>act('open','url',{url:STATE.steps.automation.anchor}));
      $('#autoack').onclick=()=>{ local.autoOK=true; $('#autodot').className='dot ok'; render(); };
    } else {
      $('#nhcmd').innerHTML = cmd('sudo sanctum net heal --install'); wireCopy($('#nhcmd'));
    }
  },
  ai: ()=>{
    $('#claudesave').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('provider','save',{kind:'claude', key:$('#claudekey').value});
      showResult($('#clauderes'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||'')); if(r.ok) local.claude=true;
    });
    $('#geminisave').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('provider','save',{kind:'gemini', key:$('#geminikey').value});
      showResult($('#geminires'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||'')); if(r.ok) local.gemini=true;
    });
  },
  backup: ()=>{
    const rc=$('#bkrecheck'); if(rc) rc.onclick=(e)=>run(e.target, async ()=>{ await getState(); render(); });
    const rr=$('#bkrestic'); if(rr) rr.onclick=(e)=>run(e.target, async ()=>{ await getState(); render(); });
    $('#bksave').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('backup','save',{ default_recipe:$('#bkrecipe').value,
        keep_daily:+$('#bkd').value, keep_weekly:+$('#bkw').value, keep_monthly:+$('#bkm').value });
      showResult($('#bkres'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||''));
      if(r.ok){ await getState(); }
    });
  },
  family: ()=>{
    if(!local.family){
      const seed=(STATE.steps.family&&STATE.steps.family.members)||[];
      local.family=JSON.parse(JSON.stringify(seed));
      if(!local.family.length) local.family.push({name:'',role:'child',devices:[{name:'',mac:''}]});
    }
    const sync=()=>{ $$('#members .card').forEach(card=>{
      const m=local.family[+card.dataset.i];
      m.name=$('.fname',card).value; m.role=$('.frole',card).value;
      m.devices=$$('.dmac',card).map((el,j)=>({name:$$('.dname',card)[j].value, mac:el.value}));
    }); };
    const draw=()=>{
      $('#members').innerHTML=local.family.map((m,i)=>`<div class="card" data-i="${i}">
        <div class="row">
          <div class="grow"><label>Name</label><input type="text" class="fname" value="${esc(m.name||'')}" placeholder="e.g. Albert"/></div>
          <div style="width:130px"><label>Role</label><select class="frole">
            <option value="child" ${m.role!=='parent'?'selected':''}>child</option>
            <option value="parent" ${m.role==='parent'?'selected':''}>parent</option></select></div>
        </div>
        ${(m.devices||[]).map((d,j)=>`<div class="row" style="margin-top:8px">
          <div class="grow"><input type="text" class="dname" data-j="${j}" value="${esc(d.name||'')}" placeholder="Device (e.g. iPhone)"/></div>
          <div class="grow"><input type="text" class="dmac" data-j="${j}" value="${esc(d.mac||'')}" placeholder="AA:BB:CC:DD:EE:FF"/></div>
        </div>`).join('')}
        <div class="row" style="margin-top:8px"><button class="btn ghost adddev" data-i="${i}">+ device</button></div>
      </div>`).join('');
      $$('#members .adddev').forEach(b=>b.onclick=()=>{ sync();
        local.family[+b.dataset.i].devices.push({name:'',mac:''}); draw(); });
    };
    draw();
    $('#addmember').onclick=()=>{ sync();
      local.family.push({name:'',role:'child',devices:[{name:'',mac:''}]}); draw(); };
    $('#famsave').onclick=(e)=>run(e.target, async ()=>{
      sync();
      const members=local.family.filter(m=>(m.name||'').trim());
      const r=await act('family','save',{members});
      showResult($('#famres'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||''));
      if(r.ok){ local.family=null; await getState(); }
    });
  },
  network: ()=>{
    $('#netscan').onclick=(e)=>run(e.target, async ()=>{
      const r=await probe('network');
      local.netScanned=true;
      const row=(l,d,s)=>`<div class="tnrow"><span class="dot ${s}"></span>
        <span class="l">${esc(l)}</span><span class="d grow">${esc(d)}</span></div>`;
      const tn=r.tailnet||{};
      $('#netrows').innerHTML =
        row('NAT', r.nat==='single'?'single — ideal':(r.nat||'unknown'),
            r.nat==='single'?'ok':(r.nat==='double'?'warn':'unk'))+
        row('Gateway', r.gateway||'not found', r.gateway?'ok':'unk')+
        row('ISP', r.isp||'unknown', r.isp&&r.isp!=='unknown'?'ok':'unk')+
        row('Firewalla', r.firewalla?'present':'not detected', r.firewalla?'ok':'unk')+
        row('Tailscale', tn.on?('on the tailnet'+(tn.suffix?(' · '+tn.suffix):'')):'not joined',
            tn.on?'ok':'warn')+
        (r.reason?`<p class="muted" style="margin-top:10px">${esc(r.reason)}</p>`:'');
    });
  },
  firewalla: ()=>{
    $('#fwtest').onclick=(e)=>run(e.target, async ()=>{
      const r=await probe('firewalla');
      showResult($('#fwproberes'), !!r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||''));
    });
    $('#fwsave').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('firewalla','save',{ url:$('#fwurl').value, token:$('#fwtoken').value,
        device_ip:$('#fwip').value, device_mac:$('#fwmac').value });
      showResult($('#fwres'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||''));
      if(r.ok){ $('#fwtoken').value=''; await getState(); }
    });
  },
  ha: ()=>{
    $('#hacheck').onclick=(e)=>run(e.target, async ()=>{
      const r=await probe('ha');
      const row=(l,d,s)=>`<div class="tnrow"><span class="dot ${s}"></span>
        <span class="l">${esc(l)}</span><span class="d grow">${esc(d)}</span></div>`;
      $('#harows').innerHTML =
        row('On the LAN', r.host+':'+r.port+(r.reachable?' answers':' — no answer'),
            r.reachable?'ok':'todo')+
        row('HA Core', r.api_running?('API running'+(r.version?(' · '+r.version):'')):
            (r.token_stored?'not verified':'needs the owner token below'), r.api_running?'ok':'unk')+
        row('Remote access', r.tailnet_node?'Tailscale node joined':'not joined (optional)',
            r.tailnet_node?'ok':'unk');
    });
    $('#hasave').onclick=(e)=>run(e.target, async ()=>{
      const r=await act('ha','save',{ url:$('#haurl').value, token:$('#hatoken').value });
      showResult($('#hares'), r.ok, (r.ok?'✓ ':'✗ ')+(r.detail||''));
      if(r.ok){ $('#hatoken').value=''; await getState(); }
    });
  },
  done: ()=>{
    $('#verify').onclick=(e)=>run(e.target, async ()=>{
      const r=await probe('verify');
      const total=r.total||0, passed=r.passed||0, failed=r.failed||0;
      const good = failed===0 && !r.error;
      let html = `<div class="card"><div class="row"><span class="dot ${good?'ok':'warn'}"></span>
        <div class="grow"><h3>Sanctum ${esc(r.tier||'')} ${good?'is healthy':'needs a look'}</h3>
        <p class="sub">${r.error ? esc(r.error) : passed+' of '+total+' checks passed'+(failed?(' · '+failed+' need attention'):'')}</p></div></div>`;
      const fails=(r.probes||[]).filter(p=>!p.passed && !p.not_applicable);
      if(fails.length) html += fails.map(p=>`<div class="tnrow"><span class="dot down"></span>
        <span class="l">${esc(p.name)}</span><span class="d grow">${esc(p.detail||p.reason||'')}</span></div>`).join('');
      html += `</div>`;
      $('#verres').innerHTML = html;
    });
  },
};

(async ()=>{ await getState(); buildPanes(); render(); })();
</script>
</body>
</html>
"""
