"""Orchestration: repo_url -> BootstrapResult.

    ingest  ->  classify  ->  generate  ->  open PR

Each stage can fail; we convert failures into a structured BootstrapResult
(status="error") rather than raising, so callers always get context back --
including the classification/workflow produced before the failure.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from . import telemetry
from .classify import classify
from .config import load_dotenv, sonar_org, sonar_token
from .contracts import BootstrapResult, ChainResult, GeneratedWorkflow
from .deploy import generate_cd
from .generate import UnsupportedError, generate
from .github import (
    PROpenError,
    ci_image_pushed,
    ensure_environment_with_reviewer,
    latest_successful_ci_sha,
    merge_pr,
    open_pr,
    open_pr_files,
    resolve_token,
    set_repo_secret,
    wait_for_ci_success,  # re-exported: graph.py calls core.wait_for_ci_success (patched in tests)
)
from .ingest import IngestError, ingest, parse_repo_url
from .sonar import provision_project


def bootstrap(
    repo_url: str,
    *,
    open_pr_flag: bool = True,
    token: str | None = None,
    allow_llm_fallback: bool = False,
    merge: bool = False,
) -> BootstrapResult:
    """Run a bootstrap and record one telemetry event (recording never fails it)."""
    start = time.monotonic()
    result = _run(
        repo_url, open_pr_flag=open_pr_flag, token=token,
        allow_llm_fallback=allow_llm_fallback, merge=merge,
    )
    try:
        telemetry.record(result, int((time.monotonic() - start) * 1000))
    except Exception as exc:  # telemetry is best-effort; never break the bootstrap
        print(f"[telemetry] could not record event: {exc}")
    return result


def _run(
    repo_url: str,
    *,
    open_pr_flag: bool = True,
    token: str | None = None,
    allow_llm_fallback: bool = False,
    merge: bool = False,
) -> BootstrapResult:
    load_dotenv()
    token = token or resolve_token()

    with tempfile.TemporaryDirectory(prefix="cicd-bootstrap-") as tmp:
        workdir = Path(tmp)

        # 1. Ingest
        try:
            snapshot = ingest(repo_url, workdir, token=token)
        except IngestError as exc:
            return BootstrapResult(repo_url=repo_url, status="error", message=str(exc))

        # 2. Classify
        try:
            classification = classify(snapshot)
        except Exception as exc:
            return BootstrapResult(repo_url=repo_url, status="error", message=f"classification failed: {exc}")

        # 3. Generate (optionally via the LLM cookbook fallback)
        try:
            workflow = generate(classification, snapshot, allow_llm_fallback=allow_llm_fallback)
        except UnsupportedError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", classification=classification, message=str(exc)
            )
        except Exception as exc:  # LLM fallback failure (AuthorError, API errors, ...)
            return BootstrapResult(
                repo_url=repo_url, status="error", classification=classification,
                message=f"LLM cookbook fallback failed: {exc}",
            )

        # 4. Open PR (unless we were asked only to generate)
        if not open_pr_flag:
            return BootstrapResult(
                repo_url=repo_url, status="generated", classification=classification,
                workflow=workflow, message="workflow generated (no PR requested)",
            )
        if not token:
            return BootstrapResult(
                repo_url=repo_url, status="error", classification=classification, workflow=workflow,
                message="no GitHub token available to open a PR (set GH_TOKEN or run `gh auth login`)",
            )

        # 3.5 Set up SonarCloud so the repo's FIRST scan works, with no manual
        # steps: create the project (Automatic Analysis off by default) and bake
        # the token into the repo's Actions secrets.
        sonar_project = _provision_sonar_project(snapshot)
        sonar_secret_set = _inject_sonar_secret(snapshot, token)

        clone_dir = workdir / snapshot.name
        try:
            pr_number, pr_url, branch = open_pr(snapshot, workflow, clone_dir, token)
        except PROpenError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", classification=classification, workflow=workflow,
                sonar_secret_set=sonar_secret_set, sonar_project=sonar_project, message=str(exc),
            )

        merged, merge_sha, merge_msg = _maybe_merge(snapshot, pr_number, token, merge)
        return BootstrapResult(
            repo_url=repo_url, status="opened", classification=classification, workflow=workflow,
            branch=branch, pr_number=pr_number, pr_url=pr_url,
            sonar_secret_set=sonar_secret_set, sonar_project=sonar_project,
            merged=merged, merge_sha=merge_sha,
            message=f"opened PR #{pr_number}{merge_msg}",
        )


def _provision_sonar_project(snapshot) -> str | None:
    """Create the repo's SonarCloud project so its first scan doesn't fail.
    Returns "created"/"exists", "error", or None if Sonar isn't configured."""
    org, token = sonar_org(), sonar_token()
    if not (org and token):
        return None
    key = f"{snapshot.owner}_{snapshot.name}"
    try:
        status = provision_project(org, key, snapshot.name, token)
        print(f"[bootstrap] SonarCloud project {key}: {status}")
        return status
    except Exception as exc:  # non-fatal: PR still opens; first scan may need manual setup
        print(f"[bootstrap] could not provision SonarCloud project {key}: {exc}")
        return "error"


