"""The tool's LLM layer, on LangChain.

Every LLM call in the tool goes through :func:`call_structured`, which uses
``langchain_anthropic.ChatAnthropic`` with **structured (schema-validated)
output**. This is the single seam for the model:

* swap models / providers here (one place),
* add LangSmith tracing here (set ``LANGSMITH_TRACING=true``),
* mock here in tests.

Everything else in the tool stays deterministic (git, GitHub/Harness APIs, YAML).
Callers: CI classification fallback and cookbook authoring, and their CD twins --
deploy-shape classification and deploy-recipe authoring.
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when a structured LLM call can't produce a valid result."""


def call_structured(
    *,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    max_tokens: int = 1024,
    timeout: float = 90.0,
) -> tuple[T, dict]:
    """Make one schema-validated LLM call via LangChain's ChatAnthropic.

    Returns ``(parsed, usage)`` where ``parsed`` is an instance of ``schema`` and
    ``usage`` is ``{"input_tokens", "output_tokens"}`` (values may be None).
    Requires ``ANTHROPIC_API_KEY``.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMError("ANTHROPIC_API_KEY is not set")

    # Imported lazily so importing the tool doesn't require langchain at rest.
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=model, max_tokens=max_tokens, timeout=timeout, max_retries=1)
    # include_raw=True so we can read token usage off the raw AIMessage.
    structured = llm.with_structured_output(schema, include_raw=True)
    result = structured.invoke([("system", system), ("human", user)])

    parsed = result.get("parsed")
    if parsed is None:
        raise LLMError(f"LLM returned no valid {schema.__name__}: {result.get('parsing_error')}")

    raw = result.get("raw")
    meta = getattr(raw, "usage_metadata", None) or {}
    usage = {"input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens")}
    return parsed, usage
