"""Hardening tests for the integrity recorder.

Every test drives the real Recorder against FakeHelper, an in-memory stand-in
for the Wolfram notebook helper. FakeHelper stores what was written, reads
back the recorder's own tags with real SHA-256 digests, and applies
annotations, so each check sees the same shape of data the kernel returns.
Tamper tests edit FakeHelper's cells between recorder calls.

Each test asserts the specific reason for a refusal, so a test cannot pass
by tripping over an earlier, unrelated check.

Tests that need the live server and kernel are in test_recorder_server.py.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp import recorder as recorder_mod
from mathematica_wstp.recorder import Recorder
from mathematica_wstp.recorder_ledger import RecorderLedger

EXECUTABLE_STYLES = ("Input", "Code")

COMPLETED = {"execution_outcome": "COMPLETED", "control_intent": "NONE",
             "abort_confirmation": "NOT_APPLICABLE", "kernel_readiness": "READY"}
TIMED_OUT = {"execution_outcome": "TIMED_OUT", "control_intent": "SYSTEM_TIMEOUT",
             "abort_confirmation": "CONFIRMED", "kernel_readiness": "READY"}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The helper fingerprints text after decoding character escapes (canonText in
# headless_notebook.wl); this table covers the escapes the tests use.
NAMED = {"Element": "\u2208", "Alpha": "\u03b1"}


def canon(text: str) -> str:
    return re.sub(r"\\\[([A-Za-z0-9]+)\]",
                  lambda m: NAMED.get(m.group(1), m.group(0)), text)


def cell(style: str, source: str, tag: str = "", evaluatable=None,
         reason: str = "") -> dict:
    return {"style": style, "source": source, "tag": tag,
            "evaluatable": evaluatable, "reason": reason}


class FakeHelper:
    """In-memory HeadlessNotebooks + Wolfram helper, faithful to MCPReadBack."""

    def __init__(self, notebook_id: str = "hnb1"):
        self.notebook_id = notebook_id
        self.docs: dict[str, list[dict]] = {notebook_id: []}
        self.files: dict[str, list[dict]] = {}
        self.fail: dict[str, str] = {}
        self.calls: list[str] = []
        self.write_transform = None
        self.drop_writes = False
        self.ignore_stamp = False
        self.transit_transform = None
        self.old_helper = False
        self._recorder = None
        self._scratch = 0

    @property
    def has_active_recorder(self) -> bool:
        return self._recorder is not None

    def _call_with_session(self, fn, notebook_id, *args, timeout=60):
        self.calls.append(fn)
        if fn in self.fail:
            return {"success": False, "error": self.fail[fn]}
        cells = self.docs[notebook_id]
        if fn == "MCPWriteCell":
            content, style = args[0], args[1]
            tag = args[4] if len(args) > 4 else ""
            stamp = args[5] if len(args) > 5 else ""
            received = self.transit_transform(content) if self.transit_transform else content
            stored = self.write_transform(received) if self.write_transform else received
            if not self.drop_writes:
                evaluatable = None if self.ignore_stamp else {"True": True, "False": False}.get(stamp)
                cells.append(cell(style, stored, tag, evaluatable))
            reply = {"success": True, "id": notebook_id, "record_tag": tag}
            if not self.old_helper:
                reply["raw_digest"] = sha(received)
                reply["canonical_digest"] = sha(canon(received))
            return reply
        if fn == "MCPSave":
            return {"success": True, "id": notebook_id, "saved": True}
        if fn == "MCPReadBack":
            return {"success": True, "id": notebook_id, "total": len(cells),
                    "cells": [self._readback(i, c) for i, c in enumerate(cells)]}
        return {"success": False, "error": f"FakeHelper does not implement {fn}"}

    @staticmethod
    def _readback(index: int, c: dict) -> dict:
        return {"index": index, "style": c["style"],
                "executable": c["style"] in EXECUTABLE_STYLES,
                "source_digest": sha(canon(c["source"])),
                "source_chars": len(c["source"]),
                "source_preview": c["source"][:200],
                "evaluatable": c["evaluatable"],
                "record_tag": c["tag"],
                "annotation_reason": c["reason"],
                "replay_tag": "", "cell_tags": []}

    def annotate_cell(self, index, evaluatable=False, reason="", notebook=None):
        self.calls.append("MCPAnnotateCell")
        if "MCPAnnotateCell" in self.fail:
            return {"success": False, "error": self.fail["MCPAnnotateCell"]}
        target = self.docs[notebook or self.notebook_id][index]
        target["evaluatable"] = evaluatable
        target["reason"] = reason
        return {"success": True, "annotated": index, "evaluatable": evaluatable}

    def save(self, notebook=None, path=None):
        self.calls.append("MCPSave")
        if "MCPSave" in self.fail:
            return {"success": False, "error": self.fail["MCPSave"]}
        return {"success": True}

    def is_open(self, path) -> bool:
        return False

    def open(self, path):
        self._scratch += 1
        sid = f"scratch{self._scratch}"
        source = self.files.get(path, self.docs[self.notebook_id])
        self.docs[sid] = copy.deepcopy(source)
        return {"success": True, "id": sid}

    def close(self, notebook=None):
        self.docs.pop(notebook, None)
        return {"success": True}

    @property
    def cells(self) -> list[dict]:
        return self.docs[self.notebook_id]

    def cell_with_tag(self, tag: str) -> dict:
        return next(c for c in self.cells if c["tag"] == tag)


class Workspace:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="rec-hard-")
        os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(self.dir, "ledgers")
        self.nb_path = os.path.join(self.dir, "test.nb")
        with open(self.nb_path, "w") as f:
            f.write("Notebook[{}]")
        self.fake = FakeHelper()
        self.rec = Recorder(self.fake, "hnb1", self.nb_path)
        self.fake._recorder = self.rec

    def record_ok(self, code: str, style: str = "Input",
                  disposition: dict = COMPLETED) -> dict:
        r = self.rec.record_and_verify(code, style=style)
        assert r.get("success") and r.get("pre_dispatch_verified"), r
        outcome = self.rec.apply_outcome(r["seq"], dict(disposition))
        assert not outcome["recording_faulted"], outcome
        return r

    def issue_names(self, verification: dict) -> list[str]:
        return [i["issue"] for i in verification.get("issues", [])]


@contextlib.contextmanager
def workspace():
    w = Workspace()
    try:
        yield w
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(w.dir, ignore_errors=True)


@contextlib.contextmanager
def fresh_kernel_stub(result: dict | None = None):
    original = recorder_mod._evaluate_in_fresh_kernel
    recorder_mod._evaluate_in_fresh_kernel = (
        lambda path, timeout=600: dict(result or {"success": True, "finalized": True}))
    try:
        yield
    finally:
        recorder_mod._evaluate_in_fresh_kernel = original


def headless_with(fake: FakeHelper, path: str):
    """A real HeadlessNotebooks whose helper calls go to FakeHelper."""
    from mathematica_wstp.notebooks import HeadlessNotebooks
    nb = HeadlessNotebooks()
    nb._sessions[fake.notebook_id] = type("S", (), {
        "notebook_id": fake.notebook_id, "path": path,
        "title": "", "created": True})()
    nb._call_with_session = fake._call_with_session
    nb._call = fake._call_with_session
    # No kernel to restart here; the restart itself is tested against a live kernel.
    nb._fresh_kernel_for_recording = lambda notebook_id: {"success": True, "previous_pid": 1, "pid": 2}
    return nb


# --- dispatch refusal before science -----------------------------------------

def test_readback_failure_returns_success_false():
    """record_and_verify returns success=False when readback fails."""
    with workspace() as w:
        w.fake.fail["MCPReadBack"] = "kernel not responding"
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert "read-back" in r["error"]
        assert w.fake.calls[:2] == ["MCPWriteCell", "MCPReadBack"]


def test_readback_failure_does_not_append_to_ledger():
    """When readback fails, no record is appended to the ledger."""
    with workspace() as w:
        w.fake.fail["MCPReadBack"] = "link dead"
        w.rec.record_and_verify("x = 1")
        assert w.rec.ledger.records == []


def test_cell_not_found_returns_success_false():
    """record_and_verify returns success=False when the tag is not in readback."""
    with workspace() as w:
        w.fake.drop_writes = True
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert "not found" in r["error"]
        assert w.rec.ledger.records == []


def test_write_failure_returns_success_false():
    """record_and_verify returns success=False when cell write fails."""
    with workspace() as w:
        w.fake.fail["MCPWriteCell"] = "write refused"
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert "MCPReadBack" not in w.fake.calls
        assert w.rec.ledger.records == []


def test_has_active_recorder_property():
    """has_active_recorder reflects whether an integrity recorder is set."""
    from mathematica_wstp.notebooks import HeadlessNotebooks
    nb = HeadlessNotebooks()
    assert not nb.has_active_recorder
    nb._recorder = object()
    assert nb.has_active_recorder
    nb._recorder = None
    assert not nb.has_active_recorder


# --- unresolved records -------------------------------------------------------

def test_no_disposition_is_unresolved():
    """A record with no disposition must be treated as unresolved."""
    with workspace() as w:
        r = w.rec.record_and_verify("x = 1")
        assert r["success"], r
        assert [u["seq"] for u in w.rec.unresolved_records()] == [r["seq"]]


def test_completed_disposition_is_resolved():
    """A record with COMPLETED disposition is not unresolved."""
    with workspace() as w:
        w.record_ok("x = 1")
        assert w.rec.unresolved_records() == []


def test_no_disposition_blocks_finalization():
    """finalize() refuses when any record has no disposition."""
    with workspace() as w:
        r = w.rec.record_and_verify("x = 1")
        assert r["success"], r
        with fresh_kernel_stub():
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False
        assert fin["error"] == "cannot finalize: unresolved records exist"
        assert [u["seq"] for u in fin["unresolved"]] == [r["seq"]]


# --- converse check: notebook to ledger -----------------------------------------

def test_converse_untagged_executable_fails_verification():
    """An executable cell without a recorder tag fails verification."""
    with workspace() as w:
        w.record_ok("x = 1")
        w.fake.cells.append(cell("Input", "injected = 1"))
        v = w.rec._verify_full()
        assert v["verified"] is False
        assert w.issue_names(v) == ["untagged_executable"]


def test_converse_narrative_cells_exempt():
    """Narrative cells (Title, Section, Text) do not need recorder tags."""
    with workspace() as w:
        w.fake.cells.extend([cell("Title", "Log"), cell("Section", "Setup"),
                             cell("Text", "Some notes")])
        section = w.rec.record_and_verify("Results", style="Section")
        assert section["success"] and section["scientific"] is False
        w.record_ok("x = 1")
        v = w.rec._verify_full()
        assert v["verified"], v


def test_converse_duplicate_tag_fails():
    """Two cells with the same recorder tag fail verification."""
    with workspace() as w:
        r = w.record_ok("x = 1")
        w.fake.cells.append(copy.deepcopy(w.fake.cell_with_tag(r["record_tag"])))
        v = w.rec._verify_full()
        assert v["verified"] is False
        assert w.issue_names(v) == ["duplicate_tag"]


def test_converse_out_of_order_fails():
    """Cells appearing out of ledger sequence order fail verification."""
    with workspace() as w:
        w.record_ok("a = 1")
        w.record_ok("b = 2")
        w.fake.cells[0], w.fake.cells[1] = w.fake.cells[1], w.fake.cells[0]
        v = w.rec._verify_full()
        assert v["verified"] is False
        assert w.issue_names(v) == ["out_of_order"]


def test_recording_boundary_rejects_existing_executable():
    """start_recording refuses when executable cells already exist."""
    with workspace() as w:
        w.fake.cells.append(cell("Input", "old = 1"))
        nb = headless_with(w.fake, w.nb_path)
        result = nb.start_recording(notebook="hnb1")
        assert result["success"] is False
        assert "pre-existing executable" in result["error"]
        assert nb._recorder is None


def test_recording_boundary_allows_narrative_only():
    """start_recording succeeds when only narrative cells exist."""
    with workspace() as w:
        w.fake.cells.extend([cell("Title", "Log"), cell("Text", "notes")])
        nb = headless_with(w.fake, w.nb_path)
        result = nb.start_recording(notebook="hnb1")
        assert result["success"], result
        assert nb._recorder is not None
        assert result["run_id"] == nb._recorder.run_id


# --- finalized artifact verification ------------------------------------------

def test_verify_finalized_catches_missing_cell():
    """Structural verification fails when a ledger record has no cell."""
    with workspace() as w:
        w.record_ok("a = 1")
        b = w.record_ok("b = 2")
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = [c for c in copy.deepcopy(w.fake.cells)
                              if c["tag"] != b["record_tag"]]
        v = w.rec._verify_finalized(path)
        assert v["verified"] is False
        assert w.issue_names(v) == ["cell_missing_in_finalized"]


def test_verify_finalized_catches_digest_change():
    """Structural verification fails when a cell's digest changed."""
    with workspace() as w:
        b = w.record_ok("b = 2")
        path = os.path.join(w.dir, "fin.nb")
        damaged = copy.deepcopy(w.fake.cells)
        next(c for c in damaged if c["tag"] == b["record_tag"])["source"] = "b = 3"
        w.fake.files[path] = damaged
        v = w.rec._verify_finalized(path)
        assert v["verified"] is False
        assert w.issue_names(v) == ["source_changed_in_finalized"]


