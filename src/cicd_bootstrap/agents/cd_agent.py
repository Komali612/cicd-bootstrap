"""The CD agent — agent 2 of 2.

Picks up after the CI agent: given a repo whose CI pipeline has built an image in
GHCR, it provisions a **Harness** CD pipeline (Git-stored ``.harness/deploy.yaml``)
and deploys the image to your laptop via the Harness delegate (pull, recreate,
health-check, rollback). It also deploys the image already in GHCR right away.

CD here is Harness-only -- there is no GitHub Actions deploy target.

Run it with:  cicd-bootstrap --serve --agent cd   (default port 8002)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..contracts import BootstrapResult
from ..core import add_cd_harness
from .common import CI_AGENT_PORT, RENDER_JS, STYLE, add_shared_routes

app = FastAPI(title="cicd-bootstrap · CD agent", version="0.1.0")
add_shared_routes(app)


class CDRequest(BaseModel):
    repo_url: str
    open_pr: bool = True
    auto_deploy: bool = False
    allow_llm_fallback: bool = False


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.post("/cd", response_model=BootstrapResult)
def cd_endpoint(req: CDRequest) -> BootstrapResult:
    # Harness only: Git-stored pipeline + webhook trigger + notify-workflow PR,
    # deployed via the laptop delegate. No GitHub Actions deploy target.
    return add_cd_harness(
        req.repo_url, auto_deploy=req.auto_deploy, open_pr_flag=req.open_pr,
        allow_llm_fallback=req.allow_llm_fallback,
    )


_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"/>'
    '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
    '<title>CD agent · cicd-bootstrap</title>' + STYLE + '</head><body>'
)

_BODY = f'''<span class="step">Agent 2 of 2 · CD</span>
<h1>🚀 CD agent</h1>
<p class="sub">Run this <strong>after</strong> the <a href="http://localhost:{CI_AGENT_PORT}/">CI agent</a> has built an image. Deploys via <strong>🟣 Harness</strong> — a Git-stored pipeline run on your Harness delegate (no runner needed). &nbsp;·&nbsp; <a href="/dashboard">📊 dashboard</a></p>
<form id="f">
  <input id="url" type="url" required placeholder="https://github.com/owner/repo" autocomplete="off"/>
  <button id="go" type="submit">Run CD agent</button>
</form>
<div class="opts">
  <label class="chk" title="Adds a small notify-harness.yml workflow so FUTURE CI builds auto-deploy. The image already in GHCR deploys now either way."><input id="pr" type="checkbox" checked/> open the notify-harness pull request (for future auto-deploys)</label>
  <label class="chk" title="Checked: deploy straight to your laptop. Unchecked: pause for a click-to-approve in Harness (approval stage)."><input id="auto" type="checkbox"/> deploy automatically (else: click to approve in Harness)</label>
  <label class="chk" title="Deploy recipes cover the common shapes (a web service on a port; a portless background worker). If a repo fits none of them, let the LLM author a deploy recipe for it (port, health check, runtime env). The LLM is used only for authoring, never just to guess a port. Needs ANTHROPIC_API_KEY in .env."><input id="llm" type="checkbox"/> LLM authors a deploy recipe when no built-in template fits</label>
</div>
<div id="out"></div>'''

_SCRIPT = '''<script>
const f=document.getElementById('f'),out=document.getElementById('out'),go=document.getElementById('go');
f.addEventListener('submit',async e=>{
  e.preventDefault();
  const repo_url=document.getElementById('url').value.trim();
  const open_pr=document.getElementById('pr').checked;
  const auto_deploy=document.getElementById('auto').checked;
  const allow_llm_fallback=document.getElementById('llm').checked;
  go.disabled=true;
  out.innerHTML='<div class="card"><span class="spin"></span>Provisioning Harness (pipeline, connector, webhook)'+(open_pr?', opening PR':'')+' &amp; deploying\\u2026</div>';
  try{
    const resp=await fetch('/cd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repo_url,open_pr,auto_deploy,allow_llm_fallback})});
    render(await resp.json());
  }catch(err){ out.innerHTML='<div class="banner err">Request failed: '+esc(String(err))+'</div>'; }
  finally{ go.disabled=false; }
});
''' + RENDER_JS + '''
</script>'''

INDEX_HTML = _HEAD + _BODY + _SCRIPT + '</body></html>'