def _inject_sonar_secret(snapshot, token: str) -> bool | None:
    """Write SONAR_TOKEN into the repo's Actions secrets when the service holds
    one. Returns True/False on attempt, or None if no token is configured."""
    value = sonar_token()
    if not value:
        return None
    try:
        set_repo_secret(snapshot.owner, snapshot.name, "SONAR_TOKEN", value, token)
        print(f"[bootstrap] set SONAR_TOKEN secret on {snapshot.owner}/{snapshot.name}")
        return True
    except Exception as exc:  # non-fatal: the PR still opens; Sonar just stays skipped
        print(f"[bootstrap] could not set SONAR_TOKEN on {snapshot.owner}/{snapshot.name}: {exc}")
        return False


# --- CD (deploy) -----------------------------------------------------------
# The delivery counterpart to bootstrap(): ingest -> generate deploy.yml -> open PR.
# There is no classification step (deploying a container is language-agnostic)
# and no LLM call. When the approval gate is requested (auto_deploy=False) we
# also provision the repo's `production` environment with a required reviewer,
# best-effort, so the "click to approve" pause actually takes effect.

def add_cd(
    repo_url: str,
    *,
    open_pr_flag: bool = True,
    token: str | None = None,
    auto_deploy: bool = False,
    auto_handoff: bool = True,
    merge: bool = False,
) -> BootstrapResult:
    """Run a CD bootstrap and record one telemetry event (recording never fails it)."""
    start = time.monotonic()
    result = _run_cd(
        repo_url, open_pr_flag=open_pr_flag, token=token,
        auto_deploy=auto_deploy, auto_handoff=auto_handoff, merge=merge,
    )
    result.kind = "cd"  # so telemetry can tell CD runs apart from CI
    # "blocked" means CI hasn't produced an image yet -- a no-op, not a real run,
    # so we don't record it as a pipeline outcome.
    if result.status != "blocked":
        try:
            telemetry.record(result, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # telemetry is best-effort; never break the bootstrap
            print(f"[telemetry] could not record event: {exc}")
    return result


def _run_cd(
    repo_url: str,
    *,
    open_pr_flag: bool = True,
    token: str | None = None,
    auto_deploy: bool = False,
    auto_handoff: bool = True,
    merge: bool = False,
) -> BootstrapResult:
    load_dotenv()
    token = token or resolve_token()

    with tempfile.TemporaryDirectory(prefix="cicd-bootstrap-") as tmp:
        workdir = Path(tmp)

        # 1. Ingest (we need owner/name/default_branch and any Dockerfile port).
        try:
            snapshot = ingest(repo_url, workdir, token=token)
        except IngestError as exc:
            return BootstrapResult(repo_url=repo_url, status="error", message=str(exc))

        # 1.5 Handoff gate: CD only makes sense once CI has actually pushed an image.
        # We gate the PR path on that (dry-run previews with open_pr=False still work,
        # so you can inspect the deploy.yml before CI has run).
        if open_pr_flag and token and not _image_available(snapshot, token):
            return BootstrapResult(
                repo_url=repo_url, status="blocked", kind="cd",
                message=(
                    f"CI hasn't produced an image yet — no successful CI run found on "
                    f"'{snapshot.default_branch}'. Merge the CI pull request and let CI run "
                    f"(it pushes the image to GHCR), then set up CD."
                ),
            )

        # 2. Generate deploy.yml (deterministic; no classify, no LLM).
        workflow = generate_cd(snapshot, auto_deploy=auto_deploy, auto_handoff=auto_handoff)
        gate = "automatic (no gate)" if auto_deploy else "manual approval"

        # 3. Open PR (unless we were asked only to generate).
        if not open_pr_flag:
            return BootstrapResult(
                repo_url=repo_url, status="generated", workflow=workflow, cd_gate=gate,
                message="CD workflow generated (no PR requested)",
            )
        if not token:
            return BootstrapResult(
                repo_url=repo_url, status="error", workflow=workflow, cd_gate=gate,
                message="no GitHub token available to open a PR (set GH_TOKEN or run `gh auth login`)",
            )

        # 3.5 Set up the approval gate (manual mode only), so the pause is real.
        if not auto_deploy:
            gate = _provision_cd_gate(snapshot, token)

        clone_dir = workdir / snapshot.name
        try:
            pr_number, pr_url, branch = open_pr_files(
                snapshot, [(workflow.path, workflow.content)], clone_dir, token,
                branch_prefix="cicd-bootstrap/add-cd",
                title="cd: add CD deploy workflow (via cicd-bootstrap)",
                body=_cd_pr_body(snapshot, workflow, auto_deploy, auto_handoff),
                commit_message="cd: add CD deploy workflow (pull image, recreate, health-check, rollback)",
            )
        except PROpenError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", workflow=workflow, cd_gate=gate, message=str(exc),
            )

        merged, merge_sha, merge_msg = _maybe_merge(snapshot, pr_number, token, merge)
        return BootstrapResult(
            repo_url=repo_url, status="opened", workflow=workflow, cd_gate=gate,
            branch=branch, pr_number=pr_number, pr_url=pr_url,
            merged=merged, merge_sha=merge_sha,
            message=f"opened PR #{pr_number}{merge_msg}",
        )