def test_verify_finalized_catches_annotation_not_preserved():
    """Structural verification fails when an annotated cell is still evaluatable."""
    with workspace() as w:
        p = w.record_ok("Pause[10]", disposition=TIMED_OUT)
        path = os.path.join(w.dir, "fin.nb")
        damaged = copy.deepcopy(w.fake.cells)
        next(c for c in damaged if c["tag"] == p["record_tag"])["evaluatable"] = True
        w.fake.files[path] = damaged
        v = w.rec._verify_finalized(path)
        assert v["verified"] is False
        assert w.issue_names(v) == ["annotation_not_preserved"]


def test_verify_finalized_passes_clean():
    """Structural verification passes when everything matches."""
    with workspace() as w:
        w.rec.record_and_verify("Setup", style="Section")
        w.record_ok("a = 1")
        w.record_ok("Pause[10]", disposition=TIMED_OUT)
        w.record_ok("b = a + 1")
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = copy.deepcopy(w.fake.cells)
        v = w.rec._verify_finalized(path)
        assert v["verified"], v
        assert v["issues"] == []


def test_finalize_returns_structural_verification():
    """finalize() result includes structural_verification when eval succeeds."""
    with workspace() as w:
        w.record_ok("a = 1")
        w.record_ok("b = a + 1")
        with fresh_kernel_stub():
            fin = w.rec.finalize(timeout=5)
        assert fin["success"], fin
        assert fin["structural_verification"]["verified"] is True
        expected = os.path.splitext(w.nb_path)[0] + f"-{w.rec.run_id}-finalized.nb"
        assert fin["finalized_path"] == expected
        assert os.path.exists(expected)
        assert fin["run_id"] == w.rec.run_id


# --- fault latch --------------------------------------------------------------

def test_fault_latch_blocks_second_call():
    """After a pre-dispatch failure, the next call is refused immediately."""
    with workspace() as w:
        w.fake.fail["MCPReadBack"] = "kernel not responding"
        assert w.rec.record_and_verify("x = 1")["success"] is False
        assert w.rec.is_faulted
        del w.fake.fail["MCPReadBack"]
        calls_before = len(w.fake.calls)
        r = w.rec.record_and_verify("y = 2")
        assert r["success"] is False
        assert "faulted" in r["error"]
        assert len(w.fake.calls) == calls_before, "a faulted recorder must not touch the notebook"


def test_fault_latch_is_durable():
    """The fault marker is written to the ledger and survives reload."""
    with workspace() as w:
        w.fake.fail["MCPReadBack"] = "kernel died"
        w.rec.record_and_verify("x = 1")
        reloaded = RecorderLedger.load(w.rec.ledger.path)
        assert reloaded.data["fault"]["phase"] == "PRE_DISPATCH"


def test_fault_latch_blocks_finalization():
    """A faulted recorder refuses finalization."""
    with workspace() as w:
        w.fake.fail["MCPReadBack"] = "kernel died"
        w.rec.record_and_verify("x = 1")
        with fresh_kernel_stub():
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False
        assert fin["error"] == "cannot finalize: recorder is faulted"


