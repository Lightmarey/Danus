"""Offline tests for danus.verify — no codex, no API spend.

Prechecks are pure-function unit-tested; the full request path (pre-checks →
subprocess spawn → verification.json readback → verdict propagation) is exercised
by pointing DANUS_CODEX_BIN at ``fake_codex.py`` (a deterministic stub) and calling the
``/verify`` endpoint function directly (avoids an httpx TestClient dependency).

Runs standalone (``python -m danus.verify.tests.test_verify``) and under pytest.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from danus.verify import prechecks
from danus.verify.service import VerifyRequest, app, verify
from danus.tests.portable import write_python_launcher

FAKE = Path(__file__).resolve().parent / "fake_codex.py"

_GOOD_STATEMENT = "For every integer n, n + 0 equals n."
_GOOD_PROOF = (
    "Zero is the additive identity of the integers, so adding zero to any integer n "
    "leaves the value unchanged. Hence n + 0 = n for every integer n, as required."
)


@contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            os.environ[k] = v if v is not None else os.environ.get(k, "")
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

def _fake_launcher(tmp: Path) -> Path:
    return write_python_launcher(tmp, "fake_codex", FAKE.read_text(encoding="utf-8"))


def _call(statement, proof, tmp):
    tmp = Path(tmp)
    fake = _fake_launcher(tmp)
    prompt = f"Statement: {statement}\nProof:\n{proof}"
    with _env(
        DANUS_CODEX_BIN=str(fake),
        VERIFIER_RESULTS_DIR=str(tmp / "runs"),
        VERIFY_AGENT_HOME=str(tmp),
        FAKE_CODEX_PROMPT=prompt,
    ):
        return verify(VerifyRequest(statement=statement, proof=proof))


def test_prechecks_units():
    assert prechecks.is_vacuous_proof("QED")[0] is True
    assert prechecks.is_vacuous_statement("x")[0] is True
    assert prechecks.is_vacuous_proof(_GOOD_PROOF)[0] is False
    assert prechecks.check_problem_md_citation("The claim holds as declared in problem.md, done.") is not None
    assert prechecks.check_vague_gestures("As it is well known that the bound follows.") is not None
    assert prechecks.check_problem_md_citation(_GOOD_PROOF) is None
    # a real statement + proof passes every pre-check
    assert prechecks.run_prechecks(_GOOD_STATEMENT, _GOOD_PROOF) is None


def test_verify_accept_via_fake_codex():
    with tempfile.TemporaryDirectory() as tmp:
        out = _call(_GOOD_STATEMENT, _GOOD_PROOF, tmp)
        assert out["verdict"] == "correct" and out["verification_report"]["critical_errors"] == []


def test_verify_reject_via_fake_codex():
    with tempfile.TemporaryDirectory() as tmp:
        out = _call(_GOOD_STATEMENT, _GOOD_PROOF + " [[FAKE:wrong]]", tmp)
        assert out["verdict"] == "wrong" and out["repair_hints"]


def test_verify_batch_mixed_via_one_fake_codex():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake = _fake_launcher(root)
        fake_candidates = [
            {"candidate_id": "a", "statement": _GOOD_STATEMENT, "proof": _GOOD_PROOF},
            {"candidate_id": "b", "statement": _GOOD_STATEMENT + " Again.",
             "proof": _GOOD_PROOF + " [[FAKE:wrong]]"},
        ]
        with _env(
            DANUS_CODEX_BIN=str(fake), VERIFIER_RESULTS_DIR=str(root / "runs"),
            VERIFY_AGENT_HOME=str(root),
            # cmd.exe strips JSON quotes from forwarded argv; feed the fake the
            # same prompt through env so this Windows plumbing test stays exact.
            FAKE_CODEX_PROMPT=(
                "Candidates (verify each independently):\n"
                + json.dumps(fake_candidates)
            ),
        ):
            response = TestClient(app).post("/verify-batch", json={
                "verification_goal": "Integer identities", "candidates": fake_candidates,
            })
            assert response.status_code == 200
            out = response.json()
        assert [item["verdict"] for item in out["verifications"]] == ["correct", "wrong"]
        assert len(list((root / "runs").iterdir())) == 1
        assert out["usage"] == {
            "input_tokens": 120, "cached_input_tokens": 80, "fresh_input_tokens": 40,
            "output_tokens": 20, "reasoning_tokens": 10,
        }


def test_verify_request_id_replays_completed_result_without_new_codex():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake = _fake_launcher(root)
        request_id = "a" * 64
        with _env(
            DANUS_CODEX_BIN=str(fake), VERIFIER_RESULTS_DIR=str(root / "runs"),
            VERIFY_AGENT_HOME=str(root),
            FAKE_CODEX_PROMPT=f"Statement: {_GOOD_STATEMENT}\nProof:\n{_GOOD_PROOF}",
        ):
            request = VerifyRequest(
                statement=_GOOD_STATEMENT, proof=_GOOD_PROOF, request_id=request_id,
            )
            first = verify(request)
            os.environ["DANUS_CODEX_BIN"] = str(root / "must-not-run")
            second = verify(request)

        assert first == second
        assert first["usage"]["input_tokens"] == 120
        assert [path.name for path in (root / "runs").iterdir() if path.is_dir()] == [
            f"request_{request_id}"
        ]


def test_verify_vacuous_rejected_400():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _call("Trivial lemma about integers.", "QED", tmp)
            assert False, "vacuous proof should be rejected"
        except HTTPException as e:
            assert e.status_code == 400


def test_verify_precheck_p1_rejected_400():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _call("Some lemma that is self-contained and long enough to pass.",
                  "The result holds as declared in problem.md, which lists it as a verified "
                  "building block, so we are done with the argument here.", tmp)
            assert False, "P1 problem.md citation should be rejected"
        except HTTPException as e:
            assert e.status_code == 400


def main() -> None:
    test_prechecks_units()
    print("  [ok] prechecks units (vacuous + P1/P5 + clean passes)")
    test_verify_accept_via_fake_codex()
    print("  [ok] /verify accept via fake_codex -> verdict correct")
    test_verify_reject_via_fake_codex()
    print("  [ok] /verify reject via fake_codex ([[FAKE:wrong]]) -> verdict wrong")
    test_verify_vacuous_rejected_400()
    print("  [ok] /verify vacuous -> 400")
    test_verify_precheck_p1_rejected_400()
    print("  [ok] /verify P1 problem.md citation -> 400")
    print("ALL VERIFY TESTS PASSED")


if __name__ == "__main__":
    main()
