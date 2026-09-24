"""Acceptance tests for Phase 5: cross-phase and adversarial scenarios.

These tests exercise the recorder across phase boundaries and with
inputs that should not happen but could: tampered cells, deleted cells,
injected cells, double finalization, and ledger persistence across
process boundaries.
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


# --- Pure-Python: ledger edge cases ------------------------------------------

def test_update_record_nonexistent_seq():
    """Updating a seq that does not exist is a silent no-op."""
    d = tempfile.mkdtemp(prefix="rec-adv-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("digest1", "x = 1", "Input", tag)

        ledger.update_record(999, disposition={"execution_outcome": "COMPLETED"})

        r = ledger.record_by_seq(1)
        assert "disposition" not in r
        assert ledger.record_by_seq(999) is None
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_ledger_survives_reload():
    """A ledger reloaded from disk has all records and can be extended."""
    d = tempfile.mkdtemp(prefix="rec-adv-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        t1 = ledger.make_tag()
        ledger.append("d1", "x = 1", "Input", t1)
        t2 = ledger.make_tag()
        ledger.append("d2", "y = 2", "Input", t2)
        ledger.update_record(1, disposition={
            "execution_outcome": "COMPLETED",
            "control_intent": "NONE",
            "abort_confirmation": "NOT_APPLICABLE",
            "kernel_readiness": "READY",
        })
        path = ledger.path

        reloaded = RecorderLedger.load(path)
        assert reloaded.run_id == run_id
        assert len(reloaded.records) == 2
        assert reloaded.next_seq == 3
        assert reloaded.record_by_seq(1)["disposition"]["execution_outcome"] == "COMPLETED"
        assert reloaded.record_by_tag(t2)["seq"] == 2

        t3 = reloaded.make_tag()
        reloaded.append("d3", "z = 3", "Input", t3)
        assert len(reloaded.records) == 3
        assert reloaded.next_seq == 4

        final = RecorderLedger.load(path)
        assert len(final.records) == 3
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_unresolved_includes_records_without_disposition():
    """A record with no disposition IS unresolved - it was never dispatched."""
    from mathematica_wstp.recorder import Recorder

    d = tempfile.mkdtemp(prefix="rec-adv-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("d1", "x = 1", "Input", tag)

        class FakeNotebooks:
            pass
        rec = Recorder.__new__(Recorder)
        rec.notebooks = FakeNotebooks()
        rec.notebook_id = "hnb1"
        rec.notebook_path = nb_path
        rec.run_id = run_id
        rec.ledger = ledger

        assert rec.has_unresolved(), "a record with no disposition must be unresolved"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_disposition_all_axis_combinations():
    """Every execution_outcome value produces a valid disposition dict."""
    from mathematica_wstp.recorder import extract_disposition
    from mathematica_wstp.session import WLResult

    cases = [
        (WLResult(success=True, text="ok"), None, None, "COMPLETED"),
        (WLResult(success=False, timed_out=True, aborted=True), None, "alive", "TIMED_OUT"),
        (WLResult(success=False, aborted=True), None, "alive", "ABORTED"),
        (WLResult(success=False, error="link dead"), None, "dead", "FAILED"),
        (WLResult(success=True, text="ok", abort_requested_during=True), None, None, "COMPLETED"),
        (WLResult(success=False, timed_out=True, aborted=True), "replaced", "alive", "TIMED_OUT"),
    ]
    for result, notice, verdict, expected_outcome in cases:
        d = extract_disposition(result, notice, verdict)
        assert d["execution_outcome"] == expected_outcome, f"expected {expected_outcome}, got {d}"
        for key in ("execution_outcome", "control_intent",
                    "abort_confirmation", "kernel_readiness"):
            assert key in d and isinstance(d[key], str)


# --- Integration: cross-phase scenarios --------------------------------------

def test_full_lifecycle_mixed_outcomes():
    """Three cells: ok, timeout (annotated), ok. Then finalize."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.evaluator import evaluate_text
    from mathematica_wstp.session import WLResult

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-mixed-{uuid.uuid4().hex[:8]}.nb")
    finalized_path = os.path.splitext(path)[0] + "-finalized.nb"
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-mixed-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Mixed lifecycle", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        finalized_path = os.path.splitext(path)[0] + f"-{recorder.run_id}-finalized.nb"

        # Cell 1: successful
        rec1 = recorder.record_and_verify("a = 10", style="Input")
        assert rec1["pre_dispatch_verified"]
        r1 = evaluate_text("a = 10", timeout=30)
        d1 = extract_disposition(r1, None, None)
        assert d1["execution_outcome"] == "COMPLETED"
        recorder.apply_outcome(rec1["seq"], d1)

        # Cell 2: fake a timeout (don't actually run Pause[999])
        rec2 = recorder.record_and_verify("Pause[999]", style="Input")
        assert rec2["pre_dispatch_verified"]
        fake_timeout = WLResult(success=False, timed_out=True, aborted=True,
                                error="timed out")
        d2 = extract_disposition(fake_timeout, None, "alive")
        outcome2 = recorder.apply_outcome(rec2["seq"], d2)
        assert outcome2["annotation"]["applied"] is True

        # Cell 3: successful
        rec3 = recorder.record_and_verify("b = a + 1", style="Input")
        assert rec3["pre_dispatch_verified"]
        r3 = evaluate_text("b = a + 1", timeout=30)
        d3 = extract_disposition(r3, None, None)
        recorder.apply_outcome(rec3["seq"], d3)

        assert not recorder.has_unresolved()

        # Verify ledger state before finalize
        assert len(recorder.ledger.records) == 3
        r = recorder.ledger.record_by_seq(2)
        assert r["annotated"] is True
        assert r["disposition"]["execution_outcome"] == "TIMED_OUT"

        # Finalize
        fin = recorder.finalize(timeout=120)
        assert fin.get("success"), fin
        assert os.path.exists(finalized_path)

        # Verify ledger records finalization
        reloaded = RecorderLedger.load(recorder.ledger.path)
        assert reloaded.data["finalization"]["success"] is True
        assert reloaded.data["finalization"]["path"] == finalized_path
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


