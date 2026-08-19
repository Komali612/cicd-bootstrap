"""Telemetry: record one event per bootstrap to a local JSONL file, and
aggregate it for the dashboard.

Local + single-user, so the store is deliberately boring: an append-only
`events.jsonl` under a data dir (``~/.cicd-bootstrap`` by default, override with
``CI_BOOTSTRAP_DATA_DIR``). Persisting to disk -- not in-memory counters -- is
the one thing that matters here, because the service process is ephemeral: it
restarts, and anything held in memory would vanish with it.

Recording never raises into the caller (telemetry must not break a bootstrap);
core.py wraps record() in a try/except.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import BootstrapResult

# Rough per-million-token USD rates, ONLY for the dashboard's cost estimate.
# These are editable assumptions, not billing truth -- classify runs on the cheap
# model, the LLM cookbook fallback on the expensive one.
CLASSIFY_RATE_IN, CLASSIFY_RATE_OUT = 1.0, 5.0       # $/Mtok (Haiku-class)
AUTHOR_RATE_IN, AUTHOR_RATE_OUT = 15.0, 75.0         # $/Mtok (Opus-class)


def data_dir() -> Path:
    base = Path(os.environ.get("CI_BOOTSTRAP_DATA_DIR") or (Path.home() / ".cicd-bootstrap"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def events_path() -> Path:
    return data_dir() / "events.jsonl"


# --- recording -----------------------------------------------------------

def record(result: BootstrapResult, duration_ms: int) -> None:
    """Append one event for a completed bootstrap. Best-effort, never raises."""
    line = json.dumps(_event(result, duration_ms))
    with events_path().open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _event(result: BootstrapResult, duration_ms: int) -> dict:
    c = result.classification
    w = result.workflow
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": _short_repo(result.repo_url),
        "repo_url": result.repo_url,
        "kind": getattr(result, "kind", "ci"),   # "ci" | "cd"
        "status": result.status,
        "cd_gate": getattr(result, "cd_gate", None),  # CD only: "automatic …" | "manual …"
        "language": getattr(c, "language", None),
        "build_system": getattr(c, "build_system", None),
        "classify_method": getattr(c, "method", None),
        "confidence": getattr(c, "confidence", None),
        "classify_input_tokens": getattr(c, "llm_input_tokens", None),
        "classify_output_tokens": getattr(c, "llm_output_tokens", None),
        "cookbook": getattr(w, "cookbook", None),
        "llm_authored": getattr(w, "llm_authored", None),
        "author_input_tokens": getattr(w, "llm_input_tokens", None),
        "author_output_tokens": getattr(w, "llm_output_tokens", None),
        "pr_number": result.pr_number,
        "sonar_secret_set": result.sonar_secret_set,
        "duration_ms": duration_ms,
        "error": result.message if result.status == "error" else None,
    }


def _short_repo(url: str) -> str:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else url


def _kind(e: dict) -> str:
    """CI vs CD for a stored event. Back-fills events written before the `kind`
    field existed by recognising the CD generator's cookbook."""
    k = e.get("kind")
    if k in ("ci", "cd"):
        return k
    return "cd" if e.get("cookbook") == "local-docker" else "ci"


def _cd_gate_label(e: dict) -> str:
    """Normalise a CD run's approval mode to 'automatic' | 'manual'."""
    g = (e.get("cd_gate") or "").lower()
    if not g:
        return "manual"  # our default when unspecified is the approval gate
    return "automatic" if "auto" in g else "manual"


# --- reading + aggregation ----------------------------------------------

def load_events() -> list[dict]:
    p = events_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summary() -> dict:
    return aggregate(load_events())


