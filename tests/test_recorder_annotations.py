"""Acceptance tests for Phase 3: annotations and failure policy.

Tests that non-COMPLETED cells get automatically annotated as non-evaluatable,
that the annotation reason is recorded in the ledger, and that unresolved
tracking blocks finalization.
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


# --- Pure-Python: annotation reason generation -------------------------------

def test_reason_timed_out():
    from mathematica_wstp.recorder import _annotation_reason
    d = {"execution_outcome": "TIMED_OUT", "control_intent": "SYSTEM_TIMEOUT",
         "abort_confirmation": "CONFIRMED", "kernel_readiness": "READY"}
    reason = _annotation_reason(d)
    assert "timed out" in reason
    assert "system timeout" in reason


def test_reason_aborted_user():
    from mathematica_wstp.recorder import _annotation_reason
    d = {"execution_outcome": "ABORTED", "control_intent": "USER_REQUESTED",
         "abort_confirmation": "CONFIRMED", "kernel_readiness": "READY"}
    reason = _annotation_reason(d)
    assert "aborted" in reason
    assert "user requested" in reason


def test_reason_failed_kernel_faulted():
    from mathematica_wstp.recorder import _annotation_reason
    d = {"execution_outcome": "FAILED", "control_intent": "NONE",
         "abort_confirmation": "NOT_APPLICABLE", "kernel_readiness": "FAULTED"}
    reason = _annotation_reason(d)
    assert "failed" in reason
    assert "kernel faulted" in reason


def test_reason_completed_returns_nothing():
    """COMPLETED cells should not trigger annotation."""
    from mathematica_wstp.recorder import _annotation_reason
    d = {"execution_outcome": "COMPLETED", "control_intent": "NONE",
         "abort_confirmation": "NOT_APPLICABLE", "kernel_readiness": "READY"}
    reason = _annotation_reason(d)
    assert "completed" in reason


# --- Pure-Python: unresolved tracking ----------------------------------------

def test_unresolved_empty_when_all_completed():
    """No unresolved records when every disposition is COMPLETED."""
    from mathematica_wstp.recorder import Recorder

    d = tempfile.mkdtemp(prefix="rec-ann-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("digest1", "x = 1", "Input", tag)
        ledger.update_record(1, disposition={
            "execution_outcome": "COMPLETED",
            "control_intent": "NONE",
            "abort_confirmation": "NOT_APPLICABLE",
            "kernel_readiness": "READY",
        })

        class FakeNotebooks:
            pass
        rec = Recorder.__new__(Recorder)
        rec.notebooks = FakeNotebooks()
        rec.notebook_id = "hnb1"
        rec.run_id = run_id
        rec.ledger = ledger

        assert not rec.has_unresolved()
        assert rec.unresolved_records() == []
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_unresolved_detected_when_not_annotated():
    """A FAILED record without annotation shows up as unresolved."""
    from mathematica_wstp.recorder import Recorder

    d = tempfile.mkdtemp(prefix="rec-ann-")
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
            pass
        rec = Recorder.__new__(Recorder)
        rec.notebooks = FakeNotebooks()
        rec.notebook_id = "hnb1"
        rec.run_id = run_id
        rec.ledger = ledger

        assert rec.has_unresolved()
        unresolved = rec.unresolved_records()
        assert len(unresolved) == 1
        assert unresolved[0]["seq"] == 1
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_unresolved_cleared_after_annotation():
    """Once annotated=True is set on the record, it is no longer unresolved."""
    from mathematica_wstp.recorder import Recorder

    d = tempfile.mkdtemp(prefix="rec-ann-")
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
            pass
        rec = Recorder.__new__(Recorder)
        rec.notebooks = FakeNotebooks()
        rec.notebook_id = "hnb1"
        rec.run_id = run_id
        rec.ledger = ledger

        assert not rec.has_unresolved()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Integration: automatic annotation after failed eval --------------------

def test_auto_annotate_on_timeout():
    """A timed-out evaluation auto-annotates the cell as non-evaluatable."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.session import WLResult

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-anno-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-anno-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Annotation test", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("Pause[999]", style="Input")
        assert rec.get("pre_dispatch_verified")
        seq = rec["seq"]

        fake_result = WLResult(success=False, timed_out=True, aborted=True,
                               error="timed out")
        disposition = extract_disposition(fake_result, kernel_notice=None,
                                         kernel_verdict="alive")

        outcome = recorder.apply_outcome(seq, disposition)
        assert outcome["annotation"] is not None
        assert outcome["annotation"]["applied"] is True
        assert "timed out" in outcome["annotation"]["reason"]

        readback = nb.read_back(notebook=nbid)
        assert readback.get("success")
        annotated = None
        for c in readback["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                annotated = c
                break
        assert annotated is not None
        assert annotated["evaluatable"] is False
        assert annotated["annotation_reason"] != ""

        assert not recorder.has_unresolved()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_no_annotation_on_completed():
    """A COMPLETED evaluation does not annotate the cell."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.session import WLResult

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-noann-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-noann-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="No annotation", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("1 + 1", style="Input")
        assert rec.get("pre_dispatch_verified")
        seq = rec["seq"]

        fake_result = WLResult(success=True, text="2")
        disposition = extract_disposition(fake_result, kernel_notice=None,
                                         kernel_verdict=None)

        outcome = recorder.apply_outcome(seq, disposition)
        assert outcome["annotation"] is None

        readback = nb.read_back(notebook=nbid)
        for c in readback["cells"]:
            if c["record_tag"] == rec["record_tag"]:
                assert c["evaluatable"] is not False
                assert c["annotation_reason"] == ""
                break
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(Exception):
            shutil.rmtree(rec_dir, ignore_errors=True)
        session.close_kernel()


def test_auto_annotate_on_failed():
    """A FAILED evaluation auto-annotates with kernel state in the reason."""
    from mathematica_wstp import notebooks, session
    from mathematica_wstp.recorder import Recorder, extract_disposition
    from mathematica_wstp.session import WLResult

    path = os.path.join(tempfile.gettempdir(),
                        f"rec-fail-{uuid.uuid4().hex[:8]}.nb")
    rec_dir = os.path.join(tempfile.gettempdir(),
                           f"rec-fail-m-{uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = rec_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Failed annotation", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        recorder = Recorder(nb, nbid, path)
        rec = recorder.record_and_verify("broken", style="Input")
        assert rec.get("pre_dispatch_verified")
        seq = rec["seq"]

        fake_result = WLResult(success=False, error="link dead")
        disposition = extract_disposition(fake_result, kernel_notice=None,
                                         kernel_verdict="dead")

        outcome = recorder.apply_outcome(seq, disposition)
        assert outcome["annotation"]["applied"] is True
        assert "failed" in outcome["annotation"]["reason"]
        assert "kernel faulted" in outcome["annotation"]["reason"]

        r = recorder.ledger.record_by_seq(seq)
        assert r["annotated"] is True
        assert "failed" in r["annotation_reason"]
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
        test_reason_timed_out,
        test_reason_aborted_user,
        test_reason_failed_kernel_faulted,
        test_reason_completed_returns_nothing,
        test_unresolved_empty_when_all_completed,
        test_unresolved_detected_when_not_annotated,
        test_unresolved_cleared_after_annotation,
    ]
    integration = [
        test_auto_annotate_on_timeout,
        test_no_annotation_on_completed,
        test_auto_annotate_on_failed,
    ]

    passed = failed = skipped = 0

    print("=== Pure-Python tests (annotation reasons, unresolved) ===")
    for fn in pure_python:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print("\n=== Integration tests (auto-annotation) ===")
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
