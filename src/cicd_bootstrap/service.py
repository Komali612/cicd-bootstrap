"""The service -- the first-class interface.

    GET  /          a one-page UI: paste a repo URL, click Run, see the result
    GET  /health    liveness probe
    POST /bootstrap {"repo_url": "...", "open_pr": true} -> BootstrapResult

Run with:  uvicorn cicd_bootstrap.service:app   (or `cicd-bootstrap --serve`)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import telemetry
from .contracts import BootstrapResult
from .core import add_cd_harness, bootstrap

app = FastAPI(title="cicd-bootstrap", version="0.1.0")


class BootstrapRequest(BaseModel):
    repo_url: str
    open_pr: bool = True
    allow_llm_fallback: bool = False  # if no cookbook matches, let the LLM author one


class CDRequest(BaseModel):
    repo_url: str
    open_pr: bool = True
    auto_deploy: bool = False  # True: deploy straight to prod; False: pause for approval
    allow_llm_fallback: bool = False  # CD: if no built-in deploy recipe matches the shape, let the LLM author one


class SetupRequest(BaseModel):
    """One request that drives BOTH agents from the unified control panel."""
    repo_url: str
    open_pr: bool = True
    allow_llm_fallback: bool = False   # CI: let the LLM author a cookbook if none matches
    auto_deploy: bool = False          # CD: deploy straight to prod vs click-to-approve
    auto_handoff: bool = True          # CD: run right after CI vs manual run in Actions


class SetupResult(BaseModel):
    """Both agents' results, returned together."""
    ci: BootstrapResult
    cd: BootstrapResult


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/telemetry/data")
def telemetry_data() -> dict:
    """Aggregated telemetry for the dashboard (also handy to curl / pipe to jq)."""
    return telemetry.summary()


@app.post("/bootstrap", response_model=BootstrapResult)
def bootstrap_endpoint(req: BootstrapRequest) -> BootstrapResult:
    # bootstrap() turns stage failures into a structured result, so we return
    # 200 with status="error" rather than raising -- the caller still gets the
    # classification/workflow context.
    return bootstrap(req.repo_url, open_pr_flag=req.open_pr, allow_llm_fallback=req.allow_llm_fallback)


@app.post("/cd", response_model=BootstrapResult)
def cd_endpoint(req: CDRequest) -> BootstrapResult:
    # Same contract as /bootstrap, but generates a CD (deploy) workflow: pull the
    # CI-built image and run it on the self-hosted runner, with health check and
    # rollback. auto_deploy=False adds a click-to-approve gate.
    return add_cd_harness(
        req.repo_url, open_pr_flag=req.open_pr, auto_deploy=req.auto_deploy,
        allow_llm_fallback=req.allow_llm_fallback,
    )


