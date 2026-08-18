# cicd-bootstrap

A service that **adds CI to a repository**. Give it a GitHub repo URL and it:

1. **checks out** the repo,
2. asks an **LLM to classify** the language and build system,
3. generates a **4-phase CI workflow** (`build → test → sonar → push`) from a
   deterministic **cookbook** for that build system, and
4. opens a **pull request** adding `.github/workflows/ci.yml`, returning the PR number.

The LLM only classifies. The CI file itself is produced by deterministic
cookbooks, so it is reproducible, reviewable, and testable offline.

```
repo_url → ingest → classify (LLM) → generate (cookbook) → open PR → PR number
```

## Design

| Stage | Module | What it does |
|-------|--------|--------------|
| Ingest | `ingest.py` | Shallow-clone the repo → a small snapshot (file tree + manifest contents). Errors if it can't reach the repo. |
| Classify | `classify.py` | LLM decides `language` + `build_system` + `test_command`; deterministic heuristic fallback. |
| Generate | `generate.py` + `cookbooks/` | Look up the cookbook for the build system and render the workflow. **No cookbook → `UnsupportedError`** — unless the caller opts in to the LLM fallback (below). |
| Open PR | `github.py` | Commit on a branch, push, open a PR. Assumes push access; errors otherwise. |

The four phases live in **one place** — `cookbooks/base.py` (the skeleton) — and
are always emitted. Each language's fill-ins live as data in
`cookbooks/cookbooks.yaml`. Sonar is skipped until a `SONAR_TOKEN` secret is set;
the image push runs on merges to the default branch or a manual run (never on a
plain pull request). These guards are
structural, not selectable.

## SonarCloud: configure once on the service, never per repo

For an org where every repo shares one SonarCloud org, set two values **once** on
the service (env or the gitignored `.env`) so developers do nothing per repo:

    SONAR_ORG=my-sonar-org        # baked into every generated workflow (else the repo owner is used)
    SONAR_TOKEN=<sonarcloud token> # the org's analysis token, held by the service

- **`SONAR_ORG`** is inlined into each generated workflow (`config.sonar_org()`),
  replacing the previous guess from the repo owner.
- **`SONAR_TOKEN`**, when present, is written into **each bootstrapped repo's
  Actions secrets** during `bootstrap` — the service fetches the repo's Actions
  public key, seals the value with a libsodium box, and `PUT`s it. So the Sonar
  step just works on the first run; nobody sets a secret by hand. Requires the
  GitHub token to have admin (`secrets: write`) on the repo. If it's not set, the
  bootstrap still succeeds and Sonar stays skipped (`sonar_secret_set` reports
  the outcome). The token is only ever encrypted for GitHub — never logged.

## Adding support for a new language / build system

Add one entry under `cookbooks:` in `src/cicd_bootstrap/cookbooks/cookbooks.yaml`:

```yaml
  npm:
    language: javascript
    setup:
      - name: Set up Node
        uses: actions/setup-node@v4
        with: { node-version: "20", cache: npm }
    build: npm ci
    test: npm test
    sonar: generic        # one of the shared strategies (maven | dotnet | generic)
    dockerfile: |
      FROM node:20-slim
      ...
```

Nothing else changes — the 4-phase skeleton and guards come from `base.py`, and
the bootstrap-ci skill reads the same file (a test asserts the two copies match).
Currently shipped: **maven**, **dotnet**, **pip**, **go**. (Coverage is measured for each; entrypoint/bootstrap files are excluded.)

## LLM fallback for unsupported stacks (opt-in)

If a repo's build system has no cookbook, you can let an LLM author *just the
cookbook fields* — toolchain setup, build/test commands, a Sonar strategy, and a
Dockerfile (`author.py`). Those slot into the **same deterministic skeleton**, so
the four phases and both guards are still code-owned; the LLM never writes the
workflow YAML. It's off by default because it turns an honest "unsupported" into
a best-effort guess that should be reviewed.

- **UI:** tick *"LLM fallback for unsupported languages"* before Run.
- **CLI:** `cicd-bootstrap <url> --llm-fallback`
- **API:** `POST /bootstrap {"repo_url": "...", "allow_llm_fallback": true}`