def test_post_eval_integrity_failure_faults():
    """A post-eval verification failure faults the recorder."""
    with workspace() as w:
        r = w.rec.record_and_verify("x = 1")
        assert r["success"], r
        w.fake.cell_with_tag(r["record_tag"])["source"] = "x = 2"
        outcome = w.rec.apply_outcome(r["seq"], dict(COMPLETED))
        assert outcome["recording_faulted"] is True
        assert w.rec.ledger.data["fault"]["phase"] == "POST_EVAL"
        stored = w.rec.ledger.record_by_seq(r["seq"])["disposition"]
        assert stored["execution_outcome"] == "COMPLETED", "the execution outcome is preserved"


# --- narrative path -----------------------------------------------------------

def test_narrative_style_returns_non_scientific():
    """A narrative-style call returns scientific=False and creates no ledger record."""
    with workspace() as w:
        r = w.rec.record_and_verify("Setup", style="Section")
        assert r["success"] is True and r["scientific"] is False
        assert w.rec.ledger.records == []
        assert w.fake.cells[-1]["style"] == "Section"
        assert w.fake.cells[-1]["tag"] == ""


def test_narrative_write_failure_faults():
    """A narrative cell write failure still faults the recorder."""
    with workspace() as w:
        w.fake.fail["MCPWriteCell"] = "write refused"
        r = w.rec.record_and_verify("Setup", style="Section")
        assert r["success"] is False
        assert w.rec.is_faulted


def test_refused_narrative_content_does_not_fault():
    """Text the helper cannot read is refused: nothing is written and the run stays intact."""
    with workspace() as w:
        original = w.fake._call_with_session

        def refusing(fn, notebook_id, *args, timeout=60):
            if fn == "MCPWriteCell":
                w.fake.calls.append(fn)
                return {"success": False, "refused": True, "reason": "not valid",
                        "error": 'refused: the formula "x^^^2" is not valid Wolfram syntax'}
            return original(fn, notebook_id, *args, timeout=timeout)

        w.fake._call_with_session = refusing
        r = w.rec.record_and_verify("a typo in $x^^^2$", style="Text")
        assert r["success"] is False and r["refused"] is True, r
        assert "not valid Wolfram syntax" in r["error"], r
        assert not w.rec.is_faulted and w.rec.ledger.records == []
        w.fake._call_with_session = original
        assert w.rec.record_and_verify("the corrected $x^2$", style="Text")["success"]


def test_prose_style_without_a_recording_is_refused():
    """Outside a recording a prose style is refused instead of running the text as code."""
    from mathematica_wstp import notebooks, server
    notebooks.reset_headless_notebooks()
    for style in ("Text", "Section", "DisplayFormula"):
        reply = server.evaluate("x = 5", style=style).structured_content
        assert reply["success"] is False and "no recording is active" in reply["error"], reply
        assert "edit_cells(action='write'" in reply["error"], reply


def test_display_formula_cells_are_narrative():
    """A displayed formula is written like prose: no ledger record, never evaluatable."""
    for style in ("DisplayFormula", "DisplayFormulaNumbered"):
        with workspace() as w:
            r = w.rec.record_and_verify("x^2 + y^2 == r^2", style=style)
            assert r["success"] is True and r["scientific"] is False, r
            assert w.rec.ledger.records == []
            assert (w.fake.cells[-1]["style"], w.fake.cells[-1]["evaluatable"]) == (style, False)


# --- style and identity ---------------------------------------------------------

def test_style_change_fails_verification():
    """Changing a cell's style without changing its digest fails verification."""
    with workspace() as w:
        r = w.record_ok("x = 1")
        w.fake.cell_with_tag(r["record_tag"])["style"] = "Code"
        v = w.rec._verify_full()
        assert v["verified"] is False
        assert w.issue_names(v) == ["style_changed"]


def test_duplicate_source_distinct_tags_passes():
    """Two cells with identical source but different tags pass verification."""
    with workspace() as w:
        r1 = w.record_ok("x = 1")
        r2 = w.record_ok("x = 1")
        assert r1["record_tag"] != r2["record_tag"]
        digests = {rec["source_digest"] for rec in w.rec.ledger.records}
        assert digests == {sha("x = 1")}
        v = w.rec._verify_full()
        assert v["verified"], v


# --- annotation and fault reporting ------------------------------------------------

def test_annotation_failure_faults_recorder():
    """A failed annotation must fault the recorder immediately."""
    with workspace() as w:
        r = w.rec.record_and_verify("Pause[10]")
        assert r["success"], r
        w.fake.fail["MCPAnnotateCell"] = "annotate failed"
        outcome = w.rec.apply_outcome(r["seq"], dict(TIMED_OUT))
        assert outcome["recording_faulted"] is True
        fault = w.rec.ledger.data["fault"]
        assert fault["phase"] == "POST_EVAL"
        assert "annotation failed" in fault["reason"]


def test_post_eval_fault_surfaces_in_outcome():
    """apply_outcome must include recording_fault when faulted."""
    with workspace() as w:
        r = w.rec.record_and_verify("x = 1")
        assert r["success"], r
        w.fake.cell_with_tag(r["record_tag"])["source"] = "x = 99"
        outcome = w.rec.apply_outcome(r["seq"], dict(COMPLETED))
        assert outcome["recording_fault"]["phase"] == "POST_EVAL"


# --- pre-dispatch checks ----------------------------------------------------------

def test_unknown_style_rejected():
    """A style that is neither narrative nor scientific must be rejected."""
    with workspace() as w:
        r = w.rec.record_and_verify("x = 1", style="ExternalLanguage")
        assert r["success"] is False
        assert "neither narrative nor scientific" in r["error"]
        assert all(f"'{st}'" in r["error"] for st in ("Input", "Text", "DisplayFormula")), r
        assert w.fake.calls == [], "rejected before touching the notebook"
        assert w.rec.ledger.records == []


def test_scientific_whitelist_accepts_input_code():
    """Input and Code styles must be accepted as scientific."""
    for style in EXECUTABLE_STYLES:
        with workspace() as w:
            r = w.rec.record_and_verify("x = 1", style=style)
            assert r["success"] and r["pre_dispatch_verified"], (style, r)
            assert w.rec.ledger.records[0]["style"] == style


def test_source_digest_mismatch_faults():
    """Read-back source digest != intended digest must fault PRE_DISPATCH."""
    with workspace() as w:
        w.fake.write_transform = lambda s: s + " "
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert r["error"] == "pre-dispatch verification failed: source mismatch"
        assert r["expected_digest"] == sha("x = 1")
        assert w.rec.ledger.data["fault"]["phase"] == "PRE_DISPATCH"
        assert w.rec.ledger.records == []


def test_pre_dispatch_full_verify_runs():
    """Full bidirectional verification must run before dispatch."""
    with workspace() as w:
        first = w.record_ok("a = 1")
        w.fake.cell_with_tag(first["record_tag"])["source"] = "a = 100"
        r = w.rec.record_and_verify("b = 2")
        assert r["success"] is False
        assert r["error"] == "pre-dispatch full verification failed"
        assert "pre_dispatch_verified" not in r
        assert w.issue_names(r["verification"]) == ["source_changed"]
        assert w.rec.ledger.data["fault"]["phase"] == "PRE_DISPATCH"


# --- sealing and lifecycle ----------------------------------------------------------

def test_sealed_recorder_refuses_science():
    """After successful finalization, record_and_verify must refuse."""
    with workspace() as w:
        w.record_ok("a = 1")
        with fresh_kernel_stub():
            assert w.rec.finalize(timeout=5)["success"]
        calls_before = len(w.fake.calls)
        r = w.rec.record_and_verify("b = 2")
        assert r["success"] is False
        assert "sealed" in r["error"]
        assert len(w.fake.calls) == calls_before


def test_sealed_recorder_refuses_second_finalize():
    """After successful finalization, finalize() must refuse."""
    with workspace() as w:
        w.record_ok("a = 1")
        with fresh_kernel_stub():
            assert w.rec.finalize(timeout=5)["success"]
            again = w.rec.finalize(timeout=5)
        assert again["success"] is False
        assert "sealed" in again["error"]


def test_second_start_recording_refused():
    """start_recording must refuse when a recorder is already active."""
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        first = nb.start_recording(notebook="hnb1")
        assert first["success"], first
        second = nb.start_recording(notebook="hnb1")
        assert second["success"] is False
        assert "already active" in second["error"]
        assert nb._recorder.run_id == first["run_id"], "the first run must be kept"