@app.post("/setup", response_model=SetupResult)
def setup_endpoint(req: SetupRequest) -> SetupResult:
    """The unified control panel: give a repo once, set up BOTH pipelines.

    Runs the CI agent (build pipeline) then the CD agent (deploy pipeline). The
    two agents are unchanged underneath; this just drives them from one request.
    """
    ci = bootstrap(req.repo_url, open_pr_flag=req.open_pr, allow_llm_fallback=req.allow_llm_fallback)
    cd = add_cd_harness(
        req.repo_url, open_pr_flag=req.open_pr,
        auto_deploy=req.auto_deploy, allow_llm_fallback=req.allow_llm_fallback,
    )
    return SetupResult(ci=ci, cd=cd)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>cicd-bootstrap · CI + CD</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 860px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }
  h1 { font-size: 1.5rem; margin-bottom: .25rem; }
  h3.sec { font-size: 1rem; margin: 22px 0 2px; }
  p.sub { color: #6b7280; margin-top: 0; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 22px 0 8px; }
  input[type=url] { flex: 1 1 340px; padding: 10px 12px; font-size: 1rem;
                    border: 1px solid #9ca3af; border-radius: 8px; }
  button { padding: 10px 16px; font-size: 1rem; font-weight: 600; border: 0; border-radius: 8px;
           background: #2a78d6; color: #fff; cursor: pointer; }
  button.step2 { background: #6b7280; }
  button:disabled { opacity: .6; cursor: progress; }
  .opts { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin: 4px 0; }
  .opts .divider { width: 1px; height: 18px; background: #d1d5db; }
  .opts .lbl { font-size: .9rem; color: #374151; font-weight: 600; }
  label.chk { font-size: .9rem; color: #6b7280; display: flex; align-items: center; gap: 6px; }
  .card { border: 1px solid #d1d5db; border-radius: 10px; padding: 16px; margin-top: 10px; }
  .banner { padding: 12px 16px; border-radius: 10px; font-weight: 600; margin-top: 8px; }
  .ok { background: rgba(12,163,12,.12); color: #0ca30c; }
  .warn { background: rgba(42,120,214,.12); color: #2a78d6; }
  .err { background: rgba(208,59,59,.12); color: #d03b3b; }
  a { color: #2a78d6; }
  code { background: rgba(127,127,127,.14); padding: 1px 5px; border-radius: 5px; }
  table { border-collapse: collapse; width: 100%; font-size: .92rem; }
  td { padding: 4px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
  td.k { color: #6b7280; width: 140px; }
  .phases { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0 0; }
  .phase { font-size: .8rem; padding: 2px 8px; border-radius: 999px; background: rgba(42,120,214,.14); color: #2a78d6; }
  pre { background: rgba(127,127,127,.12); padding: 12px; border-radius: 8px; overflow-x: auto; font-size: .82rem; }
  .spin { display: inline-block; width: 14px; height: 14px; border: 2px solid currentColor;
          border-right-color: transparent; border-radius: 50%; animation: r .7s linear infinite;
          vertical-align: -2px; margin-right: 8px; }
  @keyframes r { to { transform: rotate(360deg); } }
</style>
</head>
<body>
  <h1>🤖 cicd-bootstrap</h1>
  <p class="sub">Enter a repo <strong>once</strong>. <strong>Step 1</strong> — set up CI: it builds and pushes the image. Merge that PR and let CI run. <strong>Step 2</strong> — set up CD: it deploys that image. CD waits until an image actually exists, so the order is always CI → image → CD. &nbsp;·&nbsp; <a href="/dashboard">📊 dashboard</a></p>

  <div class="row">
    <input id="url" type="url" required placeholder="https://github.com/owner/repo" autocomplete="off"/>
    <button id="go-ci" type="button">① Set up CI</button>
    <button id="go-cd" type="button" class="step2">② Set up CD</button>
  </div>

  <div class="opts">
    <label class="chk"><input id="pr" type="checkbox" checked/> open pull requests</label>
    <label class="chk" title="If no built-in cookbook matches the CI language, let the LLM author one."><input id="llm" type="checkbox"/> LLM fallback (CI)</label>
    <span class="divider"></span>
    <span class="lbl">CD handoff:</span>
    <label class="chk" title="The deploy runs automatically as soon as CI finishes."><input type="radio" name="handoff" value="auto" checked/> auto — right after CI</label>
    <label class="chk" title="The deploy runs only when you trigger it yourself from the Actions tab."><input type="radio" name="handoff" value="manual"/> manual — in GitHub Actions</label>
    <span class="divider"></span>
    <label class="chk" title="Checked: deploy straight to production. Unchecked: pause for your click-to-approve."><input id="auto" type="checkbox"/> deploy automatically (else: click to approve)</label>
  </div>

  <div id="out"></div>

<script>
function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function row(k, v){ return '<tr><td class="k">' + k + '</td><td>' + (v==null?'':v) + '</td></tr>'; }

function renderOne(r, targetId){
  const el = document.getElementById(targetId);
  let h = '';
  if (r.status === 'opened') {
    h += '<div class="banner ok">✅ Opened PR #' + r.pr_number + ' — <a href="' + esc(r.pr_url) + '" target="_blank" rel="noopener">' + esc(r.pr_url) + '</a></div>';
  } else if (r.status === 'generated') {
    h += '<div class="banner warn">ℹ️ Workflow generated (no PR opened)</div>';
  } else if (r.status === 'blocked') {
    h += '<div class="banner warn">⏳ ' + esc(r.message) + '</div>';
  } else {
    h += '<div class="banner err">❌ ' + esc(r.message) + '</div>';
  }
  if (r.sonar_project === 'created' || r.sonar_project === 'exists') {
    h += '<div class="banner ok">🦚 SonarCloud project ' + (r.sonar_project === 'created' ? 'created (Automatic Analysis off)' : 'already set up') + '</div>';
  } else if (r.sonar_project === 'error') {
    h += '<div class="banner warn">⚠️ Could not provision the SonarCloud project — the first scan may need manual setup</div>';
  }
  if (r.sonar_secret_set === true) {
    h += '<div class="banner ok">🔐 SONAR_TOKEN written to the repo’s Actions secrets</div>';
  } else if (r.sonar_secret_set === false) {
    h += '<div class="banner warn">⚠️ Could not set SONAR_TOKEN — Sonar will stay skipped</div>';
  }
  if (r.cd_gate) {
    h += '<div class="banner warn">🚦 Approval to production: ' + esc(r.cd_gate) + '</div>';
  }
  const c = r.classification;
  if (c) {
    h += '<div class="card"><strong>Classification</strong><table>'
      + row('language', esc(c.language)) + row('build system', esc(c.build_system))
      + row('test command', esc(c.test_command))
      + row('confidence', esc(c.confidence + ' (via ' + c.method + ')'))
      + '</table></div>';
  }
  const w = r.workflow;
  if (w) {
    h += '<div class="card"><strong>Generated workflow</strong> — <code>' + esc(w.path) + '</code> <span class="sub">(cookbook: ' + esc(w.cookbook) + (w.llm_authored ? ' — 🤖 LLM-authored' : '') + ')</span>'
      + '<div class="phases">' + (w.phases||[]).map(p => '<span class="phase">' + esc(p) + '</span>').join('') + '</div>'
      + '<pre>' + esc(w.content) + '</pre></div>';
  }
  el.innerHTML = h;
}

const out = document.getElementById('out');
const goCI = document.getElementById('go-ci');
const goCD = document.getElementById('go-cd');

async function run(kind){
  const repo_url = document.getElementById('url').value.trim();
  if (!repo_url) { out.innerHTML = '<div class="banner err">Enter a repo URL first.</div>'; return; }
  const open_pr = document.getElementById('pr').checked;
  goCI.disabled = goCD.disabled = true;
  try {
    let resp, label;
    if (kind === 'ci') {
      const allow_llm_fallback = document.getElementById('llm').checked;
      label = '⚙️ CI pipeline — build &amp; push the image';
      out.innerHTML = '<div class="card"><span class="spin"></span>Setting up CI' + (open_pr ? ', opening PR' : '') + '…</div>';
      resp = await fetch('/bootstrap', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({repo_url, open_pr, allow_llm_fallback}) });
    } else {
      const auto_deploy = document.getElementById('auto').checked;
      const auto_handoff = document.querySelector('input[name=handoff]:checked').value === 'auto';
      label = '🚀 CD pipeline — deploy the image';
      out.innerHTML = '<div class="card"><span class="spin"></span>Setting up CD' + (open_pr ? ', opening PR' : '') + '…</div>';
      resp = await fetch('/cd', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({repo_url, open_pr, auto_deploy, auto_handoff}) });
    }
    const data = await resp.json();
    out.innerHTML = '<h3 class="sec">' + label + '</h3><div id="one"></div>';
    renderOne(data, 'one');
  } catch (err) {
    out.innerHTML = '<div class="banner err">Request failed: ' + esc(String(err)) + '</div>';
  } finally {
    goCI.disabled = goCD.disabled = false;
  }
}
goCI.onclick = () => run('ci');
goCD.onclick = () => run('cd');
</script>
</body>
</html>
"""


# The telemetry dashboard — self-contained (no external libraries), theme-aware,
# rendered from GET /telemetry/data. Charts are inline SVG. Palette + roles from
# the data-viz reference instance (validated categorical + status ramps).
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>cicd-bootstrap · telemetry</title>
<style>
  :root{
    color-scheme: light dark;
    --plane:#f9f9f7; --surface:#fcfcfb;
    --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
    --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300;
    --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  }
  @media (prefers-color-scheme:dark){ :root:where(:not([data-theme="light"])){
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
  }}
  :root[data-theme="dark"]{
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
  }
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--plane); color:var(--ink);
       margin:0; padding:24px; line-height:1.45; -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1120px; margin:0 auto;}
  header{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:4px;}
  h1{font-size:1.35rem; margin:0;}
  .sub{color:var(--ink-2); margin:0 0 18px;}
  a{color:var(--s1);}
  .spacer{flex:1}
  button{font:inherit; padding:6px 12px; border:1px solid var(--border); border-radius:8px;
         background:var(--surface); color:var(--ink); cursor:pointer;}
  .kpis{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:18px;}
  .tile{background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 16px;}
  .tile .v{font-size:1.7rem; font-weight:650; letter-spacing:-.01em;}
  .tile .l{color:var(--ink-2); font-size:.8rem; margin-top:2px;}
  .tile .h{color:var(--muted); font-size:.72rem; margin-top:2px;}
  .grid2{display:grid; grid-template-columns:1fr 1fr; gap:16px;}
  @media (max-width:760px){ .grid2{grid-template-columns:1fr;} }
  .card{background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; margin-bottom:16px;}
  .card h2{font-size:.95rem; margin:0 0 12px; font-weight:620;}
  .card h2 .n{color:var(--muted); font-weight:400;}
  text.axlbl{fill:var(--ink-2); font-size:12px;}
  text.val{fill:var(--ink); font-size:12px; font-variant-numeric:tabular-nums;}
  text.val.sm{font-size:11px; fill:var(--ink-2);}
  text.day{fill:var(--muted); font-size:10px;}
  .legend{display:flex; gap:14px; flex-wrap:wrap; margin-top:10px;}
  .legend span{display:inline-flex; align-items:center; gap:6px; font-size:.82rem; color:var(--ink-2);}
  .sw{width:11px; height:11px; border-radius:3px; display:inline-block;}
  table{border-collapse:collapse; width:100%; font-size:.84rem;}
  th,td{text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); white-space:nowrap;}
  th{color:var(--muted); font-weight:500;}
  td.num{font-variant-numeric:tabular-nums;}
  .pill{font-size:.72rem; padding:1px 8px; border-radius:999px; border:1px solid var(--border);}
  .st-opened{color:var(--good);} .st-error{color:var(--critical);} .st-generated{color:var(--s1);}
  .empty{color:var(--ink-2); text-align:center; padding:48px 0;}
  .muted{color:var(--muted); font-size:.78rem;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📊 cicd-bootstrap telemetry</h1>
    <div class="spacer"></div>
    <a href="/">← service</a>
    <button id="theme">◐ theme</button>
    <button id="refresh">↻ refresh</button>
  </header>
  <p class="sub">What the bootstrapping service is doing — for a service owner / platform team.</p>
  <div id="root"><p class="empty">Loading…</p></div>
  <p class="muted" id="foot"></p>
</div>
<script>
const $ = s => document.querySelector(s);
const esc = s => (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pct = v => (v==null?'—':v+'%');
const ms = v => v>=1000 ? (v/1000).toFixed(1)+'s' : (v||0)+'ms';
const num = v => (v||0).toLocaleString();

$('#theme').onclick = () => {
  const r=document.documentElement, cur=r.getAttribute('data-theme');
  r.setAttribute('data-theme', cur==='dark'?'light':(cur==='light'?'dark':'dark'));
  if(window.__data) render(window.__data);
};
$('#refresh').onclick = load;

async function load(){
  try{
    const r = await fetch('/telemetry/data'); const d = await r.json();
    window.__data = d; render(d);
    $('#foot').textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · data at ~/.cicd-bootstrap/events.jsonl';
  }catch(e){ $('#root').innerHTML = '<p class="empty">Could not load telemetry: '+esc(e)+'</p>'; }
}

function tile(v,l,h){ return '<div class="tile"><div class="v">'+v+'</div><div class="l">'+l+'</div>'+(h?'<div class="h">'+h+'</div>':'')+'</div>'; }

function render(d){
  const t=d.totals, r=d.rates, tok=d.tokens, lat=d.latency_ms;
  if(!t || t.runs===0){ $('#root').innerHTML='<p class="empty">No bootstrap runs recorded yet.<br>Bootstrap a repo from the <a href="/">service</a> and this fills in.</p>'; return; }

  const cd = d.cd || {runs:0, prs_opened:0, success_rate:0};
  let h = '<div class="kpis">'
    + tile(num(t.runs),'Total runs', (t.ci_runs||0)+' CI · '+(t.cd_runs||0)+' CD')
    + tile(pct(r.success_rate),'Success rate', t.errors+' errors')
    + tile(num(t.prs_opened),'PRs opened', t.generated+' generated only')
    + tile(num(cd.runs),'CD pipelines', cd.prs_opened+' deploys opened')
    + tile(pct(r.fallback_rate),'LLM-fallback rate', pct(r.llm_classify_rate)+' LLM-classified')
    + tile(ms(lat.p95),'p95 latency', 'p50 '+ms(lat.p50))
    + '</div>';

  h += card('Bootstraps over time <span class="n">· last 14 days</span>', '<div id="c-time"></div>');

  h += '<div class="grid2">'
     + card('Language <span class="n">· by runs</span>', '<div id="c-lang"></div>')
     + card('Build system <span class="n">· by runs</span>', '<div id="c-build"></div>')
     + '</div>';

  h += '<div class="grid2">'
     + card('Outcome', '<div id="c-status"></div>')
     + card('Classification method <span class="n">· CI only</span>', '<div id="c-method"></div>')
     + '</div>';

  h += '<div class="grid2">'
     + card('Pipeline type <span class="n">· CI vs CD</span>', '<div id="c-kind"></div>')
     + card('CD approval mode <span class="n">· auto vs click-to-approve</span>', '<div id="c-cdgate"></div>')
     + '</div>';

  h += card('Recent runs <span class="n">· newest first</span>', '<div style="overflow-x:auto">'+table(d.recent)+'</div>');
  $('#root').innerHTML = h;

  vbars($('#c-time'), d.runs_per_day);
  hbars($('#c-lang'), d.by_language);
  hbars($('#c-build'), d.by_build_system);
  segbar($('#c-status'), d.by_status, {opened:'var(--good)', generated:'var(--s1)', error:'var(--critical)'});
  segbar($('#c-method'), d.by_method, {llm:'var(--s1)', heuristic:'var(--s2)'});
  segbar($('#c-kind'), d.by_kind, {ci:'var(--s1)', cd:'var(--s3)'});
  segbar($('#c-cdgate'), d.by_cd_gate, {automatic:'var(--s4)', manual:'var(--s2)'});
}

function card(title, body){ return '<div class="card"><h2>'+title+'</h2>'+body+'</div>'; }

function hbars(el, data){
  if(!data || !data.length){ el.innerHTML='<p class="muted">no data</p>'; return; }
  const max=Math.max(1,...data.map(d=>d.count));
  const rowH=30, w=Math.max(320, el.clientWidth||520), labelW=104, valW=40, barW=w-labelW-valW;
  const hgt=data.length*rowH+6;
  let s='<svg viewBox="0 0 '+w+' '+hgt+'" width="100%" height="'+hgt+'" role="img">';
  data.forEach((d,i)=>{ const y=i*rowH+3, bw=Math.max(2, d.count/max*barW), by=y+5, bh=rowH-14;
    s+='<text x="0" y="'+(y+rowH/2)+'" dominant-baseline="middle" class="axlbl">'+esc(d.key)+'</text>';
    s+='<rect x="'+labelW+'" y="'+by+'" width="'+bw+'" height="'+bh+'" rx="4" fill="var(--s1)"/>';
    s+='<text x="'+(labelW+bw+6)+'" y="'+(y+rowH/2)+'" dominant-baseline="middle" class="val">'+d.count+'</text>'; });
  el.innerHTML = s+'</svg>';
}

function vbars(el, data){
  if(!data || !data.length){ el.innerHTML='<p class="muted">no data</p>'; return; }
  const max=Math.max(1,...data.map(d=>d.count));
  const w=Math.max(360, el.clientWidth||760), h=170, padB=24, padT=10, n=data.length, step=w/n, bw=step*0.6;
  let s='<svg viewBox="0 0 '+w+' '+h+'" width="100%" height="'+h+'" role="img">';
  s+='<line x1="0" y1="'+(h-padB)+'" x2="'+w+'" y2="'+(h-padB)+'" stroke="var(--axis)" stroke-width="1"/>';
  data.forEach((d,i)=>{ const bh=d.count/max*(h-padB-padT), x=i*step+(step-bw)/2, y=h-padB-bh;
    s+='<rect x="'+x+'" y="'+y+'" width="'+bw+'" height="'+Math.max(bh,d.count?2:0)+'" rx="4" fill="var(--s1)"><title>'+d.day+': '+d.count+'</title></rect>';
    if(d.count) s+='<text x="'+(x+bw/2)+'" y="'+(y-3)+'" text-anchor="middle" class="val sm">'+d.count+'</text>';
    if(i===0||i===n-1||i===Math.floor(n/2)) s+='<text x="'+(x+bw/2)+'" y="'+(h-8)+'" text-anchor="middle" class="day">'+d.day.slice(5)+'</text>'; });
  el.innerHTML = s+'</svg>';
}

function segbar(el, data, colors){
  if(!data || !data.length){ el.innerHTML='<p class="muted">no data</p>'; return; }
  const total=data.reduce((a,b)=>a+b.count,0)||1, w=Math.max(280, el.clientWidth||520), bh=28, gap=2;
  let x=0, s='<svg viewBox="0 0 '+w+' '+bh+'" width="100%" height="'+bh+'" role="img">';
  data.forEach(d=>{ const seg=d.count/total*w, off=x>0?gap:0;
    s+='<rect x="'+(x+off)+'" y="0" width="'+Math.max(0,seg-off)+'" height="'+bh+'" rx="3" fill="'+(colors[d.key]||'var(--s3)')+'"><title>'+esc(d.key)+': '+d.count+'</title></rect>'; x+=seg; });
  s+='</svg>';
  let lg='<div class="legend">';
  data.forEach(d=>{ lg+='<span><span class="sw" style="background:'+(colors[d.key]||'var(--s3)')+'"></span>'+esc(d.key)+' · '+d.count+' ('+Math.round(d.count/total*100)+'%)</span>'; });
  el.innerHTML = s + lg + '</div>';
}

function table(rows){
  if(!rows || !rows.length) return '<p class="muted">no runs</p>';
  let h='<table><thead><tr><th>when</th><th>repo</th><th>type</th><th>lang</th><th>build</th><th>method</th><th>outcome</th><th>cookbook</th><th class="num">PR</th><th>sonar</th><th class="num">tok</th><th class="num">ms</th></tr></thead><tbody>';
  rows.forEach(e=>{
    const when=(e.ts||'').replace('T',' ').replace('+00:00','');
    const kind = e.kind==='cd' ? '🚀 CD' : '⚙️ CI';
    const cook = e.llm_authored ? '🤖 '+esc(e.cookbook||'') : esc(e.cookbook||'—');
    const sonar = e.sonar_secret_set===true?'✓':(e.sonar_secret_set===false?'✕':'—');
    h+='<tr><td>'+esc(when)+'</td><td>'+esc(e.repo||'')+'</td><td>'+kind+'</td><td>'+esc(e.language||'—')+'</td><td>'+esc(e.build_system||'—')
      +'</td><td>'+esc(e.classify_method||'—')+'</td><td class="st-'+esc(e.status)+'">'+esc(e.status||'')+'</td><td>'+cook
      +'</td><td class="num">'+(e.pr_number?('#'+e.pr_number):'—')+'</td><td>'+sonar+'</td><td class="num">'+num(e.tokens)+'</td><td class="num">'+num(e.duration_ms)+'</td></tr>';
  });
  return h+'</tbody></table>';
}

load();
setInterval(load, 20000);
</script>
</body>
</html>
"""
