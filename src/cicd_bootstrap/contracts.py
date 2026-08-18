"""Data contracts passed between the service's stages.

The flow is deliberately linear:

    RepoSnapshot  ->  Classification  ->  GeneratedWorkflow  ->  BootstrapResult
      (ingest)         (classify, LLM)      (generate, cookbook)     (github)

Language/ecosystem are free-form strings the LLM fills in; the *generator* is
what constrains us to what we actually support (it raises if no cookbook matches
the classification).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RepoSnapshot(BaseModel):
    """A compact, LLM-friendly view of the repository under inspection."""

    repo_url: str
    owner: str
    name: str
    default_branch: str
    tree: list[str] = []  # repo-relative file paths (capped)
    manifests: dict[str, str] = {}  # path -> (truncated) contents of key manifest files


class LLMClassification(BaseModel):
    """Structured output we ask the classification LLM to produce."""

    language: str = Field(description="Primary language, lowercase, e.g. java, csharp, python, go")
    build_system: str = Field(description="Build tool / package manager actually used, e.g. maven, gradle, dotnet, pip, npm")
    test_command: str = Field(description="The shell command that runs this project's tests, e.g. 'mvn -B verify'")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(description="Files/facts that support the classification")


class LLMCookbook(BaseModel):
    """Structured output for the LLM fallback: ONLY the fields a cookbook varies.

    The LLM never writes the workflow YAML. It fills in the same slots a normal
    cookbook supplies; the deterministic skeleton assembles the four phases and
    guards around them, exactly as for a built-in cookbook.
    """

    language: str = Field(description="Primary language, lowercase (e.g. elixir, scala, haskell)")
    setup_steps_yaml: str = Field(
        description="A YAML sequence of GitHub Actions steps (after checkout) that install the "
        "toolchain, e.g. an actions/setup-* step with a version. Just the steps, as a YAML list."
    )
    build: list[str] = Field(description="Shell commands for the Build phase (install deps / compile)")
    test: list[str] = Field(description="Shell commands for the Test phase")
    sonar_strategy: str = Field(
        description="Which Sonar scanner to use: one of 'maven', 'dotnet', or 'generic'. "
        "Use 'generic' (the stack-agnostic CLI scanner) unless the build tool has a first-class one."
    )
    dockerfile: str = Field(description="A minimal, stack-appropriate multi-stage Dockerfile")


class LLMDeployShape(BaseModel):
    """Structured output for the CD deploy-shape classifier (the CD twin of
    :class:`LLMClassification`)."""

    kind: str = Field(
        description="How the container is run: 'web-service' if it listens on a "
        "network port (HTTP/gRPC server), 'worker' if it's a background process "
        "with no inbound port (queue consumer, cron, batch). If it fits neither "
        "cleanly (needs specific env to boot, a non-'/' health endpoint, multiple "
        "ports, or a custom run command), return a short kebab-case label naming "
        "the unusual shape instead."
    )
    port: int | None = Field(default=None, description="Port a web service listens on; null for a worker")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(description="Files/facts that justify the shape")


class DeployShape(BaseModel):
    """Handoff contract: cd_classify -> cd_generate (the CD twin of :class:`Classification`)."""

    kind: str          # the key the deploy-recipe registry is keyed on
    port: int | None = None
    confidence: float
    method: str        # "llm" | "heuristic"
    evidence: list[str] = []
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None


class LLMDeployRecipe(BaseModel):
    """Structured output for the CD LLM fallback: ONLY the fields a deploy recipe
    varies. The LLM never writes the pipeline YAML; the fixed deploy strategy
    (pull/recreate/health-check/rollback) is assembled around these fields."""

    publish_port: bool = Field(
        description="True if the app listens on a network port that must be published; "
        "False for a background worker with no inbound port."
    )
    port: int = Field(description="The TCP port to publish and health-check when publish_port is true (else ignored)")
    health_type: str = Field(
        description="'http' to health-check by curling the port, or 'process' to only "
        "require that the container keeps running (for workers)."
    )
    health_path: str = Field(default="/", description="For an http health check, the URL path to hit, e.g. '/health'")
    env: list[str] = Field(
        default_factory=list,
        description="Runtime environment variables the container needs to start/serve, each 'NAME=VALUE'.",
    )
    run_command: list[str] = Field(
        default_factory=list,
        description="Optional command to run the container with; leave empty to use the image's default CMD.",
    )
    reasoning: str = Field(default="", description="One sentence on how you determined this")


class Classification(BaseModel):
    """Handoff contract: classify -> generate."""

    language: str
    build_system: str  # this is the key the cookbook registry is keyed on
    test_command: str
    confidence: float
    method: str  # "llm" | "heuristic"
    evidence: list[str] = []
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None


class GeneratedWorkflow(BaseModel):
    """The CI workflow a cookbook produced. The skeleton is deterministic even
    when the cookbook's fill-ins came from the LLM fallback."""

    path: str  # e.g. ".github/workflows/app-ci.yml"
    content: str
    cookbook: str  # which cookbook produced it, e.g. "maven"
    phases: list[str] = ["build", "test", "sonar", "push"]  # always all four
    llm_authored: bool = False  # True when the cookbook fields were LLM-generated (no built-in cookbook)
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None


class BootstrapResult(BaseModel):
    """The service's final answer, returned by the HTTP endpoint and the CLI."""

    repo_url: str
    status: str  # "opened" | "generated" | "error"
    kind: str = "ci"  # "ci" (bootstrap) | "cd" (add_cd) — which pipeline this run produced
    classification: Classification | None = None
    workflow: GeneratedWorkflow | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    sonar_secret_set: bool | None = None  # True/False if we tried to write SONAR_TOKEN; None if not configured
    sonar_project: str | None = None       # "created" | "exists" | "error" | None (SonarCloud project provisioning)
    cd_gate: str | None = None             # CD only: how production is gated ("automatic" | "manual approval ...")
    merged: bool = False                   # True if we auto-merged the PR we opened
    merge_sha: str | None = None           # the resulting commit SHA on the base branch, when merged
    message: str = ""


class ChainResult(BaseModel):
    """The full CI -> image -> CD chain, driven from one 'Run CI agent' click."""

    repo_url: str
    ci: BootstrapResult                    # the CI run (its .merged/.merge_sha say if/where it merged)
    image_ready: bool = False              # True once CI succeeded on main and pushed an image
    cd: BootstrapResult | None = None      # the CD run (None if we stopped before it)
    message: str = ""