def test_mutation_blocks_finalization():
    """Tampering with a recorded cell makes finalize fail at verification."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.evaluator import evaluate_text

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-tamper-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-tamper-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Tamper test", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)

        rec = recorder.record_and_verify("original = 1", style="Input")
        r = evaluate_text("original = 1", timeout=30)
        d = extract_disposition(r, None, None)
        recorder.apply_outcome(rec["seq"], d)

        # Tamper: replace the cell source
        rb = nb.read_back(notebook=nbid)
        for c in rb["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                nb.replace_cell(c["index"], "tampered = 999", notebook=nbid)
                break

        fin = recorder.finalize(timeout=120)
        assert not fin["success"]
        assert "verification failed" in fin["error"]
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_deletion_blocks_finalization():
    """Deleting a recorded cell makes finalize fail at verification."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.evaluator import evaluate_text

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-delfin-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-delfin-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Delete blocks finalize", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("doomed = 1", style="Input")
        r = evaluate_text("doomed = 1", timeout=30)
        d = extract_disposition(r, None, None)
        recorder.apply_outcome(rec["seq"], d)

        # Delete the cell
        rb = nb.read_back(notebook=nbid)
        for c in rb["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                nb.delete_cell(c["index"], notebook=nbid)
                break

        fin = recorder.finalize(timeout=120)
        assert not fin["success"]
        assert "verification failed" in fin["error"]
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_injection_blocks_finalization():
    """An executable cell injected outside the recorder blocks finalize.

    The converse check verifies that every executable cell in the notebook
    has a ledger entry. An untagged executable injection is an integrity
    violation.
    """
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.evaluator import evaluate_text

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-inject-{uuid.uuid4().hex[:8]}.nb")
    finalized_path = os.path.splitext(path)[0] + "-finalized.nb"
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-inject-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Injection test", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("legit = 1", style="Input")
        r = evaluate_text("legit = 1", timeout=30)
        d = extract_disposition(r, None, None)
        recorder.apply_outcome(rec["seq"], d)

        # Inject an untagged executable cell
        nb.write_cell("injected = 999", style="Input", notebook=nbid)

        # Finalize must fail - the injected cell has no ledger entry
        fin = recorder.finalize(timeout=120)
        assert not fin["success"], "injection must block finalization"
        assert "verification failed" in fin.get("error", "")
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


def test_re_annotate_updates_reason():
    """Annotating a cell twice updates the reason, not duplicates it."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-reann-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-reann-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Re-annotate", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("test = 1", style="Input")
        assert rec["pre_dispatch_verified"]

        # First annotation
        rb = nb.read_back(notebook=nbid)
        idx = None
        for c in rb["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                idx = c["index"]
                break
        assert idx is not None

        nb.annotate_cell(idx, evaluatable=False, reason="first reason",
                         notebook=nbid)

        # Second annotation with different reason
        nb.annotate_cell(idx, evaluatable=True, reason="cleared",
                         notebook=nbid)

        # Verify the cell has the second reason, not both
        rb2 = nb.read_back(notebook=nbid)
        for c in rb2["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                assert c["evaluatable"] is True
                assert c["annotation_reason"] == "cleared"
                assert c["record_tag"] == rec["record_tag"]
                break
        else:
            raise AssertionError("annotated cell not found")
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_finalize_no_recorder_active():
    """finalize_recording() with no active recorder returns a clear error."""
    from mathematica_wstp import notebooks, session

    try:
        nb = notebooks.get_headless_notebooks()
        result = nb.finalize_recording()
        assert not result["success"]
        assert "no recording" in result["error"]
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        session.close_kernel()


# --- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    pure_python = [
        test_update_record_nonexistent_seq,
        test_ledger_survives_reload,
        test_unresolved_includes_records_without_disposition,
        test_disposition_all_axis_combinations,
    ]
    integration = [
        test_full_lifecycle_mixed_outcomes,
        test_mutation_blocks_finalization,
        test_deletion_blocks_finalization,
        test_injection_blocks_finalization,
        test_re_annotate_updates_reason,
        test_finalize_no_recorder_active,
    ]

    passed = failed = skipped = 0

    print("=== Pure-Python tests (adversarial ledger, disposition) ===")
    for fn in pure_python:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print("\n=== Integration tests (cross-phase, adversarial) ===")
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
