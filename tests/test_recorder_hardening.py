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
            if self.write_transform:
                content = self.write_transform(content)
            if not self.drop_writes:
                evaluatable = None if self.ignore_stamp else {"True": True, "False": False}.get(stamp)
                cells.append(cell(style, content, tag, evaluatable))
            return {"success": True, "id": notebook_id, "record_tag": tag}
        if fn == "MCPReadBack":
            return {"success": True, "id": notebook_id, "total": len(cells),
                    "cells": [self._readback(i, c) for i, c in enumerate(cells)]}
        return {"success": False, "error": f"FakeHelper does not implement {fn}"}

    @staticmethod
    def _readback(index: int, c: dict) -> dict:
        return {"index": index, "style": c["style"],
                "executable": c["style"] in EXECUTABLE_STYLES,
                "source_digest": sha(c["source"]),
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
        assert r["intended_digest"] == sha("x = 1")
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