def _image_available(snapshot, token: str) -> bool:
    """True once CI has pushed an image for this repo (the CD handoff gate)."""
    return ci_image_pushed(snapshot.owner, snapshot.name, snapshot.default_branch, token)


def _provision_cd_gate(snapshot, token: str) -> str:
    """Configure the repo's `production` environment with the owner as a required
    reviewer, so the CD job's `environment: production` pauses for approval.
    Non-fatal: the PR still opens even if the gate can't be configured."""
    try:
        ensure_environment_with_reviewer(snapshot.owner, snapshot.name, "production", token)
        print(f"[bootstrap] configured 'production' approval gate on {snapshot.owner}/{snapshot.name}")
        return "manual approval (production environment, reviewer required)"
    except Exception as exc:  # e.g. private repo on a free plan, or missing admin
        print(f"[bootstrap] could not configure the approval gate: {exc}")
        return ("manual approval — but the gate isn't configured yet; add yourself as a required "
                "reviewer on the repo's 'production' environment (Settings → Environments)")


def _cd_pr_body(snapshot, workflow, auto_deploy: bool, auto_handoff: bool = True) -> str:
    gate = (
        "Deploys **automatically** to production once triggered."
        if auto_deploy else
        "Waits for your **click-to-approve** (the `production` environment gate) before deploying."
    )
    handoff = (
        f"**Auto-handoff:** runs automatically when the **CI** workflow finishes on "
        f"`{snapshot.default_branch}` (and can also be run by hand)."
        if auto_handoff else
        "**Manual handoff:** does not run after CI on its own — trigger it yourself from "
        "**Actions → CD → Run workflow**."
    )
    return (
        "## 🚀 cicd-bootstrap (CD)\n\n"
        f"Adds `{workflow.path}` to **{snapshot.owner}/{snapshot.name}**.\n\n"
        "It pulls the image CI built from GHCR and runs it on your **self-hosted runner** using a "
        "**recreate** strategy, with a **health check** and automatic **rollback** to the previous "
        "image if the new one is unhealthy.\n\n"
        f"{handoff}\n\n"
        f"{gate}\n\n"
        "**Requires:** a self-hosted runner online with labels `self-hosted, laptop`, and Docker "
        "installed on it.\n\n"
        "Review and merge if it looks right."
    )