def test_verify_full_catches_disabled_completed():
    """A COMPLETED cell with Evaluatable->False must fail _verify_full."""
    with workspace() as w:
        r = w.record_ok("x = 1")
        w.fake.cell_with_tag(r["record_tag"])["evaluatable"] = False
        v = w.rec._verify_full()
        assert v["verified"] is False
        assert w.issue_names(v) == ["completed_cell_disabled"]


def test_verify_full_catches_enabled_annotated():
    """An annotated cell that has Evaluatable->True must fail _verify_full."""
    with workspace() as w:
        r = w.record_ok("Pause[10]", disposition=TIMED_OUT)
        w.fake.cell_with_tag(r["record_tag"])["evaluatable"] = True
        v = w.rec._verify_full()
        assert v["verified"] is False
        assert w.issue_names(v) == ["annotated_cell_enabled"]


def test_stop_unfinalized_requires_force():
    """stop_recording without force must refuse an unfinalized recorder."""
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        assert nb.start_recording(notebook="hnb1")["success"]
        refused = nb.stop_recording(force=False)
        assert refused["success"] is False
        assert "finalize" in refused["error"]
        assert nb.has_active_recorder
        forced = nb.stop_recording(force=True)
        assert forced["success"] is True
        assert forced["abandoned"] is True
        assert not nb.has_active_recorder


def test_foreign_tag_in_finalized_rejected():
    """A finalized executable cell with a tag not in the ledger must fail."""
    with workspace() as w:
        w.record_ok("a = 1")
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = copy.deepcopy(w.fake.cells) + [
            cell("Input", "evil = 1", tag="R0000000000/99")]
        v = w.rec._verify_finalized(path)
        assert v["verified"] is False
        assert w.issue_names(v) == ["tag_not_in_ledger_in_finalized"]


# --- exact replay flags ---------------------------------------------------------

def test_writes_stamp_replay_flags():
    """Scientific cells are stamped Evaluatable->True, narrative ones ->False."""
    with workspace() as w:
        w.rec.record_and_verify("Setup", style="Section")
        a = w.record_ok("a = 1")
        p = w.record_ok("Pause[10]", disposition=TIMED_OUT)
        assert w.fake.cells[0]["evaluatable"] is False
        assert w.fake.cell_with_tag(a["record_tag"])["evaluatable"] is True
        assert w.fake.cell_with_tag(p["record_tag"])["evaluatable"] is False


def test_pre_dispatch_requires_stamped_flag():
    """A written cell that did not keep its Evaluatable->True stamp is refused."""
    with workspace() as w:
        w.fake.ignore_stamp = True
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert r["error"] == "pre-dispatch verification failed: cell not stamped evaluatable"
        assert w.rec.ledger.data["fault"]["phase"] == "PRE_DISPATCH"


def test_annotated_cell_with_missing_flag_fails():
    """An annotated cell whose Evaluatable option was removed is not accepted."""
    with workspace() as w:
        p = w.record_ok("Pause[10]", disposition=TIMED_OUT)
        w.fake.cell_with_tag(p["record_tag"])["evaluatable"] = None
        live = w.rec._verify_full()
        assert w.issue_names(live) == ["annotated_cell_enabled"]
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = copy.deepcopy(w.fake.cells)
        assert w.issue_names(w.rec._verify_finalized(path)) == ["annotation_not_preserved"]


def test_completed_cell_with_missing_flag_fails():
    """A completed cell whose Evaluatable option was removed is not accepted."""
    with workspace() as w:
        a = w.record_ok("a = 1")
        w.fake.cell_with_tag(a["record_tag"])["evaluatable"] = None
        live = w.rec._verify_full()
        assert w.issue_names(live) == ["completed_cell_disabled"]
        assert live["issues"][0]["found_evaluatable"] is None
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = copy.deepcopy(w.fake.cells)
        assert w.issue_names(w.rec._verify_finalized(path)) == ["completed_cell_disabled_in_finalized"]


def test_unrecorded_evaluatable_cell_fails():
    """A cell outside the ledger that is marked evaluatable fails, whatever its style."""
    with workspace() as w:
        w.record_ok("a = 1")
        w.fake.cells.append(cell("Text", "x = 42", evaluatable=True))
        live = w.rec._verify_full()
        assert w.issue_names(live) == ["unrecorded_cell_evaluatable"]
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = copy.deepcopy(w.fake.cells)
        assert w.issue_names(w.rec._verify_finalized(path)) == ["unrecorded_cell_evaluatable_in_finalized"]


# --- unknown outcomes and bookkeeping ---------------------------------------------

def test_missing_outcome_blocks_next_record():
    """A record whose outcome was never stored stops the run before new science."""
    with workspace() as w:
        first = w.rec.record_and_verify("a = 1")
        assert first["success"], first
        calls_before = len(w.fake.calls)
        r = w.rec.record_and_verify("b = 2")
        assert r["success"] is False
        assert "never recorded" in r["error"]
        assert len(w.fake.calls) == calls_before, "refused before touching the notebook"
        fault = w.rec.ledger.data["fault"]
        assert fault["phase"] == "PRE_DISPATCH" and "[1]" in fault["reason"]


def test_outcome_for_unknown_seq_is_reported():
    """An outcome for a record that does not exist faults the run and says so."""
    from mathematica_wstp.session import WLResult
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        assert nb.start_recording(notebook="hnb1")["success"]
        out = nb.record_outcome(999, WLResult(success=True, text="1"), None, None)
        assert out["success"] is False
        assert out["recording_faulted"] is True
        assert out["recording_fault"]["phase"] == "POST_EVAL"
        assert "999" in out["recording_fault"]["reason"]


def test_each_recorded_cell_is_saved_at_once():
    """Prose, a cell before it runs, and its outcome are each saved, never through a reopen."""
    from mathematica_wstp.session import WLResult
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        direct = []

        def call(fn, *args, **kwargs):
            direct.append(fn)
            return w.fake._call_with_session(fn, *args, **kwargs)

        nb._call = call
        assert nb.start_recording(notebook="hnb1")["success"]
        assert nb.record_input("Notes", style="Text")["success"]
        assert direct == ["MCPSave"], direct
        rec = nb.record_input("x = 1")
        assert rec["success"] and "save_failed" not in rec, rec
        assert direct == ["MCPSave"] * 2, direct
        out = nb.record_outcome(rec["seq"], WLResult(success=True, text="1"), None, None)
        assert "save_failed" not in out, out
        assert direct == ["MCPSave"] * 3, direct
        # Every save went through _call, none through the reopening _call_with_session.
        assert w.fake.calls.count("MCPSave") == 3, w.fake.calls


def test_a_failed_save_is_reported_and_the_recording_goes_on():
    """The ledger is the record: a save that fails is reported, not a fault."""
    from mathematica_wstp.session import WLResult
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        assert nb.start_recording(notebook="hnb1")["success"]
        w.fake.fail["MCPSave"] = "disk full"
        assert nb.record_input("Notes", style="Text")["save_failed"] == "disk full"
        rec = nb.record_input("x = 1")
        assert rec["success"] and rec["save_failed"] == "disk full", rec
        out = nb.record_outcome(rec["seq"], WLResult(success=True, text="1"), None, None)
        assert out["save_failed"] == "disk full", out
        assert not nb._recorder.is_faulted
        del w.fake.fail["MCPSave"]
        again = nb.record_input("y = 2")
        assert again["success"] and "save_failed" not in again, again


def test_no_save_when_nothing_was_recorded():
    """A refused or failed write saves nothing."""
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        assert nb.start_recording(notebook="hnb1")["success"]
        w.fake.fail["MCPReadBack"] = "read-back broke"
        assert nb.record_input("x = 1")["success"] is False
        assert "MCPSave" not in w.fake.calls, w.fake.calls


def test_record_input_exception_is_reported():
    """An exception while recording faults the run and the reply carries the fault."""
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        assert nb.start_recording(notebook="hnb1")["success"]

        def explode(*args, **kwargs):
            raise RuntimeError("link broke mid-write")

        nb._call_with_session = explode
        out = nb.record_input("x = 1")
        assert out["success"] is False
        assert out["recording_faulted"] is True
        assert out["recording_fault"]["phase"] == "PRE_DISPATCH"


