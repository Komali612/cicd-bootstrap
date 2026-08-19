"""Open a pull request that adds the generated workflow.

We commit the workflow on a fresh branch in the already-cloned repo, push it
with a token-authenticated remote, and open a PR via the GitHub REST API. We
assume the token can push to the repo; if it cannot, the push fails and we
raise -- we do not fork or work around it.

The token is only ever placed in the push remote URL, never printed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from .contracts import GeneratedWorkflow, RepoSnapshot

API = "https://api.github.com"
BRANCH_PREFIX = "cicd-bootstrap/add-ci"


class PROpenError(Exception):
    """Raised when the branch cannot be pushed or the PR cannot be opened."""


class SecretError(Exception):
    """Raised when a repository Actions secret cannot be written."""


class EnvSetupError(Exception):
    """Raised when a deployment environment / its protection rules can't be set."""


def set_repo_secret(owner: str, name: str, secret_name: str, value: str, token: str) -> None:
    """Store `value` as an Actions secret on owner/name.

    GitHub requires the value encrypted for the repo's Actions public key with a
    libsodium sealed box. Needs a token with admin (secrets:write) on the repo.
    The secret value is never logged.
    """
    key = httpx.get(
        f"{API}/repos/{owner}/{name}/actions/secrets/public-key",
        headers=_headers(token), timeout=30,
    )
    if key.status_code >= 300:
        raise SecretError(f"fetch public key failed: {key.status_code} {key.text[:200]}")
    pub = key.json()
    payload = {"encrypted_value": _seal(pub["key"], value), "key_id": pub["key_id"]}
    put = httpx.put(
        f"{API}/repos/{owner}/{name}/actions/secrets/{secret_name}",
        headers=_headers(token), json=payload, timeout=30,
    )
    if put.status_code >= 300:
        raise SecretError(f"set secret {secret_name} failed: {put.status_code} {put.text[:200]}")


def get_authenticated_user_id(token: str) -> int:
    """The numeric id of the token's user -- needed to add them as a reviewer."""
    r = httpx.get(f"{API}/user", headers=_headers(token), timeout=30)
    if r.status_code >= 300:
        raise EnvSetupError(f"could not read authenticated user: {r.status_code} {r.text[:200]}")
    return int(r.json()["id"])


def ensure_environment_with_reviewer(owner: str, name: str, environment: str, token: str) -> None:
    """Create/update a repo deployment ``environment`` and require the token's own
    user to approve deployments to it -- this is the CD "click to approve" gate.

    Needs a token with repo admin. On plans/repos where environment protection
    rules aren't available (e.g. private repos on the free plan), GitHub rejects
    the reviewers and we raise; the caller treats that as non-fatal.
    """
    uid = get_authenticated_user_id(token)
    body = {"wait_timer": 0, "reviewers": [{"type": "User", "id": uid}]}
    r = httpx.put(
        f"{API}/repos/{owner}/{name}/environments/{environment}",
        headers=_headers(token), json=body, timeout=30,
    )
    if r.status_code >= 300:
        raise EnvSetupError(f"set environment {environment} failed: {r.status_code} {r.text[:200]}")


def ci_image_pushed(owner: str, name: str, branch: str, token: str, workflow_name: str = "CI") -> bool:
    """Has CI actually produced an image yet? Used to gate CD (CI -> image -> CD).

    We treat "a successful CI run on ``branch`` from a push/dispatch" as the signal,
    because that's exactly when the image-push phase runs -- and it's reliable with a
    normal repo token (listing GHCR packages would need a broader scope). Returns
    False (conservative) if the check can't be made.
    """
    try:
        r = httpx.get(
            f"{API}/repos/{owner}/{name}/actions/runs",
            headers=_headers(token),
            params={"branch": branch, "status": "success", "per_page": 50},
            timeout=30,
        )
    except httpx.HTTPError:
        return False
    if r.status_code >= 300:
        return False
    for run in r.json().get("workflow_runs", []):
        if run.get("name") == workflow_name and run.get("event") in ("push", "workflow_dispatch"):
            return True
    return False


