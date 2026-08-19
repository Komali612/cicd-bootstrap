"""Loosely-coupled CD configuration.

Every value the CD deploy-repo path would otherwise hard-code lives here as a setting
with a sensible default, overridable via an environment variable. A later change of
service — image registry, delegate/deploy target, deploy strategy, approvers, the set
of environments — is then a config change, not a code change.

This is what the requirements ask for: CR-2 (which steps are required and the fail-fast
criteria live in config, not hard-coded), FR-N.6 (a *generic* pipeline template
parameterised by per-app/per-env values files), and FR-N.2 (approver/notification/target
read from config). The pipeline template stays generic; specifics come from here plus the
deploy repo's ``environments/<env>.yaml`` values files.

Nothing here reaches out to a service; it only records *which* services/values to use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return tuple(default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "on", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class CDConfig:
    # --- cut-over: which CD model is the default (Phase 4) -----------------------
    # Stays "in-repo" until the deploy-repo model is proven on a live run; flip the whole
    # fleet to the deploy-repo model by setting CD_DEPLOY_MODEL=deploy-repo (reversible).
    deploy_model: str = field(default_factory=lambda: _env("CD_DEPLOY_MODEL", "in-repo"))

    # --- deploy-repo conventions -------------------------------------------------
    template_repo: str = field(default_factory=lambda: _env("CD_TEMPLATE_REPO", "_deploy-template"))
    deploy_repo_suffix: str = field(default_factory=lambda: _env("CD_DEPLOY_REPO_SUFFIX", "-deploy"))
    deploy_branch: str = field(default_factory=lambda: _env("CD_DEPLOY_BRANCH", "main"))

    # --- environments (order defines the per-env host-port offset) ---------------
    environments: tuple[str, ...] = field(
        default_factory=lambda: _env_list("CD_ENVIRONMENTS", ("dev", "staging", "prod")))

    # --- image registry (only used when the agent WRITES the image ref into a
    #     values file; the deploy step then reads the ref from that file) ---------
    registry: str = field(default_factory=lambda: _env("CD_REGISTRY", "ghcr.io"))

    # --- deploy execution (swappable target/strategy) ----------------------------
    delegate_selector: str = field(default_factory=lambda: _env("CD_DELEGATE_SELECTOR", "laptop"))
    deploy_strategy: str = field(default_factory=lambda: _env("CD_DEPLOY_STRATEGY", "recreate"))

    # --- approval (approvers come from config, not a constant) -------------------
    approver_user_groups: tuple[str, ...] = field(
        default_factory=lambda: _env_list("CD_APPROVER_USER_GROUPS", ("_project_all_users",)))

    # --- optional gates (config-toggled). health is part of the deploy step today;
    #     DAST (Fortify) and Playwright are DEFERRED — declared here so enabling them
    #     later is config + a slotted step, never a rewrite. -----------------------
    gate_health: bool = field(default_factory=lambda: _env_flag("CD_GATE_HEALTH", True))
    gate_dast: bool = field(default_factory=lambda: _env_flag("CD_GATE_DAST", False))       # Fortify — paused
    gate_playwright: bool = field(default_factory=lambda: _env_flag("CD_GATE_PLAYWRIGHT", False))

    # --- Generate -> Validate loop (FR-N.10 / NFR-3) -----------------------------
    max_attempts: int = field(default_factory=lambda: _env_int("CD_MAX_ATTEMPTS", 3))
    exception_list_path: str = field(
        default_factory=lambda: _env("CD_EXCEPTION_LIST", "open-questions/cd-exceptions.md"))

    def deploy_repo_name(self, app: str) -> str:
        return f"{app}{self.deploy_repo_suffix}"

    def env_host_port(self, base_port: int, env: str) -> int:
        """Host port for ``env``: the app's container port plus this env's position in
        ``environments`` (dev=+0, staging=+1, prod=+2 by default), so the envs don't
        collide on one delegate. The value is written into the values file and read
        back at deploy time — so the offset scheme itself is not baked into the pipeline."""
        try:
            return base_port + self.environments.index(env)
        except ValueError:
            return base_port

    def deferred_gates(self) -> list[str]:
        """Gates the caller asked for that are not implemented yet (so we never silently
        no-op a requested gate)."""
        pending = []
        if self.gate_dast:
            pending.append("dast")
        if self.gate_playwright:
            pending.append("playwright")
        return pending


def load_cd_config() -> CDConfig:
    """Build the CD config from the current environment (call after load_dotenv)."""
    return CDConfig()