# --- durable seal and finalization attempts ---------------------------------------

def test_seal_is_durable():
    """The seal is read from the ledger, so a reloaded ledger is still sealed."""
    with workspace() as w:
        w.record_ok("a = 1")
        with fresh_kernel_stub():
            assert w.rec.finalize(timeout=5)["success"]
        reborn = Recorder.__new__(Recorder)
        reborn.ledger = RecorderLedger.load(w.rec.ledger.path)
        assert reborn.is_sealed


def test_failed_finalization_can_be_retried():
    """A failed attempt keeps its artifact as evidence and a retry can succeed."""
    with workspace() as w:
        w.record_ok("a = 1")
        canonical = os.path.splitext(w.nb_path)[0] + f"-{w.rec.run_id}-finalized.nb"
        with fresh_kernel_stub({"success": False, "error": "kernel licence busy"}):
            first = w.rec.finalize(timeout=5)
        assert first["success"] is False and os.path.exists(canonical)
        assert not w.rec.is_sealed
        with fresh_kernel_stub():
            second = w.rec.finalize(timeout=5)
        assert second["success"], second
        assert second["finalized_path"] == canonical
        preserved = second["previous_attempt_preserved_as"]
        assert preserved.endswith(f"-{w.rec.run_id}-finalized-failed-1.nb")
        assert os.path.exists(preserved)
        attempts = w.rec.ledger.data["finalization_attempts"]
        assert [a["success"] for a in attempts] == [False, True]
        assert attempts[0]["preserved_as"] == preserved
        assert w.rec.is_sealed


def test_finalize_refuses_unknown_existing_artifact():
    """A file at the artifact path that this run did not produce is left alone."""
    with workspace() as w:
        w.record_ok("a = 1")
        canonical = os.path.splitext(w.nb_path)[0] + f"-{w.rec.run_id}-finalized.nb"
        with open(canonical, "w") as fh:
            fh.write("somebody else's file")
        with fresh_kernel_stub():
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False
        assert "refusing to overwrite" in fin["error"]
        with open(canonical) as fh:
            assert fh.read() == "somebody else's file"


# --- backend ---------------------------------------------------------------------

def test_start_recording_refuses_supervisor_backend():
    """Integrity recording starts only on the direct kernel."""
    from mathematica_wstp import evaluator
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        previous = evaluator.set_evaluator(type("FakeSupervisor", (), {"name": "supervisor"})())
        try:
            refused = nb.start_recording(notebook="hnb1")
        finally:
            evaluator.set_evaluator(previous)
        assert refused["success"] is False
        assert "direct kernel" in refused["error"]
        assert nb._recorder is None


def with_kernel_snapshot(w: Workspace, snapshot: dict):
    nb = headless_with(w.fake, w.nb_path)
    nb._kernel_snapshot = lambda: copy.deepcopy(snapshot)
    return nb


def ledger_files() -> list[str]:
    return sorted(os.listdir(os.environ["MATHEMATICA_WSTP_RECORDING_DIR"]))


def test_require_fresh_kernel_refuses_a_used_kernel():
    """Opted in, a kernel holding definitions or packages starts no recording."""
    used = {"user_symbols": ["a", "f1"], "packages": ["MyPackage`"]}
    with workspace() as w:
        before = ledger_files()
        nb = with_kernel_snapshot(w, used)
        refused = nb.start_recording(notebook="hnb1", require_fresh_kernel=True)
        assert refused["success"] is False and refused["kernel_not_fresh"] is True, refused
        assert refused["kernel_at_start"] == used, refused
        for part in ("a, f1", "MyPackage`", "startup files", "without require_fresh_kernel"):
            assert part in refused["error"], (part, refused["error"])
        assert nb._recorder is None and ledger_files() == before

        warned = nb.start_recording(notebook="hnb1")
        assert warned["success"] and warned["kernel_not_fresh"] is True, warned
        assert "startup files" in warned["warning"], warned
        assert "require_fresh_kernel=True refuses" in warned["warning"], warned


def test_require_fresh_kernel_refuses_a_kernel_it_could_not_check():
    """A snapshot that failed, or packages that could not be compared, is not fresh."""
    from mathematica_wstp.notebooks import _freshness_problems
    unchecked = [{"error": "could not inspect the kernel: link dead"},
                 {"user_symbols": [], "packages": [], "packages_checked": False}]
    with workspace() as w:
        for snapshot in unchecked:
            nb = with_kernel_snapshot(w, snapshot)
            refused = nb.start_recording(notebook="hnb1", require_fresh_kernel=True)
            assert refused["success"] is False and nb._recorder is None, refused
    many = _freshness_problems({"user_symbols": [f"s{i}" for i in range(7)], "packages": []})
    assert many == ["Global` definitions: s0, s1, s2, s3, s4 and 2 more"], many


def test_require_fresh_kernel_records_on_a_fresh_kernel():
    """A fresh kernel passes the check, and the ledger keeps its empty snapshot."""
    fresh = {"user_symbols": [], "packages": []}
    with workspace() as w:
        nb = with_kernel_snapshot(w, fresh)
        started = nb.start_recording(notebook="hnb1", require_fresh_kernel=True)
        assert started["success"] and started["recording"] is True, started
        assert "kernel_not_fresh" not in started and "warning" not in started, started
        assert started["kernel_restarted"]["pid"] == 2, started
        assert nb._recorder.ledger.data["kernel_at_start"] == fresh


def test_recording_starts_in_a_fresh_kernel():
    """The kernel is restarted after the checks and before the snapshot and the recorder."""
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        order = []
        nb._fresh_kernel_for_recording = lambda notebook_id: (
            order.append(("restart", notebook_id)) or {"success": True, "previous_pid": 7, "pid": 8})
        nb._kernel_snapshot = lambda: (order.append(("snapshot",)) or {"user_symbols": [], "packages": []})
        started = nb.start_recording(notebook="hnb1")
        assert started["success"] and order == [("restart", "hnb1"), ("snapshot",)], (order, started)
        assert started["kernel_restarted"] == {"success": True, "previous_pid": 7, "pid": 8}, started


def test_restart_refused_while_another_notebook_is_unsaved():
    """Another open notebook with unsaved changes stops the restart, and is named."""
    from mathematica_wstp import session
    with workspace() as w:
        nb = headless_with(w.fake, w.nb_path)
        del nb._fresh_kernel_for_recording  # the real restart step, with the kernel stubbed
        listing = {"success": True, "notebooks": [
            {"id": "hnb1", "path": w.nb_path, "dirty": True},
            {"id": "hnb2", "path": "/elsewhere/other.nb", "dirty": True}]}
        nb._call = lambda fn, *a, **k: (listing if fn == "MCPList"
                                        else w.fake._call_with_session(fn, *a, **k))
        restarts = []
        real_restart = session.restart_kernel
        session.restart_kernel = lambda: restarts.append(1) or {"previous_pid": 1, "pid": 2}
        try:
            refused = nb.start_recording(notebook="hnb1")
            assert refused["success"] is False, refused
            assert "hnb2 (/elsewhere/other.nb)" in refused["error"], refused
            assert restarts == [] and nb._recorder is None
            assert "MCPSave" not in w.fake.calls, w.fake.calls
            listing["notebooks"][1]["dirty"] = False
            started = nb.start_recording(notebook="hnb1")
            assert started["success"] and restarts == [1], started
            assert started["kernel_restarted"]["pid"] == 2, started
        finally:
            session.restart_kernel = real_restart


def test_failed_fresh_kernel_starts_no_recording():
    """If the kernel cannot be restarted, nothing is recorded and no ledger is written."""
    with workspace() as w:
        before = ledger_files()
        nb = headless_with(w.fake, w.nb_path)
        nb._fresh_kernel_for_recording = lambda notebook_id: {"success": False, "error": "no kernel"}
        refused = nb.start_recording(notebook="hnb1")
        assert refused["success"] is False and "fresh kernel" in refused["error"], refused
        assert nb._recorder is None and ledger_files() == before


def test_require_fresh_kernel_only_where_a_recording_starts():
    """Passed to an action that starts no recording, the option is refused, not ignored."""
    from mathematica_wstp import server
    for kwargs in ({"action": "list"}, {"action": "create", "title": "t"},
                   {"action": "open", "path": "x.nb"}, {"action": "finalize"}):
        reply = server.notebooks(require_fresh_kernel=True, **kwargs).structured_content
        assert reply["success"] is False, (kwargs, reply)
        assert "applies when a recording starts" in reply["error"], (kwargs, reply)


