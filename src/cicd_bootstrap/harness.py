"""Harness CD integration -- deploy the CI-built image via Harness instead of
GitHub Actions.

This is the Harness counterpart to :mod:`cicd_bootstrap.deploy` (which emits a
GitHub Actions ``deploy.yml``). Here we generate a **Harness pipeline** and drive
it through the Harness API. CI is unchanged: GitHub Actions still builds and
pushes ``ghcr.io/<owner>/<name>:<sha>`` to GHCR; Harness pulls that image and
runs it on the laptop through a **delegate** (the on-machine worker, the Harness
twin of a self-hosted runner).

The delegate runs a shell step (``onDelegate: true``, pinned to the ``laptop``
delegate selector) that drives the host Docker via the mounted socket, using the
same *recreate + health-check + rollback* strategy as the GitHub Actions path.

Config comes from ``.env`` (see ``.env.example``):
  HARNESS_ACCOUNT_ID, HARNESS_API_KEY, HARNESS_MANAGER_HOST,
  HARNESS_ORG (default "default"), HARNESS_PROJECT (default "default_project").

Nothing here is secret in transit beyond the API key, which is only ever sent in
the ``x-api-key`` header, never logged.
"""

from __future__ import annotations

import os
import re
import shlex
import time

import httpx
import yaml

from .config import load_dotenv
from .cd_cookbooks import HEALTH_HTTP, DeployRecipe
from .contracts import RepoSnapshot

# Where the pipeline lives when stored in the repo (Git Experience / "remote").
HARNESS_PIPELINE_PATH = ".harness/deploy.yaml"
# The delegate selector we tag the laptop delegate with (see add-harness-delegate.sh).
DELEGATE_SELECTOR = "laptop"


class HarnessError(Exception):
    """Raised when a Harness API call fails or config is missing."""


# --- config ---------------------------------------------------------------

class HarnessConfig:
    def __init__(self) -> None:
        load_dotenv()
        self.account = os.environ.get("HARNESS_ACCOUNT_ID", "").strip()
        self.api_key = os.environ.get("HARNESS_API_KEY", "").strip()
        self.host = (os.environ.get("HARNESS_MANAGER_HOST") or "https://app.harness.io").rstrip("/")
        self.org = os.environ.get("HARNESS_ORG", "default").strip() or "default"
        self.project = os.environ.get("HARNESS_PROJECT", "default_project").strip() or "default_project"

    def require(self) -> "HarnessConfig":
        if not self.account or not self.api_key:
            raise HarnessError(
                "Harness is not configured: set HARNESS_ACCOUNT_ID and HARNESS_API_KEY in .env"
            )
        return self

    @property
    def pipeline_base(self) -> str:
        return f"{self.host}/pipeline/api"

    @property
    def ng_base(self) -> str:
        return f"{self.host}/ng/api"

    @property
    def scope(self) -> dict[str, str]:
        return {
            "accountIdentifier": self.account,
            "orgIdentifier": self.org,
            "projectIdentifier": self.project,
        }

    def headers(self, *, yaml_body: bool = False) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/yaml" if yaml_body else "application/json",
        }


def identifier(name: str) -> str:
    """A valid Harness identifier: alphanumerics/underscore, not starting with a digit."""
    ident = re.sub(r"[^0-9A-Za-z_]", "_", name.strip())
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    return ident


# --- pipeline generation --------------------------------------------------

def _health_block(recipe: DeployRecipe) -> str:
    """The health-check loop for the deploy script, per the recipe's health type.

    * ``http``    -> the container must be running AND the port must answer.
    * ``process`` -> no port to probe; the container just has to stay running.
    The delegate is a separate container, so an http check reaches the app on the
    HOST via ``host.docker.internal`` rather than its own ``localhost``.
    """
    if recipe.health_type == HEALTH_HTTP:
        path = recipe.health_path or "/"
        if not path.startswith("/"):
            path = "/" + path
        return (
            'healthy=""; code="000"\n'
            'for i in $(seq 1 15); do\n'
            "  running=\"$(docker inspect --format '{{.State.Running}}' \"$APP\" 2>/dev/null || echo false)\"\n"
            '  if [ "$running" = "true" ]; then\n'
            f"    code=\"$(curl -s -o /dev/null -w '%{{http_code}}' \"http://host.docker.internal:$PORT{path}\" 2>/dev/null || echo 000)\"\n"
            '    if [ "$code" != "000" ]; then healthy="yes"; break; fi\n'
            '  fi\n'
            '  sleep 2\n'
            'done\n'
        )
    return (
        '# No published port: health = the worker container stays running for ~10s.\n'
        'healthy=""\n'
        'for i in $(seq 1 5); do\n'
        "  running=\"$(docker inspect --format '{{.State.Running}}' \"$APP\" 2>/dev/null || echo false)\"\n"
        '  if [ "$running" != "true" ]; then healthy=""; break; fi\n'
        '  healthy="yes"; sleep 2\n'
        'done\n'
    )


