"""Loose-coupling / config-driven CD (nothing hard-coded).

Verifies CDConfig defaults reproduce today's behaviour, that every knob is overridable
via env, and that the GitOps deploy template is registry-agnostic (image comes from the
per-env values file, per FR-N.6) with the delegate and approvers parameterised (FR-N.2).
"""

from cicd_bootstrap import cd_cookbooks, cd_deploy_repo, core
from cicd_bootstrap.cd_config import load_cd_config
from cicd_bootstrap.contracts import BootstrapResult
from cicd_bootstrap.harness import build_pipeline


def _stub_cd():
    return BootstrapResult(repo_url="x", status="opened", kind="cd")

_KNOBS = (
    "CD_REGISTRY", "CD_DELEGATE_SELECTOR", "CD_ENVIRONMENTS", "CD_APPROVER_USER_GROUPS",
    "CD_DEPLOY_BRANCH", "CD_TEMPLATE_REPO", "CD_DEPLOY_REPO_SUFFIX", "CD_DEPLOY_STRATEGY",
    "CD_GATE_HEALTH", "CD_GATE_DAST", "CD_GATE_PLAYWRIGHT",
)


def _recipe():
    return cd_cookbooks.get("web-service").with_port(8080)


def _deploy_script(p):
    return p["pipeline"]["stages"][-1]["stage"]["spec"]["execution"]["steps"][0]["step"]["spec"]["source"]["spec"]["script"]


def test_defaults_reproduce_current_behaviour(monkeypatch):
    for k in _KNOBS:
        monkeypatch.delenv(k, raising=False)
    c = load_cd_config()
    assert c.registry == "ghcr.io"
    assert c.delegate_selector == "laptop"
    assert c.deploy_strategy == "recreate"
    assert c.environments == ("dev", "staging", "prod")
    assert c.template_repo == "_deploy-template"
    assert c.deploy_branch == "main"
    assert c.approver_user_groups == ("_project_all_users",)
    assert c.deploy_repo_name("app") == "app-deploy"
    assert c.deferred_gates() == []  # DAST/Playwright off by default (Fortify paused)


def test_every_knob_is_overridable(monkeypatch):
    monkeypatch.setenv("CD_REGISTRY", "myreg.nexus.local")
    monkeypatch.setenv("CD_DELEGATE_SELECTOR", "aks-runner")
    monkeypatch.setenv("CD_ENVIRONMENTS", "dev, qa, prod")
    monkeypatch.setenv("CD_APPROVER_USER_GROUPS", "release_team, secops")
    monkeypatch.setenv("CD_DEPLOY_BRANCH", "trunk")
    monkeypatch.setenv("CD_DEPLOY_REPO_SUFFIX", "-cd")
    monkeypatch.setenv("CD_GATE_DAST", "on")
    c = load_cd_config()
    assert c.registry == "myreg.nexus.local"
    assert c.delegate_selector == "aks-runner"
    assert c.environments == ("dev", "qa", "prod")
    assert c.approver_user_groups == ("release_team", "secops")
    assert c.deploy_branch == "trunk"
    assert c.deploy_repo_name("app") == "app-cd"
    assert c.env_host_port(8080, "qa") == 8081          # qa is index 1 -> +1
    assert c.deferred_gates() == ["dast"]               # requested gate surfaced, never silently dropped


def test_gitops_template_is_registry_agnostic():
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=True, org="default", project="p",
                       env="dev", deploy_repo="app-deploy")
    s = _deploy_script(p)
    assert 'IMAGE="$IMG:$TAG"' in s          # image ref comes from the values file
    assert "sed -n 's/^image:" in s          # ...read from environments/<env>.yaml
    assert "ghcr.io" not in s                # nothing registry-specific baked into the template


def test_delegate_and_approvers_are_parameterised():
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=False, org="default", project="p",
                       env="dev", deploy_repo="app-deploy",
                       delegate_selector="aks-runner", approver_user_groups=["release_team"])
    approval = p["pipeline"]["stages"][0]["stage"]
    assert approval["type"] == "Approval"
    assert approval["when"]["condition"] == '<+pipeline.variables.env> != "dev"'   # dev auto, others pause
    assert approval["spec"]["execution"]["steps"][0]["step"]["spec"]["approvers"]["userGroups"] == ["release_team"]
    deploy = p["pipeline"]["stages"][1]["stage"]
    assert deploy["spec"]["execution"]["steps"][0]["step"]["spec"]["delegateSelectors"] == ["aks-runner"]


# --- Phase 4 cut-over: the default CD model is a reversible config switch ------

def test_cutover_default_is_in_repo(monkeypatch):
    """Unset CD_DEPLOY_MODEL -> the shipped default routes to the in-repo model, NOT
    the deploy-repo one (nothing cut over until the flip)."""
    monkeypatch.delenv("CD_DEPLOY_MODEL", raising=False)
    routed = {"deploy_repo": False}
    monkeypatch.setattr(cd_deploy_repo, "add_cd_deploy_repo",
                        lambda *a, **k: routed.__setitem__("deploy_repo", True) or _stub_cd())
    # keep the in-repo body off the network: make ingest fail fast
    from cicd_bootstrap import ingest as ingest_mod
    monkeypatch.setattr("cicd_bootstrap.core.ingest",
                        lambda *a, **k: (_ for _ in ()).throw(ingest_mod.IngestError("stub")))
    res = core.add_cd_harness("https://github.com/o/r", token="tok")
    assert routed["deploy_repo"] is False        # did not route to deploy-repo
    assert res.status == "error"                 # took the in-repo body (ingest stubbed)


def test_cutover_flip_routes_to_deploy_repo(monkeypatch):
    """CD_DEPLOY_MODEL=deploy-repo -> the same call now routes to the deploy-repo model,
    with per-run inputs (env) passed through. Reversible: unset it to roll back."""
    monkeypatch.setenv("CD_DEPLOY_MODEL", "deploy-repo")
    seen = {}
    monkeypatch.setattr(cd_deploy_repo, "add_cd_deploy_repo",
                        lambda url, **k: seen.update(k) or _stub_cd())
    core.add_cd_harness("https://github.com/o/r", token="tok", env="prod")
    assert seen and seen.get("env") == "prod"    # routed to deploy-repo, inputs threaded
