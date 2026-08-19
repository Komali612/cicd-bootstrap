# Deployment config (GitOps)

This repository holds the **desired deploy state** for one application, separate from
its source code. It is created by the CD agent from the `_deploy-template` template
repository — one `{app}-deploy` repo per app.

## Layout
- `environments/<env>.yaml` — which image tag runs in each environment (the source of
  truth). The CD agent bumps `tag` via a pull request.
- `.harness/deploy.yaml` — the Harness pipeline (written by Harness, not by hand).

## How a deploy happens
1. CI builds and pushes an image for the app.
2. The CD agent opens a PR here bumping `tag` in `environments/<env>.yaml`.
   **The agent never merges its own PR.**
3. You review and merge (production may require an approver — see CODEOWNERS).
4. Harness watches this repo; the merge triggers the pipeline, which deploys the image
   on the laptop delegate (pull → recreate → health-check → rollback).

Roll back by reverting the tag-bump commit.

## The values file (`environments/<env>.yaml`)
The single source of truth for a deploy — read by the pipeline at run time (FR-N.2/FR-N.6):

| Field | Wired? | Meaning |
|-------|--------|---------|
| `app`, `env`, `image`, `tag`, `port` | ✅ | what to deploy and where it listens (the agent bumps `tag`) |
| `approvers` | ✅ | approver group(s) that gate staging/prod |
| `rollback_image` | ✅ | explicit rollback target; empty ⇒ the last running image |
| `notify` | ⏳ | notification team for failure/rollback — *captured, not enforced yet* |
| `change_request` | ⏳ | change-management id (required for prod) — *captured, not enforced yet* |
| `cluster`, `namespace` | ⏳ | AKS target — *captured, not enforced yet* |

⏳ fields are config placeholders you fill in; their enforcement is deferred (same pattern
as the DAST gate), so nothing about them is hard-coded and wiring them later is additive.
