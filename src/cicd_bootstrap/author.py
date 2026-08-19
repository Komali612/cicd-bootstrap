"""LLM fallback: author a *cookbook* (not a workflow) for an unsupported stack.

When no built-in cookbook matches the classification, this optionally asks the
LLM to produce ONLY the fields a cookbook varies -- toolchain setup, build/test
commands, a Sonar strategy, and a Dockerfile. Those fields are slotted into the
same deterministic skeleton as any other cookbook, so the four phases, the Sonar
and Push guards, and the GHCR push are still code-owned and identical. The LLM
never writes workflow YAML.

This is opt-in (the caller passes allow_llm_fallback=True) because it turns an
honest "unsupported" into a best-effort guess that should be reviewed.
"""

from __future__ import annotations

import os

import yaml

from . import cookbooks
from .contracts import Classification, LLMCookbook, RepoSnapshot
from .cookbooks.base import Cookbook

DEFAULT_MODEL = "claude-opus-4-8"  # authoring is the harder generative task


class AuthorError(Exception):
    """Raised when the LLM fallback can't produce a usable cookbook."""


SYSTEM_PROMPT = """You are a CI engineer. A repository uses a build system we have no
built-in cookbook for. Produce ONLY the language-specific pieces needed to fill a
fixed CI skeleton -- you are NOT writing a workflow file. The skeleton already
provides checkout, the four phases (Build, Test, Sonar, Push), the Sonar/Push
guards, and the GHCR image push; do not restate any of that.

Base every choice on the manifests and file tree you are shown. Return:
- setup_steps_yaml: a YAML list of GitHub Actions steps (after checkout) that
  install the toolchain -- typically one official actions/setup-* step pinned to a
  major version, with a sensible language version. Just the steps, as a YAML
  sequence. Example:
    - name: Set up Foo
      uses: actions/setup-foo@v1
      with:
        foo-version: "1.2"
- build: the shell command(s) to install dependencies and/or compile.
- test: the shell command(s) that run the project's tests.
- sonar_strategy: 'generic' unless the build tool has a first-class scanner
  (only 'maven' and 'dotnet' do); almost always 'generic'.
- dockerfile: a minimal, stack-appropriate multi-stage Dockerfile exposing 8080.

Prefer the project's real manifests over guesses. Keep commands non-interactive."""


def author_cookbook(snapshot: RepoSnapshot, classification: Classification) -> tuple[Cookbook, dict]:
    """Ask the LLM for the cookbook fields and build an (unregistered) Cookbook."""
    llm, usage = _call(_render(snapshot, classification))

    setup = _parse_setup(llm.setup_steps_yaml)
    strategy = llm.sonar_strategy.lower().strip()
    if strategy not in cookbooks.strategy_names():
        strategy = "generic"

    cookbook = Cookbook(
        key=classification.build_system,
        language=llm.language.lower().strip() or classification.language,
        display_name=f"{classification.build_system} (LLM-authored)",
        setup=setup,
        build=_as_lines(llm.build),
        test=_as_lines(llm.test),
        sonar=cookbooks.sonar_strategy(strategy),
        default_dockerfile=llm.dockerfile,
    )
    return cookbook, usage


def _as_lines(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _parse_setup(setup_yaml: str) -> list[dict]:
    """The LLM returns setup steps as a YAML list; parse and sanity-check them."""
    try:
        steps = yaml.safe_load(setup_yaml)
    except yaml.YAMLError as exc:
        raise AuthorError(f"LLM setup steps were not valid YAML: {str(exc).splitlines()[0]}") from None
    if steps is None:
        return []
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise AuthorError("LLM setup steps must be a YAML list of step mappings")
    return steps


def _render(snapshot: RepoSnapshot, classification: Classification) -> str:
    manifests = "\n".join(f"\n## {p}\n```\n{c}\n```" for p, c in snapshot.manifests.items())
    return (
        f"Repository: {snapshot.owner}/{snapshot.name}\n"
        f"Classified as: language={classification.language}, "
        f"build_system={classification.build_system}, test_command={classification.test_command!r}\n\n"
        "# File tree\n" + "\n".join(snapshot.tree) + "\n\n"
        "# Manifest files\n" + (manifests or "(none)")
    )


def _call(user: str) -> tuple[LLMCookbook, dict]:
    from .llm import LLMError, call_structured

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthorError("ANTHROPIC_API_KEY is required for the LLM cookbook fallback")
    try:
        return call_structured(
            model=os.environ.get("AUTHORING_MODEL", DEFAULT_MODEL),
            system=SYSTEM_PROMPT,
            user=user,
            schema=LLMCookbook,
            max_tokens=2048,
            timeout=120.0,
        )
    except LLMError as exc:
        raise AuthorError(str(exc)) from None
