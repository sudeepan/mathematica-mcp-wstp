"""Hardening tests: Phases 7-10 invariant enforcement.

Each phase adds its own section. Tests are pure-Python where possible,
falling back to integration only when the invariant crosses the kernel.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp.recorder import Recorder
from mathematica_wstp.recorder_ledger import RecorderLedger


# --- helpers ---------------------------------------------------------------

def _make_recorder(tmp: str) -> tuple[Recorder, str]:
    """Build a Recorder backed by a fake notebooks object."""
    nb_path = os.path.join(tmp, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(tmp, "ledgers")

    class FakeNotebooks:
        _recorder = None

        @property
        def has_active_recorder(self) -> bool:
            return self._recorder is not None

    notebooks = FakeNotebooks()
    rec = Recorder(notebooks, "hnb1", nb_path)
    notebooks._recorder = rec
    return rec, nb_path


# --- Phase 7: fail closed before dispatch ----------------------------------

def test_readback_failure_returns_success_false():
    """record_and_verify returns success=False when readback fails."""
    d = tempfile.mkdtemp(prefix="rec-h7-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        call_log = []

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self) -> bool:
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                call_log.append(fn)
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {"success": False, "error": "kernel not responding"}
                return {"success": False, "error": "unexpected call"}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        result = rec.record_and_verify("x = 1", style="Input")

        assert not result["success"], "readback failure must return success=False"
        assert "read-back" in result.get("error", "").lower() or "verification" in result.get("error", "").lower()
        assert "MCPWriteCell" in call_log
        assert "MCPReadBack" in call_log
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_readback_failure_does_not_append_to_ledger():
    """When readback fails, no record is appended to the ledger."""
    d = tempfile.mkdtemp(prefix="rec-h7b-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self) -> bool:
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {"success": False, "error": "link dead"}
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        result = rec.record_and_verify("x = 1")
        assert not result["success"]
        assert len(rec.ledger.records) == 0, "no record should be appended on readback failure"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_cell_not_found_returns_success_false():
    """record_and_verify returns success=False when the tag is not in readback."""
    d = tempfile.mkdtemp(prefix="rec-h7c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self) -> bool:
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {
                        "success": True,
                        "total": 1,
                        "cells": [{"record_tag": "wrong-tag", "source_digest": "abc"}],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        result = rec.record_and_verify("x = 1")
        assert not result["success"]
        assert "not found" in result.get("error", "")
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_write_failure_returns_success_false():
    """record_and_verify returns success=False when cell write fails."""
    d = tempfile.mkdtemp(prefix="rec-h7d-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self) -> bool:
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": False, "error": "write refused"}
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        result = rec.record_and_verify("x = 1")
        assert not result["success"]
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_has_active_recorder_property():
    """has_active_recorder reflects whether an integrity recorder is set."""
    from mathematica_wstp.notebooks import HeadlessNotebooks
    nb = HeadlessNotebooks()
    assert not nb.has_active_recorder
    nb._recorder = object()
    assert nb.has_active_recorder
    nb._recorder = None
    assert not nb.has_active_recorder


# --- Phase 8: missing disposition = unresolved ----------------------------

def test_no_disposition_is_unresolved():
    """A record with no disposition must be treated as unresolved."""
    d = tempfile.mkdtemp(prefix="rec-h8-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("d1", "x = 1", "Input", tag)

        rec = Recorder.__new__(Recorder)
        rec.notebooks = type("FN", (), {})()
        rec.notebook_id = "hnb1"
        rec.notebook_path = nb_path
        rec.run_id = run_id
        rec.ledger = ledger

        unresolved = rec.unresolved_records()
        assert len(unresolved) == 1
        assert unresolved[0]["record_tag"] == tag
        assert rec.has_unresolved()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_completed_disposition_is_resolved():
    """A record with COMPLETED disposition is not unresolved."""
    d = tempfile.mkdtemp(prefix="rec-h8b-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("d1", "x = 1", "Input", tag)
        ledger.update_record(1, disposition={
            "execution_outcome": "COMPLETED",
            "control_intent": "NONE",
            "abort_confirmation": "NOT_APPLICABLE",
            "kernel_readiness": "READY",
        })

        rec = Recorder.__new__(Recorder)
        rec.notebooks = type("FN", (), {})()
        rec.notebook_id = "hnb1"
        rec.notebook_path = nb_path
        rec.run_id = run_id
        rec.ledger = ledger

        assert not rec.has_unresolved()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_no_disposition_blocks_finalization():
    """finalize() refuses when any record has no disposition."""
    d = tempfile.mkdtemp(prefix="rec-h8c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("d1", "x = 1", "Input", tag)

        rec = Recorder.__new__(Recorder)
        rec.notebooks = type("FN", (), {})()
        rec.notebook_id = "hnb1"
        rec.notebook_path = nb_path
        rec.run_id = run_id
        rec.ledger = ledger

        fin = rec.finalize(timeout=120)
        assert not fin["success"]
        assert "unresolved" in fin["error"]
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        # Phase 7
        test_readback_failure_returns_success_false,
        test_readback_failure_does_not_append_to_ledger,
        test_cell_not_found_returns_success_false,
        test_write_failure_returns_success_false,
        test_has_active_recorder_property,
        # Phase 8
        test_no_disposition_is_unresolved,
        test_completed_disposition_is_resolved,
        test_no_disposition_blocks_finalization,
    ]

    passed = failed = 0
    print("=== Phases 7-8: hardening tests ===")
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
