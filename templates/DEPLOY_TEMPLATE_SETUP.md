# One-time setup for the deploy-repo CD model

The CD agent's `deploy-repo` mode auto-creates a `{app}-deploy` repository per app
from a shared GitHub **template repository**. Two one-time prerequisites:

## 1. Create the `_deploy-template` GitHub template repo
- Create a repo named `_deploy-template` under the same owner as your apps.
- Copy the contents of `templates/deploy/` (this folder) into it and push.
- In the repo's **Settings**, tick **“Template repository.”**
  (The agent calls `POST /repos/{owner}/_deploy-template/generate` to stamp out
  each `{app}-deploy`.)

## 2. Token scope
- The GitHub token used by the CD agent (`resolve_token()` / `GITHUB_TOKEN` in `.env`)
  must be allowed to **create repositories** — a classic PAT with `repo`, or a
  fine-grained token with repository administration + creation.
- The current CI/CD flows only need push/PR, so this scope is new.

## 3. Harness (verified during Phase 2)
- The existing account-level GitHub connector must be able to register a webhook on the
  new deploy repos so Harness can watch `environments/**`.

Until these are in place, keep the CD agent on its default **in-repo** model.

## 4. Approvers for staging/prod
- `templates/deploy/.github/CODEOWNERS` gates `environments/staging.yaml` and
  `environments/prod.yaml` on a release approver. Replace `@RELEASE-APPROVER` with your
  GitHub user or team before using the template.
- The Harness pipeline also pauses staging/prod for a manual approval (dev deploys on
  merge with no gate). Both gates are independent — the PR review and the Harness approval.
