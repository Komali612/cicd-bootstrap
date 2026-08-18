"""Offline tests for CD deploy recipes + the optional LLM recipe fallback.

The CD counterpart to test_llm_fallback.py. We never call the real LLM. We check:

1. The *classifier heuristic*: a Dockerfile's EXPOSE (or a worker-shaped run
   command) maps to the right built-in deploy shape, with no LLM.
2. The *policy*: a shape with a built-in recipe resolves deterministically (no
   LLM); a shape with none raises without the fallback, and routes through the
   author (marking the recipe LLM-authored) with it.
3. The *invariant that matters*: the deploy strategy (recreate + health-check +
   rollback) is assembled around whatever recipe fields the LLM supplied -- the
   LLM only fills port/health/env/command, never the pipeline.
"""

from __future__ import annotations

import pytest

from cicd_bootstrap import cd_author, cd_cookbooks, cd_generate
from cicd_bootstrap.cd_classify import classify_deploy_heuristic
from cicd_bootstrap.cd_cookbooks import DeployRecipe
from cicd_bootstrap.contracts import DeployShape, RepoSnapshot
from cicd_bootstrap.harness import build_deploy_script


def _snap(dockerfile: str | None = None, name: str = "demo") -> RepoSnapshot:
    manifests = {"Dockerfile": dockerfile} if dockerfile is not None else {}
    return RepoSnapshot(
        repo_url=f"https://github.com/Komali612/{name}", owner="Komali612",
        name=name, default_branch="main",
        tree=["app/main.py"] + (["Dockerfile"] if dockerfile is not None else []),
        manifests=manifests,
    )


# --- classifier heuristic -------------------------------------------------

def test_heuristic_reads_expose_as_a_web_service():
    shape = classify_deploy_heuristic(_snap("FROM python:3.12-slim\nEXPOSE 9000\nCMD [\"python\",\"app.py\"]"))
    assert shape.kind == "web-service"
    assert shape.port == 9000
    assert shape.method == "heuristic"


def test_heuristic_detects_a_worker_from_the_run_command():
    shape = classify_deploy_heuristic(_snap('FROM python:3.12\nCMD ["celery","-A","app","worker"]'))
    assert shape.kind == "worker"
    assert shape.port is None


def test_heuristic_treats_a_plain_repo_as_a_confident_web_service():
    # No EXPOSE, no worker signal -> still a web service, and CONFIDENT (not the
    # 'unclear' case), so a normal app that omits EXPOSE never triggers the LLM.
    shape = classify_deploy_heuristic(_snap("FROM alpine\nCMD [\"/bin/true\"]"))
    assert shape.kind == "web-service"
    assert shape.port == cd_cookbooks.DEFAULT_PORT
    assert shape.confidence >= cd_generate.HEURISTIC_MATCH_CONFIDENCE

    # No Dockerfile at all is also a confident web service (that's CI's default image).
    no_df = classify_deploy_heuristic(_snap(None))
    assert no_df.kind == "web-service"
    assert no_df.confidence >= cd_generate.HEURISTIC_MATCH_CONFIDENCE


def test_heuristic_flags_genuinely_ambiguous_signals_as_unclear():
    # A port AND a worker command together -> we can't tell -> unclear (low conf).
    both = classify_deploy_heuristic(_snap('FROM x\nEXPOSE 8080\nCMD ["celery","worker"]'))
    assert both.confidence < cd_generate.HEURISTIC_MATCH_CONFIDENCE

    # Several ports -> can't tell which to publish -> unclear.
    multi = classify_deploy_heuristic(_snap("FROM x\nEXPOSE 8080\nEXPOSE 9090"))
    assert multi.confidence < cd_generate.HEURISTIC_MATCH_CONFIDENCE


# --- resolve_recipe policy ------------------------------------------------

def test_builtin_web_service_resolves_without_the_llm():
    recipe = cd_generate.resolve_recipe(_snap("FROM x\nEXPOSE 8091"))
    assert recipe.key == "web-service"
    assert recipe.publish_port is True
    assert recipe.port == 8091           # the real EXPOSE port, filled deterministically
    assert recipe.llm_authored is False