# --- character escapes ------------------------------------------------------------

def test_named_character_survives_front_end_save():
    r"""A front-end save that turns \[Element] into the glyph is not a change."""
    with workspace() as w:
        r = w.record_ok("cond = pz \\[Element] Reals && m > 0")
        stored = w.fake.cell_with_tag(r["record_tag"])
        assert "\\[Element]" in stored["source"]
        stored["source"] = stored["source"].replace("\\[Element]", "∈")
        assert w.rec._verify_full()["verified"]
        path = os.path.join(w.dir, "fin.nb")
        w.fake.files[path] = copy.deepcopy(w.fake.cells)
        assert w.rec._verify_finalized(path)["verified"]


def test_source_altered_in_transit_faults():
    """If the kernel received different text from what was sent, nothing runs."""
    with workspace() as w:
        w.fake.transit_transform = lambda s: s.replace("1", "2")
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert r["error"] == "pre-dispatch verification failed: source altered in transit"
        assert r["intended_digest"] == sha("x = 1") and r["received_digest"] == sha("x = 2")
        assert w.rec.ledger.data["fault"]["phase"] == "PRE_DISPATCH"


def test_helper_without_digests_faults():
    """An older helper that returns no digests fails closed and says why."""
    with workspace() as w:
        w.fake.old_helper = True
        r = w.rec.record_and_verify("x = 1")
        assert r["success"] is False
        assert "restart the kernel" in r["error"]
        assert w.rec.ledger.records == []


def record_with_messages(w: Workspace, code: str, names: list[str]) -> dict:
    r = w.rec.record_and_verify(code)
    assert r.get("success") and r.get("pre_dispatch_verified"), r
    outcome = w.rec.apply_outcome(r["seq"], dict(COMPLETED), message_names=names)
    assert not outcome["recording_faulted"], outcome
    return r


def fresh_run(messages: list, labels: list) -> dict:
    return {"success": True, "finalized": True,
            "messages": messages, "labels": labels}


def test_outcome_stores_message_names():
    """Message names are stored once each, without the symbol's context."""
    with workspace() as w:
        r = record_with_messages(w, "N[Sin[10^30], 20]",
                                 ["N::meprec", "MyPackage`f::x", "N::meprec"])
        assert w.rec.ledger.record_by_seq(r["seq"])["message_names"] == [
            "N::meprec", "f::x"]


def test_finalize_accepts_messages_seen_at_record_time():
    """A cell that gave a message when recorded may give it again at finalization."""
    with workspace() as w:
        a = record_with_messages(w, "y = 1/0", ["Power::infy"])
        b = record_with_messages(w, "z = 3", [])
        run = fresh_run([["cells", 7, "Power::infy"]],
                        [[7, a["record_tag"]], [8, b["record_tag"]]])
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"], fin
        assert fin["repeated_messages"] == [
            {"seq": a["seq"], "record_tag": a["record_tag"], "messages": ["Power::infy"]}]
        assert w.rec.is_sealed


def test_finalize_refuses_new_message():
    """A message a cell did not give when recorded fails finalization, and says where."""
    with workspace() as w:
        a = record_with_messages(w, "y = 1/0", ["Power::infy"])
        b = record_with_messages(w, "z = 3", [])
        run = fresh_run([["cells", 1, "Power::infy"], ["cells", 2, "Part::partw"]],
                        [[1, a["record_tag"]], [2, b["record_tag"]]])
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False and "not seen when the cells were recorded" in fin["error"]
        assert fin["unexpected_messages"] == [
            {"seq": b["seq"], "record_tag": b["record_tag"],
             "messages": ["Part::partw"], "recorded": []}]
        assert not w.rec.is_sealed
        assert w.rec.ledger.data["finalization"]["success"] is False
        with fresh_kernel_stub():
            assert w.rec.finalize(timeout=5)["success"]


def test_finalize_refuses_message_outside_recorded_cells():
    """A message while opening or saving the notebook belongs to no cell and fails."""
    with workspace() as w:
        a = record_with_messages(w, "y = 1/0", ["Power::infy"])
        for phase, line in (("save", 6), ("cells", 3)):
            run = fresh_run([[phase, line, "Power::infy"]], [[5, a["record_tag"]]])
            with fresh_kernel_stub(run):
                fin = w.rec.finalize(timeout=5)
            assert fin["success"] is False, (phase, fin)
            assert fin["unexpected_messages"][0]["issue"] == "not_from_a_recorded_cell"
        assert not w.rec.is_sealed


def test_record_without_message_names_tolerates_no_message():
    """A record stored without message names (an older ledger) tolerates none."""
    with workspace() as w:
        r = w.record_ok("y = 1/0")
        assert "message_names" not in w.rec.ledger.record_by_seq(r["seq"])
        run = fresh_run([["cells", 1, "Power::infy"]], [[1, r["record_tag"]]])
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False
        assert fin["unexpected_messages"][0]["messages"] == ["Power::infy"]



def test_messages_from_later_statements_belong_to_their_cell():
    """A cell with several statements uses several lines; all of them are its own."""
    with workspace() as w:
        a = record_with_messages(w, "x = 1\ny = 1/0", ["Power::infy"])
        b = record_with_messages(w, "z = 3", [])
        run = fresh_run([["cells", 8, "Power::infy"]],
                        [[7, a["record_tag"]], [10, b["record_tag"]]])
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"], fin
        assert [m["seq"] for m in fin["repeated_messages"]] == [a["seq"]], fin


def test_finalize_refuses_when_recorded_cells_outlast_the_timeout():
    """A run whose cells took longer than the timeout is refused before it starts."""
    with workspace() as w:
        for code in ("slow1 = 1", "slow2 = 2"):
            r = w.record_ok(code)
            rec = w.rec.ledger.record_by_seq(r["seq"])
            w.rec.ledger.update_record(r["seq"], disposition_at=rec["appended_at"] + 400.0)
        with fresh_kernel_stub():
            refused = w.rec.finalize(timeout=600)
        assert refused["success"] is False and "pass a larger timeout" in refused["error"]
        assert refused["estimated_seconds"] >= 800 and refused["timeout"] == 600, refused
        assert "finalization" not in w.rec.ledger.data
        assert not w.rec.is_faulted
        with fresh_kernel_stub():
            fin = w.rec.finalize(timeout=1000)
        assert fin["success"] and fin["timeout"] == 1000, fin


def test_kernel_state_at_start_is_kept_in_the_ledger():
    """What the kernel held before recording is stored and reported at finalize."""
    with workspace() as w:
        snapshot = {"user_symbols": ["a", "f1"], "packages": ["MyPackage`"]}
        rec = Recorder(w.fake, "hnb1", w.nb_path, kernel_snapshot=snapshot)
        w.fake._recorder = rec
        w.rec = rec
        assert RecorderLedger.load(rec.ledger.path).data["kernel_at_start"] == snapshot
        w.record_ok("result = a + 1")
        with fresh_kernel_stub():
            fin = rec.finalize(timeout=5)
        assert fin["success"] and fin["kernel_at_record_start"] == snapshot, fin



def record_result(w: Workspace, code: str, text: str, expect: str | None = None,
                  volatile: bool = False) -> tuple[dict, dict]:
    r = w.rec.record_and_verify(code, expect=expect, volatile=volatile)
    assert r.get("success") and r.get("pre_dispatch_verified"), r
    outcome = w.rec.apply_outcome(r["seq"], dict(COMPLETED), message_names=[],
                                  output_text=text)
    assert not outcome["recording_faulted"], outcome
    return r, outcome


def fresh_output(line: int, text: str) -> list:
    return [line, sha(text), text[:200], len(text)]


def test_outcome_stores_result_fingerprint_and_checkpoint():
    """The result's fingerprint is kept, and a checkpoint is judged at record time."""
    with workspace() as w:
        r, out = record_result(w, "residues = {0, 0, 0}", "{0, 0, 0}", expect="{0,0,0}")
        rec = w.rec.ledger.record_by_seq(r["seq"])
        assert rec["output_digest"] == sha("{0, 0, 0}") and rec["expect"] == "{0,0,0}"
        assert rec["checkpoint_at_record"] == "pass"
        assert out["checkpoint"] == {"expected": "{0,0,0}", "got": "{0, 0, 0}", "status": "pass"}
        r2, out2 = record_result(w, "ok = False", "False", expect="True")
        assert out2["checkpoint"]["status"] == "fail"