The result is flagged `llm_authored: true` and the PR/UI shows a review warning.
Needs `ANTHROPIC_API_KEY`.

## CD deploy recipes + LLM fallback (the same pattern, for delivery)

CD mirrors CI. Where a CI cookbook varies by *language*, a CD **deploy recipe**
varies by *deploy shape* — because a built container runs the same regardless of
language, what actually differs is how you run it. The repo is matched to a shape
by a deterministic heuristic over the Dockerfile:

| Built-in recipe | For | Effect |
|-----------------|-----|--------|
| `web-service` | an HTTP server (has `EXPOSE`) | publishes the port, HTTP health-check, rollback |
| `worker` | a portless background/queue/cron process | **no** published port, health-check = the container stays running, rollback |

A repo that clearly fits a built-in shape deploys deterministically — **no LLM
call at all**. "Clearly fits" is generous: a single `EXPOSE` is a web service, a
worker command is a worker, and a plain container with **neither** is still a web
service on 8080 (the common case). A repo is only **unclear** when its Dockerfile
signals genuinely conflict — a port *and* a worker command, or several ports with
no way to pick one.

The LLM is a strict fallback for those unclear repos: it runs **only** when the
repo doesn't clearly fit a built-in template **and** the fallback is enabled —
first to recognise the unusual shape, then to author *just the recipe fields*
(`cd_author.py`) for it (runtime env, a non-`/` health path, a custom run
command…). Whatever the LLM supplies slots into the **same fixed deploy strategy**
(pull → recreate → health-check → rollback); the LLM never writes the Harness
pipeline YAML. If a repo is unclear and the fallback is **off**, CD does not guess
— it reports *"no built-in deploy template matches this repo — enable the LLM
fallback"* (mirroring how CI reports an unsupported stack). Recipes live as data
in `src/cicd_bootstrap/cd_cookbooks/cd_cookbooks.yaml` — add a shape by adding one
entry.

- **CD agent UI:** tick *"LLM authors a deploy recipe when no built-in template fits"*.
- **API:** `POST /cd {"repo_url": "...", "allow_llm_fallback": true}`

Off by default, flagged `llm_authored: true` with a review warning, and needs
`ANTHROPIC_API_KEY`. The LLM is used **only** to author a missing recipe — never
merely to guess a port for a shape we already support.

## Telemetry & dashboard

Every bootstrap appends one event (classification method/confidence, tokens,
cookbook vs `llm_authored`, status, PR number, `sonar_secret_set`, duration) to a
local JSONL file — `~/.cicd-bootstrap/events.jsonl` by default (override with
`CI_BOOTSTRAP_DATA_DIR`). Recording is best-effort and never fails a bootstrap.

The service exposes it two ways:

- **`GET /dashboard`** — a self-contained (no external libraries) dashboard for a
  service owner / platform team: KPI tiles (runs, success rate, PRs, LLM-fallback
  rate, token spend + est. cost, p95 latency), bootstraps-over-time, language /
  build-system / outcome / method breakdowns, and a recent-runs table. Linked from
  the home page; theme-aware.
- **`GET /telemetry/data`** — the aggregated JSON behind it (also handy to `curl | jq`).

Storing to disk (not in-memory counters) is deliberate: the local service process
is ephemeral, so metrics must survive restarts.

## Running

> **First time on a new machine (or not technical)?** See **[SETUP.md](SETUP.md)** for a
> step-by-step guide — including how to get each key — or just run `bash setup.sh`.

Set secrets once in a gitignored `.env` at the repo root:

```
ANTHROPIC_API_KEY=sk-ant-...
GH_TOKEN=ghp_...
```

Run the service (the primary interface) and open http://127.0.0.1:8000/:

```bash
cicd-bootstrap --serve
```

Or use the CLI:

```bash
cicd-bootstrap https://github.com/owner/repo            # classify, generate, open PR
cicd-bootstrap https://github.com/owner/repo --no-pr    # generate only, print the YAML
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Generation is deterministic, so the suite asserts the exact workflow structure
(all four phases, ordering, guards, GHCR target) with no network or LLM.
