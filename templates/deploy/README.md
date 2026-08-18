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