def _gitops_tag_line(owner: str, deploy_repo: str) -> str:
    """Shell that resolves TAG from the deploy repo's environments/<env>.yaml (the
    source of truth) instead of taking it as a pipeline input. GH_TOKEN is injected as
    a masked secret env var by the deploy stage; the delegate needs curl + network
    egress to api.github.com."""
    return f'''ENV="<+pipeline.variables.env>"
DEPLOY_REPO="{owner}/{deploy_repo}"
echo "Resolving image tag from $DEPLOY_REPO (environments/$ENV.yaml)"
DESIRED="$(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/$DEPLOY_REPO/contents/environments/$ENV.yaml")"
TAG="$(printf '%s\\n' "$DESIRED" | sed -n 's/^tag:[[:space:]]*//p' | tr -d '"' | head -n1)"
if [ -z "$TAG" ]; then echo "No tag found in environments/$ENV.yaml of $DEPLOY_REPO"; exit 1; fi
echo "Desired tag: $TAG"
'''


def build_deploy_script(owner: str, name: str, recipe: DeployRecipe, *, env_scoped: bool = False, deploy_repo: str | None = None) -> str:
    """The recreate-with-rollback deploy step the delegate runs (drives host Docker).

    Driven by a :class:`DeployRecipe`: a ``web-service`` publishes its port and is
    health-checked over HTTP; a ``worker`` publishes no port and is healthy as long
    as the container keeps running. Runtime env and an optional run command come
    from the recipe too. The image tag comes from the pipeline variable ``imageTag``.
    """
    image = f"ghcr.io/{owner.lower()}/{name.lower()}"
    # In the deploy-repo (GitOps) model the container is env-scoped ({app}-{env}) so
    # dev/staging/prod can run side by side on one delegate; env is a pipeline variable
    # Harness resolves before the script runs. env_scoped=False keeps the original name.
    app = f"{name.lower()}-<+pipeline.variables.env>" if env_scoped else name.lower()
    port = recipe.port

    publish = f"-p {port}:{port} " if recipe.publish_port else ""
    env_flags = "".join(f"-e {shlex.quote(f'{n}={v}')} " for n, v in recipe.env)
    cmd_suffix = "".join(f" {shlex.quote(c)}" for c in recipe.run_command)
    run_new = f'docker run -d --name "$APP" --restart unless-stopped {publish}{env_flags}"$IMAGE"{cmd_suffix}'
    run_prev = f'docker run -d --name "$APP" --restart unless-stopped {publish}{env_flags}"$PREV"{cmd_suffix}'

    port_line = f'PORT="{port}"\n' if recipe.publish_port else ""
    where = " on port $PORT" if recipe.publish_port else " (no published port)"
    success = (
        "Deploy successful -- '$APP' is up and answering (HTTP $code)."
        if recipe.health_type == HEALTH_HTTP
        else "Deploy successful -- '$APP' is up and staying running."
    )

    # GitOps (deploy_repo): read the tag from environments/<env>.yaml. Otherwise the
    # tag is the pipeline's imageTag input (the in-repo model, unchanged).
    tag_line = _gitops_tag_line(owner, deploy_repo) if deploy_repo else 'TAG="<+pipeline.variables.imageTag>"\n'
    return (
        "set +e\n"
        f"{tag_line}"
        f'IMAGE="{image}:$TAG"\n'
        f'APP="{app}"\n'
        f"{port_line}"
        f"echo \"Deploying $IMAGE  ->  container '$APP'{where}\"\n"
        "\n"
        "# Remember the currently-running image so we can roll back to it if needed.\n"
        "PREV=\"$(docker inspect --format '{{.Config.Image}}' \"$APP\" 2>/dev/null || true)\"\n"
        'echo "Currently running image: ${PREV:-<none>}"\n'
        "\n"
        'if ! docker pull "$IMAGE"; then\n'
        '  echo "Could not pull $IMAGE -- leaving the current deployment untouched."\n'
        "  exit 1\n"
        "fi\n"
        "\n"
        'docker rm -f "$APP" >/dev/null 2>&1 || true\n'
        f"{run_new}\n"
        "\n"
        f"{_health_block(recipe)}"
        "\n"
        'if [ -n "$healthy" ]; then\n'
        f'  echo "{success}"\n'
        "  exit 0\n"
        "fi\n"
        "\n"
        'echo "New container failed its health check. Recent logs:"\n'
        'docker logs --tail 50 "$APP" 2>&1 || true\n'
        'if [ -n "$PREV" ] && [ "$PREV" != "$IMAGE" ]; then\n'
        '  echo "Rolling back to previous image: $PREV"\n'
        '  docker rm -f "$APP" >/dev/null 2>&1 || true\n'
        f"  {run_prev}\n"
        '  echo "Rolled back to $PREV."\n'
        "else\n"
        '  echo "No previous image to roll back to (first deploy?)."\n'
        "fi\n"
        "exit 1\n"
    )


