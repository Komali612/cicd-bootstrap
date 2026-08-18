"""CD deploy-recipe engine: one fixed deploy strategy + a registry loaded from data.

The CD counterpart to :mod:`cicd_bootstrap.cookbooks`. Where a CI cookbook varies
by *language / build system*, a CD recipe varies by *deploy shape* -- because a
built container runs the same regardless of the source language, the thing that
actually differs between apps is how you *run* it: whether it publishes a network
port, how you health-check it, and any runtime env it needs.

The deploy *strategy* itself -- pull the image, recreate the container,
health-check it, roll back to the previous image on failure -- is fixed and lives
in the Harness generator (:mod:`cicd_bootstrap.harness`). A recipe only supplies
the shape-specific fill-ins on :class:`DeployRecipe`. Built-in recipes are
declared as data in ``cd_cookbooks.yaml``; unusual shapes are authored by the LLM
(:mod:`cicd_bootstrap.cd_author`) into the *same* dataclass, so the strategy stays
code-owned exactly as the CI skeleton does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import yaml

# The shared data file for the built-in recipes.
DATA_PATH = Path(__file__).with_name("cd_cookbooks.yaml")

# The port a recipe assumes when the repo's Dockerfile has no EXPOSE line -- the
# same port every built-in CI cookbook's Dockerfile exposes.
DEFAULT_PORT = 8080

# Health-check styles a recipe may use.
HEALTH_HTTP = "http"        # curl the published port (a web service)
HEALTH_PROCESS = "process"  # no port; the container just has to stay running (a worker)
HEALTH_TYPES = (HEALTH_HTTP, HEALTH_PROCESS)


@dataclass(frozen=True)
class DeployRecipe:
    """Everything shape-specific needed to fill the fixed deploy strategy.

    Built from ``cd_cookbooks.yaml`` for built-in shapes, or by the LLM fallback
    for unusual ones (``llm_authored=True``). You should not construct one by hand.
    """

    key: str                # registry key = deploy shape, e.g. "web-service", "worker"
    display_name: str
    publish_port: bool      # True -> `docker run -p PORT:PORT` (a networked service)
    port: int               # the port to publish / health-check (ignored when publish_port is False)
    health_type: str        # "http" (curl the port) | "process" (container stays up)
    health_path: str = "/"  # HTTP path to hit for an http health check
    env: tuple[tuple[str, str], ...] = ()    # runtime env vars: (NAME, VALUE) pairs
    run_command: tuple[str, ...] = ()         # optional `docker run` command override
    llm_authored: bool = False
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None

    def with_port(self, port: int) -> "DeployRecipe":
        """A copy with the port filled in (used once the real EXPOSE port is known)."""
        return replace(self, port=port)


# --- registry ------------------------------------------------------------

_REGISTRY: dict[str, DeployRecipe] = {}


def register(recipe: DeployRecipe) -> None:
    _REGISTRY[recipe.key] = recipe


def get(kind: str | None) -> DeployRecipe | None:
    return _REGISTRY.get(kind.lower().strip()) if kind else None


def supported() -> list[str]:
    return sorted(_REGISTRY)


# --- loading cd_cookbooks.yaml -------------------------------------------

def load(path: Path = DATA_PATH) -> None:
    """Populate the registry from the data file. Called once at import."""
    raw = yaml.safe_load(path.read_text())
    _REGISTRY.clear()
    for key, spec in raw.get("recipes", {}).items():
        health_type = spec.get("health_type", HEALTH_HTTP)
        if health_type not in HEALTH_TYPES:
            raise ValueError(
                f"deploy recipe {key!r} has unknown health_type {health_type!r} "
                f"(have: {', '.join(HEALTH_TYPES)})"
            )
        register(DeployRecipe(
            key=key,
            display_name=spec.get("display_name", key),
            publish_port=bool(spec.get("publish_port", True)),
            port=int(spec.get("port", DEFAULT_PORT)),
            health_type=health_type,
            health_path=spec.get("health_path", "/"),
            env=tuple((str(e["name"]), str(e["value"])) for e in spec.get("env", [])),
            run_command=tuple(str(c) for c in spec.get("run_command", [])),
        ))


# Populate the registry from the data file on import.
load()
