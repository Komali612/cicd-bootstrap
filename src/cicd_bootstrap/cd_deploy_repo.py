"""CD via a per-app **deploy repository** (the GitOps model).

The alternative to :func:`cicd_bootstrap.core.add_cd_harness` (which stores the
Harness pipeline inside the *app* repo). Here the CD agent:

1. makes sure a separate ``{app}-deploy`` repo exists (created from the
   ``_deploy-template`` GitHub template repo),
2. provisions the Harness pipeline **inside that deploy repo**
   (``.harness/deploy.yaml``), env-parameterised,
3. ensures Harness **watches** the deploy repo (a Git trigger on
   ``environments/**``),
4. opens a PR that bumps the image ``tag`` in ``environments/<env>.yaml`` --
   and **never merges it**. Merging is the deploy signal.

Creating a repo is outward-facing, so it only happens when the caller passes
``create_missing_repo=True`` (a confirmed action in the UI); otherwise a missing
deploy repo returns a ``blocked`` result asking for confirmation.

This path is opt-in behind the CD agent's ``deploy-repo`` toggle; the default
in-repo path is unchanged.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .config import load_dotenv
from .contracts import BootstrapResult, GeneratedWorkflow
from .github import (
    PROpenError,
    ci_image_pushed,
    create_repo_from_template,
    latest_successful_ci_sha,
    open_pr_in_repo,
    repo_exists,
    resolve_token,
)
from .ingest import IngestError, ingest

TEMPLATE_REPO = "_deploy-template"
DEPLOY_REPO_SUFFIX = "-deploy"
# Deploy repos are created with "main" as the default branch (GitHub's default for
# newly generated repos); Phase 2 confirms this against a real template.
DEPLOY_BRANCH = "main"


def deploy_repo_name(app: str) -> str:
    return f"{app}{DEPLOY_REPO_SUFFIX}"


def _env_file_yaml(app: str, owner: str, env: str, tag: str, port: int) -> str:
    """The desired-state file the agent writes into environments/<env>.yaml."""
    return (
        f"# Desired deploy state for {app} in {env}. The CD agent bumps `tag`; merging\n"
        f"# this file triggers Harness to deploy the image on the laptop delegate.\n"
        f"app: {app}\n"
        f"env: {env}\n"
        f"image: ghcr.io/{owner.lower()}/{app.lower()}\n"
        f"tag: {tag}\n"
        f"port: {port}\n"
    )


def add_cd_deploy_repo(
    repo_url: str,
    *,
    env: str = "dev",
    token: str | None = None,
    auto_deploy: bool = True,
    allow_llm_fallback: bool = False,
    create_missing_repo: bool = False,
    template_repo: str = TEMPLATE_REPO,
) -> BootstrapResult:
    """Set up CD for a repo using the per-app deploy-repo model. Requires HARNESS_*
    in .env and a GitHub token that can create repositories (see
    templates/DEPLOY_TEMPLATE_SETUP.md)."""
    from . import harness  # local import: Harness is optional; only needed on this path

    load_dotenv()
    token = token or resolve_token()
    if not token:
        return BootstrapResult(
            repo_url=repo_url, status="error", kind="cd",
            message="A GitHub token is required (set GITHUB_TOKEN in .env).",
        )

    with tempfile.TemporaryDirectory(prefix="cicd-bootstrap-") as tmp:
        try:
            snapshot = ingest(repo_url, Path(tmp), token=token)
        except IngestError as exc:
            return BootstrapResult(repo_url=repo_url, status="error", kind="cd", message=str(exc))

        owner, app = snapshot.owner, snapshot.name
        deploy_repo = deploy_repo_name(app)

        # Same image gate as the in-repo path: deploying only makes sense once CI has
        # actually pushed an image to GHCR.
        if not ci_image_pushed(owner, app, snapshot.default_branch, token):
            return BootstrapResult(
                repo_url=repo_url, status="blocked", kind="cd",
                message=(
                    f"CI hasn't produced an image yet — no successful CI run found on "
                    f"'{snapshot.default_branch}'. Merge the CI pull request and let CI run, "
                    f"then set up CD."
                ),
            )

        # Resolve the deploy recipe (image shape / port / health), reusing the same
        # engine as the in-repo path so behaviour matches.
        from .cd_author import AuthorError
        from .cd_generate import UnsupportedDeployError, resolve_recipe

        try:
            recipe = resolve_recipe(snapshot, allow_llm_fallback=allow_llm_fallback)
        except UnsupportedDeployError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd",
                message=f"{exc} (tick 'LLM authors a deploy recipe' to have one written).",
            )
        except AuthorError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd",
                message=f"couldn't author a deploy recipe for this repo: {exc}",
            )

        # Ensure the {app}-deploy repo exists. Creating a repo is outward-facing, so we
        # only do it on an explicit, confirmed request; otherwise ask for confirmation.
        created = False
        if not repo_exists(owner, deploy_repo, token):
            if not create_missing_repo:
                return BootstrapResult(
                    repo_url=repo_url, status="blocked", kind="cd",
                    message=(
                        f"No deploy repo yet. Confirm to create '{owner}/{deploy_repo}' from the "
                        f"'{template_repo}' template (re-run with 'create the deploy repo' ticked)."
                    ),
                )
            try:
                create_repo_from_template(owner, template_repo, owner, deploy_repo, token)
                created = True
            except PROpenError as exc:
                return BootstrapResult(
                    repo_url=repo_url, status="error", kind="cd",
                    message=(
                        f"Couldn't create '{owner}/{deploy_repo}' from '{template_repo}': {exc}. "
                        f"Is the template repo set up and can the token create repos? "
                        f"(see templates/DEPLOY_TEMPLATE_SETUP.md)"
                    ),
                )

        # Provision the Harness pipeline INSIDE the deploy repo (env-parameterised),
        # then have Harness watch the deploy repo.
        try:
            pipeline_yaml = harness.build_pipeline_yaml(
                snapshot, auto_deploy=auto_deploy, recipe=recipe, env=env, deploy_repo=deploy_repo,
            )
            pid = harness.store_pipeline_in_repo(
                snapshot, token, auto_deploy=auto_deploy, branch=DEPLOY_BRANCH,
                pipeline_yaml=pipeline_yaml, repo=deploy_repo,
            )
        except harness.HarnessError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd",
                message=f"Harness provisioning failed: {exc}",
            )

        trigger_note = ""
        try:
            harness.ensure_git_trigger(
                pid, harness.GITHUB_CONNECTOR_ID, deploy_repo, branch=DEPLOY_BRANCH, env=env,
            )
        except harness.HarnessError as exc:  # non-fatal: the PR still opens
            trigger_note = f" (couldn't set up the Harness git trigger yet: {exc})"

        gate = "automatic (no gate)" if auto_deploy else "manual approval (Harness approval stage)"
        workflow = GeneratedWorkflow(
            path=f"{deploy_repo}/{harness.HARNESS_PIPELINE_PATH}",
            content=pipeline_yaml,
            cookbook=recipe.key,
            phases=["pull", "deploy", "health-check", "rollback"],
            llm_authored=recipe.llm_authored,
            llm_input_tokens=recipe.llm_input_tokens,
            llm_output_tokens=recipe.llm_output_tokens,
        )

        # Open the tag-bump PR into the deploy repo (never merged -- merging deploys).
        tag = latest_successful_ci_sha(owner, app, snapshot.default_branch, token) or ""
        env_path = f"environments/{env}.yaml"
        content = _env_file_yaml(app, owner, env, tag, recipe.port)
        try:
            pr_number, pr_url, branch = open_pr_in_repo(
                owner, deploy_repo, [(env_path, content)], token,
                base=DEPLOY_BRANCH,
                branch_prefix=f"cicd-bootstrap/deploy-{env}",
                title=f"deploy({app}): {env} → {tag[:7]}",
                body=(
                    f"Set **{app}** in **{env}** to `{tag[:12]}`.\n\n"
                    f"Image: `ghcr.io/{owner.lower()}/{app.lower()}:{tag[:12]}`\n\n"
                    f"**Merging this PR deploys it** — Harness watches this repo. "
                    f"The CD agent does not merge its own PR."
                ),
                commit_message=f"deploy({app}): {env} -> {tag[:7]}",
            )
        except PROpenError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd", cd_gate=gate, workflow=workflow,
                message=(
                    f"Provisioned the pipeline in '{owner}/{deploy_repo}', but couldn't open the "
                    f"tag-bump PR: {exc}"
                ),
            )

        created_note = f" Created the deploy repo '{owner}/{deploy_repo}'." if created else ""
        return BootstrapResult(
            repo_url=repo_url, status="opened", kind="cd", cd_gate=gate, workflow=workflow,
            pr_number=pr_number, pr_url=pr_url, branch=branch,
            message=(
                f"Opened a deploy PR in '{owner}/{deploy_repo}' setting {env} → {tag[:7]}. "
                f"Merge it to deploy (Harness watches the repo).{created_note}{trigger_note}"
            ),
        )