def _deploy_stage(owner: str, name: str, recipe: DeployRecipe, *, env_scoped: bool = False,
                  deploy_repo: str | None = None) -> dict:
    # GitOps: inject the GitHub token as a masked secret env var so the deploy script
    # can read environments/<env>.yaml from the deploy repo.
    env_vars = ([{"name": "GH_TOKEN", "type": "Secret", "value": identifier(TOKEN_SECRET_NAME)}]
                if deploy_repo else [])
    return {
        "stage": {
            "name": "Deploy",
            "identifier": "Deploy",
            "type": "Custom",
            "spec": {
                "execution": {
                    "steps": [
                        {
                            "step": {
                                "type": "ShellScript",
                                "name": "Deploy to laptop",
                                "identifier": "Deploy",
                                "timeout": "15m",
                                "spec": {
                                    "shell": "Bash",
                                    "onDelegate": True,
                                    "delegateSelectors": [DELEGATE_SELECTOR],
                                    "source": {
                                        "type": "Inline",
                                        "spec": {"script": build_deploy_script(owner, name, recipe, env_scoped=env_scoped, deploy_repo=deploy_repo)},
                                    },
                                    "environmentVariables": env_vars,
                                    "outputVariables": [],
                                },
                            }
                        }
                    ]
                }
            },
            "tags": {},
        }
    }


def _approval_stage() -> dict:
    return {
        "stage": {
            "name": "Approval",
            "identifier": "Approval",
            "type": "Approval",
            "spec": {
                "execution": {
                    "steps": [
                        {
                            "step": {
                                "type": "HarnessApproval",
                                "name": "Approve deploy",
                                "identifier": "Approve",
                                "timeout": "1d",
                                "spec": {
                                    "approvalMessage": "Approve deployment to the laptop?",
                                    "includePipelineExecutionHistory": True,
                                    "isAutoRejectEnabled": False,
                                    "approvers": {
                                        "userGroups": ["_project_all_users"],
                                        "minimumCount": 1,
                                        "disallowPipelineExecutor": False,
                                    },
                                    "approverInputs": [],
                                },
                            }
                        }
                    ]
                }
            },
            "tags": {},
        }
    }