def test_finalize_reports_reproduced_results_and_seals():
    with workspace() as w:
        a, _ = record_result(w, "x = 2", "2")
        b, _ = record_result(w, "ok = True", "True", expect="True")
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]], [2, b["record_tag"]]],
               "outputs": [fresh_output(1, "2"), fresh_output(2, "True")]}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] and w.rec.is_sealed, fin
        assert fin["status"] == {"structure": "verified", "fresh_run": "completed",
                                 "messages": "as_recorded", "reproduction": "same",
                                 "checkpoints": "pass"}, fin
        assert fin["reproduction"] == {"status": "same", "same": 2}, fin


def test_changed_result_blocks_sealing():
    """A result that differs in the fresh kernel fails finalization, and the file is kept."""
    with workspace() as w:
        a, _ = record_result(w, "result = a + 1", "4")
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]]], "outputs": [fresh_output(1, "1 + a")]}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False and not w.rec.is_sealed, fin
        assert "differs from the recorded one" in fin["error"], fin
        assert fin["reproduction"]["differs"] == [
            {"seq": a["seq"], "record_tag": a["record_tag"], "recorded": "4",
             "fresh": "1 + a", "recorded_digest": sha("4"),
             "fresh_digest": sha("1 + a")}], fin
        assert os.path.exists(fin["finalized_path"])
        attempt = w.rec.ledger.data["finalization"]
        assert attempt["success"] is False and attempt["status"]["reproduction"] == "differs"


def test_same_and_volatile_results_seal():
    with workspace() as w:
        a, _ = record_result(w, "x = 2", "2")
        b, _ = record_result(w, "t = AbsoluteTime[]", "3.9e9", volatile=True)
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]], [2, b["record_tag"]]],
               "outputs": [fresh_output(1, "2"), fresh_output(2, "4.0e9")]}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] and w.rec.is_sealed, fin
        assert fin["reproduction"] == {"status": "same", "same": 1,
                                       "volatile": [b["seq"]]}, fin


def test_missing_result_blocks_sealing():
    """A cell the fresh run gave no result for cannot count as reproduced."""
    with workspace() as w:
        a, _ = record_result(w, "x = 2", "2")
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]]], "outputs": []}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False and not w.rec.is_sealed, fin
        assert fin["reproduction"]["status"] == "incomplete", fin
        assert "no result from the fresh run" in fin["error"], fin


@contextlib.contextmanager
def counting_stub(result: dict):
    """A fresh-kernel stub that also counts how often the fresh run was started."""
    calls = []
    original = recorder_mod._evaluate_in_fresh_kernel
    def run(path, timeout=600):
        calls.append(path)
        return dict(result)
    recorder_mod._evaluate_in_fresh_kernel = run
    try:
        yield calls
    finally:
        recorder_mod._evaluate_in_fresh_kernel = original


def differing_attempt(w: Workspace) -> tuple[dict, dict, dict]:
    """Record a cell, run a finalization in which it differs; return the pieces."""
    a, _ = record_result(w, "result = a + 1", "4")
    run = {"success": True, "finalized": True, "messages": [],
           "labels": [[1, a["record_tag"]]], "outputs": [fresh_output(1, "1 + a")]}
    with fresh_kernel_stub(run):
        first = w.rec.finalize(timeout=5)
    assert first["success"] is False and first["status"]["reproduction"] == "differs"
    return a, run, first


def test_accepted_difference_seals_and_says_so():
    """A difference the last attempt reported can be accepted, and is shown as accepted."""
    with workspace() as w:
        a, run, first = differing_attempt(w)
        digest = first["reproduction"]["differs"][0]["fresh_digest"]
        acceptance = [{"seq": a["seq"], "fresh_digest": digest,
                       "reason": "a was defined outside the record on purpose"}]
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5, accept_differences=acceptance)
        assert fin["success"] and w.rec.is_sealed, fin
        assert fin["status"]["reproduction"] == "accepted", fin
        taken = fin["reproduction"]["accepted"]
        assert taken == [{"seq": a["seq"], "record_tag": a["record_tag"],
                          "recorded": "4", "fresh": "1 + a",
                          "recorded_digest": sha("4"), "fresh_digest": digest,
                          "reason": "a was defined outside the record on purpose",
                          "accepted_from_attempt": 1}], fin
        stored = w.rec.ledger.data["finalization"]
        assert stored["success"] is True and stored["reproduction"]["accepted"] == taken
        assert fin["anchors"]["reproduction"] == "accepted", fin
        later = verifier(w).verify_record(fin["finalized_path"])
        assert later["reproduction"]["status"] == "accepted", later


def test_acceptances_that_are_refused():
    """Only a reported difference, with its fingerprint and a reason, and never a checkpoint."""
    with workspace() as w:
        a, _ = record_result(w, "result = a + 1", "4")
        with counting_stub({"success": True}) as calls:
            refused = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": sha("1 + a"), "reason": "r"}])
        assert refused["success"] is False and "there is none" in refused["error"], refused
        assert calls == [] and "finalization" not in w.rec.ledger.data

    with workspace() as w:
        a, run, first = differing_attempt(w)
        b, _ = record_result(w, "ok = True", "True", expect="True")
        digest = first["reproduction"]["differs"][0]["fresh_digest"]
        attempts = len(w.rec.ledger.data["finalization_attempts"])
        cases = [
            ({"seq": 99, "fresh_digest": digest, "reason": "r"}, "no recorded cell"),
            ({"seq": b["seq"], "fresh_digest": digest, "reason": "r"}, "checkpoint"),
            ({"seq": a["seq"], "fresh_digest": sha("something else"), "reason": "r"},
             "not the fresh result"),
            ({"seq": a["seq"], "fresh_digest": digest, "reason": "  "}, "needs a reason"),
        ]
        for item, words in cases:
            with counting_stub(run) as calls:
                refused = w.rec.finalize(timeout=5, accept_differences=[item])
            assert refused["success"] is False and words in refused["error"], (item, refused)
            assert calls == [], item
        with counting_stub(run) as calls:
            twice = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": digest, "reason": "r"},
                {"seq": a["seq"], "fresh_digest": digest, "reason": "r"}])
        assert "accepted twice" in twice["error"] and calls == []
        assert len(w.rec.ledger.data["finalization_attempts"]) == attempts


def test_cell_not_among_the_last_differences_is_refused():
    with workspace() as w:
        a, run, first = differing_attempt(w)
        b, _ = record_result(w, "x = 2", "2")
        with counting_stub(run) as calls:
            refused = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": b["seq"], "fresh_digest": sha("2"), "reason": "r"}])
        assert "did not differ in the last finalization attempt" in refused["error"], refused
        assert calls == []


def test_accepted_cell_that_changes_again_fails():
    """An acceptance binds to the fresh result it named; a new value is a new difference."""
    with workspace() as w:
        a, run, first = differing_attempt(w)
        digest = first["reproduction"]["differs"][0]["fresh_digest"]
        moved = dict(run, outputs=[fresh_output(1, "2 + a")])
        with fresh_kernel_stub(moved):
            fin = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": digest, "reason": "r"}])
        assert fin["success"] is False and not w.rec.is_sealed, fin
        assert fin["reproduction"]["differs"][0]["accepted_fresh_digest"] == digest, fin


def two_differing_cells(w: Workspace) -> tuple[dict, dict, dict]:
    """Record two cells and run a finalization in which both differ."""
    a, _ = record_result(w, "u = Module[{x}, x]", "x$17178")
    b, _ = record_result(w, "v = RandomInteger[10^6]", "979118")
    run = {"success": True, "finalized": True, "messages": [],
           "labels": [[1, a["record_tag"]], [2, b["record_tag"]]],
           "outputs": [fresh_output(1, "x$16960"), fresh_output(2, "885115")]}
    with fresh_kernel_stub(run):
        first = w.rec.finalize(timeout=5)
    assert [d["seq"] for d in first["reproduction"]["differs"]] == [a["seq"], b["seq"]]
    return a, b, run


