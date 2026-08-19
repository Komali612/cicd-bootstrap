"""Validate + retry-then-escalate for the deploy-repo CD path (FR-N.10 / NFR-3)."""

from pathlib import Path

import yaml

from cicd_bootstrap import cd_cookbooks, cd_deploy_repo, harness
from cicd_bootstrap.cd_validate import (
    record_exception,
    validate_deploy_artifacts,
    validate_pipeline_yaml,
    validate_values_file,
)
from cicd_bootstrap.contracts import RepoSnapshot

_GOOD_VALUES = "app: app\nenv: dev\nimage: ghcr.io/o/app\ntag: abc123\nport: 8080\n"


def _recipe():
    return cd_cookbooks.get("web-service").with_port(8080)


def _good_pipeline(auto_deploy: bool = True) -> str:
    snap = RepoSnapshot(repo_url="https://github.com/O/app", owner="O", name="app",
                        default_branch="main", tree=["Dockerfile"],
                        manifests={"Dockerfile": "FROM x\nEXPOSE 8080\n"})
    return harness.build_pipeline_yaml(snap, auto_deploy=auto_deploy, recipe=_recipe(),
                                       env="dev", deploy_repo="app-deploy")


# --- unit: the validator -----------------------------------------------------

def test_valid_artifacts_pass():
    assert validate_deploy_artifacts(_good_pipeline(), _GOOD_VALUES, expect_approval=False) == []


def test_missing_deploy_stage_flagged():
    probs = validate_pipeline_yaml("pipeline:\n  identifier: x\n  stages: []\n", expect_approval=False)
    assert any("no Deploy" in p for p in probs)


def test_broken_yaml_flagged():
    assert validate_pipeline_yaml("pipeline: {\n  not: yaml", expect_approval=False)


def test_missing_rollback_and_health_flagged():
    p = yaml.safe_load(_good_pipeline())
    spec = p["pipeline"]["stages"][-1]["stage"]["spec"]["execution"]["steps"][0]["step"]["spec"]["source"]["spec"]
    spec["script"] = 'echo "deploy"; docker run app\n'  # no rollback / health / failure exit
    probs = validate_pipeline_yaml(yaml.dump(p), expect_approval=False)
    assert any("rollback" in x for x in probs)
    assert any("health check" in x for x in probs)


def test_values_problems_flagged():
    assert any("empty image tag" in p for p in validate_values_file("app: a\nenv: dev\nimage: i\ntag: \nport: 8080\n"))
    assert any("missing 'image'" in p for p in validate_values_file("app: a\nenv: dev\ntag: t\nport: 8080\n"))


def test_approval_expected_but_missing_flagged():
    # auto_deploy pipeline has no Approval stage; asking for approval must flag it
    probs = validate_pipeline_yaml(_good_pipeline(auto_deploy=True), expect_approval=True)
    assert any("approval expected" in p for p in probs)


def test_record_exception_appends(tmp_path):
    path = tmp_path / "open-questions" / "cd-exceptions.md"
    record_exception(str(path), repo_url="https://github.com/O/app", app="app", env="prod", reason="bad wiring")
    text = path.read_text()
    assert "app" in text and "prod" in text and "bad wiring" in text


# --- orchestration: retry then escalate --------------------------------------

def test_orchestrator_escalates_after_max_attempts(monkeypatch, tmp_path):
    """Generation that never validates -> retried up to the cap, then escalated to the
    exception list with NO PR and NO Harness provisioning (NFR-3)."""
    snap = RepoSnapshot(repo_url="https://github.com/O/researcher", owner="O", name="researcher",
                        default_branch="main", tree=["Dockerfile"],
                        manifests={"Dockerfile": "FROM x\nEXPOSE 8080\n"})
    monkeypatch.setenv("CD_MAX_ATTEMPTS", "2")
    ex_file = tmp_path / "oq" / "cd.md"
    monkeypatch.setenv("CD_EXCEPTION_LIST", str(ex_file))

    monkeypatch.setattr(cd_deploy_repo, "ingest", lambda url, wd, token=None: snap)
    monkeypatch.setattr(cd_deploy_repo, "ci_image_pushed", lambda *a: True)
    monkeypatch.setattr(cd_deploy_repo, "latest_successful_ci_sha", lambda *a: "abc1234")
    monkeypatch.setattr(cd_deploy_repo, "repo_exists", lambda *a: True)
    monkeypatch.setattr("cicd_bootstrap.cd_generate.resolve_recipe",
                        lambda snapshot, allow_llm_fallback=False: _recipe())
    # every generation yields a structurally-invalid pipeline -> validation always fails
    monkeypatch.setattr(harness, "build_pipeline_yaml",
                        lambda *a, **k: "pipeline:\n  identifier: x\n  stages: []\n")
    hit = {"pr": False, "store": False}
    monkeypatch.setattr(cd_deploy_repo, "open_pr_in_repo",
                        lambda *a, **k: hit.__setitem__("pr", True) or (1, "u", "b"))
    monkeypatch.setattr(harness, "store_pipeline_in_repo",
                        lambda *a, **k: hit.__setitem__("store", True) or "pid")

    res = cd_deploy_repo.add_cd_deploy_repo("https://github.com/O/researcher", env="dev", token="tok")
    assert res.status == "error"
    assert "exception list" in res.message.lower()
    assert hit["pr"] is False and hit["store"] is False      # no partial PR, no provisioning
    assert ex_file.exists() and "researcher" in ex_file.read_text()
