"""Acceptance tests for Phase 2: recorder core loop.

Tests the full record-verify-dispatch-verify cycle and raw disposition axes.
Pure-Python tests cover the Recorder class logic and disposition extraction.
Integration tests exercise the end-to-end flow through a live kernel.
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


# --- Pure-Python: disposition extraction -----------------------------------

def test_disposition_completed():
    """A successful evaluation produces COMPLETED/NONE/NOT_APPLICABLE/READY."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=True, text="42")
    d = extract_disposition(result, kernel_notice=None, kernel_verdict=None)
    assert d["execution_outcome"] == "COMPLETED"
    assert d["control_intent"] == "NONE"
    assert d["abort_confirmation"] == "NOT_APPLICABLE"
    assert d["kernel_readiness"] == "READY"


def test_disposition_timed_out_kernel_alive():
    """A timeout with kernel alive produces TIMED_OUT/SYSTEM_TIMEOUT/CONFIRMED/READY."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=False, timed_out=True, aborted=True,
                      error="timed out")
    d = extract_disposition(result, kernel_notice=None, kernel_verdict="alive")
    assert d["execution_outcome"] == "TIMED_OUT"
    assert d["control_intent"] == "SYSTEM_TIMEOUT"
    assert d["abort_confirmation"] == "CONFIRMED"
    assert d["kernel_readiness"] == "READY"


def test_disposition_timed_out_kernel_dead():
    """A timeout with dead kernel produces TIMED_OUT/SYSTEM_TIMEOUT/CONFIRMED/FAULTED."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=False, timed_out=True, aborted=True,
                      error="timed out")
    d = extract_disposition(result, kernel_notice=None, kernel_verdict="dead")
    assert d["execution_outcome"] == "TIMED_OUT"
    assert d["control_intent"] == "SYSTEM_TIMEOUT"
    assert d["abort_confirmation"] == "CONFIRMED"
    assert d["kernel_readiness"] == "FAULTED"


def test_disposition_timed_out_kernel_unverified():
    """A timeout with unverified kernel produces UNCERTAIN/FAULTED."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=False, timed_out=True, aborted=True,
                      error="timed out")
    d = extract_disposition(result, kernel_notice=None,
                            kernel_verdict="unverified")
    assert d["abort_confirmation"] == "UNCERTAIN"
    assert d["kernel_readiness"] == "FAULTED"


def test_disposition_abort_requested_during():
    """An abort during a successful eval: COMPLETED but USER_REQUESTED."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=True, text="partial", abort_requested_during=True)
    d = extract_disposition(result, kernel_notice=None, kernel_verdict=None)
    assert d["execution_outcome"] == "COMPLETED"
    assert d["control_intent"] == "USER_REQUESTED"


def test_disposition_kernel_replaced():
    """A kernel replacement sets kernel_readiness to RESTARTED."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=True, text="42")
    d = extract_disposition(result, kernel_notice="kernel was replaced",
                            kernel_verdict=None)
    assert d["kernel_readiness"] == "RESTARTED"


def test_disposition_failed():
    """A non-timeout, non-abort failure produces FAILED."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    result = WLResult(success=False, error="link dead")
    d = extract_disposition(result, kernel_notice=None, kernel_verdict=None)
    assert d["execution_outcome"] == "FAILED"
    assert d["control_intent"] == "NONE"
    assert d["abort_confirmation"] == "NOT_APPLICABLE"


# --- Pure-Python: ledger update_record -------------------------------------

