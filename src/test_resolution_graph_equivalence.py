"""Offline parity of graph orchestration and the unchanged production resolver."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from src.agent.resolution_graph import build_graph, resolve_official_job_graph
from src.enrichment import official_job_resolver as resolver
from src.test_ats_adapters import analytics_job, mock_client as ats_mock_client
from src.test_official_job_resolver import (
    job_posting_html, lensa_job, mock_client, search_html,
)


FIRST = "https://boards.greenhouse.io/seismic/jobs/123"
SECOND = "https://jobs.lever.co/seismic/456"


def candidate(url=FIRST):
    return resolver.OfficialJobCandidate(
        url=url, label="Senior Analytics Engineer", source="greenhouse",
        title="Senior Analytics Engineer", company="Seismic",
    )


def routes_for(*urls, company="Seismic"):
    return {"search": httpx.Response(200, text=search_html(*urls)), **{
        url: httpx.Response(200, text=job_posting_html("Senior Analytics Engineer", company),
                            headers={"content-type": "text/html"}) for url in urls
    }}


def comparable(result):
    fields = asdict(result)
    fields.pop("official_url_resolved_at")
    return fields


class ResolutionGraphEquivalenceTests(unittest.TestCase):
    def _compare(self, expected, *, urls=(FIRST,), ats=None, ats_error=False,
                 routes=None, circuit_error=None, search_error=None,
                 expired=False, workday=False, job=None, ats_terminal=True):
        outcomes = []
        jobs = []
        for run in (resolver.resolve_official_job, resolve_official_job_graph):
            fresh = copy.deepcopy(job if job is not None else lensa_job())
            transport = ats_mock_client if ats else mock_client
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    "SERPER_API_KEY": "", "TAVILY_API_KEY": "",
                    "PUBLIC_SEARCH_PROVIDER": "duckduckgo",
                }))
                discovery = stack.enter_context(patch.object(
                    resolver, "discover_ats_candidates", return_value=copy.deepcopy(ats or []),
                    side_effect=RuntimeError("adapter failed") if ats_error else None,
                ))
                if expired:
                    stack.enter_context(patch.object(resolver, "time", SimpleNamespace(
                        monotonic=Mock(side_effect=[0, 0, 0, 1_000_000])
                    )))
                circuit = None
                if circuit_error:
                    circuit = Mock()
                    circuit.search.side_effect = circuit_error
                if search_error:
                    stack.enter_context(patch.object(resolver, "search_candidates", side_effect=search_error))
                if workday:
                    homepage = "https://pfizer.wd1.myworkdayjobs.com/PfizerCareers"
                    def search(fresh_job, client):
                        fresh_job["public_search_trace"] = {"organic_results": [{"url": homepage}]}
                        return []
                    stack.enter_context(patch.object(resolver, "search_candidates", side_effect=search))
                client = stack.enter_context(transport(routes if routes is not None else routes_for(*urls)))
                if run is resolve_official_job_graph:
                    saver = stack.enter_context(SqliteSaver.from_conn_string(":memory:"))
                    outcome = run(fresh, client, circuit, checkpointer=saver)
                    snapshot = saver.get_tuple({"configurable": {"thread_id": fresh["record_key"]}})
                    self.assertIsNotNone(snapshot)
                    values = snapshot.checkpoint["channel_values"]
                    json.dumps(values)  # Includes result and nested description/validation.
                    self.assertNotIn("client", values)
                    self.assertNotIn("search_circuit", values)
                    nodes = [entry["node"] for entry in values["trace"]]
                    self.assertEqual(nodes[0], "ats_discovery")
                    self.assertTrue(all(entry["elapsed_ms"] >= 0 for entry in values["trace"]))
                    if ats and ats_terminal and expected in {resolver.OFFICIAL_FOUND_VERIFIED, resolver.OFFICIAL_AMBIGUOUS,
                                           resolver.OFFICIAL_SEARCH_DEFERRED}:
                        self.assertNotIn("public_search", nodes)
                else:
                    outcome = run(fresh, client, circuit)
                self.assertEqual(outcome.status, expected)
                self.assertEqual(discovery.call_count, 1)
                outcomes.append(comparable(outcome))
                jobs.append(fresh)
        self.assertEqual(outcomes[0], outcomes[1])
        return jobs

    def test_verified_via_ats(self):
        self._compare(resolver.OFFICIAL_FOUND_VERIFIED, ats=[candidate()], job=analytics_job())

    def test_verified_via_search(self):
        self._compare(resolver.OFFICIAL_FOUND_VERIFIED)

    def test_ambiguous_multiple_search_matches(self):
        jobs = self._compare(resolver.OFFICIAL_AMBIGUOUS, urls=(FIRST, SECOND))
        for job in jobs:
            self.assertEqual({entry["url"] for entry in job["candidate_validation_trace"]}, {FIRST, SECOND})
            self.assertTrue(all(entry["accepted"] for entry in job["candidate_validation_trace"]))

    def test_no_official_match(self):
        self._compare(resolver.OFFICIAL_NOT_FOUND, urls=())

    def test_blocked_employer_site(self):
        routes = routes_for(FIRST)
        routes[FIRST] = httpx.Response(403, text="blocked")
        self._compare(resolver.OFFICIAL_BLOCKED, routes=routes)

    def test_search_deferred_via_circuit(self):
        self._compare(resolver.OFFICIAL_SEARCH_DEFERRED,
                      circuit_error=resolver.SearchDeferredError("search circuit is open"))

    def test_deadline_expired_before_search(self):
        self._compare(resolver.OFFICIAL_SEARCH_DEFERRED, expired=True)

    def test_identity_mismatch_rejection(self):
        self._compare(resolver.OFFICIAL_NOT_FOUND,
                      routes=routes_for(FIRST, company="Unrelated Employer"))

    def test_ats_error_falls_back_to_search(self):
        self._compare(resolver.OFFICIAL_FOUND_VERIFIED, ats_error=True)

    def test_workday_homepage_lead(self):
        posting = ("https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/"
                   "job/New-York/Senior-Analytics-Engineer_R123")
        endpoint = "https://pfizer.wd1.myworkdayjobs.com/wday/cxs/pfizer/PfizerCareers/jobs"
        jobs = self._compare(resolver.OFFICIAL_FOUND_VERIFIED, workday=True,
            job=dict(lensa_job(), company_name="Pfizer"), routes={
                endpoint: httpx.Response(200, json={"jobPostings": [{
                    "title": "Senior Analytics Engineer",
                    "externalPath": "/job/New-York/Senior-Analytics-Engineer_R123",
                    "locationsText": "Remote",
                }]}),
                posting: httpx.Response(200, text=job_posting_html("Senior Analytics Engineer", "Pfizer"),
                                       headers={"content-type": "text/html"}),
            })
        for job in jobs:
            self.assertEqual(job["public_search_trace"]["workday_listing_candidates_found"], 1)

    def test_permission_error_from_search(self):
        self._compare(resolver.OFFICIAL_BLOCKED, search_error=PermissionError("search blocked"))

    def test_unexpected_error_from_search(self):
        self._compare(resolver.OFFICIAL_ERROR, search_error=RuntimeError("search failed"))

    def test_ats_not_found_falls_through(self):
        generic = candidate("https://boards.greenhouse.io/seismic")
        self._compare(resolver.OFFICIAL_FOUND_VERIFIED, ats=[generic], ats_terminal=False)

    def test_ats_ambiguity_stops_before_search(self):
        self._compare(resolver.OFFICIAL_AMBIGUOUS, ats=[candidate(), candidate(SECOND)],
                      urls=(FIRST, SECOND))

    def test_ambiguous_interrupt_and_resume(self):
        for decision in (f"accept:{FIRST}", "reject"):
            with self.subTest(decision=decision), \
                 tempfile.TemporaryDirectory(prefix="resolution-review-") as directory, \
                 mock_client(routes_for(FIRST, SECOND)) as client, \
                 patch.object(resolver, "discover_ats_candidates", return_value=[]), \
                 patch.dict(os.environ, {"PUBLIC_SEARCH_PROVIDER": "duckduckgo"}):
                path = str(Path(directory) / "checkpoints.sqlite")
                config = {"configurable": {"thread_id": "review-job"}, "callbacks": []}
                context = {"client": client, "search_circuit": None}
                with SqliteSaver.from_conn_string(path) as saver:
                    graph = build_graph(checkpointer=saver)
                    state = graph.invoke({"job": lensa_job(), "trace": []}, config, context=context)
                    self.assertEqual(state["result"]["status"], resolver.OFFICIAL_AMBIGUOUS)
                    self.assertIn("__interrupt__", state)
                    review = state["__interrupt__"][0].value
                    self.assertEqual(review["job"]["company_name"], "Seismic")
                    self.assertEqual({entry["url"] for entry in review["candidate_validation_trace"]},
                                     {FIRST, SECOND})
                    before = state["result"]
                # Reopen SQLite and rebuild: no client/circuit has been persisted.
                with SqliteSaver.from_conn_string(path) as saver, \
                     patch.object(resolver, "resolve_from_candidates", side_effect=AssertionError("replayed resolution")):
                    graph = build_graph(checkpointer=saver)
                    state = graph.invoke(Command(resume=decision), config, context=context)
                    self.assertNotIn("__interrupt__", state)
                    self.assertEqual(state["human_decision"], decision)
                    self.assertEqual(state["result"], before)
                    self.assertEqual(graph.get_state(config).next, ())

    def test_checkpoint_serializable_and_thread_isolated(self):
        with tempfile.TemporaryDirectory(prefix="resolution-state-") as directory:
            path = Path(directory) / "state.sqlite"
            graph = build_graph(checkpoint_path=path)
            try:
                with mock_client(routes_for(FIRST)) as client, \
                     patch.object(resolver, "discover_ats_candidates", return_value=[]), \
                     patch.dict(os.environ, {"PUBLIC_SEARCH_PROVIDER": "duckduckgo"}):
                    for thread_id in ("one", "two"):
                        graph.invoke({"job": dict(lensa_job(), record_key=thread_id), "trace": []},
                                     {"configurable": {"thread_id": thread_id}},
                                     context={"client": client, "search_circuit": None})
            finally:
                graph.checkpointer.conn.close()
            with SqliteSaver.from_conn_string(str(path)) as saver:
                graph = build_graph(checkpointer=saver)
                for thread_id in ("one", "two"):
                    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
                    self.assertEqual(snapshot.values["job"]["record_key"], thread_id)
                    json.dumps(snapshot.values)
                    self.assertNotIn("client", snapshot.values)
                    self.assertNotIn("search_circuit", snapshot.values)
                    self.assertIsInstance(snapshot.values["result"]["description_result"], dict)
            self.assertEqual(os.environ["LANGSMITH_TRACING"], "false")


if __name__ == "__main__":
    unittest.main()
