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
        self.run_id = f"R{uuid.uuid4().hex[:10]}"
        self.ledger = RecorderLedger.create(notebook_path, notebook_id,
                                            self.run_id)

    def record_and_verify(self, code: str, style: str = "Input"
                          ) -> dict[str, Any]:
        """Write a tagged cell, append to ledger, verify via pre-dispatch read-back."""
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
                "success": True,
                "record_tag": tag,
                "pre_dispatch_verified": False,
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
        """Store the raw disposition axes for a record, then verify post-eval."""
        self.ledger.update_record(seq, disposition=disposition,
                                  disposition_at=time.time())

        verification = self._verify_full()
        return {
            "seq": seq,
            "disposition": disposition,
            "post_eval_verification": verification,
        }

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
