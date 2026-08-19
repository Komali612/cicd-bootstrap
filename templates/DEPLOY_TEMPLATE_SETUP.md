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

## 4. Configuration knobs (loosely coupled — override any of these via env)
Nothing is hard-coded; each has a sensible default. Set in `.env` to swap a service:

| Env var | Default | Purpose |
|---|---|---|
| `CD_REGISTRY` | `ghcr.io` | image registry written into the values file (swap for Nexus, etc.) |
| `CD_DELEGATE_SELECTOR` | `laptop` | Harness delegate/target selector |
| `CD_DEPLOY_STRATEGY` | `recreate` | deploy strategy (blue/green reserved) |
| `CD_ENVIRONMENTS` | `dev,staging,prod` | environments (order sets per-env host-port offset) |
| `CD_APPROVER_USER_GROUPS` | `_project_all_users` | Harness approver group(s) for staging/prod |
| `CD_TEMPLATE_REPO` | `_deploy-template` | template repo the `{app}-deploy` repos are stamped from |
| `CD_DEPLOY_REPO_SUFFIX` | `-deploy` | naming of the per-app deploy repo |
| `CD_DEPLOY_BRANCH` | `main` | default branch of the deploy repos |
| `CD_GATE_DAST` / `CD_GATE_PLAYWRIGHT` | `off` | optional gates (Fortify/Playwright — not implemented yet) |
| `CD_MAX_ATTEMPTS` | `3` | Generate→Validate retries before escalating to the exception list |
| `CD_EXCEPTION_LIST` | `open-questions/cd-exceptions.md` | where validation failures are recorded for human review |

## 5. Phase 4 cut-over (flip the default — reversible)
The default CD model is `in-repo` until the deploy-repo model is proven on a live run.
To cut the whole fleet over:

1. Finish the live run once (steps 1–3 above are in place: `_deploy-template` repo, token
   scope, a sample repo with a CI image) and confirm merge → Harness deploy works.
2. Set `CD_DEPLOY_MODEL=deploy-repo` in `.env`. That flips the CD agent UI default, the
   CLI default, and `add_cd_harness(deploy_model=None)`. **Roll back** by unsetting it.
3. Only *after* a release of confidence, remove the in-repo-only bits (the
   `notify-harness` workflow, the per-pipeline webhook trigger, and the
   `HARNESS_WEBHOOK_URL` secret write). They are kept until then.