def build_pipeline(owner: str, name: str, recipe: DeployRecipe, *, auto_deploy: bool, org: str, project: str,
                   env: str | None = None, deploy_repo: str | None = None) -> dict:
    """The Harness pipeline dict: (optional approval ->) deploy on the laptop delegate.

    Three shapes:

    * ``env=None`` (default) -> the in-repo pipeline, byte-for-byte unchanged:
      ``imageTag`` is the only input.
    * ``deploy_repo`` set (the deploy-repo / GitOps model) -> the container is
      env-scoped (``{app}-{env}``) and the image tag is read at run time from the
      deploy repo's ``environments/<env>.yaml``, so only ``env`` is an input.
    * ``env`` set without ``deploy_repo`` -> env-scoped container but the tag is still
      an ``imageTag`` input (a stepping stone; not used by the agent)."""
    env_scoped = env is not None
    gitops = deploy_repo is not None
    stages: list[dict] = []
    if not auto_deploy:
        stages.append(_approval_stage())
    stages.append(_deploy_stage(owner, name, recipe, env_scoped=env_scoped, deploy_repo=deploy_repo))
    if gitops:
        # GitOps: the tag is read from environments/<env>.yaml at run time, so the only
        # pipeline input is which environment to deploy.
        variables: list[dict] = [
            {"name": "env", "type": "String", "description": "target environment (dev/staging/prod)",
             "required": True, "value": "<+input>"}
        ]
    else:
        variables = [
            {"name": "imageTag", "type": "String", "description": "GHCR image tag (the CI commit SHA)",
             "required": True, "value": "<+input>"}
        ]
        if env_scoped:
            variables.append(
                {"name": "env", "type": "String", "description": "target environment (dev/staging/prod)",
                 "required": True, "value": "<+input>"}
            )
    return {
        "pipeline": {
            "name": f"Deploy {name}",
            "identifier": identifier(f"deploy_{name}"),
            "projectIdentifier": project,
            "orgIdentifier": org,
            "tags": {},
            "stages": stages,
            "variables": variables,
        }
    }


def build_pipeline_yaml(snapshot: RepoSnapshot, *, auto_deploy: bool = True,
                        recipe: DeployRecipe | None = None, allow_llm_fallback: bool = False,
                        env: str | None = None, deploy_repo: str | None = None) -> str:
    cfg = HarnessConfig()
    if recipe is None:
        # Resolve the deploy recipe (built-in shape, or LLM-authored when none
        # matches). Imported lazily to avoid an import cycle at module load.
        from .cd_generate import resolve_recipe

        recipe = resolve_recipe(snapshot, allow_llm_fallback=allow_llm_fallback)
    pipe = build_pipeline(
        snapshot.owner, snapshot.name, recipe,
        auto_deploy=auto_deploy, org=cfg.org, project=cfg.project, env=env, deploy_repo=deploy_repo,
    )
    return yaml.dump(pipe, sort_keys=False, default_flow_style=False, width=4096)


# --- Harness API ----------------------------------------------------------

def _raise_for(resp: httpx.Response, what: str) -> dict:
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:400]}
    if resp.status_code >= 300 or body.get("status") == "ERROR":
        msgs = body.get("responseMessages") or body.get("message") or body.get("raw")
        raise HarnessError(f"{what} failed ({resp.status_code}): {msgs}")
    return body


def create_pipeline(pipeline_yaml: str, cfg: HarnessConfig | None = None) -> str:
    """Create (or fail if exists) a pipeline from YAML. Returns its identifier."""
    cfg = (cfg or HarnessConfig()).require()
    resp = httpx.post(
        f"{cfg.pipeline_base}/pipelines/v2",
        params=cfg.scope, headers=cfg.headers(yaml_body=True),
        content=pipeline_yaml, timeout=60,
    )
    body = _raise_for(resp, "create pipeline")
    return (body.get("data") or {}).get("identifier", "")


def update_pipeline(pipeline_id: str, pipeline_yaml: str, cfg: HarnessConfig | None = None) -> None:
    """Update an existing pipeline's YAML (idempotent re-provisioning)."""
    cfg = (cfg or HarnessConfig()).require()
    resp = httpx.put(
        f"{cfg.pipeline_base}/pipelines/v2/{pipeline_id}",
        params=cfg.scope, headers=cfg.headers(yaml_body=True),
        content=pipeline_yaml, timeout=60,
    )
    _raise_for(resp, "update pipeline")


def upsert_pipeline(pipeline_yaml: str, pipeline_id: str, cfg: HarnessConfig | None = None) -> str:
    """Create the pipeline, or update it if it already exists."""
    cfg = (cfg or HarnessConfig()).require()
    try:
        return create_pipeline(pipeline_yaml, cfg)
    except HarnessError:
        update_pipeline(pipeline_id, pipeline_yaml, cfg)
        return pipeline_id


