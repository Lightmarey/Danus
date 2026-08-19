"""Tests for danus.gateway — role gating + tool wiring over danus.core.

The verify service is mocked (we replace ``server._verify``), so fact_submit is
exercised without a live verifier or codex. Config is read from the environment
at call time, so each test sets DANUS_* around a temp project dir.

Runs standalone (``python -m danus.gateway.tests.test_gateway``) and under pytest.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from danus.control import ControlStore
from danus.core import FactGraph, GlobalMemory
from danus.gateway import build_app, tools_for
from danus.gateway import server


@contextmanager
def _env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _mock_verify(verdict, repair_hints="", raise_exc=None):
    """Replace server._verify with a stub; restore after."""
    orig = server._verify

    def fake(statement, proof):
        if raise_exc is not None:
            raise raise_exc
        return {"verdict": verdict, "repair_hints": repair_hints,
                "verification_report": {"summary": "mock"}}

    server._verify = fake
    try:
        yield
    finally:
        server._verify = orig


def _prepare_v2(path: str | Path, worker: str | None = None) -> dict:
    project = Path(path)
    (project / "project.json").write_text(
        json.dumps({"name": project.name, "control_version": 2}), encoding="utf-8",
    )
    store = ControlStore(project)
    store.scaffold()
    if worker is None:
        return {}
    target = store.propose_target({
        "statement": "Prove S.", "allowed_assumptions": [],
        "forbidden_assumptions": [], "required_conclusions": ["S"],
        "fallback_candidates": [],
    })
    store.approve_target(target["version"])
    obligation, route = "v0001-root-1", "gateway-route"
    store.add_route({
        "id": route, "obligation_id": obligation,
        "method_family": "direct", "expected_result": "S",
        "input_fact_ids": [],
    })
    assignment = store.assign(
        worker, obligation_id=obligation, route_id=route, task="Prove S",
    )
    return {
        "display_title": "Gateway test fact",
        "target_version": "v0001", "obligation_id": obligation,
        "route_id": route, "assignment_epoch": assignment["epoch"],
        "claim_role": "unconditional", "assumptions_used": [],
    }


def _stage(
    binding, statement, proof, title, goal="Shared theorem goal", *,
    closes_obligation=False, closure_statement=None,
):
    links = {
        "verification_goal": goal,
        **{key: binding[key] for key in (
            "target_version", "obligation_id", "route_id", "assignment_epoch",
        )},
        "display_title": title, "predecessors": [], "intuition": "",
        "external_refs": [], "claim_role": "unconditional",
        "assumptions_used": [], "closes_obligation": closes_obligation,
    }
    if closure_statement is not None:
        links["closure_statement"] = closure_statement
    return {"source_id": server.gm_add(
        "proof_attempt", claim=statement, evidence=proof,
        verifiable=True, links=links,
    )["id"]}


def test_role_table():
    # main can never fabricate a fact
    assert "fact_submit" not in tools_for("main")
    assert "fact_submit_batch" not in tools_for("main")
    assert "fact_revoke" in tools_for("main")
    # verifier is read-only: bounded fact/glossary reads plus literature lookup
    assert tools_for("verifier") == ["fact_get", "glossary_get", "search_arxiv_theorems"]
    # worker is the only role that can submit a fact
    assert "fact_submit" not in tools_for("worker")
    assert "fact_submit_batch" in tools_for("worker")
    assert "route_context" not in tools_for("worker")
    assert "obligation_context" not in tools_for("worker")
    # all three get the read view + literature grounding
    for r in ("worker", "main", "verifier"):
        assert "search_arxiv_theorems" in tools_for(r)
    # unknown / misconfigured role fails CLOSED to the read-only verifier set
    assert tools_for("nope") == tools_for("verifier")
    assert "fact_submit" not in tools_for("nope") and "gm_add" not in tools_for("nope")
    # build_app registers without error for every role
    for r in ("worker", "main", "verifier", "all"):
        assert build_app(r) is not None


def test_gm_and_fact_search_over_temp_project():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="tester"
    ):
        _prepare_v2(d)
        out = server.gm_add("plan", claim="reduce to q>=2", evidence="")
        assert out["kind"] == "plan" and out["id"]
        hits = server.gm_search("reduce")
        assert hits["results_by_kind"]["plan"]["count"] == 1
        # fact_search over an empty graph is well-formed
        assert server.fact_search("anything")["results"] == []


def test_gm_add_staged_candidate_infers_assignment_scope():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_ROLE="worker",
    ):
        binding = _prepare_v2(d, "worker_high")
        source_id = server.gm_add(
            "proof_attempt", claim="A staged claim", evidence="A complete staged proof",
            verifiable=True,
            links={
                "verification_goal": "Shared theorem goal",
                "display_title": "Staged candidate title", "claim_role": "unconditional",
                "assumptions_used": [],
            },
        )["id"]
        entry = next(item for item in GlobalMemory(Path(d)).read("proof_attempt") if item["id"] == source_id)
        for key in ("target_version", "obligation_id", "route_id"):
            assert entry["links"][key] == binding[key]
        assert entry["links"]["assignment_epoch"] == binding["assignment_epoch"]


def test_fact_submit_accept_writes_fact_and_traces():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct"):
        binding = _prepare_v2(d, "worker_high")
        res = server.fact_submit(
            statement="S(n)=n^2", proof="induction; QED", **binding,
        )
        assert res["accepted"] is True and res["fact_id"]
        # the fact really landed in the graph
        fg = FactGraph(Path(d))
        assert fg.exists(res["fact_id"])
        # a verification trace was always written to global memory
        gm = GlobalMemory(Path(d))
        traces = gm.read("verification")
        assert traces and traces[-1]["verdict"] == "correct"
        assert traces[-1]["fact_id"] == res["fact_id"]


def test_fact_submit_reject_writes_nothing_but_traces():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("wrong", repair_hints="gap in step 2"):
        binding = _prepare_v2(d, "worker_high")
        res = server.fact_submit(statement="bad", proof="hand-wave", **binding)
        assert res["accepted"] is False and res["repair_hints"] == "gap in step 2"
        fg = FactGraph(Path(d))
        assert fg.list() == []  # nothing written
        gm = GlobalMemory(Path(d))
        assert gm.read("verification")[-1]["verdict"] == "wrong"  # but traced


def test_fact_submit_batch_one_launch_independent_verdicts():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        calls = []
        original = server._verify_batch

        def fake(goal, candidates):
            calls.append((goal, candidates))
            return {
                "verifications": [
                    {"candidate_id": "1", "verdict": "correct", "repair_hints": "",
                     "verification_report": {"summary": "ok", "critical_errors": [], "gaps": []}},
                    {"candidate_id": "2", "verdict": "wrong", "repair_hints": "missing case",
                     "verification_report": {"summary": "gap", "critical_errors": [], "gaps": [{}]}},
                ],
                "usage": {"input_tokens": 120, "cached_input_tokens": 80},
            }

        server._verify_batch = fake
        try:
            result = server.fact_submit_batch(
                candidates=[
                    _stage(binding, "One plus one equals two.", "This is integer addition.",
                           "Integer addition lemma"),
                    _stage(binding, "Every integer is even.", "Assume it is so.",
                           "False parity claim"),
                ],
                verification_goal="Shared theorem goal",
            )
        finally:
            server._verify_batch = original
        assert len(calls) == 1 and calls[0][0] == "Shared theorem goal" and len(calls[0][1]) == 2
        assert result["accepted"] is False and result["accepted_count"] == 1
        assert result["results"][0]["fact_id"] and result["results"][1]["repair_hints"] == "missing case"
        assert len(FactGraph(Path(d)).list()) == 1
        traces = GlobalMemory(Path(d)).read("verification")
        assert [trace["verdict"] for trace in traces[-2:]] == ["correct", "wrong"]
        obstacle = GlobalMemory(Path(d)).read("obstacle")[-1]
        refuted = next(entry for entry in GlobalMemory(Path(d)).read("proof_attempt") if entry["status"] == "refuted")
        assert obstacle["links"]["source_id"] == refuted["id"]
        assert "missing case" in obstacle["evidence"]


def test_fact_submit_batch_flushes_single_durable_source_without_waiting():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        staged = _stage(
            binding, "A singleton candidate is true.",
            "A complete proof of the singleton candidate.", "Singleton candidate fact",
        )
        with _mock_verify("correct"):
            result = server.fact_submit_batch(
                candidates=[staged], verification_goal="Shared theorem goal",
            )
        assert result["accepted"] is True and result["results"][0]["fact_id"]
        source = GlobalMemory(Path(d)).read("proof_attempt")[-1]
        assert source["status"] == "verified" and source["fact_id"] == result["results"][0]["fact_id"]


def test_batch_closure_separates_self_contained_fact_from_exact_obligation_binding():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        store = ControlStore(Path(d))
        obligation_statement = store.obligation(binding["obligation_id"])["statement"]
        detailed_statement = (
            "Let n be a positive integer and define S to be the assertion n=n. "
            "Then S holds."
        )
        calls = []
        original = server._verify

        def fake(statement, proof):
            calls.append((statement, proof))
            return {"verdict": "correct", "verification_report": {"summary": "ok"}}

        server._verify = fake
        try:
            first = server.fact_submit_batch(
                candidates=[_stage(
                    binding, detailed_statement, "First complete proof.",
                    "Detailed target theorem",
                )],
                verification_goal="Shared theorem goal",
            )
            closing = server.fact_submit_batch(
                candidates=[_stage(
                    binding, detailed_statement, "Second complete proof.",
                    "Detailed closing theorem", closes_obligation=True,
                    closure_statement=obligation_statement,
                )],
                verification_goal="Shared theorem goal",
            )
        finally:
            server._verify = original

        assert first["accepted"] is True
        assert closing["accepted"] is True
        assert closing["results"][0]["closure"]["closed"] is True
        assert store.obligation_state(binding["obligation_id"]) == "closed"
        assert len(calls) == 2  # a separate closure binding is never trusted via reuse
        assert obligation_statement in calls[-1][1]
        fact = FactGraph(Path(d)).get_raw(closing["results"][0]["fact_id"])
        assert fact is not None and detailed_statement in fact


def test_batch_closure_rejects_wrong_binding_before_verifier():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        original = server._verify
        server._verify = lambda *_args: (_ for _ in ()).throw(
            AssertionError("a mismatched closure binding must not spend verifier tokens")
        )
        try:
            result = server.fact_submit_batch(
                candidates=[_stage(
                    binding, "A detailed self-contained theorem.", "Proof.",
                    "Wrong closure binding", closes_obligation=True,
                    closure_statement="A different obligation.",
                )],
                verification_goal="Shared theorem goal",
            )
        finally:
            server._verify = original
        assert result["verdict"] == "control_error"
        assert "does not exactly match" in result["error"]
        assert FactGraph(Path(d)).list() == []


def test_fact_submit_batch_rejects_duplicate_statements_before_verifier():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        first = _stage(
            binding, "The duplicate theorem is true.", "First attempted proof.",
            "Duplicate theorem first",
        )
        second = _stage(
            binding, "  The duplicate theorem   is true. ", "Second attempted proof.",
            "Duplicate theorem second",
        )
        original = server._verify_batch
        server._verify_batch = lambda *_args: (_ for _ in ()).throw(
            AssertionError("duplicate statements must not spend verifier tokens")
        )
        try:
            result = server.fact_submit_batch(
                candidates=[first, second], verification_goal="Shared theorem goal",
            )
        finally:
            server._verify_batch = original

        assert result["verdict"] == "control_error"
        assert "distinct statements" in result["error"]
        assert FactGraph(Path(d)).list() == []


def test_nested_batch_uses_the_worker_half_open_probe():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        store = ControlStore(Path(d))
        parent = store.reserve_call(
            component="worker_round", max_wall_seconds=30, worker="worker_high",
            assignment_epoch=binding["assignment_epoch"], target_version=binding["target_version"],
            obligation_id=binding["obligation_id"], route_id=binding["route_id"],
        )
        store.retry_backend("codex", reason="test nested half-open probe")
        assert store.claim_backend_call("codex")["state"] == "half_open"
        try:
            with _env(DANUS_CALL_RESERVATION_ID=parent["id"]), _mock_verify("correct"):
                result = server.fact_submit_batch(
                    candidates=[_stage(binding, "Nested candidate is true.", "Proof.", "Nested candidate")],
                    verification_goal="Shared theorem goal",
                )
        finally:
            store.cancel_call_reservation(parent["id"], reason="test complete")
        assert result["accepted"] is True and result["results"][0]["fact_id"]


def test_fact_submit_batch_cancel_writes_no_truth():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        original = server._verify_batch
        server._verify_batch = lambda goal, candidates: (_ for _ in ()).throw(RuntimeError("499 cancelled"))
        try:
            result = server.fact_submit_batch(
                candidates=[
                    _stage(binding, "Candidate A is true.", "A complete proof of candidate A.",
                           "Cancellation candidate A"),
                    _stage(binding, "Candidate B is true.", "A complete proof of candidate B.",
                           "Cancellation candidate B"),
                ],
                verification_goal="Shared theorem goal",
            )
        finally:
            server._verify_batch = original
        assert result["accepted"] is False and "cancelled" in result["error"]
        assert FactGraph(Path(d)).list() == []
        assert GlobalMemory(Path(d)).read("verification") == []
        assert {entry["status"] for entry in GlobalMemory(Path(d)).read("proof_attempt")} == {"unverified"}


def test_fact_submit_batch_partial_write_is_retryable():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "worker_high")
        original_verify = server._verify_batch
        original_add = FactGraph.add
        calls = {"add": 0}

        server._verify_batch = lambda goal, candidates: {
            "verifications": [
                {"candidate_id": str(index), "verdict": "correct", "repair_hints": "",
                 "verification_report": {"summary": "ok", "critical_errors": [], "gaps": []}}
                for index in (1, 2)
            ],
            "usage": {"input_tokens": 100},
        }

        def fail_second_add(self, **kwargs):
            calls["add"] += 1
            if calls["add"] == 2:
                raise OSError("injected disk failure")
            return original_add(self, **kwargs)

        FactGraph.add = fail_second_add
        staged = [
            _stage(binding, "Recovery candidate A.", "A complete proof for recovery A.",
                   "Recovery candidate A"),
            _stage(binding, "Recovery candidate B.", "A complete proof for recovery B.",
                   "Recovery candidate B"),
        ]
        try:
            result = server.fact_submit_batch(
                candidates=staged,
                verification_goal="Shared theorem goal",
            )
        finally:
            FactGraph.add = original_add
            server._verify_batch = original_verify
        assert result["accepted"] is False
        assert result["accepted_count"] == 1 and result["verifier_accepted_count"] == 2
        assert len(FactGraph(Path(d)).list()) == 1
        original_single = server._verify
        server._verify = lambda *args: (_ for _ in ()).throw(AssertionError("must not reverify"))
        try:
            retry = server.fact_submit_batch(
                candidates=[staged[1]], verification_goal="Shared theorem goal",
            )
        finally:
            server._verify = original_single
        assert retry["accepted"] is True and retry["results"][0]["fact_id"]
        assert retry["verified_count"] == 0 and retry["recovered_write_count"] == 1
        assert len(FactGraph(Path(d)).list()) == 2


def test_fact_submit_verify_error_is_clean():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct", raise_exc=RuntimeError("service down")):
        binding = _prepare_v2(d, "w")
        res = server.fact_submit(statement="s", proof="p", **binding)
        assert res["accepted"] is False and res["verdict"] == "error"
        assert "service down" in res["error"]


def test_fact_submit_accept_but_write_failed_still_traces():
    # A storage failure after verification still records the verifier result.
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="worker_high",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ), _mock_verify("correct"):
        binding = _prepare_v2(d, "worker_high")
        original = FactGraph.add
        FactGraph.add = lambda self, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        try:
            res = server.fact_submit(statement="B", proof="proof B", **binding)
            assert res["accepted"] is True and res["fact_id"] is None and res["write_error"]
            assert GlobalMemory(Path(d)).read("verification")[-1]["verdict"] == "correct"
        finally:
            FactGraph.add = original


def test_fact_submit_glossary_check_never_blocks():
    # a raising undefined_symbols must not block submission (advisory heuristic)
    orig = FactGraph.undefined_symbols

    def boom(self, **kw):
        raise RuntimeError("glossary heuristic bug")

    FactGraph.undefined_symbols = boom
    try:
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
            DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
        ), _mock_verify("correct"):
            binding = _prepare_v2(d, "w")
            res = server.fact_submit(statement="X thing", proof="because", **binding)
            assert res["accepted"] is True and res["undefined_symbols"] == []
    finally:
        FactGraph.undefined_symbols = orig


def test_fact_submit_nondict_verify_body_is_clean():
    # a valid-JSON but non-dict verify response must not crash the gate
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="w",
        DANUS_VERIFY_URL="http://mock", DANUS_PROBLEM_ID="P",
    ):
        binding = _prepare_v2(d, "w")
        orig = server._verify
        server._verify = lambda statement, proof: ["not", "a", "dict"]
        try:
            res = server.fact_submit(statement="s", proof="p", **binding)
            assert res["accepted"] is False and res["verdict"] == "error"
            assert "non-dict" in res["error"]
            assert FactGraph(Path(d)).list() == []  # nothing written
        finally:
            server._verify = orig


def test_role_env_default_and_build_app():
    # build_app(None) reads DANUS_ROLE (server._role) — exercises the env branch
    with _env(DANUS_ROLE="worker"):
        assert server._role() == "worker"
        app = build_app()  # role=None -> defaults to _role() (env)
        assert app is not None
    with _env(DANUS_ROLE=None):
        assert server._role() == "verifier"  # unset falls back read-only (fail-closed)


def test_verifier_fact_get_returns_compact_signed_statement():
    full = {
        "fact_id": "abc", "title": "T", "statement": "S", "status": "active",
        "intuition": "large", "author": "worker", "problem_id": "P",
        "predecessors": ["p"], "successors": ["q"], "scopes": [{"route_id": "r"}],
    }
    original_query = server.ResearchQuery
    original_project = server._project

    class FakeQuery:
        def __init__(self, project):
            pass

        def fact_get(self, fact_id, *, include_proof=False):
            assert fact_id == "abc"
            return dict(full, **({"proof": "P"} if include_proof else {}))

    server.ResearchQuery = FakeQuery
    server._project = lambda project=None: Path("ignored")
    try:
        with _env(DANUS_ROLE="verifier"):
            assert server.fact_get("abc") == {
                "fact_id": "abc", "title": "T", "statement": "S", "status": "active",
            }
            assert server.fact_get("abc", include_proof=True)["proof"] == "P"
        with _env(DANUS_ROLE="worker"):
            assert server.fact_get("abc")["predecessors"] == ["p"]
    finally:
        server.ResearchQuery = original_query
        server._project = original_project


def test_glossary_get_returns_one_exact_definition():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_ROLE="verifier",
    ):
        _prepare_v2(d)
        graph = FactGraph(Path(d))
        graph.glossary_path.parent.mkdir(parents=True, exist_ok=True)
        graph.glossary_path.write_text(
            json.dumps({"beta-critical L2 slab growth": "exact project bound"}),
            encoding="utf-8",
        )
        assert server.glossary_get("beta-critical L2 slab growth") == {
            "term": "beta-critical L2 slab growth",
            "definition": "exact project bound",
            "source": "project",
        }
        assert server.glossary_get("R")["source"] == "global"
        assert server.glossary_get("definitely missing") == {
            "term": "definitely missing", "definition": None, "source": None,
        }


def test_project_by_name_without_agents_root_uses_default():
    # without an override, project names resolve under cwd/runtime/projects
    with _env(DANUS_AGENTS_ROOT=None, DANUS_PROJECT_DIR="/tmp/whatever"):
        try:
            server._project("proj_a")
            assert False, "missing default project should raise"
        except RuntimeError as e:
            assert "no such project" in str(e)


def test_verify_http_roundtrip_and_errors():
    # exercise the REAL _verify (local HTTP, offline-safe on 127.0.0.1)
    import http.server
    import threading

    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            captured["body"] = self.rfile.read(n).decode("utf-8")
            captured["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"verdict": "correct", "verification_report": {"ok": true}}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/verify"
    try:
        # not set -> RuntimeError
        with _env(DANUS_VERIFY_URL=None):
            try:
                server._verify("s", "p")
                assert False, "should raise when DANUS_VERIFY_URL unset"
            except RuntimeError as e:
                assert "DANUS_VERIFY_URL" in str(e)
        # a real POST round-trip; the body is the JSON we sent
        with _env(
            DANUS_VERIFY_URL=url, DANUS_VERIFY_TIMEOUT="5",
            DANUS_PROJECT_DIR="C:/tmp/project", DANUS_AUTHOR="high",
        ):
            out = server._verify("S(n)=n^2", "induction")
            assert out["verdict"] == "correct"
        assert '"statement": "S(n)=n^2"' in captured["body"]
        assert '"timeout_seconds": 5' in captured["body"]
        body = json.loads(captured["body"])
        assert body["project_dir"] == str(Path("C:/tmp/project").resolve())
        assert body["cancel_path"] == str(Path("C:/tmp/project") / "workers" / "high" / ".stop")
        assert captured["ctype"] == "application/json"
        # a garbage timeout falls back to the default (no crash)
        with _env(DANUS_VERIFY_URL=url, DANUS_VERIFY_TIMEOUT="not-an-int"):
            assert server._verify("s", "p")["verdict"] == "correct"
        assert '"timeout_seconds": 900' in captured["body"]
    finally:
        srv.shutdown()


def test_fact_revoke_taints_without_deleting_mathematics():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AGENTS_ROOT=None, DANUS_AUTHOR="main_agent",
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        child = fg.add(problem_id="P", author="w", statement="B from A", proof="uses A",
                       predecessors=[base])
        _prepare_v2(d)
        out = server.fact_revoke(base, reason="A was wrong")
        assert set(out["tainted_pending_review"]["event"]["affected_fact_ids"]) == {base, child}
        assert fg.exists(base) and fg.exists(child)


def test_search_arxiv_theorems_delegates(monkeypatch=None):
    # the tool is a thin wrapper over danus.integrations.search; stub it (offline)
    orig = server._arxiv_search
    server._arxiv_search = lambda query, num_results=10: {
        "query": query, "num_results": num_results, "results": [{"title": "T"}]}
    try:
        out = server.search_arxiv_theorems("Beatty sequence", num_results=3)
        assert out["query"] == "Beatty sequence" and out["num_results"] == 3
        assert out["results"] == [{"title": "T"}]
    finally:
        server._arxiv_search = orig


def test_project_resolution_by_name_and_validation():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "proj_a").mkdir()
        links = _prepare_v2(Path(root) / "proj_a", "main_agent")
        with _env(DANUS_AGENTS_ROOT=root, DANUS_PROJECT_DIR=None, DANUS_AUTHOR="main_agent"):
            # main addresses a project by name
            out = server.gm_add(
                "master_guidance", claim="try route X", evidence="", project="proj_a",
                links={key: links[key] for key in (
                    "target_version", "obligation_id", "route_id",
                )},
            )
            assert out["id"]
            assert GlobalMemory(Path(root) / "proj_a").read("master_guidance")
            # path-escape / bad names are rejected
            for bad in ("../evil", "a/b", "", "/abs"):
                try:
                    server.gm_search("x", project=bad)
                    assert False, f"should reject project name {bad!r}"
                except RuntimeError:
                    pass
            # unknown project rejected
            try:
                server.gm_search("x", project="missing")
                assert False, "should reject unknown project"
            except RuntimeError:
                pass


def test_main_module_builds_and_runs():
    # `python -m danus.gateway` builds an app from DANUS_ROLE and calls .run();
    # stub FastMCP.run so no stdio server actually starts.
    import runpy
    from danus._mcp import FastMCP

    orig_run = FastMCP.run
    calls = {"n": 0}
    FastMCP.run = lambda self, *a, **k: calls.__setitem__("n", calls["n"] + 1)
    try:
        with _env(DANUS_ROLE="verifier"):
            runpy.run_module("danus.gateway", run_name="__main__")
        assert calls["n"] == 1
    finally:
        FastMCP.run = orig_run


def main() -> None:
    test_role_table()
    print("  [ok] role table (main no fact_submit; verifier read-only; worker submits)")
    test_role_env_default_and_build_app()
    print("  [ok] build_app reads DANUS_ROLE; _role default")
    test_project_by_name_without_agents_root_uses_default()
    print("  [ok] project-by-name without override uses cwd default")
    test_verify_http_roundtrip_and_errors()
    print("  [ok] _verify HTTP round-trip + unset-URL + bad-timeout fallback")
    test_fact_revoke_taints_without_deleting_mathematics()
    print("  [ok] fact_revoke taints descendants without deleting mathematics")
    test_search_arxiv_theorems_delegates()
    print("  [ok] search_arxiv_theorems delegates to integrations.search")
    test_main_module_builds_and_runs()
    print("  [ok] python -m danus.gateway builds app + calls run()")
    test_gm_and_fact_search_over_temp_project()
    print("  [ok] gm_add / gm_search / fact_search over a temp project")
    test_fact_submit_accept_writes_fact_and_traces()
    print("  [ok] fact_submit accept -> writes fact + verification trace")
    test_fact_submit_reject_writes_nothing_but_traces()
    print("  [ok] fact_submit reject -> writes nothing, still traces")
    test_fact_submit_verify_error_is_clean()
    print("  [ok] fact_submit verify-error -> clean error, no verdict")
    test_fact_submit_accept_but_write_failed_still_traces()
    print("  [ok] fact_submit accept-but-write-failed -> fact_id None + write_error, still traces correct")
    test_fact_submit_glossary_check_never_blocks()
    print("  [ok] fact_submit glossary heuristic never blocks submission")
    test_fact_submit_nondict_verify_body_is_clean()
    print("  [ok] fact_submit non-dict verify body -> clean error, nothing written")
    test_project_resolution_by_name_and_validation()
    print("  [ok] project resolution by name + path-escape validation")
    print("ALL GATEWAY TESTS PASSED")


if __name__ == "__main__":
    main()