def _maybe_merge(snapshot, pr_number: int, token: str, merge: bool):
    """Auto-merge the just-opened PR if requested. Returns (merged, merge_sha, msg_suffix)."""
    if not merge:
        return False, None, ""
    try:
        sha = merge_pr(snapshot.owner, snapshot.name, pr_number, token)
        print(f"[bootstrap] auto-merged PR #{pr_number} on {snapshot.owner}/{snapshot.name}")
        return True, sha, " and merged it"
    except Exception as exc:  # non-fatal: the PR stays open for a manual merge
        print(f"[bootstrap] could not auto-merge PR #{pr_number}: {exc}")
        return False, None, f" (auto-merge failed: {exc})"


# --- the full chain: CI -> image -> CD, from one call ------------------------
# Driven from the user's laptop (the agents live here) because the image push
# happens in GitHub's cloud, which can't reach in to start the local CD agent.

def run_ci_then_cd(
    repo_url: str,
    *,
    token: str | None = None,
    allow_llm_fallback: bool = False,
    auto_deploy: bool = True,
    wait_timeout_s: int = 900,
) -> ChainResult:
    """One click: open+merge the CI PR, wait for CI to push the image, then
    open+merge the CD PR. The deploy then runs on its own (auto-handoff) on the
    build that the CD merge triggers. Waits on GitHub, so it takes a few minutes."""
    token = token or resolve_token()
    owner, name = parse_repo_url(repo_url)

    # Orchestrated as a LangGraph StateGraph (see graph.py): ci -> wait -> cd,
    # with conditional branching and a checkpointer. Imported lazily to avoid an
    # import cycle. The graph nodes call bootstrap/wait_for_ci_success/
    # add_cd_harness on this module at call time (so mocks/patches still apply).
    import uuid

    from . import graph

    final = graph.build_cicd_graph().invoke(
        {
            "repo_url": repo_url, "token": token, "owner": owner, "name": name,
            "allow_llm_fallback": allow_llm_fallback, "auto_deploy": auto_deploy,
            "wait_timeout_s": wait_timeout_s, "image_ready": False,
        },
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )
    return ChainResult(
        repo_url=repo_url,
        ci=final.get("ci"),
        image_ready=bool(final.get("image_ready")),
        cd=final.get("cd"),
        message=final.get("message", ""),
    )


# --- CD via Harness --------------------------------------------------------
# The alternative deploy target: instead of a GitHub Actions deploy.yml, provision
# a Harness pipeline (stored in the repo as .harness/deploy.yaml, written by
# Harness itself) plus a webhook trigger, and open a PR adding a tiny "notify
# Harness" workflow so CI pings Harness to deploy after each successful build.

