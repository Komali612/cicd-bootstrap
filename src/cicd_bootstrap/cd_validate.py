"""Validate generated CD artifacts before provisioning (FR-N.10 / NFR-3).

Structural / lint checks on the generated Harness pipeline and the per-env values file
— a *sandboxed* check that never pulls a real image, deploys to a real cluster, or runs a
real DAST/Playwright pass. It confirms stage ordering and that the health-gate rollback is
actually wired (the health-failure path reverts to the prior version instead of falling
through to "done").

The orchestrator runs generate → validate up to ``CDConfig.max_attempts`` and, if it still
fails, escalates via :func:`record_exception` (writing to the exception list) instead of
raising a partial PR — the standard NFR-3 exception path.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import yaml

REQUIRED_VALUES_KEYS = ("app", "env", "image", "tag", "port")


def validate_values_file(content: str) -> list[str]:
    """Problems with a per-env values file (empty list = valid)."""
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        return [f"values file is not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return ["values file is not a mapping"]
    problems = [f"values file missing '{k}'" for k in REQUIRED_VALUES_KEYS if k not in data]
    if "tag" in data and not str(data.get("tag") or "").strip():
        problems.append("values file has an empty image tag")
    return problems


def validate_pipeline_yaml(content: str, *, expect_approval: bool) -> list[str]:
    """Problems with a generated Harness deploy pipeline (empty list = valid)."""
    try:
        doc = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        return [f"pipeline is not valid YAML: {exc}"]
    pipe = doc.get("pipeline") if isinstance(doc, dict) else None
    if not isinstance(pipe, dict):
        return ["pipeline: missing top-level 'pipeline' mapping"]

    problems: list[str] = []
    if not pipe.get("identifier"):
        problems.append("pipeline: missing identifier")

    stages = [s.get("stage", {}) for s in pipe.get("stages", []) if isinstance(s, dict)]
    types = [s.get("type") for s in stages]

    deploy_stages = [s for s in stages if s.get("type") == "Custom"]
    if not deploy_stages:
        problems.append("pipeline: no Deploy (Custom) stage")
    else:
        steps = (((deploy_stages[-1].get("spec") or {}).get("execution") or {}).get("steps") or [])
        step_spec = (steps[0].get("step", {}) if steps else {}).get("spec") or {}
        if not step_spec.get("onDelegate"):
            problems.append("deploy step: not pinned onDelegate")
        if not step_spec.get("delegateSelectors"):
            problems.append("deploy step: no delegate selector")
        script = ((step_spec.get("source") or {}).get("spec") or {}).get("script", "") or ""
        # Rollback wiring: the health-failure path must revert to the prior image...
        if "Rolling back" not in script or "$PREV" not in script:
            problems.append("deploy script: rollback-to-previous wiring missing")
        # ...a health check must exist...
        if "healthy" not in script:
            problems.append("deploy script: health check missing")
        # ...and a health failure must NOT silently mark success.
        if "exit 1" not in script:
            problems.append("deploy script: no failure exit (a failed health check must not mark success)")

    if expect_approval:
        if "Approval" not in types:
            problems.append("pipeline: approval expected but no Approval stage")
        elif "Custom" in types and types.index("Approval") > types.index("Custom"):
            problems.append("pipeline: Approval stage must come before the Deploy stage")
    return problems


def validate_deploy_artifacts(pipeline_yaml: str, values_yaml: str, *, expect_approval: bool) -> list[str]:
    """All structural problems with the generated pipeline + values file (empty = valid)."""
    return (validate_pipeline_yaml(pipeline_yaml, expect_approval=expect_approval)
            + validate_values_file(values_yaml))


def record_exception(path: str, *, repo_url: str, app: str, env: str, reason: str) -> str:
    """Append a CD exception to the exception list (the NFR-3 escalation path).

    Returns the path written. Creating a partial PR is explicitly avoided upstream; this is
    the "write to open-questions/" destination for human review."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    first_write = not p.exists()
    with p.open("a", encoding="utf-8") as fh:
        if first_write:
            fh.write("# CD exception list\n\n"
                     "Repos whose CD generation failed validation after the retry cap — for human review.\n\n")
        fh.write(f"- {ts}  **{app}** ({env}) — {repo_url}\n  - {reason}\n")
    return str(p)
