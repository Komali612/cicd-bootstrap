"""CD deploy-recipe registry.

Every built-in recipe is declared as data in ``cd_cookbooks.yaml`` and loaded
when ``base`` is imported. To add a deploy shape, edit that file -- there is no
per-shape Python module to write. Unusual shapes are authored by the LLM at
runtime (see :mod:`cicd_bootstrap.cd_author`).
"""

from __future__ import annotations

from .base import (
    DEFAULT_PORT,
    HEALTH_HTTP,
    HEALTH_PROCESS,
    HEALTH_TYPES,
    DeployRecipe,
    get,
    load,
    register,
    supported,
)

__all__ = [
    "DEFAULT_PORT", "HEALTH_HTTP", "HEALTH_PROCESS", "HEALTH_TYPES",
    "DeployRecipe", "get", "load", "register", "supported",
]
