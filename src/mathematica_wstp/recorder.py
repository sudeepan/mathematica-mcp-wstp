"""The recording integrity verifier.

Wraps HeadlessNotebooks to enforce the recorder protocol: every cell written
during a recording session gets a durable identity, is verified against the
notebook before dispatch, and has its raw disposition axes recorded after
evaluation returns.

The recorder does not own the evaluation. It records what happened and
verifies that the notebook still matches the ledger afterwards.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from typing import Any, TYPE_CHECKING

from .recorder_ledger import RecorderLedger

if TYPE_CHECKING:
    from .notebooks import HeadlessNotebooks

logger = logging.getLogger("mathematica_wstp.recorder")


class Recorder:
    """One recording session's integrity verifier."""

    def __init__(self, notebooks: HeadlessNotebooks, notebook_id: str,
                 notebook_path: str):
        self.notebooks = notebooks
        self.notebook_id = notebook_id
        self.notebook_path = notebook_path
        self.run_id = f"R{uuid.uuid4().hex[:10]}"
        self.ledger = RecorderLedger.create(notebook_path, notebook_id,
                                            self.run_id)

    def record_and_verify(self, code: str, style: str = "Input"
                          ) -> dict[str, Any]:
        """Write a tagged cell, append to ledger, verify via pre-dispatch read-back.

        Returns success=False if any step fails. The caller must not dispatch
        the scientific evaluation when success is False.
        """
        tag = self.ledger.make_tag()

        write_result = self.notebooks._call_with_session(
            "MCPWriteCell", self.notebook_id, code, style, "End", 0, tag)
        if not write_result.get("success"):
            logger.warning("recorder write failed: %s", write_result.get("error"))
            return write_result

        readback = self.notebooks._call_with_session(
            "MCPReadBack", self.notebook_id, timeout=30)
        if not readback.get("success"):
            logger.warning("pre-dispatch read-back failed: %s",
                           readback.get("error"))
            return {
                "success": False,
                "error": "pre-dispatch verification failed: read-back error",
                "record_tag": tag,
                "readback_error": readback.get("error"),
            }

        our_cell = None
        for c in readback.get("cells", []):
            if c.get("record_tag") == tag:
                our_cell = c
                break

        if our_cell is None:
            return {
                "success": False,
                "error": f"pre-dispatch verification failed: "
                         f"cell with tag {tag} not found in notebook",
            }

        record = self.ledger.append(
            source_digest=our_cell["source_digest"],
            source_preview=our_cell.get("source_preview", code[:200]),
            style=style,
            record_tag=tag,
        )

        return {
            "success": True,
            "seq": record["seq"],
            "record_tag": tag,
            "source_digest": our_cell["source_digest"],
            "pre_dispatch_verified": True,
            "notebook_cell_count": readback["total"],
        }

    def apply_outcome(self, seq: int, disposition: dict[str, str]
                      ) -> dict[str, Any]:
        """Store the raw disposition axes for a record, then verify post-eval.

        When the execution outcome is not COMPLETED, the cell is automatically
        annotated as non-evaluatable so finalization will not re-run it.
        """
        self.ledger.update_record(seq, disposition=disposition,
                                  disposition_at=time.time())

        annotation = self._auto_annotate(seq, disposition)

        verification = self._verify_full()
        return {
            "seq": seq,
            "disposition": disposition,
            "annotation": annotation,
            "post_eval_verification": verification,
        }

    def _auto_annotate(self, seq: int, disposition: dict[str, str]
                       ) -> dict[str, Any] | None:
        """Mark non-COMPLETED cells as non-evaluatable in the notebook."""
        outcome = disposition.get("execution_outcome", "")
        if outcome == "COMPLETED":
            return None

        record = self.ledger.record_by_seq(seq)
        if record is None:
            return None

        tag = record["record_tag"]
        readback = self.notebooks._call_with_session(
            "MCPReadBack", self.notebook_id, timeout=30)
        if not readback.get("success"):
            logger.warning("annotation read-back failed: %s",
                           readback.get("error"))
            return {"applied": False, "error": readback.get("error")}

        cell_index = None
        for c in readback.get("cells", []):
            if c.get("record_tag") == tag:
                cell_index = c["index"]
                break

        if cell_index is None:
            return {"applied": False, "error": f"cell {tag} not found"}

        reason = _annotation_reason(disposition)
        result = self.notebooks.annotate_cell(
            cell_index, evaluatable=False, reason=reason,
            notebook=self.notebook_id)
        applied = result.get("success", False)

        self.ledger.update_record(seq, annotated=applied,
                                  annotation_reason=reason)
        return {"applied": applied, "reason": reason}

    def unresolved_records(self) -> list[dict[str, Any]]:
        """Records with a non-COMPLETED outcome that have not been annotated."""
        unresolved = []
        for r in self.ledger.records:
            disp = r.get("disposition")
            if disp is None:
                continue
            if disp.get("execution_outcome") == "COMPLETED":
                continue
            if not r.get("annotated"):
                unresolved.append(r)
        return unresolved

    def has_unresolved(self) -> bool:
        return len(self.unresolved_records()) > 0

    def finalize(self, timeout: int = 600) -> dict[str, Any]:
        """Copy the recording notebook and evaluate it in a fresh kernel.

        Preflight refuses when any record is unresolved (non-COMPLETED and
        not yet annotated). The prototype kernel is never touched.
        """
        unresolved = self.unresolved_records()
        if unresolved:
            return {
                "success": False,
                "error": "cannot finalize: unresolved records exist",
                "unresolved": [{"seq": r["seq"], "tag": r["record_tag"]}
                               for r in unresolved],
            }

        verification = self._verify_full()
        if not verification.get("verified"):
            return {
                "success": False,
                "error": "pre-finalization verification failed",
                "verification": verification,
            }

        nb_path = self.notebook_path
        if not nb_path:
            return {"success": False,
                    "error": "recording notebook has no disk path"}

        saved = self.notebooks.save(notebook=self.notebook_id)
        if not saved.get("success"):
            return saved

        base, ext = os.path.splitext(nb_path)
        finalized_path = f"{base}-finalized{ext}"
        shutil.copy2(nb_path, finalized_path)
        logger.info("finalize: copied %s -> %s", nb_path, finalized_path)

        result = _evaluate_in_fresh_kernel(finalized_path, timeout)

        self.ledger.update_record(
            self.ledger.records[-1]["seq"] if self.ledger.records else 0,
            finalized_at=time.time(),
            finalized_path=finalized_path,
            finalization_success=result.get("success", False),
        )

        self.ledger.data["finalization"] = {
            "path": finalized_path,
            "success": result.get("success", False),
            "at": time.time(),
        }
        from .recorder_ledger import _write_atomically
        _write_atomically(self.ledger.path, self.ledger.data)

        result["recording_path"] = nb_path
        result["finalized_path"] = finalized_path
        result["run_id"] = self.run_id
        return result

    def _verify_full(self) -> dict[str, Any]:
        """Full read-back: compare every ledger entry against the notebook."""
        readback = self.notebooks._call_with_session(
            "MCPReadBack", self.notebook_id, timeout=30)
        if not readback.get("success"):
            return {"verified": False, "error": readback.get("error")}

        cells = readback.get("cells", [])
        tagged_cells = {c["record_tag"]: c for c in cells
                        if c.get("record_tag")}

        issues = []
        for record in self.ledger.records:
            tag = record["record_tag"]
            cell = tagged_cells.get(tag)
            if cell is None:
                issues.append({"seq": record["seq"], "tag": tag,
                               "issue": "cell_missing"})
            elif cell["source_digest"] != record["source_digest"]:
                issues.append({"seq": record["seq"], "tag": tag,
                               "issue": "source_changed",
                               "expected": record["source_digest"],
                               "found": cell["source_digest"]})

        return {
            "verified": len(issues) == 0,
            "notebook_cells": readback["total"],
            "ledger_records": len(self.ledger.records),
            "issues": issues,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "notebook_id": self.notebook_id,
            "ledger": self.ledger.summary(),
        }