def add_cd_harness(
    repo_url: str,
    *,
    token: str | None = None,
    auto_deploy: bool = True,
    open_pr_flag: bool = True,
    allow_llm_fallback: bool = False,
    deploy_model: str | None = None,
    env: str = "dev",
    create_missing_repo: bool = False,
) -> BootstrapResult:
    """Set up CD-via-Harness for a repo. Requires HARNESS_* in .env (see .env.example).

    ``allow_llm_fallback`` lets the LLM work out the deploy port when the repo's
    Dockerfile has no ``EXPOSE`` (for unusual apps); otherwise it defaults to 8080.

    ``deploy_model`` selects the CD model; when ``None`` it takes the configured default
    (``CD_DEPLOY_MODEL``, shipped as ``in-repo``). Phase 4 cut-over = set
    ``CD_DEPLOY_MODEL=deploy-repo`` once the deploy-repo model is live-proven (reversible).

    * ``deploy-repo`` routes to the per-app deploy-repo (GitOps) model
      (:func:`cd_deploy_repo.add_cd_deploy_repo`).
    * ``in-repo`` stores the pipeline in the app repo (the body below, unchanged).
    """
    if deploy_model is None:
        from .cd_config import load_cd_config

        deploy_model = load_cd_config().deploy_model
    if deploy_model == "deploy-repo":
        from .cd_deploy_repo import add_cd_deploy_repo

        return add_cd_deploy_repo(
            repo_url, env=env, token=token, auto_deploy=auto_deploy,
            allow_llm_fallback=allow_llm_fallback, create_missing_repo=create_missing_repo,
        )

    from . import harness  # local import: Harness is optional; only needed on this path

    load_dotenv()
    token = token or resolve_token()

    with tempfile.TemporaryDirectory(prefix="cicd-bootstrap-") as tmp:
        workdir = Path(tmp)
        try:
            snapshot = ingest(repo_url, workdir, token=token)
        except IngestError as exc:
            return BootstrapResult(repo_url=repo_url, status="error", kind="cd", message=str(exc))

        # Same image gate as the GitHub Actions CD: deploying only makes sense once
        # CI has actually pushed an image to GHCR.
        if token and not _image_available(snapshot, token):
            return BootstrapResult(
                repo_url=repo_url, status="blocked", kind="cd",
                message=(
                    f"CI hasn't produced an image yet — no successful CI run found on "
                    f"'{snapshot.default_branch}'. Merge the CI pull request and let CI run, then set up Harness CD."
                ),
            )

        from .cd_author import AuthorError
        from .cd_generate import UnsupportedDeployError

        try:
            provisioned = harness.deploy_via_harness(
                snapshot, token or "", auto_deploy=auto_deploy, allow_llm_fallback=allow_llm_fallback,
            )
        except UnsupportedDeployError as exc:
            # No built-in template clearly fits this repo and the fallback was off.
            # Inform the user rather than guessing a deploy shape (mirrors CI).
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd",
                message=f"{exc} (tick 'LLM authors a deploy recipe' on the CD agent to have one written).",
            )
        except AuthorError as exc:
            # The fallback was asked to author a recipe for a repo that fits no
            # built-in template, but the LLM call couldn't produce one. Surface it
            # rather than silently guessing.
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd",
                message=f"couldn't author a deploy recipe for this repo: {exc}",
            )
        except harness.HarnessError as exc:
            return BootstrapResult(
                repo_url=repo_url, status="error", kind="cd",
                message=f"Harness provisioning failed: {exc}",
            )
        pid, webhook_url = provisioned["pipeline_id"], provisioned["webhook_url"]
        recipe = provisioned["recipe"]

        # Summarise what was generated (which deploy shape, LLM-authored or not) so
        # the UI/telemetry show it exactly as they do for a CI cookbook.
        workflow = GeneratedWorkflow(
            path=harness.HARNESS_PIPELINE_PATH,
            content=provisioned["pipeline_yaml"],
            cookbook=recipe.key,
            phases=["pull", "deploy", "health-check", "rollback"],
            llm_authored=recipe.llm_authored,
            llm_input_tokens=recipe.llm_input_tokens,
            llm_output_tokens=recipe.llm_output_tokens,
        )

        # Store the webhook URL as a repo secret so the notify workflow can ping it.
        try:
            set_repo_secret(snapshot.owner, snapshot.name, "HARNESS_WEBHOOK_URL", webhook_url, token)
        except Exception as exc:  # non-fatal: the PR still opens; the user can add the secret by hand
            print(f"[harness] could not set HARNESS_WEBHOOK_URL secret: {exc}")

        # Deploy the image that's ALREADY in GHCR now (ping the webhook with the
        # latest CI image tag), so pointing the CD UI at a repo deploys immediately
        # instead of waiting for the next CI run.
        deploy_note = ""
        tag = latest_successful_ci_sha(snapshot.owner, snapshot.name, snapshot.default_branch, token) if token else None
        if tag:
            try:
                harness.trigger_deploy(webhook_url, tag)
                if recipe.publish_port:
                    deploy_note = (f" Triggered a deploy of the current image (tag {tag[:7]}) on your laptop "
                                   f"delegate — it should come up at http://localhost:{recipe.port}/ shortly.")
                else:
                    deploy_note = (f" Triggered a deploy of the current image (tag {tag[:7]}) on your laptop "
                                   f"delegate — the '{recipe.key}' worker should be running shortly.")
            except Exception as exc:  # non-fatal: provisioning still succeeded
                deploy_note = f" (couldn't auto-trigger the deploy: {exc})"

        gate = "automatic (no gate)" if auto_deploy else "manual approval (Harness approval stage)"
        notify = harness.render_notify_workflow("CI", snapshot.default_branch)
        if not open_pr_flag:
            return BootstrapResult(
                repo_url=repo_url, status="generated", kind="cd", cd_gate=gate, workflow=workflow,
                message=f"Harness pipeline '{pid}' created (stored in the repo as {harness.HARNESS_PIPELINE_PATH}).{deploy_note}",
            )

        clone_dir = workdir / snapshot.name
        try:
            pr_number, pr_url, branch = open_pr_files(
                snapshot, [(harness.NOTIFY_WORKFLOW_PATH, notify)], clone_dir, token,
                branch_prefix="cicd-bootstrap/add-harness-cd",
                title="cd: deploy via Harness (notify workflow, via cicd-bootstrap)",
                body=_harness_pr_body(snapshot, pid, auto_deploy),
                commit_message="cd: ping Harness to deploy after CI (Harness CD)",
            )
        except PROpenError as exc:
            return BootstrapResult(repo_url=repo_url, status="error", kind="cd", cd_gate=gate,
                                   workflow=workflow, message=str(exc))

        return BootstrapResult(
            repo_url=repo_url, status="opened", kind="cd", cd_gate=gate, workflow=workflow,
            branch=branch, pr_number=pr_number, pr_url=pr_url,
            message=(
                f"opened PR #{pr_number} — Harness pipeline '{pid}' is stored in the repo "
                f"(.harness/deploy.yaml).{deploy_note}"
            ),
        )


def _harness_pr_body(snapshot, pipeline_id: str, auto_deploy: bool) -> str:
    gate = ("Deploys **automatically** once triggered."
            if auto_deploy else
            "Waits for a **click-to-approve** in Harness before deploying.")
    return (
        "## 🚀 cicd-bootstrap (CD via Harness)\n\n"
        f"Sets up **Harness** to deploy **{snapshot.owner}/{snapshot.name}** to your laptop.\n\n"
        f"- The deploy pipeline is stored in this repo at `{'.harness/deploy.yaml'}` (written by Harness).\n"
        f"- This PR adds `.github/workflows/notify-harness.yml`: after **CI** succeeds it pings a Harness "
        "webhook (URL kept in the `HARNESS_WEBHOOK_URL` repo secret) so Harness pulls the new image and "
        "runs it on your `laptop` delegate, with a health check and automatic rollback.\n\n"
        f"{gate}\n\n"
        "**Requires:** the Harness delegate online on your laptop (`bash scripts/add-harness-delegate.sh`).\n\n"
        "Review and merge if it looks right."
    )
