"""FR-N.2 config fields in the per-env values file + rollback_image wiring."""

import yaml

from cicd_bootstrap import cd_cookbooks, cd_deploy_repo
from cicd_bootstrap.cd_validate import validate_deploy_artifacts
from cicd_bootstrap.contracts import RepoSnapshot
from cicd_bootstrap.harness import build_deploy_script, build_pipeline_yaml


def _recipe():
    return cd_cookbooks.get("web-service").with_port(8080)


def _values():
    return cd_deploy_repo._env_file_yaml("app", "Owner", "dev", "abc123", 8080, "ghcr.io",
                                         ["release_team", "secops"])


def test_values_file_carries_frn2_config():
    data = yaml.safe_load(_values())          # valid YAML
    assert data["approvers"] == ["release_team", "secops"]     # approvers from config
    for key in ("rollback_image", "notify", "change_request", "cluster", "namespace"):
        assert key in data, f"values file missing FR-N.2 field {key}"


def test_deploy_script_honors_rollback_image():
    s = build_deploy_script("Owner", "app", _recipe(), env_scoped=True, deploy_repo="app-deploy")
    assert "sed -n 's/^rollback_image:" in s                    # reads rollback_image from the file
    assert 'ROLLBACK_TARGET="${ROLLBACK:-$PREV}"' in s          # explicit target, else last running image
    assert '"$ROLLBACK_TARGET"' in s                            # rollback runs that target
    assert "Rolling back to: $ROLLBACK_TARGET" in s


def test_in_repo_rollback_is_safe_without_the_field():
    # in-repo path never reads rollback_image, but the target still defaults to $PREV
    s = build_deploy_script("Owner", "app", _recipe(), env_scoped=False, deploy_repo=None)
    assert "sed -n 's/^rollback_image:" not in s               # in-repo never reads the field
    assert 'ROLLBACK_TARGET="${ROLLBACK:-$PREV}"' in s
    assert '"$ROLLBACK_TARGET"' in s


def test_validation_passes_with_frn2_values():
    snap = RepoSnapshot(repo_url="x", owner="Owner", name="app", default_branch="main",
                        tree=["Dockerfile"], manifests={"Dockerfile": "FROM x\nEXPOSE 8080\n"})
    pipe = build_pipeline_yaml(snap, auto_deploy=False, recipe=_recipe(), env="dev", deploy_repo="app-deploy")
    assert validate_deploy_artifacts(pipe, _values(), expect_approval=True) == []