def test_acceptance_must_name_every_difference():
    """Accepting part of what an attempt reported is refused before any fresh run."""
    with workspace() as w:
        a, b, run = two_differing_cells(w)
        with counting_stub(run) as calls:
            partial = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": sha("x$16960"), "reason": "r"}])
        assert partial["success"] is False and calls == [], partial
        assert f"also found seq {b['seq']} different" in partial["error"], partial

        # Accepted together: cell a repeats, cell b changes again, so it fails.
        moved = dict(run, outputs=[fresh_output(1, "x$16960"), fresh_output(2, "442872")])
        with fresh_kernel_stub(moved):
            second = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": sha("x$16960"), "reason": "r"},
                {"seq": b["seq"], "fresh_digest": sha("885115"), "reason": "r"}])
        assert second["success"] is False and not w.rec.is_sealed, second
        assert [x["seq"] for x in second["reproduction"]["accepted"]] == [a["seq"]]
        assert [x["seq"] for x in second["reproduction"]["differs"]] == [b["seq"]]

        # The cell accepted in that attempt is not carried over: name it again.
        with counting_stub(moved) as calls:
            again = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": b["seq"], "fresh_digest": sha("442872"), "reason": "r"}])
        assert f"also found seq {a['seq']} different" in again["error"] and calls == []
        with fresh_kernel_stub(moved):
            sealed = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": sha("x$16960"), "reason": "r"},
                {"seq": b["seq"], "fresh_digest": sha("442872"), "reason": "r"}])
        assert sealed["success"] and w.rec.is_sealed, sealed
        assert sealed["status"]["reproduction"] == "accepted", sealed


def test_differing_checkpoint_rules_out_any_acceptance():
    """When a checkpoint's result differed, no acceptance of the other cells can seal."""
    with workspace() as w:
        a, _ = record_result(w, "result = a + 1", "4")
        b, _ = record_result(w, "ok = TrueQ[result == 4]", "True", expect="True")
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]], [2, b["record_tag"]]],
               "outputs": [fresh_output(1, "1 + a"), fresh_output(2, "False")]}
        with fresh_kernel_stub(run):
            first = w.rec.finalize(timeout=5)
        assert first["success"] is False
        with counting_stub(run) as calls:
            refused = w.rec.finalize(timeout=5, accept_differences=[
                {"seq": a["seq"], "fresh_digest": sha("1 + a"), "reason": "r"}])
        assert refused["success"] is False and calls == [], refused
        assert f"checkpoint seq {b['seq']} differed" in refused["error"], refused


def test_failed_checkpoint_blocks_sealing():
    """A checkpoint that fails in the fresh kernel stops the seal and keeps the file."""
    with workspace() as w:
        a, _ = record_result(w, "checkPassed = TrueQ[res === 0]", "True", expect="True")
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]]], "outputs": [fresh_output(1, "False")]}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False and not w.rec.is_sealed, fin
        assert "checkpoint did not pass" in fin["error"], fin
        assert fin["checkpoints"] == {"status": "fail", "results": [
            {"seq": a["seq"], "record_tag": a["record_tag"], "expected": "True",
             "got": "False", "status": "fail"}]}, fin
        assert os.path.exists(fin["finalized_path"])
        attempt = w.rec.ledger.data["finalization"]
        assert attempt["success"] is False and attempt["status"]["checkpoints"] == "fail"


def test_missing_checkpoint_result_fails():
    """No fresh result for a checkpoint cell is not a pass."""
    with workspace() as w:
        a, _ = record_result(w, "ok = True", "True", expect="True")
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]]], "outputs": []}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"] is False, fin
        assert fin["checkpoints"]["results"][0]["status"] == "missing", fin
        assert fin["reproduction"]["status"] == "incomplete", fin


def test_volatile_cell_difference_is_not_reported():
    with workspace() as w:
        a, _ = record_result(w, "t = AbsoluteTime[]", "3.9e9", volatile=True)
        assert w.rec.ledger.record_by_seq(a["seq"])["volatile"] is True
        run = {"success": True, "finalized": True, "messages": [],
               "labels": [[1, a["record_tag"]]], "outputs": [fresh_output(1, "4.0e9")]}
        with fresh_kernel_stub(run):
            fin = w.rec.finalize(timeout=5)
        assert fin["success"], fin
        assert fin["reproduction"] == {"status": "same", "same": 0,
                                       "volatile": [a["seq"]]}, fin


def test_expect_on_narrative_cell_is_refused():
    with workspace() as w:
        r = w.rec.record_and_verify("Results", style="Section", expect="True")
        assert r["success"] is False and "Input or Code" in r["error"], r
        assert not w.rec.is_faulted and w.rec.ledger.records == []


def test_literal_comparison_ignores_whitespace_outside_strings():
    assert recorder_mod._same_literal("{0, 0, 0}", "{0,0,0}")
    assert recorder_mod._same_literal('"pass"', ' "pass" ')
    assert not recorder_mod._same_literal('"a b"', '"ab"')
    assert not recorder_mod._same_literal("1 + a", "4")



def verifier(w: Workspace):
    """A real HeadlessNotebooks that reads the finalized file through FakeHelper."""
    nb = headless_with(w.fake, w.nb_path)
    nb.open, nb.close, nb.is_open = w.fake.open, w.fake.close, w.fake.is_open
    return nb


def file_sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def sealed_run(w: Workspace) -> tuple[dict, dict]:
    a, _ = record_result(w, "x = 2", "2")
    run = {"success": True, "finalized": True, "messages": [],
           "labels": [[1, a["record_tag"]]], "outputs": [fresh_output(1, "2")]}
    with fresh_kernel_stub(run):
        fin = w.rec.finalize(timeout=5)
    assert fin["success"] and w.rec.is_sealed, fin
    return a, fin


def test_finalize_returns_anchors_of_both_files():
    with workspace() as w:
        _, fin = sealed_run(w)
        assert fin["anchors"] == {"run_id": w.rec.run_id,
                                  "file_sha256": file_sha(fin["finalized_path"]),
                                  "ledger_sha256": file_sha(w.rec.ledger.path),
                                  "reproduction": "same"}, fin
        assert "cannot later rewrite" in fin["anchors_note"]


def test_failed_finalize_returns_no_anchors():
    with workspace() as w:
        a, run, first = differing_attempt(w)
        assert "anchors" not in first, first


def test_anchors_catch_an_edited_ledger():
    with workspace() as w:
        _, fin = sealed_run(w)
        anchors = fin["anchors"]
        with open(w.rec.ledger.path, "a") as fh:
            fh.write("\n")
        checked = verifier(w).verify_record(
            fin["finalized_path"], expected_file_sha256=anchors["file_sha256"],
            expected_ledger_sha256=anchors["ledger_sha256"])
        assert checked["anchors_match"] == {"file": True, "ledger": False}, checked
        assert checked["verified"] is False, checked


def test_anchors_catch_a_consistent_edit_of_file_and_ledger():
    """Changing a recorded cell and its ledger together passes locally, not against anchors."""
    with workspace() as w:
        a, fin = sealed_run(w)
        anchors, path = fin["anchors"], fin["finalized_path"]
        edited = copy.deepcopy(w.fake.cells)
        next(c for c in edited if c["tag"] == a["record_tag"])["source"] = "x = 3"
        w.fake.files[path] = edited
        with open(path, "a") as fh:
            fh.write("(* edited *)")
        ledger = RecorderLedger.load(w.rec.ledger.path)
        ledger.record_by_seq(a["seq"])["source_digest"] = sha(canon("x = 3"))
        ledger.data["finalization"]["file_sha256"] = file_sha(path)
        ledger.data["finalization_attempts"][-1]["file_sha256"] = file_sha(path)
        recorder_mod._write_atomically(ledger.path, ledger.data)

        plain = verifier(w).verify_record(path)
        assert plain["verified"] is True and plain["changes"] == "none", plain
        assert "local ledger only" in plain["note"], plain

        anchored = verifier(w).verify_record(
            path, expected_file_sha256=anchors["file_sha256"],
            expected_ledger_sha256=anchors["ledger_sha256"])
        assert anchored["anchors_match"] == {"file": False, "ledger": False}, anchored
        assert anchored["verified"] is False, anchored


# --- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]

    passed = failed = 0
    print("=== Recorder hardening tests ===")
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