def execute_pipeline(pipeline_id: str, image_tag: str, cfg: HarnessConfig | None = None) -> str:
    """Trigger a run, passing the image tag as the runtime input. Returns planExecutionId."""
    cfg = (cfg or HarnessConfig()).require()
    inputs = yaml.dump(
        {"pipeline": {"identifier": pipeline_id,
                      "variables": [{"name": "imageTag", "type": "String", "value": image_tag}]}},
        sort_keys=False,
    )
    resp = httpx.post(
        f"{cfg.pipeline_base}/pipeline/execute/{pipeline_id}",
        params={**cfg.scope, "moduleType": "cd"},
        headers=cfg.headers(yaml_body=True), content=inputs, timeout=60,
    )
    body = _raise_for(resp, "execute pipeline")
    data = body.get("data") or {}
    return data.get("planExecutionId") or (data.get("planExecution") or {}).get("uuid", "")


def execution_status(plan_execution_id: str, cfg: HarnessConfig | None = None) -> str:
    """Current status of a run: Running/Success/Failed/Aborted/etc."""
    cfg = (cfg or HarnessConfig()).require()
    resp = httpx.get(
        f"{cfg.pipeline_base}/pipelines/execution/v2/{plan_execution_id}",
        params=cfg.scope, headers=cfg.headers(), timeout=30,
    )
    body = _raise_for(resp, "execution status")
    return ((body.get("data") or {}).get("pipelineExecutionSummary") or {}).get("status", "Unknown")


def wait_for_execution(
    plan_execution_id: str, cfg: HarnessConfig | None = None, *, timeout_s: int = 600, interval_s: int = 10
) -> str:
    """Poll until the run reaches a terminal status; return it."""
    cfg = cfg or HarnessConfig()
    terminal = {"Success", "Failed", "Aborted", "Errored", "Expired", "ApprovalRejected", "IgnoreFailed"}
    deadline = time.monotonic() + timeout_s
    status = "Unknown"
    while time.monotonic() < deadline:
        status = execution_status(plan_execution_id, cfg)
        if status in terminal:
            return status
        time.sleep(interval_s)
    return status


# --- Git storage: secret + GitHub connector + remote (Git-stored) pipeline ----
# For "pipeline stored in Git" (Harness Git Experience), Harness needs a GitHub
# connector to read/write the repo, which needs the GitHub token stored as a
# Harness secret. Then we create the pipeline as a REMOTE entity: Harness itself
# writes the YAML into the repo. The token is only ever sent to Harness, never logged.

TOKEN_SECRET_NAME = "cicd github token"
GITHUB_CONNECTOR_ID = "cicd_github"


def ensure_secret_text(name: str, value: str, cfg: HarnessConfig | None = None) -> str:
    """Create (or update) an inline text secret; returns its identifier."""
    cfg = (cfg or HarnessConfig()).require()
    ident = identifier(name)
    body = {"secret": {
        "type": "SecretText", "name": name, "identifier": ident,
        "orgIdentifier": cfg.org, "projectIdentifier": cfg.project,
        "spec": {"secretManagerIdentifier": "harnessSecretManager", "valueType": "Inline", "value": value},
    }}
    resp = httpx.post(f"{cfg.ng_base}/v2/secrets", params=cfg.scope, headers=cfg.headers(), json=body, timeout=30)
    if resp.status_code < 300 and resp.json().get("status") == "SUCCESS":
        return ident
    resp = httpx.put(f"{cfg.ng_base}/v2/secrets/{ident}", params=cfg.scope, headers=cfg.headers(), json=body, timeout=30)
    _raise_for(resp, "ensure secret")
    return ident


def ensure_github_connector(
    owner: str, cfg: HarnessConfig | None = None, *,
    token_ref: str | None = None, validation_repo: str | None = None,
) -> str:
    """Create (or update) an account-level GitHub connector; returns its identifier."""
    cfg = (cfg or HarnessConfig()).require()
    token_ref = token_ref or identifier(TOKEN_SECRET_NAME)
    spec = {
        "type": "Account", "url": f"https://github.com/{owner}",
        "authentication": {"type": "Http", "spec": {
            "type": "UsernameToken", "spec": {"username": owner, "tokenRef": token_ref}}},
        "apiAccess": {"type": "Token", "spec": {"tokenRef": token_ref}},
        "executeOnDelegate": False,
    }
    if validation_repo:
        spec["validationRepo"] = validation_repo
    body = {"connector": {
        "name": "cicd github", "identifier": GITHUB_CONNECTOR_ID, "type": "Github",
        "orgIdentifier": cfg.org, "projectIdentifier": cfg.project, "spec": spec,
    }}
    resp = httpx.post(f"{cfg.ng_base}/connectors", params=cfg.scope, headers=cfg.headers(), json=body, timeout=30)
    if resp.status_code < 300 and resp.json().get("status") == "SUCCESS":
        return GITHUB_CONNECTOR_ID
    resp = httpx.put(f"{cfg.ng_base}/connectors", params=cfg.scope, headers=cfg.headers(), json=body, timeout=30)
    _raise_for(resp, "ensure github connector")
    return GITHUB_CONNECTOR_ID


