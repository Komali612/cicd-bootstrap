"""Classify the *deploy shape* of a repo -- the CD counterpart to :mod:`classify`.

CI asks "what language / build system is this?"; CD asks "how is this container
run?" -- as a networked web service, a portless background worker, or something
unusual enough to need a bespoke recipe. The output is a :class:`DeployShape`
whose ``kind`` is the key the CD recipe registry is looked up by.

Two entry points, and it matters which one the caller uses:

* :func:`classify_deploy_heuristic` -- deterministic, Dockerfile-only, never
  touches the LLM. This is what :mod:`cicd_bootstrap.cd_generate` calls first.
* :func:`classify_deploy` -- the LLM (advisory, only when ANTHROPIC_API_KEY is set
  and confident) with the heuristic as its own fallback. ``cd_generate`` reaches
  for this **only** when the heuristic couldn't clearly place the repo *and* the
  caller opted into the LLM fallback -- so an obvious repo is classified with no
  LLM call at all.
"""

from __future__ import annotations

import os
import re

from . import cd_cookbooks
from .contracts import DeployShape, LLMDeployShape, RepoSnapshot

CONFIDENCE_THRESHOLD = 0.8
DEFAULT_MODEL = "claude-haiku-4-5"  # shape classification is the cheap LLM use case

_EXPOSE_LINE_RE = re.compile(r"(?mi)^\s*EXPOSE\s+(.+)$")
# Signals in a Dockerfile CMD/ENTRYPOINT that the container is a background worker
# (no inbound port) rather than a server.
_RUN_LINE_RE = re.compile(r"(?im)^\s*(CMD|ENTRYPOINT)\b.*$")
_WORKER_RE = re.compile(r"(?i)\b(worker|consumer|celery|sidekiq|rq|beat|scheduler|cron|queue)\b")


def _expose_ports(df: str) -> list[int]:
    """Distinct ports named on any EXPOSE line (a single line may list several)."""
    seen: list[int] = []
    for m in _EXPOSE_LINE_RE.finditer(df):
        for tok in re.findall(r"\d{2,5}", m.group(1)):
            port = int(tok)
            if port not in seen:
                seen.append(port)
    return seen

SYSTEM_PROMPT = """You classify how a containerized application is RUN in production.

You are given the repository's file tree and its manifest files (Dockerfile,
configs). Determine:
- kind: 'web-service' if the app listens on a network port (an HTTP or gRPC
  server); 'worker' if it is a background process with no inbound port (a queue
  consumer, cron/scheduled job, or batch process). If it fits NEITHER cleanly --
  e.g. it needs specific environment variables to boot, a non-'/' health endpoint,
  multiple ports, or a custom run command -- return a short kebab-case label
  naming the unusual shape instead (so a bespoke recipe can be authored).
- port: the TCP port a web service listens on (null for a worker).
- confidence: 0..1, honest about ambiguity.
- evidence: concrete files or facts that justify the call.

Base every choice on the Dockerfile and file tree; prefer real signals over guesses."""


def classify_deploy(snapshot: RepoSnapshot) -> DeployShape:
    """Decide the deploy shape for ``snapshot`` (LLM if confident, else heuristic)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            llm, usage = _classify_with_llm(snapshot)
            if llm.confidence >= CONFIDENCE_THRESHOLD:
                return DeployShape(
                    kind=llm.kind.lower().strip(),
                    port=llm.port,
                    confidence=llm.confidence,
                    method="llm",
                    evidence=llm.evidence,
                    llm_input_tokens=usage.get("input_tokens"),
                    llm_output_tokens=usage.get("output_tokens"),
                )
            print(
                f"[cd-classify] LLM confidence {llm.confidence:.2f} below threshold "
                f"{CONFIDENCE_THRESHOLD}; falling back to heuristic"
            )
        except Exception as exc:
            print(f"[cd-classify] LLM shape classification failed ({exc}); using heuristic")
    else:
        print("[cd-classify] ANTHROPIC_API_KEY not set; using heuristic fallback")

    return classify_deploy_heuristic(snapshot)


def _dockerfile(snapshot: RepoSnapshot) -> str:
    for path, content in snapshot.manifests.items():
        if path.rsplit("/", 1)[-1] == "Dockerfile":
            return content or ""
    return ""


def classify_deploy_heuristic(snapshot: RepoSnapshot) -> DeployShape:
    """A deterministic shape guess from the Dockerfile -- never calls the LLM.

    Confident (at/above the caller's match threshold) for the ordinary cases: a
    single ``EXPOSE`` is a web service, a worker command is a worker, and a repo
    with neither is a web service by default (the common case, and what CI's own
    default image is). It is deliberately *unconfident* only when the signals are
    genuinely ambiguous -- a port AND a worker command, or several ports with no
    way to pick one -- which is the caller's cue to fall back to the LLM (or to
    report that no built-in template fits)."""
    df = _dockerfile(snapshot)
    ports = _expose_ports(df)
    run_lines = "\n".join(mo.group(0) for mo in _RUN_LINE_RE.finditer(df))
    is_worker = bool(run_lines and _WORKER_RE.search(run_lines))

    # Ambiguous: we genuinely can't tell -> low confidence (the "unclear" case).
    if len(ports) >= 2:
        return DeployShape(
            kind="web-service", port=ports[0], confidence=0.4, method="heuristic",
            evidence=[f"multiple EXPOSE ports {ports} — can't tell which to publish"],
        )
    if ports and is_worker:
        return DeployShape(
            kind="web-service", port=ports[0], confidence=0.4, method="heuristic",
            evidence=[f"both an EXPOSE port ({ports[0]}) and a worker-like command — ambiguous"],
        )

    # Clear single signals.
    if ports:
        return DeployShape(
            kind="web-service", port=ports[0], confidence=0.9, method="heuristic",
            evidence=[f"Dockerfile EXPOSE {ports[0]}"],
        )
    if is_worker:
        return DeployShape(
            kind="worker", port=None, confidence=0.9, method="heuristic",
            evidence=["Dockerfile CMD/ENTRYPOINT looks like a background worker"],
        )

    # No explicit signal: a container is a web service by default -- the common
    # case, and what CI's default image exposes. Confident enough to skip the LLM.
    where = "Dockerfile has no EXPOSE and no worker command" if df else "no Dockerfile in the repo"
    return DeployShape(
        kind="web-service", port=cd_cookbooks.DEFAULT_PORT, confidence=0.8, method="heuristic",
        evidence=[f"{where}; treating as a web service on {cd_cookbooks.DEFAULT_PORT}"],
    )


def _classify_with_llm(snapshot: RepoSnapshot) -> tuple[LLMDeployShape, dict]:
    from .llm import call_structured

    return call_structured(
        model=os.environ.get("CD_CLASSIFIER_MODEL", DEFAULT_MODEL),
        system=SYSTEM_PROMPT,
        user=_render(snapshot),
        schema=LLMDeployShape,
        max_tokens=512,
        timeout=60.0,
    )


def _render(snapshot: RepoSnapshot) -> str:
    manifests = "\n".join(f"\n## {p}\n```\n{c}\n```" for p, c in snapshot.manifests.items())
    return (
        f"Repository: {snapshot.owner}/{snapshot.name}\n\n"
        "# File tree\n" + "\n".join(snapshot.tree) + "\n\n"
        "# Manifest files (Dockerfile, configs)\n" + (manifests or "(none)")
    )
