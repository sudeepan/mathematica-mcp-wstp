"""Hardening tests: Phases 7-14 invariant enforcement.

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


# --- Phase 10: post-finalization structural verification ------------------

def _make_finalized_stub(finalized_cells):
    """Build a StubNotebooks that serves finalized_cells via open/readback/close."""

    class StubNotebooks:
        _recorder = None
        _opened = set()

        def is_open(self, path):
            return path in self._opened

        def open(self, path):
            self._opened.add(path)
            return {"success": True, "id": "fin-scratch"}

        def close(self, notebook=None):
            self._opened.discard(notebook)
            return {"success": True}

        def _call_with_session(self, fn, *args, **kwargs):
            if fn == "MCPReadBack":
                return {
                    "success": True,
                    "total": len(finalized_cells),
                    "cells": finalized_cells,
                }
            return {"success": True}

        def save(self, **kwargs):
            return {"success": True}

    return StubNotebooks()


def test_verify_finalized_catches_missing_cell():
    """Structural verification fails when a ledger record has no cell."""
    d = tempfile.mkdtemp(prefix="rec-h10-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        notebooks = _make_finalized_stub([])
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        result = rec._verify_finalized("/tmp/finalized.nb")
        assert not result["verified"]
        assert any(i["issue"] == "cell_missing_in_finalized" for i in result["issues"])
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_verify_finalized_catches_digest_change():
    """Structural verification fails when a cell's digest changed."""
    d = tempfile.mkdtemp(prefix="rec-h10b-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        notebooks = _make_finalized_stub([
            {"index": 1, "style": "Input", "executable": True,
             "record_tag": "R001-1", "source_digest": "CHANGED",
             "evaluatable": True},
        ])
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        result = rec._verify_finalized("/tmp/finalized.nb")
        assert not result["verified"]
        assert any(i["issue"] == "source_changed_in_finalized" for i in result["issues"])
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_verify_finalized_catches_annotation_not_preserved():
    """Structural verification fails when an annotated cell is still evaluatable."""
    d = tempfile.mkdtemp(prefix="rec-h10c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        notebooks = _make_finalized_stub([
            {"index": 1, "style": "Input", "executable": True,
             "record_tag": "R001-1", "source_digest": "aaa",
             "evaluatable": True},
        ])
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)
        rec.ledger.update_record(1, annotated=True, annotation_reason="timed out")

        result = rec._verify_finalized("/tmp/finalized.nb")
        assert not result["verified"]
        assert any(i["issue"] == "annotation_not_preserved" for i in result["issues"])
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_verify_finalized_passes_clean():
    """Structural verification passes when everything matches."""
    d = tempfile.mkdtemp(prefix="rec-h10d-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        notebooks = _make_finalized_stub([
            {"index": 1, "style": "Input", "executable": True,
             "record_tag": "R001-1", "source_digest": "aaa",
             "evaluatable": True},
            {"index": 2, "style": "Input", "executable": True,
             "record_tag": "R001-2", "source_digest": "bbb",
             "evaluatable": False},
        ])
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        t1 = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", t1)
        t2 = rec.ledger.make_tag()
        rec.ledger.append("bbb", "Pause[999]", "Input", t2)
        rec.ledger.update_record(2, annotated=True, annotation_reason="timed out")

        result = rec._verify_finalized("/tmp/finalized.nb")
        assert result["verified"], f"clean state should pass: {result['issues']}"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_finalize_returns_structural_verification():
    """finalize() result includes structural_verification when eval succeeds."""
    d = tempfile.mkdtemp(prefix="rec-h10e-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        with open(nb_path, "w") as f:
            f.write("Notebook[{}]")

        notebooks = _make_finalized_stub([
            {"index": 1, "style": "Input", "executable": True,
             "record_tag": "R001-1", "source_digest": "aaa",
             "evaluatable": True},
        ])

        import mathematica_wstp.recorder as rec_mod
        original_eval = rec_mod._evaluate_in_fresh_kernel

        def mock_eval(path, timeout=600):
            return {"success": True, "finalized": True}

        rec_mod._evaluate_in_fresh_kernel = mock_eval
        try:
            rec = Recorder(notebooks, "hnb1", nb_path)
            rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
            tag = rec.ledger.make_tag()
            rec.ledger.append("aaa", "x = 1", "Input", tag)
            rec.ledger.update_record(1, disposition={
                "execution_outcome": "COMPLETED",
                "control_intent": "NONE",
                "abort_confirmation": "NOT_APPLICABLE",
                "kernel_readiness": "READY",
            })

            fin = rec.finalize(timeout=120)
            assert fin["success"], f"finalize should succeed: {fin}"
            assert "structural_verification" in fin
            assert fin["structural_verification"]["verified"]
        finally:
            rec_mod._evaluate_in_fresh_kernel = original_eval
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Phase 11: fault latch, positive gate, narrative guard ----------------

def test_fault_latch_blocks_second_call():
    """After a pre-dispatch failure, the next call is refused immediately."""
    d = tempfile.mkdtemp(prefix="rec-h11-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        call_count = [0]

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self):
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    call_count[0] += 1
                    if call_count[0] == 1:
                        return {"success": True}
                    return {"success": True}
                if fn == "MCPReadBack":
                    if call_count[0] == 1:
                        return {"success": False, "error": "link dead"}
                    return {"success": True, "total": 0, "cells": []}
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        r1 = rec.record_and_verify("x = 1")
        assert not r1["success"], "first call should fail (readback failure)"
        assert rec.is_faulted, "recorder must be faulted after failure"

        r2 = rec.record_and_verify("y = 2")
        assert not r2["success"], "second call must be refused"
        assert "faulted" in r2.get("error", "").lower()
        assert call_count[0] == 1, "second call must not attempt a write"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_fault_latch_is_durable():
    """The fault marker is written to the ledger and survives reload."""
    d = tempfile.mkdtemp(prefix="rec-h11b-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {"success": False, "error": "kernel died"}
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.record_and_verify("x = 1")
        assert rec.is_faulted
        ledger_path = rec.ledger.path

        reloaded = RecorderLedger.load(ledger_path)
        assert "fault" in reloaded.data
        assert reloaded.data["fault"]["phase"] == "PRE_DISPATCH"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_fault_latch_blocks_finalization():
    """A faulted recorder refuses finalization."""
    d = tempfile.mkdtemp(prefix="rec-h11c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {"success": False, "error": "dead"}
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.record_and_verify("x = 1")
        assert rec.is_faulted

        fin = rec.finalize(timeout=120)
        assert not fin["success"]
        assert "faulted" in fin["error"]
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_post_eval_integrity_failure_faults():
    """A post-eval verification failure faults the recorder."""
    d = tempfile.mkdtemp(prefix="rec-h11d-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        call_phase = [0]

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    if call_phase[0] == 0:
                        call_phase[0] = 1
                        return {
                            "success": True, "total": 1,
                            "cells": [{"index": 1, "style": "Input",
                                       "executable": True,
                                       "record_tag": "R001-1",
                                       "source_digest": "aaa"}],
                        }
                    return {
                        "success": True, "total": 1,
                        "cells": [{"index": 1, "style": "Input",
                                   "executable": True,
                                   "record_tag": "R001-1",
                                   "source_digest": "TAMPERED"}],
                    }
                return {"success": False}

            def annotate_cell(self, *args, **kwargs):
                return {"success": True}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        outcome = rec.apply_outcome(1, {
            "execution_outcome": "COMPLETED",
            "control_intent": "NONE",
            "abort_confirmation": "NOT_APPLICABLE",
            "kernel_readiness": "READY",
        })
        assert not outcome["post_eval_verification"]["verified"]
        assert rec.is_faulted
        assert outcome["recording_faulted"] is True
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_narrative_style_returns_non_scientific():
    """A narrative-style call returns scientific=False and creates no ledger record."""
    d = tempfile.mkdtemp(prefix="rec-h11e-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)

        result = rec.record_and_verify("Introduction", style="Section")
        assert result["success"]
        assert result["scientific"] is False
        assert len(rec.ledger.records) == 0, "narrative cells must not create ledger records"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_narrative_write_failure_faults():
    """A narrative cell write failure still faults the recorder."""
    d = tempfile.mkdtemp(prefix="rec-h11f-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                return {"success": False, "error": "write refused"}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)

        result = rec.record_and_verify("Title text", style="Title")
        assert not result["success"]
        assert rec.is_faulted
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_style_change_fails_verification():
    """Changing a cell's style without changing its digest fails verification."""
    d = tempfile.mkdtemp(prefix="rec-h11g-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True, "total": 1,
                        "cells": [{"index": 1, "style": "Code",
                                   "executable": True,
                                   "record_tag": "R001-1",
                                   "source_digest": "aaa"}],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        tag = rec.ledger.make_tag()
        rec.ledger.append("aaa", "x = 1", "Input", tag)

        result = rec._verify_full()
        assert not result["verified"]
        assert any(i["issue"] == "style_changed" for i in result["issues"])
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_duplicate_source_distinct_tags_passes():
    """Two cells with identical source but different tags pass verification."""
    d = tempfile.mkdtemp(prefix="rec-h11h-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None

            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True, "total": 2,
                        "cells": [
                            {"index": 1, "style": "Input", "executable": True,
                             "record_tag": "R001-1", "source_digest": "same"},
                            {"index": 2, "style": "Input", "executable": True,
                             "record_tag": "R001-2", "source_digest": "same"},
                        ],
                    }
                return {"success": False}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        rec.ledger = RecorderLedger.create(nb_path, "hnb1", rec.run_id)
        t1 = rec.ledger.make_tag()
        rec.ledger.append("same", "x = 1", "Input", t1)
        t2 = rec.ledger.make_tag()
        rec.ledger.append("same", "x = 1", "Input", t2)

        result = rec._verify_full()
        assert result["verified"], f"duplicate source with distinct tags should pass: {result['issues']}"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_positive_gate_rejects_missing_seq():
    """The positive dispatch gate refuses a result with no seq."""
    d = tempfile.mkdtemp(prefix="rec-h11i-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNotebooks:
            _recorder = None
            _recording_target = "hnb1"

            @property
            def has_active_recorder(self):
                return self._recorder is not None

            def record_input(self, code, style="Input"):
                return {"success": True, "pre_dispatch_verified": True}

        notebooks = StubNotebooks()
        notebooks._recorder = object()
        rec_result = notebooks.record_input("x = 1")

        verified = (isinstance(rec_result, dict)
                    and rec_result.get("success") is True
                    and rec_result.get("pre_dispatch_verified") is True
                    and rec_result.get("seq") is not None)
        assert not verified, "missing seq must fail the positive gate"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_positive_gate_accepts_verified_record():
    """The positive dispatch gate accepts a fully verified record."""
    rec_result = {
        "success": True,
        "pre_dispatch_verified": True,
        "seq": 1,
        "record_tag": "R001-1",
        "source_digest": "abc",
    }
    verified = (isinstance(rec_result, dict)
                and rec_result.get("success") is True
                and rec_result.get("pre_dispatch_verified") is True
                and rec_result.get("seq") is not None)
    assert verified, "a fully verified record must pass the positive gate"


# --- Phase 13: integration fix tests ----------------------------------------

def test_annotation_failure_faults_recorder():
    """A failed annotation must fault the recorder immediately."""
    d = tempfile.mkdtemp(prefix="rec-h13a-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        call_count = {"n": 0}

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self):
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                call_count["n"] += 1
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    if call_count["n"] <= 3:
                        return {
                            "success": True, "total": 1,
                            "cells": [{"index": 1, "style": "Input",
                                        "record_tag": "R001-1",
                                        "source_digest": "d1",
                                        "evaluatable": True}],
                        }
                    return {
                        "success": False,
                        "error": "kernel not responding",
                    }
                return {"success": False}

            def annotate_cell(self, index, **kwargs):
                return {"success": False, "error": "annotate failed"}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        rec_result = rec.record_and_verify("Pause[10]", style="Input")
        assert rec_result["success"], "initial record must succeed"

        outcome = rec.apply_outcome(
            rec_result["seq"],
            {"execution_outcome": "TIMED_OUT", "timed_out": "true"})

        assert outcome["recording_faulted"], \
            "annotation failure must fault the recorder"
        assert rec.is_faulted
        assert rec.ledger.data["fault"]["phase"] == "POST_EVAL"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_finalize_routes_to_recorder_finalizer():
    """When recorder is active, finalize action must route to finalize_recording."""
    d = tempfile.mkdtemp(prefix="rec-h13b-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        from mathematica_wstp.notebooks import HeadlessNotebooks

        routed = {"to_recorder": False, "to_legacy": False}

        class PatchedNotebooks(HeadlessNotebooks):
            def __init__(self):
                self._sessions = {}
                self._recorder = None
                self._recording_target = None

            @property
            def has_active_recorder(self):
                return self._recorder is not None

            @property
            def recording(self):
                return self._recording_target

            def _resolve(self, notebook):
                return notebook or self._recording_target

            def finalize_recording(self, timeout=600):
                routed["to_recorder"] = True
                return {"success": True, "path": "recorder_finalize"}

            def finalize(self, notebook=None, timeout=600):
                routed["to_legacy"] = True
                return {"success": True, "path": "legacy_finalize"}

        nb = PatchedNotebooks()
        nb._recorder = object()
        nb._recording_target = "hnb1"

        if nb.has_active_recorder:
            target = nb._resolve(None)
            if target and target != nb.recording:
                raise AssertionError("target mismatch")
            result = nb.finalize_recording(timeout=600)
        else:
            result = nb.finalize(notebook=None, timeout=600)

        assert routed["to_recorder"], \
            "finalize must route to recorder finalizer when recorder is active"
        assert not routed["to_legacy"], \
            "legacy finalizer must not be called when recorder is active"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_post_eval_fault_surfaces_in_outcome():
    """apply_outcome must include recording_fault when faulted."""
    d = tempfile.mkdtemp(prefix="rec-h13c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        call_count = {"n": 0}

        class StubNotebooks:
            _recorder = None

            @property
            def has_active_recorder(self):
                return self._recorder is not None

            def _call_with_session(self, fn, *args, **kwargs):
                call_count["n"] += 1
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    if call_count["n"] <= 3:
                        return {
                            "success": True, "total": 1,
                            "cells": [{"index": 1, "style": "Input",
                                        "record_tag": "R001-1",
                                        "source_digest": "d1",
                                        "evaluatable": True}],
                        }
                    return {"success": True, "total": 1, "cells": [
                        {"index": 1, "style": "Input",
                         "record_tag": "R001-1",
                         "source_digest": "WRONG",
                         "evaluatable": True}]}

            def annotate_cell(self, index, **kwargs):
                return {"success": True}

        notebooks = StubNotebooks()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        rec_result = rec.record_and_verify("x = 1", style="Input")
        assert rec_result["success"]

        outcome = rec.apply_outcome(
            rec_result["seq"],
            {"execution_outcome": "COMPLETED"})

        assert outcome["recording_faulted"], \
            "digest mismatch must fault"
        assert "recording_fault" in outcome, \
            "faulted outcome must include recording_fault object"
        assert outcome["recording_fault"]["phase"] == "POST_EVAL"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Phase 14: closure pass tests -------------------------------------------

def test_unknown_style_rejected():
    """A style that is neither narrative nor scientific must be rejected."""
    d = tempfile.mkdtemp(prefix="rec-h14a-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        rec, _ = _make_recorder(d)
        result = rec.record_and_verify("x = 1", style="ExternalLanguage")
        assert not result["success"], "unknown style must be rejected"
        assert "neither narrative nor scientific" in result.get("error", "")
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_scientific_whitelist_accepts_input_code():
    """Input and Code styles must be accepted as scientific."""
    for style in ("Input", "Code"):
        d = tempfile.mkdtemp(prefix="rec-h14b-")
        os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
        try:
            nb_path = os.path.join(d, "test.nb")
            import hashlib as _hl

            class StubNB:
                _recorder = None
                @property
                def has_active_recorder(self):
                    return self._recorder is not None
                def _call_with_session(self, fn, *args, **kwargs):
                    if fn == "MCPWriteCell":
                        return {"success": True}
                    if fn == "MCPReadBack":
                        code = "x = 1"
                        digest = _hl.sha256(code.encode("utf-8")).hexdigest()
                        return {
                            "success": True, "total": 1,
                            "cells": [{"index": 1, "style": style,
                                        "record_tag": f"R001-1",
                                        "source_digest": digest,
                                        "executable": True,
                                        "evaluatable": True,
                                        "source_preview": code}],
                        }

            notebooks = StubNB()
            rec = Recorder(notebooks, "hnb1", nb_path)
            notebooks._recorder = rec
            result = rec.record_and_verify("x = 1", style=style)
            assert result["success"], f"style {style} must be accepted"
        finally:
            os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
            shutil.rmtree(d, ignore_errors=True)


def test_source_digest_mismatch_faults():
    """Read-back source digest != intended digest must fault PRE_DISPATCH."""
    d = tempfile.mkdtemp(prefix="rec-h14c-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")

        class StubNB:
            _recorder = None
            @property
            def has_active_recorder(self):
                return self._recorder is not None
            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {
                        "success": True, "total": 1,
                        "cells": [{"index": 1, "style": "Input",
                                    "record_tag": "R001-1",
                                    "source_digest": "wrong_digest",
                                    "executable": True,
                                    "evaluatable": True}],
                    }

        notebooks = StubNB()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec
        result = rec.record_and_verify("x = 1", style="Input")
        assert not result["success"]
        assert rec.is_faulted
        assert rec.ledger.data["fault"]["phase"] == "PRE_DISPATCH"
        assert "source" in result.get("error", "").lower() or "digest" in result.get("error", "").lower()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_pre_dispatch_full_verify_runs():
    """Full bidirectional verification must run before dispatch."""
    d = tempfile.mkdtemp(prefix="rec-h14d-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        import hashlib as _hl
        call_count = {"n": 0}

        class StubNB:
            _recorder = None
            @property
            def has_active_recorder(self):
                return self._recorder is not None
            def _call_with_session(self, fn, *args, **kwargs):
                call_count["n"] += 1
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    code = "x = 1"
                    digest = _hl.sha256(code.encode("utf-8")).hexdigest()
                    cells = [{"index": 1, "style": "Input",
                              "record_tag": "R001-1",
                              "source_digest": digest,
                              "executable": True,
                              "evaluatable": True,
                              "source_preview": code}]
                    if call_count["n"] >= 4:
                        cells.append({"index": 2, "style": "Input",
                                      "executable": True,
                                      "source_preview": "injected"})
                    return {"success": True, "total": len(cells),
                            "cells": cells}

        notebooks = StubNB()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec
        result = rec.record_and_verify("x = 1", style="Input")
        assert not result["success"], \
            "injected untagged executable must fail full verification"
        assert rec.is_faulted
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_sealed_recorder_refuses_science():
    """After successful finalization, record_and_verify must refuse."""
    d = tempfile.mkdtemp(prefix="rec-h14e-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        rec, _ = _make_recorder(d)
        rec._sealed = True
        result = rec.record_and_verify("x = 1", style="Input")
        assert not result["success"]
        assert "sealed" in result.get("error", "").lower()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_sealed_recorder_refuses_second_finalize():
    """After successful finalization, finalize() must refuse."""
    d = tempfile.mkdtemp(prefix="rec-h14f-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        rec, _ = _make_recorder(d)
        rec._sealed = True
        result = rec.finalize()
        assert not result["success"]
        assert "sealed" in result.get("error", "").lower()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_second_start_recording_refused():
    """start_recording must refuse when a recorder is already active."""
    d = tempfile.mkdtemp(prefix="rec-h14g-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        from mathematica_wstp.notebooks import HeadlessNotebooks

        class PatchedNB(HeadlessNotebooks):
            def __init__(self):
                self._sessions = {}
                self._recorder = None
                self._recording_target = None

        nb = PatchedNB()
        nb._recorder = object()
        nb._recording_target = "hnb1"
        result = nb.start_recording("hnb2")
        assert not result["success"]
        assert "already active" in result.get("error", "").lower()
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_verify_full_catches_disabled_completed():
    """A COMPLETED cell with Evaluatable->False must fail _verify_full."""
    d = tempfile.mkdtemp(prefix="rec-h14h-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        import hashlib as _hl
        code = "x = 1"
        digest = _hl.sha256(code.encode("utf-8")).hexdigest()

        class StubNB:
            _recorder = None
            @property
            def has_active_recorder(self):
                return self._recorder is not None
            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {
                        "success": True, "total": 1,
                        "cells": [{"index": 1, "style": "Input",
                                    "record_tag": "R001-1",
                                    "source_digest": digest,
                                    "executable": True,
                                    "evaluatable": False}],
                    }

        notebooks = StubNB()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        rec.ledger.append(source_digest=digest,
                          source_preview=code, style="Input",
                          record_tag="R001-1")
        rec.ledger.update_record(1,
            disposition={"execution_outcome": "COMPLETED"},
            disposition_at=1.0)

        result = rec._verify_full()
        assert not result["verified"], \
            "COMPLETED cell with Evaluatable->False must fail"
        issues = [i["issue"] for i in result["issues"]]
        assert "completed_cell_disabled" in issues
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_verify_full_catches_enabled_annotated():
    """An annotated cell that has Evaluatable->True must fail _verify_full."""
    d = tempfile.mkdtemp(prefix="rec-h14i-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        import hashlib as _hl
        code = "Pause[10]"
        digest = _hl.sha256(code.encode("utf-8")).hexdigest()

        class StubNB:
            _recorder = None
            @property
            def has_active_recorder(self):
                return self._recorder is not None
            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPWriteCell":
                    return {"success": True}
                if fn == "MCPReadBack":
                    return {
                        "success": True, "total": 1,
                        "cells": [{"index": 1, "style": "Input",
                                    "record_tag": "R001-1",
                                    "source_digest": digest,
                                    "executable": True,
                                    "evaluatable": True}],
                    }

        notebooks = StubNB()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        rec.ledger.append(source_digest=digest,
                          source_preview=code, style="Input",
                          record_tag="R001-1")
        rec.ledger.update_record(1,
            disposition={"execution_outcome": "TIMED_OUT"},
            disposition_at=1.0,
            annotated=True, annotation_reason="timed out")

        result = rec._verify_full()
        assert not result["verified"], \
            "annotated cell with Evaluatable->True must fail"
        issues = [i["issue"] for i in result["issues"]]
        assert "annotated_cell_enabled" in issues
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_stop_unfinalized_requires_force():
    """stop_recording without force must refuse an unfinalized recorder."""
    d = tempfile.mkdtemp(prefix="rec-h14j-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        from mathematica_wstp.notebooks import HeadlessNotebooks

        class PatchedNB(HeadlessNotebooks):
            def __init__(self):
                self._sessions = {}
                self._recorder = None
                self._recording_target = None

        nb = PatchedNB()
        rec, _ = _make_recorder(d)
        nb._recorder = rec
        nb._recording_target = "hnb1"

        result = nb.stop_recording(force=False)
        assert not result["success"], "must refuse without force"
        assert "finalize" in result.get("error", "").lower()

        result = nb.stop_recording(force=True)
        assert result["success"], "force=True must succeed"
        assert result.get("abandoned"), "should report abandoned"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_foreign_tag_in_finalized_rejected():
    """A finalized executable cell with a tag not in the ledger must fail."""
    d = tempfile.mkdtemp(prefix="rec-h14k-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        nb_path = os.path.join(d, "test.nb")
        import hashlib as _hl
        code = "x = 1"
        digest = _hl.sha256(code.encode("utf-8")).hexdigest()

        class StubNB:
            _recorder = None
            @property
            def has_active_recorder(self):
                return self._recorder is not None
            def is_open(self, path):
                return True
            def open(self, path):
                return {"success": True, "id": "scratch"}
            def close(self, notebook=None):
                return {"success": True}
            def _call_with_session(self, fn, *args, **kwargs):
                if fn == "MCPReadBack":
                    return {
                        "success": True, "total": 2,
                        "cells": [
                            {"index": 1, "style": "Input",
                             "record_tag": "R001-1",
                             "source_digest": digest,
                             "executable": True},
                            {"index": 2, "style": "Input",
                             "record_tag": "FOREIGN-99",
                             "source_digest": "abc",
                             "executable": True},
                        ],
                    }

        notebooks = StubNB()
        rec = Recorder(notebooks, "hnb1", nb_path)
        notebooks._recorder = rec

        rec.ledger.append(source_digest=digest,
                          source_preview=code, style="Input",
                          record_tag="R001-1")

        result = rec._verify_finalized("/tmp/fake-finalized.nb")
        assert not result["verified"], \
            "foreign tag must fail finalized verification"
        issues = [i["issue"] for i in result["issues"]]
        assert "tag_not_in_ledger_in_finalized" in issues
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
        # Phase 10
        test_verify_finalized_catches_missing_cell,
        test_verify_finalized_catches_digest_change,
        test_verify_finalized_catches_annotation_not_preserved,
        test_verify_finalized_passes_clean,
        test_finalize_returns_structural_verification,
        # Phase 11
        test_fault_latch_blocks_second_call,
        test_fault_latch_is_durable,
        test_fault_latch_blocks_finalization,
        test_post_eval_integrity_failure_faults,
        test_narrative_style_returns_non_scientific,
        test_narrative_write_failure_faults,
        test_style_change_fails_verification,
        test_duplicate_source_distinct_tags_passes,
        test_positive_gate_rejects_missing_seq,
        test_positive_gate_accepts_verified_record,
        # Phase 13
        test_annotation_failure_faults_recorder,
        test_finalize_routes_to_recorder_finalizer,
        test_post_eval_fault_surfaces_in_outcome,
        # Phase 14
        test_unknown_style_rejected,
        test_scientific_whitelist_accepts_input_code,
        test_source_digest_mismatch_faults,
        test_pre_dispatch_full_verify_runs,
        test_sealed_recorder_refuses_science,
        test_sealed_recorder_refuses_second_finalize,
        test_second_start_recording_refused,
        test_verify_full_catches_disabled_completed,
        test_verify_full_catches_enabled_annotated,
        test_stop_unfinalized_requires_force,
        test_foreign_tag_in_finalized_rejected,
    ]

    passed = failed = 0
    print("=== Phases 7-14: hardening tests ===")
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
