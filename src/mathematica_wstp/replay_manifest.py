"""A durable record of what a replay intended, written before it starts.

The supervisor's ledger is authoritative for what a kernel actually did. This is
authoritative for what the notebook layer meant to do, and it exists because of
a measured fact: replaying a notebook writes outputs into the session document
inside the kernel, not to the file. Nothing reaches disk until someone saves.

So a run identity kept only in memory is lost by exactly the failure that makes
reconciliation necessary. A client that dies cannot ask "did child 7 of my run
ever reach the kernel?" if it no longer knows the run was called R042f621acc.
The manifest is therefore written, and fsynced, BEFORE the first child is
submitted -- that ordering is the whole point of the file, not a detail of it.

Three states a child moves through, which Phase B showed are genuinely
different facts and must not be collapsed:

    PLANNED             we intend to evaluate this input
    SUBMITTED           it was handed to an evaluator; the answer may be lost
    EXECUTED            the evaluator answered
    OUTPUT_IN_SESSION   its output was applied to the live session document
    SOURCE_DIVERGED     the input is no longer the one this run planned to run

A saved-to-disk state belongs above this and is not claimed here: the notebook
file is only written when something saves it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from typing import Any

SCHEMA = "replay-manifest-v1"
DEFAULT_DIRNAME = ".mcp-replays"


def manifest_dir(notebook_path: str) -> str:
    """Where manifests live: beside the notebook unless told otherwise."""
    override = os.environ.get("MATHEMATICA_WSTP_REPLAY_DIR")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(notebook_path)), DEFAULT_DIRNAME)


def _write_atomically(path: str, payload: dict[str, Any]) -> None:
    """Write so that a crash leaves either the old file or the new one.

    A half-written manifest is worse than none: it would be read on reconnect as
    an authoritative statement about which children were submitted.
    """
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


class ReplayManifest:
    """The notebook layer's durable record of one replay."""

    def __init__(self, path: str, data: dict[str, Any]):
        self.path = path
        self.data = data

    # -- creation ----------------------------------------------------------

    @classmethod
    def create(cls, notebook_path: str, notebook_id: str, run_id: str,
               plan: list[dict[str, Any]], notebook_digest: str | None = None) -> ReplayManifest:
        """Persist the plan. Returns only once it is on disk and fsynced."""
        path = os.path.join(manifest_dir(notebook_path), f"{run_id}.json")
        data = {
            "schema": SCHEMA,
            "run_id": run_id,
            "created": time.time(),
            "notebook": {"path": os.path.abspath(notebook_path),
                         "session_id": notebook_id,
                         "digest": notebook_digest},
            "children": [
                {"ordinal": item["ordinal"],
                 "child_id": item["child_id"],
                 "idempotency_key": item["idempotency_key"],
                 "input_digest": item.get("input_digest"),
                 "state": "PLANNED",
                 "request_id": None,
                 "evaluation_token": None,
                 "backend": None,
                 "output_in_session": False}
                for item in plan
            ],
        }
        _write_atomically(path, data)
        return cls(path, data)

    @classmethod
    def load(cls, path: str) -> ReplayManifest:
        with open(path) as fh:
            return cls(path, json.load(fh))

    @classmethod
    def for_notebook(cls, notebook_path: str) -> list[str]:
        """Every manifest written for this notebook, newest last."""
        directory = manifest_dir(notebook_path)
        if not os.path.isdir(directory):
            return []
        found = [os.path.join(directory, name) for name in os.listdir(directory)
                 if name.endswith(".json") and not name.startswith(".tmp-")]
        return sorted(found, key=os.path.getmtime)

    # -- updates -----------------------------------------------------------

    def child(self, ordinal: int) -> dict[str, Any] | None:
        for entry in self.data["children"]:
            if entry["ordinal"] == ordinal:
                return entry
        return None

    def mark(self, ordinal: int, **fields: Any) -> None:
        """Record what is now known about one child, durably."""
        entry = self.child(ordinal)
        if entry is None:
            return
        entry.update(fields)
        _write_atomically(self.path, self.data)

    # -- reading -----------------------------------------------------------

    def block(self, reason: str, child_id: str | None = None) -> None:
        """Record that automatic reconciliation of this run must stop.

        Deliberately not destructive. The children that ran still ran, and their
        history is worth exactly as much as it was a moment ago; what has ended
        is this run's licence to keep going on its own.
        """
        self.data["blocked"] = {"reason": reason, "child_id": child_id,
                                "at": time.time()}
        _write_atomically(self.path, self.data)

    @property
    def blocked(self) -> dict[str, Any] | None:
        return self.data.get("blocked")

    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    def unfinished(self) -> list[dict[str, Any]]:
        """Children whose fate a reconnecting client would have to establish."""
        return [c for c in self.data["children"]
                if c["state"] in ("PLANNED", "SUBMITTED")]

    def summary(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for entry in self.data["children"]:
            states[entry["state"]] = states.get(entry["state"], 0) + 1
        return {"run_id": self.run_id, "path": self.path,
                "children": len(self.data["children"]), "states": states}
