"""Loose-coupling / config-driven CD (nothing hard-coded).

Verifies CDConfig defaults reproduce today's behaviour, that every knob is overridable
via env, and that the GitOps deploy template is registry-agnostic (image comes from the
per-env values file, per FR-N.6) with the delegate and approvers parameterised (FR-N.2).
"""

from cicd_bootstrap import cd_cookbooks
from cicd_bootstrap.cd_config import load_cd_config
from cicd_bootstrap.harness import build_pipeline

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
