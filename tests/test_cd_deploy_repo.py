"""Tests for the deploy-repo (GitOps) CD model.

Pure/deterministic: the network-touching calls (ingest, GitHub, Harness) are
monkeypatched, so we assert *orchestration* -- repo created only when missing and
confirmed, pipeline stored in the {app}-deploy repo, Harness git trigger set up, and
a tag-bump PR opened but NEVER merged.
"""

import yaml

from cicd_bootstrap import cd_cookbooks, cd_deploy_repo, core, harness
from cicd_bootstrap.contracts import RepoSnapshot
from cicd_bootstrap.harness import build_pipeline


def _snap() -> RepoSnapshot:
    return RepoSnapshot(
        repo_url="https://github.com/Owner/researcher",
        owner="Owner", name="researcher", default_branch="main",
        tree=["Dockerfile"], manifests={"Dockerfile": "FROM python:3.12\nEXPOSE 8080\n"},
    )


def _recipe():
    return cd_cookbooks.get("web-service").with_port(8080)


class _Calls(dict):
    """Records monkeypatched call args by key."""


def _patch(monkeypatch, *, exists: bool, image: bool = True) -> _Calls:
    calls = _Calls()
    snap = _snap()

    monkeypatch.setattr(cd_deploy_repo, "ingest", lambda url, wd, token=None: snap)
    monkeypatch.setattr(cd_deploy_repo, "ci_image_pushed", lambda o, n, b, t: image)
    monkeypatch.setattr(cd_deploy_repo, "latest_successful_ci_sha", lambda o, n, b, t: "abc1234deadbeef")
    monkeypatch.setattr(cd_deploy_repo, "repo_exists", lambda o, n, t: exists)
    monkeypatch.setattr("cicd_bootstrap.cd_generate.resolve_recipe",
                        lambda snapshot, allow_llm_fallback=False: _recipe())

    def fake_create(t_owner, t_repo, owner, name, token, **kw):
        calls["create"] = {"template": f"{t_owner}/{t_repo}", "repo": f"{owner}/{name}"}
        return f"https://github.com/{owner}/{name}.git"

    def fake_store(snapshot, token, *, auto_deploy, branch, pipeline_yaml, repo):
        calls["store"] = {"repo": repo, "branch": branch}
        return "deploy_researcher"

    def fake_trigger(pid, connector, repo, *, branch="main", env="dev", **kw):
        calls["trigger"] = {"pid": pid, "repo": repo, "env": env}
        calls.setdefault("trigger_envs", []).append(env)
        return f"gitops_deploy_researcher_{env}"

    def fake_open_pr(owner, name, files, token, *, base, branch_prefix, title, body, commit_message):
        calls["pr"] = {"repo": f"{owner}/{name}", "files": files, "title": title}
        return 7, f"https://github.com/{owner}/{name}/pull/7", "cicd-bootstrap/deploy-dev-1"

    monkeypatch.setattr(cd_deploy_repo, "create_repo_from_template", fake_create)
    monkeypatch.setattr(harness, "store_pipeline_in_repo", fake_store)
    monkeypatch.setattr(harness, "ensure_git_trigger", fake_trigger)
    monkeypatch.setattr(cd_deploy_repo, "open_pr_in_repo", fake_open_pr)
    return calls


# --- pure pipeline shape (no mocks) -----------------------------------------

def _deploy_script(p):
    return p["pipeline"]["stages"][0]["stage"]["spec"]["execution"]["steps"][0]["step"]["spec"]["source"]["spec"]["script"]


def _deploy_step(p):
    return p["pipeline"]["stages"][0]["stage"]["spec"]["execution"]["steps"][0]["step"]["spec"]


def test_gitops_pipeline_reads_tag_from_env_file():
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=True, org="default", project="p",
                       env="dev", deploy_repo="app-deploy")
    # GitOps: only `env` is an input -- the tag is read from the env file, not passed in.
    assert [v["name"] for v in p["pipeline"]["variables"]] == ["env"]
    script = _deploy_script(p)
    assert 'APP="app-<+pipeline.variables.env>"' in script            # env-scoped container
    assert "environments/$ENV.yaml" in script                         # reads the desired-state file
    assert "$GH_TOKEN" in script                                      # via the injected secret
    assert 'DEPLOY_REPO="Owner/app-deploy"' in script                 # points at the deploy repo
    assert "api.github.com/repos/$DEPLOY_REPO/contents/environments/$ENV.yaml" in script
    # the token is injected as a masked secret env var
    ev = _deploy_step(p)["environmentVariables"]
    assert ev == [{"name": "GH_TOKEN", "type": "Secret", "value": "cicd_github_token"}]


def test_env_scoped_pipeline_adds_env_input_and_scopes_container():
    # env set, but no deploy_repo -> env-scoped container yet still an imageTag input.
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=True, org="default", project="p", env="dev")
    assert [v["name"] for v in p["pipeline"]["variables"]] == ["imageTag", "env"]
    assert 'APP="app-<+pipeline.variables.env>"' in _deploy_script(p)