def aggregate(events: list[dict]) -> dict:
    n = len(events)
    ci_events = [e for e in events if _kind(e) == "ci"]
    cd_events = [e for e in events if _kind(e) == "cd"]

    opened = sum(1 for e in events if e.get("status") == "opened")
    generated = sum(1 for e in events if e.get("status") == "generated")
    errors = sum(1 for e in events if e.get("status") == "error")
    prs = sum(1 for e in events if e.get("pr_number"))
    successes = opened + generated

    # CI-specific rates are computed over CI runs only, so CD runs don't skew them.
    llm_cls = sum(1 for e in ci_events if e.get("classify_method") == "llm")
    classified = sum(1 for e in ci_events if e.get("classify_method"))
    authored = sum(1 for e in ci_events if e.get("llm_authored"))
    generated_wf = sum(1 for e in ci_events if e.get("cookbook"))  # CI workflows produced
    sonar_attempts = [e for e in events if e.get("sonar_secret_set") is not None]
    sonar_ok = sum(1 for e in sonar_attempts if e.get("sonar_secret_set"))

    # CD-specific rollup.
    cd_success = sum(1 for e in cd_events if e.get("status") in ("opened", "generated"))
    cd = {
        "runs": len(cd_events),
        "prs_opened": sum(1 for e in cd_events if e.get("pr_number")),
        "success_rate": _pct(cd_success, len(cd_events)),
    }
    by_kind = [d for d in (
        {"key": "ci", "count": len(ci_events)},
        {"key": "cd", "count": len(cd_events)},
    ) if d["count"]]
    by_cd_gate = _count_by([{"gate": _cd_gate_label(e)} for e in cd_events], "gate")

    tok = {
        "classify_in": _sum(events, "classify_input_tokens"),
        "classify_out": _sum(events, "classify_output_tokens"),
        "author_in": _sum(events, "author_input_tokens"),
        "author_out": _sum(events, "author_output_tokens"),
    }
    tok["total"] = sum(tok.values())
    cost = (
        tok["classify_in"] / 1e6 * CLASSIFY_RATE_IN
        + tok["classify_out"] / 1e6 * CLASSIFY_RATE_OUT
        + tok["author_in"] / 1e6 * AUTHOR_RATE_IN
        + tok["author_out"] / 1e6 * AUTHOR_RATE_OUT
    )

    durations = sorted(e["duration_ms"] for e in events if isinstance(e.get("duration_ms"), (int, float)))

    return {
        "totals": {
            "runs": n,
            "ci_runs": len(ci_events),
            "cd_runs": len(cd_events),
            "unique_repos": len({e.get("repo") for e in events if e.get("repo")}),
            "opened": opened,
            "generated": generated,
            "errors": errors,
            "prs_opened": prs,
        },
        "rates": {
            "success_rate": _pct(successes, n),
            "llm_classify_rate": _pct(llm_cls, classified),
            "fallback_rate": _pct(authored, generated_wf),
            "sonar_set_rate": _pct(sonar_ok, len(sonar_attempts)),
        },
        "cd": cd,
        "by_kind": by_kind,
        "by_cd_gate": by_cd_gate,
        "tokens": tok,
        "cost_estimate_usd": round(cost, 4),
        "latency_ms": {
            "p50": _pctile(durations, 0.50),
            "p95": _pctile(durations, 0.95),
            "avg": round(sum(durations) / len(durations)) if durations else 0,
        },
        "by_language": _count_by(events, "language"),
        "by_build_system": _count_by(events, "build_system"),
        "by_status": _count_by(events, "status"),
        "by_method": _count_by(events, "classify_method"),
        "runs_per_day": _runs_per_day(events, days=14),
        "recent": _recent(events, limit=25),
    }


def _sum(events: list[dict], key: str) -> int:
    return sum(int(e[key]) for e in events if isinstance(e.get(key), (int, float)))


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _pctile(sorted_vals: list[float], q: float) -> int:
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return int(sorted_vals[i])


def _count_by(events: list[dict], key: str) -> list[dict]:
    counts: dict[str, int] = {}
    for e in events:
        v = e.get(key)
        if v:
            counts[v] = counts.get(v, 0) + 1
    return [{"key": k, "count": c} for k, c in sorted(counts.items(), key=lambda kv: -kv[1])]


def _runs_per_day(events: list[dict], days: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    window = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    counts = {d.isoformat(): 0 for d in window}
    for e in events:
        ts = str(e.get("ts", ""))[:10]
        if ts in counts:
            counts[ts] += 1
    return [{"day": d, "count": counts[d]} for d in counts]


def _recent(events: list[dict], limit: int) -> list[dict]:
    keep = (
        "ts", "repo", "language", "build_system", "classify_method", "status",
        "cookbook", "llm_authored", "pr_number", "sonar_secret_set", "duration_ms",
    )
    rows = []
    for e in reversed(events[-limit:]):
        row = {k: e.get(k) for k in keep}
        row["kind"] = _kind(e)
        row["cd_gate"] = e.get("cd_gate")
        row["tokens"] = (
            (e.get("classify_input_tokens") or 0) + (e.get("classify_output_tokens") or 0)
            + (e.get("author_input_tokens") or 0) + (e.get("author_output_tokens") or 0)
        )
        rows.append(row)
    return rows
