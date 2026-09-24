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


# --- Phase 9: recording boundary and converse check ----------------------

def test_converse_untagged_executable_fails_verification():
    """An executable cell without a recorder tag fails verification."""
    d = tempfile.mkdtemp(prefix="rec-h9-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True,
                        "total": 2,
                        "cells": [
                            {"index": 1, "style": "Input", "executable": True,
                             "record_tag": "R001-1", "source_digest": "aaa"},
                            {"index": 2, "style": "Input", "executable": True,
                             "record_tag": "", "source_digest": "bbb"},
                        ],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        result = rec._verify_full()
        assert not result["verified"]
        issues = result["issues"]
        untagged = [i for i in issues if i["issue"] == "untagged_executable"]
        assert len(untagged) == 1
        assert untagged[0]["index"] == 2
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_converse_narrative_cells_exempt():
    """Narrative cells (Title, Section, Text) do not need recorder tags."""
    d = tempfile.mkdtemp(prefix="rec-h9b-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True,
                        "total": 3,
                        "cells": [
                            {"index": 1, "style": "Title", "executable": False,
                             "record_tag": "", "source_digest": "t1"},
                            {"index": 2, "style": "Section", "executable": False,
                             "record_tag": "", "source_digest": "s1"},
                            {"index": 3, "style": "Input", "executable": True,
                             "record_tag": "R001-1", "source_digest": "aaa"},
                        ],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        result = rec._verify_full()
        assert result["verified"], f"narrative cells should not cause issues: {result['issues']}"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_converse_duplicate_tag_fails():
    """Two cells with the same recorder tag fail verification."""
    d = tempfile.mkdtemp(prefix="rec-h9c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True,
                        "total": 2,
                        "cells": [
                            {"index": 1, "style": "Input", "executable": True,
                             "record_tag": "R001-1", "source_digest": "aaa"},
                            {"index": 2, "style": "Input", "executable": True,
                             "record_tag": "R001-1", "source_digest": "aaa"},
                        ],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        result = rec._verify_full()
        assert not result["verified"]
        dupes = [i for i in result["issues"] if i["issue"] == "duplicate_tag"]
        assert len(dupes) == 1
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_converse_out_of_order_fails():
    """Cells appearing out of ledger sequence order fail verification."""
    d = tempfile.mkdtemp(prefix="rec-h9d-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True,
                        "total": 2,
                        "cells": [
                            {"index": 1, "style": "Input", "executable": True,
                             "record_tag": "R001-2", "source_digest": "bbb"},
                            {"index": 2, "style": "Input", "executable": True,
                             "record_tag": "R001-1", "source_digest": "aaa"},
                        ],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        t1 = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", t1)
        t2 = rec.ledger.make_tag()
        rec.ledger.append("bbb", "y = 2", "Input", t2)

        result = rec._verify_full()
        assert not result["verified"]
        ooo = [i for i in result["issues"] if i["issue"] == "out_of_order"]
        assert len(ooo) == 1
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_recording_boundary_rejects_existing_executable():
    """start_recording refuses when executable cells already exist."""
    from mathematica_wstp.notebooks import HeadlessNotebooks

    d = tempfile.mkdtemp(prefix="rec-h9e-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        call_responses = {
            "MCPReadBack": {
                "success": True,
                "total": 1,
                "cells": [
                    {"index": 1, "style": "Input", "executable": True,
                     "record_tag": "", "source_digest": "abc"},
                ],
            },
        }

        nb = HeadlessNotebooks()
        nb._sessions["hnb1"] = type("S", (), {
            "notebook_id": "hnb1", "path": "/tmp/test.nb",
            "title": "", "created": True,
        })()

        original_call = nb._call_with_session

        def mock_call(fn, *args, **kwargs):
            if fn in call_responses:
                return call_responses[fn]
            return {"success": False, "error": "not mocked"}

        nb._call_with_session = mock_call
        result = nb.start_recording(notebook="hnb1")
        assert not result["success"]
        assert "pre-existing executable" in result["error"]
        assert nb._recorder is None
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_recording_boundary_allows_narrative_only():
    """start_recording succeeds when only narrative cells exist."""
    from mathematica_wstp.notebooks import HeadlessNotebooks

    d = tempfile.mkdtemp(prefix="rec-h9f-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        call_responses = {
            "MCPReadBack": {
                "success": True,
                "total": 2,
                "cells": [
                    {"index": 1, "style": "Title", "executable": False,
                     "record_tag": "", "source_digest": "t1"},
                    {"index": 2, "style": "Section", "executable": False,
                     "record_tag": "", "source_digest": "s1"},
                ],
            },
        }

        nb = HeadlessNotebooks()
        nb._sessions["hnb1"] = type("S", (), {
            "notebook_id": "hnb1", "path": "/tmp/test.nb",
            "title": "", "created": True,
        })()

        def mock_call(fn, *args, **kwargs):
            if fn in call_responses:
                return call_responses[fn]
            return {"success": False, "error": "not mocked"}

        nb._call_with_session = mock_call
        result = nb.start_recording(notebook="hnb1")
        assert result["success"], f"narrative-only notebook should allow recording: {result}"
        assert nb._recorder is not None
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
        # Phase 9
        test_converse_untagged_executable_fails_verification,
        test_converse_narrative_cells_exempt,
        test_converse_duplicate_tag_fails,
        test_converse_out_of_order_fails,
        test_recording_boundary_rejects_existing_executable,
        test_recording_boundary_allows_narrative_only,
    ]

    passed = failed = 0
    print("=== Phases 7-9: hardening tests ===")
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
