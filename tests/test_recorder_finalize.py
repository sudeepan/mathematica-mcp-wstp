"""Acceptance tests for Phase 4: finalization.

Tests the preflight checks (unresolved records block finalization),
the copy-to-fresh-kernel cycle, and that the finalized notebook is
a separate file from the recording.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp.recorder_ledger import RecorderLedger


# --- Pure-Python: preflight checks -------------------------------------------

def test_finalize_blocked_by_unresolved():
    """Finalization refuses when a non-COMPLETED record has no annotation."""
    from mathematica_wstp.recorder import Recorder

    d = tempfile.mkdtemp(prefix="rec-fin-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("digest1", "x = 1", "Input", tag)
        ledger.update_record(1, disposition={
            "execution_outcome": "FAILED",
            "control_intent": "NONE",
            "abort_confirmation": "NOT_APPLICABLE",
            "kernel_readiness": "FAULTED",
        })

        class FakeNotebooks:
            _sessions = {}
        rec = Recorder.__new__(Recorder)
        rec.notebooks = FakeNotebooks()
        rec.notebook_id = "hnb1"
        rec.notebook_path = nb_path
        rec.run_id = run_id
        rec.ledger = ledger

        result = rec.finalize()
        assert not result["success"]
        assert "unresolved" in result["error"]
        assert len(result["unresolved"]) == 1
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_finalize_allowed_when_annotated():
    """Once the failed record is annotated, finalize no longer blocks on it.

    This test only checks the preflight - it will fail at the save step
    since there is no real notebook, but the unresolved check passes.
    """
    from mathematica_wstp.recorder import Recorder

    d = tempfile.mkdtemp(prefix="rec-fin-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("digest1", "x = 1", "Input", tag)
        ledger.update_record(1, disposition={
            "execution_outcome": "TIMED_OUT",
            "control_intent": "SYSTEM_TIMEOUT",
            "abort_confirmation": "CONFIRMED",
            "kernel_readiness": "READY",
        })
        ledger.update_record(1, annotated=True,
                             annotation_reason="timed out; system timeout")

        class FakeNotebooks:
            _sessions = {}
            def save(self, **kw):
                return {"success": False, "error": "no real notebook"}
            def _call_with_session(self, *a, **kw):
                return {"success": True, "cells": [
                    {"record_tag": tag, "source_digest": "digest1",
                     "index": 0}
                ], "total": 1}
        rec = Recorder.__new__(Recorder)
        rec.notebooks = FakeNotebooks()
        rec.notebook_id = "hnb1"
        rec.notebook_path = nb_path
        rec.run_id = run_id
        rec.ledger = ledger

        result = rec.finalize()
        assert "unresolved" not in result
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Integration: full finalization cycle ------------------------------------

def test_finalize_end_to_end():
    """Record cells, finalize, verify the finalized notebook is separate."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.evaluator import evaluate_text
    from mathematica_wstp.session import WLResult

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-finalize-{uuid.uuid4().hex[:8]}.nb")
    finalized_path = os.path.splitext(path)[0] + "-finalized.nb"
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-finalize-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Finalize E2E", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)

        rec1 = recorder.record_and_verify("x = 2 + 3", style="Input")
        assert rec1.get("pre_dispatch_verified")
        result1 = evaluate_text("x = 2 + 3", timeout=30)
        disp1 = extract_disposition(result1, None, None)
        recorder.apply_outcome(rec1["seq"], disp1)

        rec2 = recorder.record_and_verify("y = x^2", style="Input")
        assert rec2.get("pre_dispatch_verified")
        result2 = evaluate_text("y = x^2", timeout=30)
        disp2 = extract_disposition(result2, None, None)
        recorder.apply_outcome(rec2["seq"], disp2)

        assert not recorder.has_unresolved()

        fin = recorder.finalize(timeout=120)
        assert fin.get("success"), fin
        assert fin.get("finalized") is True
        assert fin["recording_path"] == path
        assert fin["finalized_path"] == finalized_path
        assert os.path.exists(finalized_path)
        assert os.path.exists(path)
        assert finalized_path != path

        reloaded = RecorderLedger.load(recorder.ledger.path)
        assert reloaded.data.get("finalization", {}).get("success") is True
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        for p in (path, finalized_path):
            with contextlib.suppress(OSError):
                os.unlink(p)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_finalize_skips_non_evaluatable():
    """A cell annotated non-evaluatable is skipped during finalization."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.session import WLResult

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-skipeval-{uuid.uuid4().hex[:8]}.nb")
    finalized_path = os.path.splitext(path)[0] + "-finalized.nb"
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-skipeval-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Skip eval", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)

        rec1 = recorder.record_and_verify("good = 42", style="Input")
        disp_ok = {"execution_outcome": "COMPLETED", "control_intent": "NONE",
                   "abort_confirmation": "NOT_APPLICABLE",
                   "kernel_readiness": "READY"}
        recorder.apply_outcome(rec1["seq"], disp_ok)

        rec2 = recorder.record_and_verify("Pause[9999]", style="Input")
        disp_fail = {"execution_outcome": "TIMED_OUT",
                     "control_intent": "SYSTEM_TIMEOUT",
                     "abort_confirmation": "CONFIRMED",
                     "kernel_readiness": "READY"}
        outcome = recorder.apply_outcome(rec2["seq"], disp_fail)
        assert outcome["annotation"]["applied"] is True

        assert not recorder.has_unresolved()

        fin = recorder.finalize(timeout=120)
        assert fin.get("success"), fin
        assert os.path.exists(finalized_path)
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        for p in (path, finalized_path):
            with contextlib.suppress(OSError):
                os.unlink(p)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_finalize_via_notebooks_api():
    """finalize_recording() on HeadlessNotebooks delegates to the recorder."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.evaluator import evaluate_text

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-nbfin-{uuid.uuid4().hex[:8]}.nb")
    finalized_path = os.path.splitext(path)[0] + "-finalized.nb"
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-nbfin-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="NB Finalize", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        started = nb.start_recording(nbid)
        assert started.get("success")

        rec = nb.record_input("z = 7")
        assert rec.get("success")

        result = evaluate_text("z = 7", timeout=30)
        notice = session.take_kernel_change_notice()
        nb.record_outcome(rec["seq"], result, notice, None)

        fin = nb.finalize_recording(timeout=120)
        assert fin.get("success"), fin
        assert os.path.exists(finalized_path)

        nb.stop_recording()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        for p in (path, finalized_path):
            with contextlib.suppress(OSError):
                os.unlink(p)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


# --- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    pure_python = [
        test_finalize_blocked_by_unresolved,
        test_finalize_allowed_when_annotated,
    ]
    integration = [
        test_finalize_end_to_end,
        test_finalize_skips_non_evaluatable,
        test_finalize_via_notebooks_api,
    ]

    passed = failed = skipped = 0

    print("=== Pure-Python tests (preflight checks) ===")
    for fn in pure_python:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print("\n=== Integration tests (finalization cycle) ===")
    for fn in integration:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except ImportError as exc:
            skipped += 1
            print(f"  SKIP  {fn.__name__} ({exc})")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