def create_pipeline_remote(
    pipeline_yaml: str, pipeline_id: str, *,
    connector_ref: str, repo: str, branch: str,
    file_path: str = HARNESS_PIPELINE_PATH,
    commit_msg: str = "cicd-bootstrap: add Harness deploy pipeline",
    cfg: HarnessConfig | None = None,
) -> str:
    """Create a REMOTE (Git-stored) pipeline: Harness writes the YAML into the repo."""
    cfg = (cfg or HarnessConfig()).require()
    params = {
        **cfg.scope, "storeType": "REMOTE", "connectorRef": connector_ref,
        "repoName": repo, "branch": branch, "filePath": file_path,
        "commitMsg": commit_msg, "isNewBranch": "false",
    }
    resp = httpx.post(
        f"{cfg.pipeline_base}/pipelines/v2",
        params=params, headers=cfg.headers(yaml_body=True), content=pipeline_yaml, timeout=60,
    )
    try:
        body = _raise_for(resp, "create remote pipeline")
    except HarnessError as exc:
        # Idempotent: if the pipeline is already stored in the repo, that's fine.
        low = str(exc).lower()
        if "already" in low or "duplicate" in low or "exist" in low:
            return pipeline_id
        raise
    return (body.get("data") or {}).get("identifier", "") or pipeline_id


def store_pipeline_in_repo(
    snapshot: RepoSnapshot, github_token: str, *,
    auto_deploy: bool = True, branch: str | None = None, cfg: HarnessConfig | None = None,
    allow_llm_fallback: bool = False, pipeline_yaml: str | None = None,
    repo: str | None = None,
) -> str:
    """One call: ensure the token secret + GitHub connector, then create the deploy
    pipeline as a Git-stored file in the repo (Harness commits ``.harness/deploy.yaml``).
    Returns the pipeline identifier. Pass ``pipeline_yaml`` to reuse an already-built
    pipeline (so the recipe is resolved once); otherwise it is built here.

    ``repo`` overrides where the pipeline is stored (defaults to the app repo,
    ``snapshot.name``). The deploy-repo model passes the ``{app}-deploy`` repo so the
    pipeline lives there instead of in the app repo."""
    cfg = (cfg or HarnessConfig()).require()
    target_repo = repo or snapshot.name
    branch = branch or snapshot.default_branch
    token_ref = ensure_secret_text(TOKEN_SECRET_NAME, github_token, cfg)
    connector = ensure_github_connector(snapshot.owner, cfg, token_ref=token_ref, validation_repo=target_repo)
    if pipeline_yaml is None:
        pipeline_yaml = build_pipeline_yaml(snapshot, auto_deploy=auto_deploy, allow_llm_fallback=allow_llm_fallback)
    pid = identifier(f"deploy_{snapshot.name}")
    return create_pipeline_remote(
        pipeline_yaml, pid, connector_ref=connector, repo=target_repo, branch=branch, cfg=cfg,
    )


# --- webhook trigger: "CI pings Harness to deploy" -------------------------
# Rather than have Harness poll GHCR (which needs a heavy service-based pipeline),
# CI pings a custom webhook at the end of a successful run, passing the image tag.
# The trigger maps the pinged tag onto the pipeline's imageTag input.

WEBHOOK_TRIGGER_ID = "ci_notify"