def test_in_repo_pipeline_unchanged_when_env_none():
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=True, org="default", project="p")
    assert [v["name"] for v in p["pipeline"]["variables"]] == ["imageTag"]
    script = p["pipeline"]["stages"][0]["stage"]["spec"]["execution"]["steps"][0]["step"]["spec"]["source"]["spec"]["script"]
    assert 'APP="app"' in script


# --- orchestration ----------------------------------------------------------

def test_creates_repo_when_missing_and_opens_unmerged_pr(monkeypatch):
    calls = _patch(monkeypatch, exists=False)
    res = cd_deploy_repo.add_cd_deploy_repo(
        "https://github.com/Owner/researcher", env="dev", token="tok",
        create_missing_repo=True,
    )
    assert res.status == "opened"
    assert calls["create"]["repo"] == "Owner/researcher-deploy"
    assert calls["create"]["template"].endswith("/_deploy-template")
    assert calls["store"]["repo"] == "researcher-deploy"           # pipeline stored in deploy repo
    assert calls["trigger"]["repo"] == "researcher-deploy"          # Harness watches deploy repo
    # PR bumps environments/dev.yaml with the CI tag, in the deploy repo
    assert calls["pr"]["repo"] == "Owner/researcher-deploy"
    path, content = calls["pr"]["files"][0]
    assert path == "environments/dev.yaml"
    assert "tag: abc1234deadbeef" in content
    assert "ghcr.io/owner/researcher" in content
    assert res.pr_url.endswith("/pull/7")
    # never merged: the result reports "opened", not "merged"
    assert res.merged is False


def test_blocked_when_repo_missing_and_not_confirmed(monkeypatch):
    calls = _patch(monkeypatch, exists=False)
    res = cd_deploy_repo.add_cd_deploy_repo(
        "https://github.com/Owner/researcher", env="dev", token="tok",
        create_missing_repo=False,
    )
    assert res.status == "blocked"
    assert "create" not in calls                                   # no repo created
    assert "confirm" in res.message.lower()


def test_skips_create_when_repo_exists(monkeypatch):
    calls = _patch(monkeypatch, exists=True)
    res = cd_deploy_repo.add_cd_deploy_repo(
        "https://github.com/Owner/researcher", env="staging", token="tok",
        create_missing_repo=False,
    )
    assert res.status == "opened"
    assert "create" not in calls                                   # existing repo reused
    assert calls["pr"]["files"][0][0] == "environments/staging.yaml"


def test_blocked_when_no_image(monkeypatch):
    _patch(monkeypatch, exists=True, image=False)
    res = cd_deploy_repo.add_cd_deploy_repo(
        "https://github.com/Owner/researcher", env="dev", token="tok",
    )
    assert res.status == "blocked"
    assert "image" in res.message.lower()


def test_add_cd_harness_dispatch_routes_to_deploy_repo(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "cicd_bootstrap.cd_deploy_repo.add_cd_deploy_repo",
        lambda url, **kw: seen.update(kw) or _stub_result(),
    )
    core.add_cd_harness("https://github.com/Owner/researcher", deploy_model="deploy-repo", env="dev")
    assert seen["env"] == "dev"


# --- Phase 3: multi-environment (approval, per-env host port, per-env triggers) ----

def test_gitops_approval_gates_only_non_dev():
    # dev auto-deploys on merge; staging/prod pause for a Harness approval.
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=False, org="default", project="p",
                       env="dev", deploy_repo="app-deploy")
    stages = [s["stage"] for s in p["pipeline"]["stages"]]
    assert [s["type"] for s in stages] == ["Approval", "Custom"]
    assert stages[0]["when"]["condition"] == '<+pipeline.variables.env> != "dev"'


def test_gitops_deploy_script_reads_host_port_from_env_file():
    p = build_pipeline("Owner", "app", _recipe(), auto_deploy=True, org="default", project="p",
                       env="dev", deploy_repo="app-deploy")
    script = _deploy_script(p)
    assert "s/^port:" in script          # reads the host port from environments/<env>.yaml
    assert "-p $PORT:8080" in script      # host $PORT -> container's own port (8080)


def test_creates_a_trigger_per_environment(monkeypatch):
    calls = _patch(monkeypatch, exists=True)
    cd_deploy_repo.add_cd_deploy_repo("https://github.com/Owner/researcher", env="dev", token="tok")
    assert calls["trigger_envs"] == ["dev", "staging", "prod"]   # one trigger per env


def test_env_file_uses_per_env_host_port(monkeypatch):
    calls = _patch(monkeypatch, exists=True)
    cd_deploy_repo.add_cd_deploy_repo("https://github.com/Owner/researcher", env="staging", token="tok")
    _, content = calls["pr"]["files"][0]
    assert "port: 8081" in content        # staging = container port (8080) + 1


def _stub_result():
    from cicd_bootstrap.contracts import BootstrapResult
    return BootstrapResult(repo_url="x", status="opened", kind="cd")
