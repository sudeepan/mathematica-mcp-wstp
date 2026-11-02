"""Tests for verify_derivation: three verdicts, a numerical witness, a clean scope.

The verdict and the two-precision rule are decided in Python from the facts
the kernel reports, so they are tested here without Mathematica. The
integration tests run the tool against a live kernel and are skipped where
no Wolfram installation can be found.

    .venv/bin/python tests/test_derivation.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import uuid
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp import server  # noqa: E402
from mathematica_wstp.server import _derivation_verdict, _numeric_witness  # noqa: E402


def sample(v30, v60, zero=False, clean=True, point=None) -> dict:
    return {"point": point or {"x": "-9"}, "v30": list(v30), "v60": list(v60),
            "zero": zero, "clean": clean}


def facts(simplified="unevaluated", samples=(), **extra) -> dict:
    return {"parsed": True, "simplified": simplified, "variables": ["x"],
            "functions": [], "points_tried": 5, "points_admitted": 5,
            "samples": list(samples), **extra}


# --- the decision, without Mathematica --------------------------------------

def test_fullsimplify_true_proves():
    assert _derivation_verdict(facts("True"))[0] == "proved"


def test_timeout_without_witness_is_undecided():
    verdict, why = _derivation_verdict(facts("timeout", [sample((0, 0), (0, 0), zero=True)]))
    assert verdict == "undecided" and "did not finish" in why


def test_unproved_and_zero_everywhere_is_undecided():
    verdict, why = _derivation_verdict(facts("unevaluated", [sample((0, 0), (0, 0), zero=True)] * 5))
    assert verdict == "undecided" and "5 evaluated" in why


def test_witness_agreeing_at_both_precisions_disproves():
    s = sample((18.0, 0.0), (18.0, 0.0))
    assert _derivation_verdict(facts("unevaluated", [s]))[0] == "disproved"
    assert _numeric_witness([s]) == {"point": {"x": "-9"}, "value": "18"}


def test_witness_at_one_precision_only_is_undecided():
    s = sample((18.0, 0.0), (17.5, 0.0))
    assert _numeric_witness([s]) is None
    assert _derivation_verdict(facts("unevaluated", [s]))[0] == "undecided"


def test_witness_with_a_message_is_not_trusted():
    s = sample((18.0, 0.0), (18.0, 0.0), clean=False)
    assert _derivation_verdict(facts("unevaluated", [s]))[0] == "undecided"


def test_fullsimplify_false_without_witness_is_undecided():
    verdict, why = _derivation_verdict(facts("False"))
    assert verdict == "undecided" and "reports the sides unequal" in why


def test_complex_witness_is_reported_with_both_parts():
    s = sample((1.0, 2.0), (1.0, 2.0))
    assert _numeric_witness([s])["value"] == "1 + 2 I"


def test_undecided_reasons_name_the_obstacle():
    assert "functions without definitions: f" in _derivation_verdict(
        facts(functions=["f"]))[1]
    assert "satisfied the assumptions" in _derivation_verdict(
        facts(points_admitted=0), assumptions="x > 0")[1]
    assert _derivation_verdict({"parsed": False}) == (
        "undecided", "a side or the assumptions could not be parsed")


# --- against a live kernel ---------------------------------------------------

def run(steps, **kw) -> dict:
    return server.verify_derivation(steps, **kw).structured_content


def test_a_true_identity_is_proved():
    r = run(["x^2 - 1", "(x - 1)(x + 1)"])
    assert r["all_verified"] and r["checks"][0]["verdict"] == "proved", r
    assert r["scope"] == "clean"


def test_a_false_step_is_disproved_with_a_witness():
    r = run(["Sqrt[x^2]", "x"])
    check = r["checks"][0]
    assert check["verdict"] == "disproved" and r["first_failing_step"] == 1, r
    assert Fraction(check["witness"]["point"]["x"]) < 0, check
    assert run(["Sqrt[x^2]", "x"], assumptions="x > 0")["checks"][0]["verdict"] == "proved"


def test_session_definitions_do_not_apply_by_default():
    from mathematica_wstp.evaluator import evaluate_text
    name = "dv" + uuid.uuid4().hex[:8].upper()
    assert evaluate_text(f"{name} = 2", timeout=30).success
    try:
        assert run([f"{name}^2", "4"])["checks"][0]["verdict"] == "disproved"
        scoped = run([f"{name}^2", "4"], contexts=["Global`"])
        assert scoped["checks"][0]["verdict"] == "proved" and scoped["scope"] == ["Global`"]
    finally:
        evaluate_text(f"Remove[{name}]", timeout=30)


def test_an_unproved_true_identity_is_undecided_never_disproved():
    """K(1/2) is Gamma(1/4)^2/(4 Sqrt[Pi]), which FullSimplify does not show."""
    r = run(["EllipticK[1/2]", "Gamma[1/4]^2/(4 Sqrt[Pi])"], timeout=20)
    assert r["checks"][0]["verdict"] == "undecided", r
    assert r["first_failing_step"] is None and r["first_undecided_step"] == 1


def test_the_session_is_left_as_it_was():
    from mathematica_wstp.evaluator import evaluate_text
    assert evaluate_text("SeedRandom[42]; mcpDvR = RandomReal[]; SeedRandom[42];", timeout=30).success
    run(["Sqrt[x^2]", "x"])
    assert evaluate_text("mcpDvR == RandomReal[]", timeout=30).text.strip() == "True"
    leftover = evaluate_text('Select[Contexts[], StringStartsQ[#, "MCPDerivation"] &]', timeout=30)
    assert leftover.text.strip() == "{}", leftover.text
    evaluate_text("Remove[mcpDvR]", timeout=30)


if __name__ == "__main__":
    import traceback
    from wolfram_available import missing_wolfram

    pure_python = [
        test_fullsimplify_true_proves,
        test_timeout_without_witness_is_undecided,
        test_unproved_and_zero_everywhere_is_undecided,
        test_witness_agreeing_at_both_precisions_disproves,
        test_witness_at_one_precision_only_is_undecided,
        test_witness_with_a_message_is_not_trusted,
        test_fullsimplify_false_without_witness_is_undecided,
        test_complex_witness_is_reported_with_both_parts,
        test_undecided_reasons_name_the_obstacle,
    ]
    integration = [
        test_a_true_identity_is_proved,
        test_a_false_step_is_disproved_with_a_witness,
        test_session_definitions_do_not_apply_by_default,
        test_an_unproved_true_identity_is_undecided_never_disproved,
        test_the_session_is_left_as_it_was,
    ]
    passed = failed = skipped = 0
    print("=== verify_derivation: the decision ===")
    for fn in pure_python:
        try:
            fn(); passed += 1; print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1; print(f"  FAIL  {fn.__name__}"); traceback.print_exc()
    no_wolfram = missing_wolfram()
    print("\n=== verify_derivation: against a live kernel ===")
    try:
        for fn in integration:
            if no_wolfram:
                skipped += 1; print(f"  SKIP  {fn.__name__} ({no_wolfram})"); continue
            try:
                fn(); passed += 1; print(f"  PASS  {fn.__name__}")
            except Exception:
                failed += 1; print(f"  FAIL  {fn.__name__}"); traceback.print_exc()
    finally:
        if not no_wolfram:
            from mathematica_wstp import session
            with contextlib.suppress(Exception):
                session.close_kernel()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
