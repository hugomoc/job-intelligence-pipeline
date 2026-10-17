"""Opt-in graph orchestration; the production resolver remains unchanged."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from langsmith import tracing_context

from src.enrichment import official_job_resolver as resolver


class NodeTiming(TypedDict):
    node: str
    elapsed_ms: float


class ResolutionState(TypedDict, total=False):
    """Checkpoint data: plain values, never live clients or circuits."""
    job: dict
    deadline: float
    candidates: list[dict]
    trace: list[NodeTiming]
    result: dict | None
    human_decision: str


class ResolutionContext(TypedDict):
    """Invocation-only objects; supply again when resuming a graph."""
    client: httpx.Client
    search_circuit: resolver.PublicSearchCircuit | None


def _pack_result(result: resolver.OfficialJobResolutionResult) -> dict:
    packed = asdict(result)
    packed["official_url_resolved_at"] = result.official_url_resolved_at.isoformat()
    return packed


def _unpack_result(packed: dict) -> resolver.OfficialJobResolutionResult:
    values = dict(packed)
    values["official_url_resolved_at"] = datetime.fromisoformat(values["official_url_resolved_at"])
    if values.get("description_result") is not None:
        values["description_result"] = resolver.JobDescriptionResult(**values["description_result"])
    if values.get("identity_validation") is not None:
        values["identity_validation"] = resolver.JobIdentityValidation(**values["identity_validation"])
    return resolver.OfficialJobResolutionResult(**values)


def _finish(state: ResolutionState, node: str, started: float, **updates) -> dict:
    return {"job": state["job"], "trace": [*state.get("trace", []), {
        "node": node, "elapsed_ms": round(1000 * (time.perf_counter() - started), 1),
    }], **updates}


def _ats_discovery(state: ResolutionState, runtime: Runtime[ResolutionContext]) -> dict:
    started = time.perf_counter()
    job = state["job"]
    timing = job.setdefault("enrichment_timing", {})
    deadline = resolver.time.monotonic() + resolver.OFFICIAL_RESOLUTION_BUDGET_SECONDS
    ats_started = resolver.time.monotonic()
    try:
        candidates = resolver.discover_ats_candidates(job=job, client=runtime.context["client"])
    except Exception:
        candidates = []
    finally:
        timing["ats_discovery_ms"] = round(1000 * (resolver.time.monotonic() - ats_started), 1)
    return _finish(state, "ats_discovery", started, deadline=deadline,
                   candidates=[asdict(candidate) for candidate in candidates], result=None)


def _ats_resolution(state: ResolutionState, runtime: Runtime[ResolutionContext]) -> dict:
    started = time.perf_counter()
    result = resolver.resolve_from_candidates(
        job=state["job"], client=runtime.context["client"],
        candidates=[resolver.OfficialJobCandidate(**candidate) for candidate in state["candidates"]],
        source="ats", deadline=state["deadline"],
    )
    terminal = result.status in {
        resolver.OFFICIAL_FOUND_VERIFIED, resolver.OFFICIAL_AMBIGUOUS,
        resolver.OFFICIAL_SEARCH_DEFERRED,
    }
    return _finish(state, "ats_resolution", started,
                   result=_pack_result(result) if terminal else None)


def _deadline_check(state: ResolutionState) -> dict:
    started = time.perf_counter()
    result = None
    if resolver.time.monotonic() >= state["deadline"]:
        result = resolver.OfficialJobResolutionResult(
            status=resolver.OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
            official_url_source="search", official_url_resolved_at=resolver.now_utc(),
            official_url_confidence=None,
            official_url_validation_reason="official resolution time budget exceeded before public search",
            resolved_title=None, resolved_company=None, resolved_location=None,
        )
    return _finish(state, "deadline_check", started,
                   result=_pack_result(result) if result else None)


def _public_search(state: ResolutionState, runtime: Runtime[ResolutionContext]) -> dict:
    started = time.perf_counter()
    job, client = state["job"], runtime.context["client"]
    circuit = runtime.context["search_circuit"]
    result = None
    candidates = []
    try:
        candidates = circuit.search(job, client) if circuit else resolver.search_candidates(job, client)
    except resolver.SearchDeferredError as error:
        result = resolver.OfficialJobResolutionResult(
            status=resolver.OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
            official_url_source="search", official_url_resolved_at=resolver.now_utc(),
            official_url_confidence=None, official_url_validation_reason=str(error),
            resolved_title=None, resolved_company=None, resolved_location=None,
            error_message=str(error),
        )
    except PermissionError as error:
        result = resolver.OfficialJobResolutionResult(
            status=resolver.OFFICIAL_BLOCKED, official_job_url=None,
            official_url_source="search", official_url_resolved_at=resolver.now_utc(),
            official_url_confidence=None, official_url_validation_reason=str(error),
            resolved_title=None, resolved_company=None, resolved_location=None,
            error_message=str(error),
        )
    except Exception as error:
        result = resolver.OfficialJobResolutionResult(
            status=resolver.OFFICIAL_ERROR, official_job_url=None,
            official_url_source="search", official_url_resolved_at=resolver.now_utc(),
            official_url_confidence=None, official_url_validation_reason=str(error),
            resolved_title=None, resolved_company=None, resolved_location=None,
            error_message=f"{type(error).__name__}: {error}",
        )
    return _finish(state, "public_search", started,
                   candidates=[asdict(candidate) for candidate in candidates],
                   result=_pack_result(result) if result else None)


def _workday_leads(state: ResolutionState, runtime: Runtime[ResolutionContext]) -> dict:
    started = time.perf_counter()
    job = state["job"]
    timing = job.setdefault("enrichment_timing", {})
    candidates = [resolver.OfficialJobCandidate(**candidate) for candidate in state["candidates"]]
    seen_urls = {candidate.url for candidate in candidates}
    search_trace = job.get("public_search_trace") or {}
    workday_homepages = [
        hit["url"] for hit in search_trace.get("organic_results", [])
        if resolver.ats_type_for_url(hit.get("url", "")) == "workday"
        and not resolver.looks_like_individual_posting_url(hit.get("url", ""))
    ][:1]
    for homepage in workday_homepages:
        if resolver.time.monotonic() >= state["deadline"]:
            break
        lead_started = resolver.time.monotonic()
        leads = resolver.workday.discover_from_search_homepage(job, runtime.context["client"], homepage)
        timing["workday_listing_discovery_ms"] = round(1000 * (resolver.time.monotonic() - lead_started), 1)
        search_trace["workday_listing_candidates_found"] = len(leads)
        for lead in leads:
            if lead.url not in seen_urls:
                candidates.append(lead)
                seen_urls.add(lead.url)
    return _finish(state, "workday_leads", started,
                   candidates=[asdict(candidate) for candidate in candidates])


def _search_resolution(state: ResolutionState, runtime: Runtime[ResolutionContext]) -> dict:
    started = time.perf_counter()
    result = resolver.resolve_from_candidates(
        job=state["job"], client=runtime.context["client"],
        candidates=[resolver.OfficialJobCandidate(**candidate) for candidate in state["candidates"]],
        source="search", deadline=state["deadline"],
    )
    return _finish(state, "search_resolution", started, result=_pack_result(result))


def _human_review(state: ResolutionState) -> dict:
    started = time.perf_counter()
    job = state["job"]
    decision = interrupt({
        "job": {name: job.get(name) for name in ("record_key", "title", "company_name", "location")},
        "candidate_validation_trace": job.get("candidate_validation_trace", []),
    })
    if not isinstance(decision, str) or not (decision == "reject" or decision.startswith("accept:")):
        raise ValueError("Review decision must be 'accept:<url>' or 'reject'")
    return _finish(state, "human_review", started, human_decision=decision)


def _after_discovery(state: ResolutionState) -> str:
    return "ats_resolution" if state["candidates"] else "deadline_check"


def _after_result(state: ResolutionState) -> str:
    if state.get("result"):
        return "human_review" if state["result"]["status"] == resolver.OFFICIAL_AMBIGUOUS else END
    return "deadline_check"


def _after_deadline(state: ResolutionState) -> str:
    return END if state.get("result") else "public_search"


def _after_search(state: ResolutionState) -> str:
    return END if state.get("result") else "workday_leads"


def build_graph(checkpointer=None, *, checkpoint_path: str | Path | None = None):
    """Compile an opt-in graph. Close graph.checkpointer.conn when owned here.

    Pass a caller-owned SqliteSaver to manage its lifetime explicitly. Otherwise
    checkpoint_path selects the SQLite file; the default is a fresh temp directory.
    Clients/circuits must be passed to invoke(..., context=ResolutionContext(...)).
    """
    # Opting into this package never opts into external LangSmith reporting.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGCHAIN_TRACING"] = "false"
    if checkpointer is None:
        path = Path(checkpoint_path) if checkpoint_path is not None else Path(
            tempfile.mkdtemp(prefix="job-resolution-graph-")
        ) / "checkpoints.sqlite"
        checkpointer = SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    builder = StateGraph(ResolutionState, context_schema=ResolutionContext)
    for name, node in (
        ("ats_discovery", _ats_discovery), ("ats_resolution", _ats_resolution),
        ("deadline_check", _deadline_check), ("public_search", _public_search),
        ("workday_leads", _workday_leads), ("search_resolution", _search_resolution),
        ("human_review", _human_review),
    ):
        builder.add_node(name, node)
    builder.add_edge(START, "ats_discovery")
    builder.add_conditional_edges("ats_discovery", _after_discovery)
    builder.add_conditional_edges("ats_resolution", _after_result)
    builder.add_conditional_edges("deadline_check", _after_deadline)
    builder.add_conditional_edges("public_search", _after_search)
    builder.add_edge("workday_leads", "search_resolution")
    builder.add_conditional_edges("search_resolution", _after_result)
    builder.add_edge("human_review", END)
    return builder.compile(checkpointer=checkpointer)


def resolve_official_job_graph(
    job: dict, client: httpx.Client, search_circuit=None, checkpointer=None,
) -> resolver.OfficialJobResolutionResult:
    """Return the baseline result, even when AMBIGUOUS review is pending.

    For explicit interrupt/resume control, use build_graph and Command directly.
    The default saver lives outside the repo. Pass a SqliteSaver for a known path.
    """
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": str(job.get("record_key") or uuid4())}, "callbacks": []}
    try:
        with tracing_context(enabled=False):
            state = graph.invoke({"job": job, "trace": [], "result": None}, config,
                                 context={"client": client, "search_circuit": search_circuit})
        job.update(state["job"])
        return _unpack_result(state["result"])
    finally:
        if checkpointer is None:
            graph.checkpointer.conn.close()
