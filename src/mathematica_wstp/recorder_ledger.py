"""A durable record of what the recorder wrote into a recording notebook.

The recorder audit ledger is the second authority (alongside the recording
notebook itself) for what a recording session intended to produce. It exists
because notebook mutations are in-memory until saved, and the notebook
expression can be tampered with by any evaluate() call that reaches the same
kernel.

The ledger is written, and fsynced, BEFORE the cell is dispatched to the
evaluator. That ordering mirrors ReplayManifest and exists for the same
reason: a record that survives only as long as the process that wrote it is
not a record.

Each entry carries:

    seq             monotonic within the run, starting at 1
    record_tag      the TaggingRules value stamped on the cell (run_id/seq)
    source_digest   SHA-256 of the canonical source text (same pipeline as
                    MCPInputDigests / boxText)
    source_preview  first 200 chars of the source, for human inspection
    style           the cell style (Input, Code, Section, ...)
    appended_at     epoch float

Disposition fields (execution_outcome, control_intent, etc.) are added in
Phase 2 when the recorder core loop is built.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from typing import Any

SCHEMA = "recorder-ledger-v1"
DEFAULT_DIRNAME = ".mcp-recordings"


def ledger_dir(notebook_path: str) -> str:
    override = os.environ.get("MATHEMATICA_WSTP_RECORDING_DIR")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(notebook_path)),
                        DEFAULT_DIRNAME)


def _write_atomically(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class RecorderLedger:
    """The recorder's durable record of one recording session."""

    def __init__(self, path: str, data: dict[str, Any]):
        self.path = path
        self.data = data

    @classmethod
    def create(cls, notebook_path: str, notebook_id: str,
               run_id: str) -> RecorderLedger:
        path = os.path.join(ledger_dir(notebook_path), f"{run_id}.json")
        data = {
            "schema": SCHEMA,
            "run_id": run_id,
            "created": time.time(),
            "notebook": {
                "path": os.path.abspath(notebook_path),
                "session_id": notebook_id,
            },
            "records": [],
            "next_seq": 1,
        }
        _write_atomically(path, data)
        return cls(path, data)

    @classmethod
    def load(cls, path: str) -> RecorderLedger:
        with open(path) as fh:
            return cls(path, json.load(fh))

    @classmethod
    def for_notebook(cls, notebook_path: str) -> list[str]:
        directory = ledger_dir(notebook_path)
        if not os.path.isdir(directory):
            return []
        found = [os.path.join(directory, name) for name in os.listdir(directory)
                 if name.endswith(".json") and not name.startswith(".tmp-")]
        return sorted(found, key=os.path.getmtime)

    def append(self, source_digest: str, source_preview: str,
               style: str, record_tag: str) -> dict[str, Any]:
        seq = self.data["next_seq"]
        record: dict[str, Any] = {
            "seq": seq,
            "record_tag": record_tag,
            "source_digest": source_digest,
            "source_preview": source_preview[:200],
            "style": style,
            "appended_at": time.time(),
        }
        self.data["records"].append(record)
        self.data["next_seq"] = seq + 1
        _write_atomically(self.path, self.data)
        return record

    def update_record(self, seq: int, **fields: Any) -> None:
        """Update fields on an existing record and flush to disk."""
        record = self.record_by_seq(seq)
        if record is None:
            return
        record.update(fields)
        _write_atomically(self.path, self.data)

    def record_by_seq(self, seq: int) -> dict[str, Any] | None:
        for r in self.data["records"]:
            if r["seq"] == seq:
                return r
        return None

    def record_by_tag(self, tag: str) -> dict[str, Any] | None:
        for r in self.data["records"]:
            if r["record_tag"] == tag:
                return r
        return None

    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.data["records"]

    @property
    def next_seq(self) -> int:
        return self.data["next_seq"]

    def make_tag(self) -> str:
        return f"{self.run_id}/{self.data['next_seq']}"

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": self.path,
            "records": len(self.data["records"]),
            "next_seq": self.data["next_seq"],
        }