def ensure_webhook_trigger(pipeline_id: str, cfg: HarnessConfig | None = None, *,
                           trigger_id: str | None = None, branch: str = "main") -> str:
    """Create (or update) the custom webhook trigger on ``pipeline_id``; return its URL.

    Trigger identifiers are unique per *project*, so we derive a per-pipeline id.
    Remote (Git-stored) pipelines require ``pipelineBranchName`` so Harness can read
    the pipeline from Git to validate the trigger.
    """
    cfg = (cfg or HarnessConfig()).require()
    trigger_id = trigger_id or f"notify_{identifier(pipeline_id)}"
    input_yaml = yaml.dump(
        {"pipeline": {"identifier": pipeline_id,
                      "variables": [{"name": "imageTag", "type": "String", "value": "<+trigger.payload.tag>"}]}},
        sort_keys=False,
    )
    trig = {"trigger": {
        "name": "CI notify", "identifier": trigger_id, "enabled": True,
        "orgIdentifier": cfg.org, "projectIdentifier": cfg.project,
        "pipelineIdentifier": pipeline_id, "pipelineBranchName": branch,
        "source": {"type": "Webhook", "spec": {"type": "Custom",
                   "spec": {"payloadConditions": [], "headerConditions": []}}},
        "inputYaml": input_yaml,
    }}
    body = yaml.dump(trig, sort_keys=False, default_flow_style=False, width=4096)
    params = {**cfg.scope, "targetIdentifier": pipeline_id}
    resp = httpx.post(f"{cfg.pipeline_base}/triggers", params=params,
                      headers=cfg.headers(yaml_body=True), content=body, timeout=30)
    try:
        _raise_for(resp, "create webhook trigger")
    except HarnessError as exc:
        low = str(exc).lower()
        if "already" in low or "duplicate" in low or "exist" in low:  # idempotent -> update
            resp = httpx.put(f"{cfg.pipeline_base}/triggers/{trigger_id}", params=params,
                             headers=cfg.headers(yaml_body=True), content=body, timeout=30)
            _raise_for(resp, "update webhook trigger")
        else:
            raise
    return get_trigger_webhook_url(pipeline_id, trigger_id, cfg)


def get_trigger_webhook_url(pipeline_id: str, trigger_id: str | None = None,
                            cfg: HarnessConfig | None = None) -> str:
    cfg = (cfg or HarnessConfig()).require()
    trigger_id = trigger_id or f"notify_{identifier(pipeline_id)}"
    resp = httpx.get(f"{cfg.pipeline_base}/triggers/{trigger_id}",
                     params={**cfg.scope, "targetIdentifier": pipeline_id}, headers=cfg.headers(), timeout=30)
    body = _raise_for(resp, "get trigger")
    return (body.get("data") or {}).get("webhookUrl", "")


# GitHub Actions workflow that pings the Harness webhook after CI succeeds, so
# Harness deploys the image CI just pushed. Committed to the repo alongside CI.
NOTIFY_WORKFLOW_PATH = ".github/workflows/notify-harness.yml"


def render_notify_workflow(ci_workflow_name: str = "CI", branch: str = "main") -> str:
    """A tiny workflow: on CI success, POST the built SHA to the Harness webhook."""
    return f"""\
# Generated by cicd-bootstrap (Harness CD). Pings Harness to deploy the image CI
# just pushed. The webhook URL is stored as the HARNESS_WEBHOOK_URL repo secret.
name: Notify Harness
on:
  workflow_run:
    workflows: ["{ci_workflow_name}"]
    types: [completed]
jobs:
  notify:
    if: ${{{{ github.event.workflow_run.conclusion == 'success'
        && github.event.workflow_run.head_branch == '{branch}' }}}}
    runs-on: ubuntu-latest
    steps:
      - name: Ping Harness to deploy this image
        run: |
          curl -sS -X POST "${{{{ secrets.HARNESS_WEBHOOK_URL }}}}" \\
            -H "Content-Type: application/json" \\
            -d "{{\\"tag\\":\\"${{{{ github.event.workflow_run.head_sha }}}}\\"}}"
"""


def trigger_deploy(webhook_url: str, image_tag: str) -> bool:
    """Ping the pipeline's webhook to deploy a specific image tag -- exactly what the
    CI ``notify-harness`` workflow does. Lets us deploy the image already in GHCR."""
    resp = httpx.post(webhook_url, json={"tag": image_tag},
                      headers={"Content-Type": "application/json"}, timeout=30)
    return resp.status_code < 300


