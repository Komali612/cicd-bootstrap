"""Resolve a repo to a CD deploy recipe -- the CD counterpart to :mod:`generate`.

Deterministic first, the LLM strictly as a fallback:

* If the repo CLEARLY fits a built-in shape -- a Dockerfile ``EXPOSE`` (a web
  service) or an unmistakable worker command -- we use that built-in recipe and
  never call the LLM.
* Only when the repo does NOT clearly fit a built-in template *and* the caller
  opted in (``allow_llm_fallback=True``) do we consult the LLM: first to recognise
  the shape, then -- if that shape has no built-in recipe -- to author one
  (:mod:`cicd_bootstrap.cd_author`). The LLM never writes the pipeline YAML; it
  only fills the recipe's fields.
* When the repo is unclear and the fallback is off, we do NOT guess -- we report
  that no built-in deploy template matches (``UnsupportedDeployError``), so the
  caller can tell the user to enable the LLM fallback. "Unclear" means the
  Dockerfile signals genuinely conflict (a port AND a worker command, or several
  ports); an ordinary repo -- even one with no EXPOSE line -- is confidently a web
  service and resolves deterministically.

So the LLM runs *only* as a fallback for a repo that has no matching template --
never to classify an obvious repo, and never merely to guess a port.
"""

from __future__ import annotations

from . import cd_cookbooks
from .cd_classify import classify_deploy, classify_deploy_heuristic
from .cd_cookbooks import DeployRecipe
from .contracts import DeployShape, RepoSnapshot

# At/above this confidence the deterministic heuristic has clearly placed the repo
# (a real EXPOSE line, an unmistakable worker command, or a plain container with
# neither -> web service), so we use it as-is and never call the LLM. Below it, the
# repo's signals are genuinely ambiguous -- the "unclear" case.
HEURISTIC_MATCH_CONFIDENCE = 0.8


class UnsupportedDeployError(Exception):
    """Raised when no built-in recipe matches the repo's deploy shape (and no
    fallback was allowed) -- the CD twin of :class:`generate.UnsupportedError`."""


def _builtin(shape: DeployShape) -> DeployRecipe:
    """The built-in recipe for a shape, with the real port filled in for a service."""
    recipe = cd_cookbooks.get(shape.kind) or cd_cookbooks.get("web-service")
    if recipe.publish_port:
        recipe = recipe.with_port(shape.port or recipe.port)
    return recipe


def resolve_recipe(snapshot: RepoSnapshot, *, allow_llm_fallback: bool = False) -> DeployRecipe:
    """Classify ``snapshot`` and return the deploy recipe to run it with.

    The LLM is consulted only when the repo does not clearly fit a built-in
    template AND ``allow_llm_fallback`` is set; obvious repos are resolved purely
    deterministically, with no LLM call at all. An unclear repo with the fallback
    off raises :class:`UnsupportedDeployError` rather than guessing.
    """
    # 1. Deterministic pass -- no LLM.
    shape = classify_deploy_heuristic(snapshot)
    if shape.confidence >= HEURISTIC_MATCH_CONFIDENCE:
        return _builtin(shape)  # an obvious repo -- resolved with no LLM at all

    # 2. The repo's signals are ambiguous (no built-in template clearly fits).
    if not allow_llm_fallback:
        reason = (shape.evidence[0] if shape.evidence else "the deploy shape is ambiguous")
        raise UnsupportedDeployError(
            f"no built-in deploy template matches this repo — {reason}. "
            f"Enable the LLM fallback to author a recipe for it."
        )

    # 3. Fallback enabled: NOW consult the LLM to recognise the shape, and author a
    # bespoke recipe if it turns out to be an unusual one.
    shape = classify_deploy(snapshot)
    if cd_cookbooks.get(shape.kind) is not None:
        return _builtin(shape)

    from .cd_author import author_recipe

    recipe, _usage = author_recipe(snapshot, shape)
    return recipe