def latest_successful_ci_sha(
    owner: str, name: str, branch: str, token: str, workflow_name: str = "CI"
) -> str | None:
    """The head SHA of the most recent successful CI run on ``branch`` -- i.e. the
    tag of the newest image CI pushed to GHCR. Returns None if none is found.

    Used to deploy the image that's *already* in GHCR (e.g. from the CD UI) without
    waiting for a fresh CI run.
    """
    try:
        r = httpx.get(
            f"{API}/repos/{owner}/{name}/actions/runs",
            headers=_headers(token),
            params={"branch": branch, "status": "success", "per_page": 50},
            timeout=30,
        )
    except httpx.HTTPError:
        return None
    if r.status_code >= 300:
        return None
    # Runs come back newest-first, so the first matching run is the latest image.
    for run in r.json().get("workflow_runs", []):
        if run.get("name") == workflow_name and run.get("event") in ("push", "workflow_dispatch"):
            return run.get("head_sha")
    return None


def ci_run_status(owner: str, name: str, sha: str, token: str, workflow_name: str = "CI") -> str:
    """Single-shot status of the CI run for commit ``sha`` (no polling/blocking):
    'success' | 'failure' | 'running' | 'none' (no run created yet). Lets a UI poll."""
    try:
        r = httpx.get(
            f"{API}/repos/{owner}/{name}/actions/runs",
            headers=_headers(token), params={"head_sha": sha, "per_page": 20}, timeout=30,
        )
    except httpx.HTTPError:
        return "running"
    if r.status_code >= 300:
        return "running"
    for run in r.json().get("workflow_runs", []):
        if run.get("name") == workflow_name:
            if run.get("status") == "completed":
                return "success" if run.get("conclusion") == "success" else "failure"
            return "running"
    return "none"


def merge_pr(owner: str, name: str, pr_number: int, token: str) -> str:
    """Merge an open PR and return the resulting commit SHA on the base branch.

    Tries the allowed merge methods in turn (a repo may disable some). Raises
    PROpenError if the PR can't be merged (e.g. conflicts, or branch protection).
    """
    last = "no attempt"
    for method in ("squash", "merge", "rebase"):
        r = httpx.put(
            f"{API}/repos/{owner}/{name}/pulls/{pr_number}/merge",
            headers=_headers(token), json={"merge_method": method}, timeout=30,
        )
        if r.status_code < 300:
            return r.json().get("sha", "")
        last = f"{r.status_code} {r.text[:150]}"
        if r.status_code != 405:  # 405 = this merge method is disabled -> try the next one
            break
    raise PROpenError(f"could not auto-merge PR #{pr_number}: {last}")


def wait_for_ci_success(
    owner: str, name: str, sha: str, token: str,
    *, workflow_name: str = "CI", timeout_s: int = 900, interval_s: int = 12,
) -> bool:
    """Poll until the CI workflow run for commit ``sha`` finishes; True if it succeeded.

    This is how the chain waits for the image: after merging the CI PR, CI runs on
    the merge commit and (on success) pushes the image to GHCR. Returns False on a
    non-success conclusion or if it doesn't finish within ``timeout_s``.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"{API}/repos/{owner}/{name}/actions/runs",
                headers=_headers(token), params={"head_sha": sha, "per_page": 20}, timeout=30,
            )
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code < 300:
            for run in r.json().get("workflow_runs", []):
                if run.get("name") == workflow_name and run.get("status") == "completed":
                    return run.get("conclusion") == "success"
        time.sleep(interval_s)
    return False


def _seal(public_key_b64: str, value: str) -> str:
    """libsodium sealed box → base64, the exact format GitHub expects."""
    import base64

    from nacl import encoding, public

    pk = public.PublicKey(public_key_b64.encode(), encoder=encoding.Base64Encoder)
    sealed = public.SealedBox(pk).encrypt(value.encode())
    return base64.b64encode(sealed).decode()


def resolve_token() -> str | None:
    """Find a GitHub token: env first, then the gh CLI."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip()
    # A subprocess may have a narrower PATH than the interactive shell (common
    # under conda), so try known install locations too.
    for exe in (shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "auth", "token"], capture_output=True, text=True, timeout=15)
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def open_pr(
    snapshot: RepoSnapshot,
    workflow: GeneratedWorkflow,
    clone_dir: Path,
    token: str,
) -> tuple[int, str, str]:
    """Commit the CI workflow on a fresh branch, push it, and open a PR.

    Returns (pr_number, pr_url, branch).
    """
    return open_pr_files(
        snapshot,
        [(workflow.path, workflow.content)],
        clone_dir,
        token,
        branch_prefix=BRANCH_PREFIX,
        title="ci: add CI workflow (via cicd-bootstrap)",
        body=_pr_body(snapshot, workflow),
        commit_message=f"ci: add {workflow.cookbook} CI workflow (build, test, sonar, push)",
    )


