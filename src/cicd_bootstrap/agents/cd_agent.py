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

from ..cd_config import load_cd_config
from ..contracts import BootstrapResult
from ..core import add_cd_harness
from .common import CI_AGENT_PORT, RENDER_JS, STYLE, add_shared_routes

app = FastAPI(title="cicd-bootstrap · CD agent", version="0.1.0")
add_shared_routes(app)

# The configured default CD model (Phase 4 cut-over flips this via CD_DEPLOY_MODEL);
# the UI pre-selects it, but the user can still switch per run.
_DEFAULT_MODEL = load_cd_config().deploy_model


class CDRequest(BaseModel):
    repo_url: str
    open_pr: bool = True
    auto_deploy: bool = False
    allow_llm_fallback: bool = False
    deploy_model: str | None = None    # None -> configured default (CD_DEPLOY_MODEL); else in-repo|deploy-repo
    env: str = "dev"                   # deploy-repo model: which environment
    create_deploy_repo: bool = False   # deploy-repo model: allow creating {app}-deploy


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.post("/cd", response_model=BootstrapResult)
def cd_endpoint(req: CDRequest) -> BootstrapResult:
    # in-repo (default): Git-stored pipeline in the app repo + notify-workflow PR.
    # deploy-repo: a per-app {app}-deploy repo holds the pipeline + per-env desired
    # state; the agent opens a tag-bump PR there (Harness watches the deploy repo).
    return add_cd_harness(
        req.repo_url, deploy_model=req.deploy_model, env=req.env,
        auto_deploy=req.auto_deploy, open_pr_flag=req.open_pr,
        allow_llm_fallback=req.allow_llm_fallback, create_missing_repo=req.create_deploy_repo,
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
  <label class="chk" title="in-repo: the Harness pipeline is stored in the app repo (current behaviour)."><input type="radio" name="model" value="in-repo" {"checked" if _DEFAULT_MODEL != "deploy-repo" else ""}/> in-repo <span class="sub">(pipeline in the app repo)</span></label>
  <label class="chk" title="deploy-repo: a separate app-deploy repo holds the pipeline and per-env desired state; the agent opens a tag-bump PR there and Harness watches that repo (GitOps)."><input type="radio" name="model" value="deploy-repo" {"checked" if _DEFAULT_MODEL == "deploy-repo" else ""}/> deploy-repo <span class="sub">(separate app-deploy repo · GitOps)</span></label>
</div>
<div class="opts deploy-repo-only" style="display:none">
  <label class="chk">environment&nbsp;<select id="env"><option value="dev">dev</option><option value="staging">staging</option><option value="prod">prod</option></select></label>
  <label class="chk" title="The deploy-repo model needs a per-app app-deploy repo. Tick to let the agent CREATE it from the _deploy-template template repo if it does not exist (this creates a GitHub repository)."><input id="createrepo" type="checkbox"/> create the deploy repo if missing</label>
</div>
<div class="opts">
  <label class="chk" title="Adds a small notify-harness.yml workflow so FUTURE CI builds auto-deploy. The image already in GHCR deploys now either way. (in-repo model only)"><input id="pr" type="checkbox" checked/> open the notify-harness pull request (for future auto-deploys)</label>
  <label class="chk" title="Checked: deploy straight to your laptop. Unchecked: pause for a click-to-approve in Harness (approval stage)."><input id="auto" type="checkbox"/> deploy automatically (else: click to approve in Harness)</label>
  <label class="chk" title="Deploy recipes cover the common shapes (a web service on a port; a portless background worker). If a repo fits none of them, let the LLM author a deploy recipe for it (port, health check, runtime env). The LLM is used only for authoring, never just to guess a port. Needs ANTHROPIC_API_KEY in .env."><input id="llm" type="checkbox"/> LLM authors a deploy recipe when no built-in template fits</label>
</div>
<div id="out"></div>'''

_SCRIPT = '''<script>
const f=document.getElementById('f'),out=document.getElementById('out'),go=document.getElementById('go');
function model(){ return document.querySelector('input[name=model]:checked').value; }
function syncModel(){ const dr=model()==='deploy-repo'; document.querySelectorAll('.deploy-repo-only').forEach(e=>e.style.display=dr?'':'none'); }
document.querySelectorAll('input[name=model]').forEach(r=>r.addEventListener('change',syncModel)); syncModel();
f.addEventListener('submit',async e=>{
  e.preventDefault();
  const repo_url=document.getElementById('url').value.trim();
  const open_pr=document.getElementById('pr').checked;
  const auto_deploy=document.getElementById('auto').checked;
  const allow_llm_fallback=document.getElementById('llm').checked;
  const deploy_model=model();
  const env=document.getElementById('env').value;
  const create_deploy_repo=document.getElementById('createrepo').checked;
  go.disabled=true;
  const busy=deploy_model==='deploy-repo'
    ? 'Setting up the '+esc(env)+' deploy in the {app}-deploy repo (pipeline, trigger, PR)\\u2026'
    : 'Provisioning Harness (pipeline, connector, webhook)'+(open_pr?', opening PR':'')+' &amp; deploying\\u2026';
  out.innerHTML='<div class="card"><span class="spin"></span>'+busy+'</div>';
  try{
    const resp=await fetch('/cd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repo_url,open_pr,auto_deploy,allow_llm_fallback,deploy_model,env,create_deploy_repo})});
    render(await resp.json());
  }catch(err){ out.innerHTML='<div class="banner err">Request failed: '+esc(String(err))+'</div>'; }
  finally{ go.disabled=false; }
});
''' + RENDER_JS + '''
</script>'''

INDEX_HTML = _HEAD + _BODY + _SCRIPT + '</body></html>'