def test_ledger_update_record():
    """update_record stores disposition axes and flushes to disk."""
    d = tempfile.mkdtemp(prefix="rec-core-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("digest1", "x = 1", "Input", tag)

        disposition = {
            "execution_outcome": "COMPLETED",
            "control_intent": "NONE",
            "abort_confirmation": "NOT_APPLICABLE",
            "kernel_readiness": "READY",
        }
        ledger.update_record(1, disposition=disposition, disposition_at=1000.0)

        reloaded = RecorderLedger.load(ledger.path)
        r = reloaded.record_by_seq(1)
        assert r["disposition"] == disposition
        assert r["disposition_at"] == 1000.0
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Integration: full record-verify-dispatch-verify cycle -----------------

def test_recorder_end_to_end():
    """The full cycle: start recording, evaluate, check ledger and disposition."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-e2e-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-e2e-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Recorder E2E", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        started = nb.start_recording(nbid)
        assert started.get("success"), started
        assert started.get("run_id")
        assert os.path.exists(started.get("ledger", ""))

        rec = nb.record_input("x = 1 + 2", style="Input")
        assert rec.get("success"), rec
        assert rec.get("seq") == 1
        assert rec.get("pre_dispatch_verified") is True
        assert rec.get("source_digest")
        assert rec.get("record_tag")

        # Each record's outcome is stored before the next record may start.
        from mathematica_wstp.evaluator import evaluate_text
        nb.record_outcome(rec["seq"], evaluate_text("x = 1 + 2", timeout=30), None, None)

        rec2 = nb.record_input("y = x^2", style="Input")
        assert rec2.get("seq") == 2

        readback = nb.read_back(notebook=nbid)
        assert readback.get("success")
        tagged = [c for c in readback["cells"] if c["record_tag"]]
        assert len(tagged) == 2

        stopped = nb.stop_recording(force=True)
        assert stopped.get("success")
        assert stopped.get("abandoned") is True
        assert stopped["recorder"]["ledger"]["records"] == 2
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_recorder_detects_mutation():
    """Post-eval verification catches a source change between record and verify."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-mutate-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-mutate-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Mutation detect", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("original = 1", style="Input")
        assert rec.get("pre_dispatch_verified")
        seq = rec["seq"]

        tagged_index = None
        rb = nb.read_back(notebook=nbid)
        for c in rb["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                tagged_index = c["index"]
                break
        assert tagged_index is not None

        nb.replace_cell(tagged_index, "tampered = 999", notebook=nbid)

        verification = recorder._verify_full()
        assert not verification["verified"], "should detect the tampered cell"
        assert any(i["issue"] == "source_changed" for i in verification["issues"])
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_recorder_detects_deletion():
    """Post-eval verification catches a deleted cell."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-delete-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-delete-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Delete detect", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("doomed = 1", style="Input")
        assert rec.get("pre_dispatch_verified")

        tagged_index = None
        rb = nb.read_back(notebook=nbid)
        for c in rb["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                tagged_index = c["index"]
                break
        assert tagged_index is not None

        nb.delete_cell(tagged_index, notebook=nbid)

        verification = recorder._verify_full()
        assert not verification["verified"]
        assert any(i["issue"] == "cell_missing" for i in verification["issues"])
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_disposition_stored_in_ledger_on_disk():
    """After a real evaluation, the ledger on disk has the disposition axes."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.evaluator import evaluate_text
    from mathematica_wstp.recorder import extract_disposition

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-disp-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-disp-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Disposition store", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        started = nb.start_recording(nbid)
        assert started.get("success"), started
        ledger_path = started["ledger"]

        rec = nb.record_input("1 + 1")
        assert rec.get("success") and rec.get("seq") == 1

        result = evaluate_text("1 + 1", timeout=30)
        notice = session.take_kernel_change_notice()
        kernel_verdict = None
        if result.timed_out:
            kernel_verdict, _ = session.verify_current_kernel()

        outcome = nb.record_outcome(1, result, notice, kernel_verdict)
        assert outcome is not None
        assert outcome["disposition"]["execution_outcome"] == "COMPLETED"

        reloaded = RecorderLedger.load(ledger_path)
        r = reloaded.record_by_seq(1)
        assert r["disposition"]["execution_outcome"] == "COMPLETED"
        assert r["disposition"]["kernel_readiness"] == "READY"

        nb.stop_recording()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


# --- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    pure_python = [
        test_disposition_completed,
        test_disposition_timed_out_kernel_alive,
        test_disposition_timed_out_kernel_dead,
        test_disposition_timed_out_kernel_unverified,
        test_disposition_abort_requested_during,
        test_disposition_kernel_replaced,
        test_disposition_failed,
        test_ledger_update_record,
    ]
    integration = [
        test_recorder_end_to_end,
        test_recorder_detects_mutation,
        test_recorder_detects_deletion,
        test_disposition_stored_in_ledger_on_disk,
    ]

    passed = failed = skipped = 0

    print("=== Pure-Python tests (disposition, ledger update) ===")
    for fn in pure_python:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print("\n=== Integration tests (recorder core loop) ===")
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