def deploy_via_harness(
    snapshot: RepoSnapshot, github_token: str, *,
    auto_deploy: bool = True, branch: str | None = None, cfg: HarnessConfig | None = None,
    allow_llm_fallback: bool = False,
) -> dict[str, str]:
    """Full Harness CD provisioning for a repo: secret + GitHub connector + the
    Git-stored deploy pipeline + the CI-notify webhook trigger. Returns
    ``{"pipeline_id", "webhook_url", "recipe", "pipeline_yaml"}``. Caller stores the
    URL as a repo secret and opens a PR adding the notify workflow (see
    core.add_cd_harness); ``recipe``/``pipeline_yaml`` let it report what was
    generated (which deploy shape, and whether the recipe was LLM-authored)."""
    cfg = (cfg or HarnessConfig()).require()
    branch = branch or snapshot.default_branch
    # Resolve the deploy recipe once so we can both provision it and report it.
    from .cd_generate import resolve_recipe

    recipe = resolve_recipe(snapshot, allow_llm_fallback=allow_llm_fallback)
    pipeline_yaml = build_pipeline_yaml(snapshot, auto_deploy=auto_deploy, recipe=recipe)
    pid = store_pipeline_in_repo(snapshot, github_token, auto_deploy=auto_deploy, branch=branch, cfg=cfg,
                                 pipeline_yaml=pipeline_yaml)
    webhook_url = ensure_webhook_trigger(pid, cfg, branch=branch)
    return {"pipeline_id": pid, "webhook_url": webhook_url, "recipe": recipe, "pipeline_yaml": pipeline_yaml}


# --- deploy-repo model: Harness watches the {app}-deploy repo ----------------
# Instead of CI pinging a custom webhook, Harness itself watches the deploy repo: a
# push to environments/** runs the pipeline. This is the "Harness watches the deploy
# repo" choice -- no extra service to host.

GITOPS_TRIGGER_PREFIX = "gitops"


def ensure_git_trigger(
    pipeline_id: str, connector_ref: str, repo: str, cfg: HarnessConfig | None = None, *,
    branch: str = "main", path_glob: str = "environments/**", env: str = "dev",
) -> str:
    """Create (or update) a GitHub *push* trigger on ``repo`` that runs ``pipeline_id``
    when files under ``environments/**`` change. Idempotent; returns the trigger id.

    The pipeline reads the image tag from ``environments/<env>.yaml`` itself (the deploy
    repo is the source of truth), so the trigger only needs to supply which ``env`` to
    deploy."""
    cfg = (cfg or HarnessConfig()).require()
    trigger_id = f"{GITOPS_TRIGGER_PREFIX}_{identifier(pipeline_id)}"
    input_yaml = yaml.dump(
        {"pipeline": {"identifier": pipeline_id, "variables": [
            {"name": "env", "type": "String", "value": env},
        ]}},
        sort_keys=False,
    )
    trig = {"trigger": {
        "name": "GitOps deploy", "identifier": trigger_id, "enabled": True,
        "orgIdentifier": cfg.org, "projectIdentifier": cfg.project,
        "pipelineIdentifier": pipeline_id, "pipelineBranchName": branch,
        "source": {"type": "Webhook", "spec": {"type": "Github", "spec": {
            "type": "Push",
            "spec": {
                "connectorRef": connector_ref,
                "repoName": repo,
                "autoAbortPreviousExecutions": False,
                "payloadConditions": [
                    {"key": "targetBranch", "operator": "Equals", "value": branch},
                    {"key": "changedFiles", "operator": "Contains", "value": path_glob},
                ],
                "headerConditions": [],
                "actions": [],
            },
        }}},
        "inputYaml": input_yaml,
    }}
    body = yaml.dump(trig, sort_keys=False, default_flow_style=False, width=4096)
    params = {**cfg.scope, "targetIdentifier": pipeline_id}
    resp = httpx.post(f"{cfg.pipeline_base}/triggers", params=params,
                      headers=cfg.headers(yaml_body=True), content=body, timeout=30)
    try:
        _raise_for(resp, "create git trigger")
    except HarnessError as exc:
        low = str(exc).lower()
        if "already" in low or "duplicate" in low or "exist" in low:  # idempotent -> update
            resp = httpx.put(f"{cfg.pipeline_base}/triggers/{trigger_id}", params=params,
                             headers=cfg.headers(yaml_body=True), content=body, timeout=30)
            _raise_for(resp, "update git trigger")
        else:
            raise
    return trigger_id