def test_builtin_worker_resolves_without_the_llm():
    recipe = cd_generate.resolve_recipe(_snap('FROM x\nCMD ["rq","worker"]'))
    assert recipe.key == "worker"
    assert recipe.publish_port is False
    assert recipe.llm_authored is False


def _no_llm(_snapshot):
    raise AssertionError("the LLM classifier must not be called for this case")


def test_obvious_web_service_never_calls_the_llm_even_with_fallback(monkeypatch):
    # An EXPOSE'd repo clearly fits a template -> deterministic, no LLM, even when
    # the fallback box is ticked.
    monkeypatch.setattr(cd_generate, "classify_deploy", _no_llm)
    recipe = cd_generate.resolve_recipe(_snap("FROM x\nEXPOSE 8080"), allow_llm_fallback=True)
    assert recipe.key == "web-service"
    assert recipe.llm_authored is False


def test_plain_repo_without_fallback_deploys_web_service_no_llm(monkeypatch):
    # No EXPOSE, no worker signal, fallback OFF -> confident web-service, no LLM,
    # and (crucially) NO 'no template' error for an ordinary app that omits EXPOSE.
    monkeypatch.setattr(cd_generate, "classify_deploy", _no_llm)
    recipe = cd_generate.resolve_recipe(_snap('FROM alpine\nCMD ["/bin/true"]'), allow_llm_fallback=False)
    assert recipe.key == "web-service"
    assert recipe.llm_authored is False


def test_ambiguous_repo_without_fallback_reports_no_template(monkeypatch):
    # Genuinely ambiguous (port + worker) AND fallback off -> inform the user there
    # is no matching template, without ever calling the LLM.
    monkeypatch.setattr(cd_generate, "classify_deploy", _no_llm)
    with pytest.raises(cd_generate.UnsupportedDeployError):
        cd_generate.resolve_recipe(_snap('FROM x\nEXPOSE 8080\nCMD ["celery","worker"]'), allow_llm_fallback=False)


def test_ambiguous_repo_routes_through_author_only_with_fallback(monkeypatch):
    monkeypatch.setattr(cd_generate, "classify_deploy",
                        lambda snap: DeployShape(kind="grpc-mesh", port=None, confidence=1.0, method="test"))
    authored = DeployRecipe(
        key="grpc-mesh", display_name="grpc-mesh (LLM-authored)", publish_port=True, port=50051,
        health_type="http", health_path="/grpc.health", env=(("GRPC_PORT", "50051"),),
        run_command=(), llm_authored=True, llm_input_tokens=7, llm_output_tokens=9,
    )
    monkeypatch.setattr(cd_author, "author_recipe", lambda snap, shape: (authored, {"input_tokens": 7, "output_tokens": 9}))

    # An ambiguous Dockerfile (port + worker) is what sends resolve_recipe to the LLM.
    ambiguous = _snap('FROM x\nEXPOSE 8080\nCMD ["celery","worker"]')
    recipe = cd_generate.resolve_recipe(ambiguous, allow_llm_fallback=True)
    assert recipe.llm_authored is True
    assert recipe.key == "grpc-mesh"

    # The invariant: the fixed deploy strategy assembles around the LLM's fields.
    script = build_deploy_script("Komali612", "demo", recipe)
    assert "-p 50051:50051" in script          # the LLM's port is published
    assert "-e GRPC_PORT=50051" in script      # the LLM's env is passed through
    assert "$PORT/grpc.health" in script       # the LLM's health path is probed
    assert "Rolling back" in script            # rollback is still code-owned


# --- author parsing -------------------------------------------------------

def test_parse_env_accepts_name_value_and_rejects_garbage():
    assert cd_author._parse_env(["A=1", "B=x=y"]) == (("A", "1"), ("B", "x=y"))
    assert cd_author._parse_env([]) == ()
    with pytest.raises(cd_author.AuthorError):
        cd_author._parse_env(["NOEQUALS"])
    with pytest.raises(cd_author.AuthorError):
        cd_author._parse_env(["=novalue"])