def _evaluate_in_fresh_kernel(nb_path: str, timeout: int = 600
                              ) -> dict[str, Any]:
    """Spin up a temporary kernel, run NotebookEvaluate, shut it down.

    Cells with Evaluatable->False are skipped by NotebookEvaluate. The
    prototype kernel is never touched.
    """
    from .kernel import Kernel, KernelError

    temp_kernel = Kernel()
    try:
        temp_kernel.start(timeout=60)
    except KernelError as exc:
        return {"success": False,
                "error": f"could not start finalization kernel: {exc}"}

    escaped = nb_path.replace("\\", "\\\\").replace('"', '\\"')
    code = (
        'Module[{nbo, evalResult, ok = True, detail = ""},'
        '  Quiet[Check['
        f'    UsingFrontEnd['
        f'      nbo = NotebookOpen["{escaped}", Visible -> False];'
        '      If[Head[nbo] =!= NotebookObject,'
        '        ok = False; detail = "NotebookOpen failed";,'
        '        evalResult = NotebookEvaluate[nbo, InsertResults -> True];'
        '        NotebookSave[nbo];'
        '        NotebookClose[nbo]'
        '      ]'
        '    ],'
        '    ok = False; detail = ToString[$MessageList]'
        '  ], {FrontEndObject::notavail}];'
        '  <|"success" -> ok, "detail" -> detail|>'
        ']'
    )

    try:
        result = temp_kernel.evaluate_json(code, timeout=float(timeout))
        if isinstance(result, dict) and result.get("success"):
            return {"success": True, "finalized": True}
        detail = result.get("detail", "") if isinstance(result, dict) else str(result)
        return {"success": False, "error": f"NotebookEvaluate: {detail}"}
    except Exception as exc:
        return {"success": False, "error": f"finalization kernel error: {exc}"}
    finally:
        try:
            temp_kernel.close(grace=5.0)
        except Exception:
            logger.warning("failed to close finalization kernel", exc_info=True)


