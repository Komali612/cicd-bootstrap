"""Deterministic tests for the Harness CD generator (no network / no account)."""

import yaml

from cicd_bootstrap import cd_cookbooks
from cicd_bootstrap.cd_cookbooks import DeployRecipe
from cicd_bootstrap.harness import (
    build_deploy_script,
    build_pipeline,
    identifier,
    render_notify_workflow,
)


def _web(port: int = 8080) -> DeployRecipe:
    return cd_cookbooks.get("web-service").with_port(port)


def _worker() -> DeployRecipe:
    return cd_cookbooks.get("worker")


def test_identifier_sanitizes():
    assert identifier("cicd-test-full") == "cicd_test_full"
    assert identifier("9lives").startswith("_")   # can't start with a digit


def test_pipeline_yaml_is_valid_and_deploys_on_delegate():
    p = build_pipeline("Owner", "my-app", _web(), auto_deploy=True, org="default", project="default_project")
    parsed = yaml.safe_load(yaml.dump(p))          # round-trips as valid YAML
    pipe = parsed["pipeline"]
    assert pipe["identifier"] == "deploy_my_app"
    assert pipe["variables"][0]["name"] == "imageTag"
    stage = pipe["stages"][0]["stage"]
    assert stage["type"] == "Custom"
    step = stage["spec"]["execution"]["steps"][0]["step"]
    assert step["spec"]["onDelegate"] is True
    assert step["spec"]["delegateSelectors"] == ["laptop"]


def test_approval_stage_toggles_with_auto_deploy():
    manual = build_pipeline("Owner", "app", _web(80), auto_deploy=False, org="default", project="p")
    auto = build_pipeline("Owner", "app", _web(80), auto_deploy=True, org="default", project="p")
    assert [s["stage"]["type"] for s in manual["pipeline"]["stages"]] == ["Approval", "Custom"]
    assert [s["stage"]["type"] for s in auto["pipeline"]["stages"]] == ["Custom"]


def test_deploy_script_pulls_ghcr_and_health_checks_via_host():
    s = build_deploy_script("Owner", "My-App", _web(8091))
    assert "ghcr.io/owner/my-app:" in s              # lowercased image ref
    assert "<+pipeline.variables.imageTag>" in s     # tag comes from the pipeline input
    assert 'PORT="8091"' in s                         # the detected port
    assert "-p 8091:8091" in s                        # the port is published
    assert "host.docker.internal:$PORT" in s          # health check reaches the host, not the delegate
    assert "Rolling back" in s                        # rollback path present


def test_worker_deploy_script_publishes_no_port_and_health_checks_the_process():
    s = build_deploy_script("Owner", "My-Worker", _worker())
    assert "ghcr.io/owner/my-worker:" in s
    assert "-p " not in s                             # a worker publishes no port
    assert "host.docker.internal" not in s            # no HTTP probe for a portless worker
    assert ".State.Running" in s                      # health = the container stays running
    assert "Rolling back" in s                        # rollback still applies


def test_authored_recipe_injects_env_and_run_command():
    recipe = DeployRecipe(
        key="web-service-with-config", display_name="x", publish_port=True, port=9000,
        health_type="http", health_path="/healthz",
        env=(("LOG_LEVEL", "info"), ("DB_URL", "postgres://db")),
        run_command=("./serve", "--prod"), llm_authored=True,
    )
    s = build_deploy_script("Owner", "svc", recipe)
    assert "-p 9000:9000" in s
    assert "-e LOG_LEVEL=info" in s                   # runtime env is passed through
    assert "DB_URL=postgres://db" in s
    assert "$PORT/healthz" in s                       # the custom health path is probed
    assert "./serve --prod" in s                      # the run-command override is appended


def test_notify_workflow_pings_harness_after_ci():
    w = render_notify_workflow("CI", "main")
    assert "HARNESS_WEBHOOK_URL" in w                 # uses the repo secret
    assert "workflow_run" in w and "head_sha" in w    # fires after CI, sends the built SHA
    assert 'workflows: ["CI"]' in w