def open_pr_files(
    snapshot: RepoSnapshot,
    files: list[tuple[str, str]],
    clone_dir: Path,
    token: str,
    *,
    branch_prefix: str,
    title: str,
    body: str,
    commit_message: str,
) -> tuple[int, str, str]:
    """Commit one or more (path, content) files on a fresh branch, push, open a PR.

    Any file that would clobber an existing one is written to a ``-bootstrap``
    sibling instead (e.g. ``app-ci.yml`` -> ``app-cicd-bootstrap.yml``). This is how both the
    CI and CD generators land their workflow. Returns (pr_number, pr_url, branch).
    """
    owner, name, base = snapshot.owner, snapshot.name, snapshot.default_branch
    branch = f"{branch_prefix}-{int(time.time())}"

    _git(clone_dir, "checkout", "-b", branch)
    for path, content in files:
        target = clone_dir / path
        if target.exists():
            target = target.with_name(f"{target.stem}-bootstrap{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(clone_dir, "add", str(target.relative_to(clone_dir)))

    _git(
        clone_dir, "-c", "user.name=cicd-bootstrap[bot]",
        "-c", "user.email=cicd-bootstrap@users.noreply.github.com",
        "commit", "-m", commit_message,
    )

    push_url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
    try:
        _git(clone_dir, "push", push_url, f"{branch}:{branch}")
    except PROpenError as exc:
        raise PROpenError(
            f"could not push to {owner}/{name} (do you have write access?): {_redact(str(exc), token)}"
        ) from None

    resp = httpx.post(
        f"{API}/repos/{owner}/{name}/pulls",
        headers=_headers(token),
        json={"title": title, "head": branch, "base": base, "body": body},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise PROpenError(f"GitHub API {resp.status_code} opening PR: {resp.text[:400]}")
    data = resp.json()
    return data["number"], data["html_url"], branch


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _pr_body(snapshot: RepoSnapshot, workflow: GeneratedWorkflow) -> str:
    return (
        "## 🤖 cicd-bootstrap\n\n"
        f"Adds `{workflow.path}` to **{snapshot.owner}/{snapshot.name}**, generated from the "
        f"**{workflow.cookbook}** cookbook after classifying the repository.\n\n"
        "The workflow defines all four CI phases: **build → test → sonar → push**. "
        "Sonar runs once a `SONAR_TOKEN` secret is set; the image push runs on merges to "
        f"`{snapshot.default_branch}`.\n\n"
        "Review and merge if it looks right."
    )


def _git(cwd: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=120
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise PROpenError(f"git {' '.join(args[:2])} failed: {exc.stderr.strip()[:300]}") from exc


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


# --- deploy-repo model: create per-app deploy repos + open tag-bump PRs -------
# Used by the "deploy-repo" CD model (see cd_deploy_repo.py): the CD agent creates
# a separate {app}-deploy repo from a template and opens a PR that bumps the image
# tag in environments/<env>.yaml. It never merges the PR -- merging is the deploy
# signal (Harness watches the deploy repo).

def repo_exists(owner: str, name: str, token: str) -> bool:
    """True if owner/name exists and the token can see it."""
    resp = httpx.get(f"{API}/repos/{owner}/{name}", headers=_headers(token), timeout=30)
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise PROpenError(f"GitHub API {resp.status_code} checking {owner}/{name}: {resp.text[:300]}")


def create_repo_from_template(
    template_owner: str, template_repo: str, owner: str, name: str, token: str,
    *, private: bool = True, description: str = "", wait_s: int = 20,
) -> str:
    """Create owner/name from the GitHub *template* repo template_owner/template_repo.

    Returns the new repo's https URL. Idempotent: if owner/name already exists it is
    returned unchanged. Requires a token that can create repositories.
    """
    if repo_exists(owner, name, token):
        return f"https://github.com/{owner}/{name}.git"
    body = {
        "owner": owner, "name": name, "private": private,
        "include_all_branches": False,
        "description": description or f"Deploy config for {name.removesuffix('-deploy')}",
    }
    resp = httpx.post(
        f"{API}/repos/{template_owner}/{template_repo}/generate",
        headers=_headers(token), json=body, timeout=60,
    )
    if resp.status_code not in (201, 202):
        # 422 can mean it already exists (race) -- tolerate that.
        if resp.status_code == 422 and repo_exists(owner, name, token):
            return f"https://github.com/{owner}/{name}.git"
        raise PROpenError(
            f"could not create {owner}/{name} from template "
            f"{template_owner}/{template_repo} ({resp.status_code}): {resp.text[:300]}"
        )
    # The generated repo can take a moment to become cloneable.
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if repo_exists(owner, name, token):
            break
        time.sleep(2)
    return f"https://github.com/{owner}/{name}.git"


def open_pr_in_repo(
    owner: str, name: str, files: list[tuple[str, str]], token: str,
    *, base: str | None = None, branch_prefix: str, title: str, body: str,
    commit_message: str,
) -> tuple[int, str, str]:
    """Clone owner/name, OVERWRITE `files` on a fresh branch, push, and open a PR.

    Unlike :func:`open_pr_files` (which sidesteps clobbers and works in an
    already-cloned app repo), this clones the target repo itself and overwrites the
    given files -- used to bump the image tag in a deploy repo's
    ``environments/<env>.yaml``. Returns (pr_number, pr_url, branch). Does NOT merge.
    """
    with tempfile.TemporaryDirectory(prefix="cicd-deploy-pr-") as tmp:
        clone_dir = Path(tmp) / name
        clone_url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
        try:
            _git(Path(tmp), "clone", "--depth", "1", clone_url, str(clone_dir))
        except PROpenError as exc:
            raise PROpenError(f"could not clone {owner}/{name}: {_redact(str(exc), token)}") from None
        base = base or _git(clone_dir, "symbolic-ref", "--short", "HEAD").strip()
        branch = f"{branch_prefix}-{int(time.time())}"
        _git(clone_dir, "checkout", "-b", branch)
        for path, content in files:
            target = clone_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            _git(clone_dir, "add", str(target.relative_to(clone_dir)))
        _git(
            clone_dir, "-c", "user.name=cicd-bootstrap[bot]",
            "-c", "user.email=cicd-bootstrap@users.noreply.github.com",
            "commit", "-m", commit_message,
        )
        try:
            _git(clone_dir, "push", clone_url, f"{branch}:{branch}")
        except PROpenError as exc:
            raise PROpenError(f"could not push to {owner}/{name}: {_redact(str(exc), token)}") from None
        resp = httpx.post(
            f"{API}/repos/{owner}/{name}/pulls",
            headers=_headers(token),
            json={"title": title, "head": branch, "base": base, "body": body},
            timeout=30,
        )
        if resp.status_code >= 300:
            raise PROpenError(
                f"GitHub API {resp.status_code} opening PR in {owner}/{name}: {resp.text[:400]}"
            )
        data = resp.json()
        return data["number"], data["html_url"], branch