def _annotation_reason(disposition: dict[str, str]) -> str:
    """Human-readable reason for why a cell was marked non-evaluatable."""
    outcome = disposition.get("execution_outcome", "FAILED")
    intent = disposition.get("control_intent", "NONE")
    readiness = disposition.get("kernel_readiness", "READY")

    parts = [outcome.lower().replace("_", " ")]
    if intent == "SYSTEM_TIMEOUT":
        parts.append("system timeout")
    elif intent == "USER_REQUESTED":
        parts.append("user requested")
    if readiness in ("FAULTED", "RESTARTED"):
        parts.append(f"kernel {readiness.lower()}")
    return "; ".join(parts)


def extract_disposition(result: Any, kernel_notice: str | None,
                        kernel_verdict: str | None) -> dict[str, str]:
    """Derive raw disposition axes from an evaluation result.

    Each axis records one independent fact. They are never collapsed into a
    simpler enum - that is a display concern, not a recording concern.
    """
    if result.success:
        execution_outcome = "COMPLETED"
    elif result.timed_out:
        execution_outcome = "TIMED_OUT"
    elif result.aborted:
        execution_outcome = "ABORTED"
    else:
        execution_outcome = "FAILED"

    if result.timed_out:
        control_intent = "SYSTEM_TIMEOUT"
    elif result.abort_requested_during or result.aborted:
        control_intent = "USER_REQUESTED"
    else:
        control_intent = "NONE"

    if not (result.timed_out or result.aborted or result.abort_requested_during):
        abort_confirmation = "NOT_APPLICABLE"
    elif kernel_verdict == "alive":
        abort_confirmation = "CONFIRMED"
    elif kernel_verdict == "dead":
        abort_confirmation = "CONFIRMED"
    elif kernel_verdict == "unverified":
        abort_confirmation = "UNCERTAIN"
    else:
        abort_confirmation = "NOT_CHECKED"

    if kernel_notice:
        kernel_readiness = "RESTARTED"
    elif kernel_verdict == "dead":
        kernel_readiness = "FAULTED"
    elif kernel_verdict == "unverified":
        kernel_readiness = "FAULTED"
    elif kernel_verdict == "alive":
        kernel_readiness = "READY"
    else:
        kernel_readiness = "READY"

    return {
        "execution_outcome": execution_outcome,
        "control_intent": control_intent,
        "abort_confirmation": abort_confirmation,
        "kernel_readiness": kernel_readiness,
    }
