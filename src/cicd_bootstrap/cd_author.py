"""LLM fallback: author a *deploy recipe* for an unusual deploy shape.

The CD counterpart to :mod:`cicd_bootstrap.author`. When the classified deploy
shape has no built-in recipe, this optionally asks the LLM for ONLY the
shape-specific fields a recipe varies -- whether a port is published, which port,
how to health-check it, any runtime env, and an optional run command. Those slot
into the *same* fixed deploy strategy (pull -> recreate -> health-check ->
rollback), so the strategy stays code-owned; the LLM never writes the Harness
pipeline YAML.

Opt-in (the caller passes allow_llm_fallback=True), because it turns an honest
"unsupported shape" into a best-effort guess that should be reviewed.
"""

from __future__ import annotations

import os

from .cd_cookbooks import HEALTH_HTTP, HEALTH_PROCESS, HEALTH_TYPES, DeployRecipe
from .contracts import DeployShape, LLMDeployRecipe, RepoSnapshot

DEFAULT_MODEL = "claude-opus-4-8"  # authoring is the harder generative task


class AuthorError(Exception):
    """Raised when the LLM fallback can't produce a usable deploy recipe."""


SYSTEM_PROMPT = """You are a deployment engineer. A container has a deploy shape we
have no built-in recipe for. Produce ONLY the fields needed to RUN it -- you are
NOT writing a pipeline. The pipeline already pulls the image, recreates the
container, health-checks it, and rolls back to the previous image on failure; do
not restate any of that.

Base every choice on the Dockerfile and file tree you are shown. Return:
- publish_port: true if the app listens on a network port that must be published.
- port: that port (any value if publish_port is false -- it is ignored).
- health_type: 'http' to health-check by curling the port, or 'process' to only
  require that the container keeps running (use 'process' for workers).
- health_path: for an http check, the path to hit (e.g. '/health'); default '/'.
- env: runtime environment variables as 'NAME=VALUE' -- only ones the container
  needs to start or serve. Do not invent secrets.
- run_command: only if the image's default CMD must be overridden; else empty."""


def author_recipe(snapshot: RepoSnapshot, shape: DeployShape) -> tuple[DeployRecipe, dict]:
    """Ask the LLM for the recipe fields and build a :class:`DeployRecipe`."""
    llm, usage = _call(_render(snapshot, shape))

    health_type = llm.health_type.lower().strip()
    if health_type not in HEALTH_TYPES:
        health_type = HEALTH_HTTP if llm.publish_port else HEALTH_PROCESS
    port = llm.port if 1 <= llm.port <= 65535 else 8080

    recipe = DeployRecipe(
        key=shape.kind,
        display_name=f"{shape.kind} (LLM-authored)",
        publish_port=bool(llm.publish_port),
        port=port,
        health_type=health_type,
        health_path=(llm.health_path or "/").strip(),
        env=_parse_env(llm.env),
        run_command=tuple(str(c) for c in llm.run_command),
        llm_authored=True,
        llm_input_tokens=usage.get("input_tokens"),
        llm_output_tokens=usage.get("output_tokens"),
    )
    return recipe, usage


def _parse_env(pairs: list[str]) -> tuple[tuple[str, str], ...]:
    """The LLM returns env as 'NAME=VALUE' strings; parse and sanity-check them."""
    out: list[tuple[str, str]] = []
    for item in pairs or []:
        text = str(item)
        if "=" not in text:
            raise AuthorError(f"env var {item!r} is not in NAME=VALUE form")
        name, value = text.split("=", 1)
        name = name.strip()
        if not name:
            raise AuthorError(f"env var {item!r} has an empty name")
        out.append((name, value))
    return tuple(out)


def _render(snapshot: RepoSnapshot, shape: DeployShape) -> str:
    manifests = "\n".join(f"\n## {p}\n```\n{c}\n```" for p, c in snapshot.manifests.items())
    return (
        f"Repository: {snapshot.owner}/{snapshot.name}\n"
        f"Classified deploy shape: {shape.kind}\n\n"
        "# File tree\n" + "\n".join(snapshot.tree) + "\n\n"
        "# Manifest files\n" + (manifests or "(none)")
    )


def _call(user: str) -> tuple[LLMDeployRecipe, dict]:
    from .llm import LLMError, call_structured

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthorError("ANTHROPIC_API_KEY is required for the LLM deploy-recipe fallback")
    try:
        return call_structured(
            model=os.environ.get("AUTHORING_MODEL", DEFAULT_MODEL),
            system=SYSTEM_PROMPT,
            user=user,
            schema=LLMDeployRecipe,
            max_tokens=1024,
            timeout=120.0,
        )
    except LLMError as exc:
        raise AuthorError(str(exc)) from None
