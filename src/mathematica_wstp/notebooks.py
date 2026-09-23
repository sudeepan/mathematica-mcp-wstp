"""Notebook operations that need no front end.

The addon's notebook commands drive a live front-end window. On a headless host
there is no window, so ``notebooks(action="open")`` fails outright, ``cells``
has nothing to list, and ``evaluate(target="notebook")`` has nowhere to write —
the server degrades to evaluating loose expressions, which is not what "run this
notebook" means.

This module implements the same vocabulary against the ``.nb`` file on disk,
driven through the persistent kernel by ``helpers/headless_notebook.wl``. A
"notebook" here is the ``Notebook[...]`` expression, and evaluating a cell
replays its box content exactly as Shift+Enter would.

Sessions live in the kernel, so a kernel restart drops them. Because that
restart can happen silently mid-workflow, every call that reports an unknown
session reopens the file from this module's registry and retries once, rather
than surfacing an error the caller cannot act on.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import uuid
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .replay_manifest import ReplayManifest

logger = logging.getLogger("mathematica_wstp.notebooks")

# Guards the registry only. Kernel evaluation is serialised inside session.py.
_registry_lock = threading.Lock()


def _wl_string(value: str) -> str:
    """Escape a Python string into a WL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _wl_arg(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _wl_string("" if value is None else str(value))


@dataclass
class _Session:
    """What this process needs to rebuild a kernel session it may have lost."""

    notebook_id: str
    path: str
    title: str = ""
    created: bool = False  # True when opened via create rather than from disk


def _with_timeout_policy(reply: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Say which per-cell budget a call actually ran under.

    Three entry points have three different defaults, and none of them used to
    announce the one in force. A notebook whose cells need minutes, replayed
    under a default of seconds, reports timed-out cells -- which reads as the
    science failing rather than as the caller having inherited a number they
    never chose. ``setdefault`` so a helper that reports its own budget wins.
    """
    if isinstance(reply, dict):
        reply.setdefault("execution_timeout_seconds", int(timeout))
    return reply


@dataclass
class HeadlessNotebooks:
    """Front-end-free notebook sessions backed by the persistent kernel."""

    _sessions: dict[str, _Session] = field(default_factory=dict)
    _counter: int = 0
    _recording_target: str | None = field(default=None, repr=False)

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _helper_path() -> str:
        return str(Path(__file__).parent / "helpers" / "headless_notebook.wl")

    def _call(self, function: str, *args: Any, timeout: int = 60,
              correlation: dict[str, str] | None = None,
              idempotency_key: str | None = None) -> dict[str, Any]:
        """Invoke one helper function and parse its JSON reply.

        The helper is loaded behind an in-kernel sentinel, so a restarted kernel
        reloads it on the next call instead of failing with an undefined symbol.
        """
        from .evaluator import get_evaluator

        helper = _wl_string(self._helper_path())
        arglist = ", ".join(_wl_arg(a) for a in args)
        # Normal[] unwraps the helper's ByteArray into a byte list the link can
        # carry. The helper returns bytes rather than a string so that notebook
        # content -- \[Gamma] and every other non-ASCII character a physics
        # notebook is full of -- survives the trip intact.
        # The reply MUST be a ByteArray. If the helper returns anything else --
        # $Aborted from an abort inside a cell, $Failed, an unevaluated symbol --
        # then Normal[] of it is not a byte list, WSGetInteger8List fails with
        # "WSGet out of sequence", and the link is left in an error state that
        # made the whole session unusable. Measured: a setup cell whose package
        # refuses to load twice aborted in 0.3s and cost an entire replay. So
        # catch the abort and coerce anything unexpected into a JSON error that
        # the transport CAN carry.
        code = (
            "Module[{mcpRes},"
            "  mcpRes = CheckAbort[Module[{},"
            f"    If[!TrueQ[$MCPHeadlessNotebookLoaded],"
            f"      If[Get[{helper}] =!= $Failed, $MCPHeadlessNotebookLoaded = True]];"
            f"    MCPHeadlessNotebook`{function}[{arglist}]"
            "  ], $Aborted];"
            "  If[Head[mcpRes] === ByteArray, mcpRes,"
            "    ExportByteArray[<|\"success\" -> False,"
            "      \"error\" -> \"the helper returned \" <> ToString[Head[mcpRes]]"
            "        <> \", not an encoded reply\","
            "      \"aborted\" -> TrueQ[mcpRes === $Aborted],"
            "      \"raw\" -> StringTake[ToString[Short[mcpRes, 3]], UpTo[300]]|>,"
            "      \"RawJSON\", \"Compact\" -> True]]"
            "]"
        )
        # One evaluation, one handle. The backend decides how bytes cross the
        # link; this layer only needs the bytes and, later, the identity of the
        # execution that produced them.
        result = get_evaluator().submit_bytes(
            code, timeout=timeout, idempotency_key=idempotency_key,
            correlation=correlation).wait(timeout + 30)
        if not result.success:
            return {
                "success": False,
                "error": result.detail or "kernel evaluation failed",
                "timed_out": result.timed_out,
                "headless": True,
            }
        text = (result.data or b"").decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Could not parse headless notebook reply as JSON",
                "raw": text[:2000],
                "headless": True,
            }
        if isinstance(parsed, dict):
            parsed.setdefault("headless", True)
            parsed.setdefault("execution_method", result.backend)
            # Only a backend with durable identity has these; the direct one
            # reports local bookkeeping, which is why it also reports that it is
            # not authenticated.
            parsed.setdefault("execution_id", result.request_id)
            parsed.setdefault("execution_token", result.token)
            parsed.setdefault("execution_authenticated", result.authenticated)
            return parsed
        return {"success": False, "error": "Unexpected reply shape", "raw": text[:2000], "headless": True}

    def _call_with_session(self, function: str, notebook_id: str, *args: Any, timeout: int = 60,
                           correlation: dict[str, str] | None = None,
                           idempotency_key: str | None = None) -> dict[str, Any]:
        """``_call`` that transparently reopens a session the kernel has lost.

        A silent kernel swap is the failure mode most likely to waste a long
        workflow: every later call fails on state that used to be there. Cell
        *contents* come from the file, so reopening restores everything this
        layer owns — but variables the notebook defined are genuinely gone, so
        the caller is told the replay has to restart.
        """
        result = self._call(function, notebook_id, *args, timeout=timeout,
                            correlation=correlation, idempotency_key=idempotency_key)
        if result.get("success") or "No such headless notebook session" not in str(result.get("error", "")):
            return result

        with _registry_lock:
            session = self._sessions.get(notebook_id)
        if session is None:
            return result

        logger.info("headless session %s vanished (kernel restart?); reopening %s", notebook_id, session.path)
        if session.created and not os.path.exists(session.path):
            reopened = self._call("MCPCreate", notebook_id, session.path, session.title)
        else:
            reopened = self._call("MCPOpen", notebook_id, session.path)
        if not reopened.get("success"):
            return result

        retried = self._call(function, notebook_id, *args, timeout=timeout)
        retried["session_reopened"] = True
        retried["kernel_state_lost"] = True
        retried["next_step"] = (
            "The kernel restarted, so this notebook's session was rebuilt from disk. "
            "Variables defined by earlier cells are gone — re-run the cells you depend on."
        )
        return retried

    def _new_id(self) -> str:
        with _registry_lock:
            self._counter += 1
            return f"hnb{self._counter}"

    def _resolve(self, notebook: str | None) -> str | None:
        """Map a caller-supplied handle to a session id.

        Accepts a session id, a file path, or None (meaning "the only open
        notebook", which is what a single-notebook workflow always means).
        """
        with _registry_lock:
            if notebook:
                if notebook in self._sessions:
                    return notebook
                target = os.path.abspath(os.path.expanduser(notebook))
                for sid, session in self._sessions.items():
                    if session.path == target:
                        return sid
                return None
            if len(self._sessions) == 1:
                return next(iter(self._sessions))
            return None

    # -- operations -------------------------------------------------------

    def is_open(self, path: str) -> bool:
        """True when this path already has a session somebody is holding.

        Callers that open a file only to read it need this: ``open``
        de-duplicates by path, so a path already open comes back as the
        existing session's id rather than a fresh one, and closing it
        afterwards would destroy a session its owner still holds.
        """
        return self._resolve(os.path.abspath(os.path.expanduser(path))) is not None

    def open(self, path: str) -> dict[str, Any]:
        abs_path = os.path.abspath(os.path.expanduser(path))
        existing = self._resolve(abs_path)
        notebook_id = existing or self._new_id()
        result = self._call("MCPOpen", notebook_id, abs_path)
        if result.get("success"):
            with _registry_lock:
                self._sessions[notebook_id] = _Session(notebook_id, abs_path)
        else:
            # The id is already minted and appears in the reply, so a caller that
            # ignores this will hold a handle nothing can resolve — and the next
            # evaluate(target="notebook") silently runs in the kernel instead.
            # Say so loudly rather than hand back a dead handle.
            logger.warning(
                "headless open did not register %s (%s): handle is not usable",
                notebook_id,
                result.get("error", "no success flag in reply"),
            )
            result["registered"] = False
        return result

    def create(self, title: str = "Untitled", path: str | None = None) -> dict[str, Any]:
        notebook_id = self._new_id()
        target = os.path.abspath(os.path.expanduser(path)) if path else ""
        result = self._call("MCPCreate", notebook_id, target, title)
        if result.get("success"):
            with _registry_lock:
                self._sessions[notebook_id] = _Session(notebook_id, target, title=title, created=True)
            if not target:
                result["note"] = (
                    "Headless notebook exists in memory only. "
                    "Call notebooks(action='save', path=...) to write it to disk."
                )
        return result

    def close(self, notebook: str | None = None) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        result = self._call("MCPClose", notebook_id)
        with _registry_lock:
            self._sessions.pop(notebook_id, None)
        if not result.get("success") and "No such headless notebook session" in str(result.get("error", "")):
            # The kernel had already lost this session (a restart, or a swap we
            # did not see). Every other session-taking method reopens and retries
            # via _call_with_session, but reopening a notebook purely to close it
            # is pointless: the caller's goal is already met, and the registry
            # entry is dropped above either way. Report success, not a failure
            # the caller can do nothing about.
            logger.info("headless close: kernel had already lost %s; treating as closed", notebook_id)
            return {
                "success": True,
                "id": notebook_id,
                "closed": True,
                "already_closed": True,
                "headless": True,
            }
        return result

    def list(self) -> dict[str, Any]:
        """Notebooks this process can actually address.

        Reads the registry rather than asking the kernel, because the registry is
        what `_resolve` consults: a kernel-side listing could show notebooks that
        every other method then fails to find. Kernel entries with no registry
        row are reported separately as `unregistered` instead of being hidden.
        """
        with _registry_lock:
            rows = [
                {"id": s.notebook_id, "path": s.path, "title": s.title, "created": s.created}
                for s in self._sessions.values()
            ]
            known = {s.notebook_id for s in self._sessions.values()}
        out: dict[str, Any] = {"success": True, "notebooks": rows, "headless": True}
        kernel_side = self._call("MCPList")
        if kernel_side.get("success"):
            stray = [
                n.get("id")
                for n in kernel_side.get("notebooks", [])
                if n.get("id") and n.get("id") not in known
            ]
            if stray:
                out["unregistered"] = stray
        return out

    def session_path(self, notebook: str | None = None) -> str | None:
        """The file a session is bound to, without asking the kernel anything.

        Listing or reconciling a replay has to work while the kernel is busy
        with the very run being asked about, so this deliberately costs no
        round trip. ``info`` answers the same question but needs the kernel.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return None
        with _registry_lock:
            return self._sessions[notebook_id].path

    def info(self, notebook: str | None = None) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        with _registry_lock:
            session = self._sessions[notebook_id]
        result = self._call_with_session("MCPCells", notebook_id, 0, 0, False)
        if not result.get("success"):
            return result
        return {
            "success": True,
            "id": notebook_id,
            "path": session.path,
            "cell_count": result.get("total", 0),
            "headless": True,
        }

    def cells(
        self,
        notebook: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        include_content: bool = True,
        style: str | None = None,
    ) -> dict[str, Any]:
        """List cells, optionally only those of one style.

        The style filter is applied kernel-side before offset/limit, because
        filtering a page after the fact would drop matches lying outside it.
        Returned indices stay notebook-wide so they remain valid for evaluation.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        return self._call_with_session(
            "MCPCells", notebook_id, int(offset), int(limit or 0), bool(include_content), str(style or "")
        )

    def evaluate_cell(self, index: int, notebook: str | None = None, timeout: int = 60) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        reply = self._call_with_session(
            "MCPEvaluateCell", notebook_id, int(index), int(timeout), timeout=timeout + 15
        )
        return _with_timeout_policy(reply, timeout)

    def verify_against(self, reference: str, notebook: str | None = None,
                       timeout: int = 300) -> dict[str, Any]:
        """Compare the replayed document against the notebook it came from."""
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        return self._call_with_session(
            "MCPVerifyAgainst", notebook_id, str(reference), timeout=timeout)

    def evaluate_range(
        self,
        start: int = 0,
        end: int = -1,
        notebook: str | None = None,
        timeout: int = 60,
        stop_on_error: bool = True,
        write_outputs: bool = False,
    ) -> dict[str, Any]:
        """Evaluate a span of cells in one kernel evaluation.

        BASELINE span execution: the loop runs inside Wolfram, so the span is a
        single execution with a single identity, and its output edits are applied
        together at the end. ``replay_cells`` is the per-cell alternative, which
        trades round trips for per-cell identity and incremental durability.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        # The per-cell timeout bounds each cell; the transport has to outlast the
        # whole span, so give it room for every cell in the range to use its budget.
        span = max(1, (end - start + 1) if end >= 0 else 64)
        # A file, because there is no other channel: while the span runs, the
        # kernel is inside one evaluation and nothing can be evaluated in it to
        # set a flag. abort_current touches this path, and the loop reads it
        # between cells to tell a user abort from a cell's own Abort[].
        from . import session as _session

        sentinel = os.path.join(tempfile.gettempdir(),
                                f"mcp-wstp-abort-{os.getpid()}-{notebook_id}")
        with contextlib.suppress(OSError):
            os.unlink(sentinel)
        _session.set_abort_sentinel(sentinel)
        try:
            reply = self._call_with_session(
                "MCPEvaluateRange",
                notebook_id,
                int(start),
                int(end),
                int(timeout),
                bool(stop_on_error),
                bool(write_outputs),
                sentinel,
                timeout=timeout * span + 30,
            )
            return _with_timeout_policy(reply, timeout)
        finally:
            _session.set_abort_sentinel(None)
            with contextlib.suppress(OSError):
                os.unlink(sentinel)

    def replay_cells(
        self,
        notebook: str | None = None,
        first: int = 1,
        last: int = -1,
        timeout: int = 60,
        stop_on_error: bool = True,
        write_outputs: bool = False,
        run: str | None = None,
    ) -> dict[str, Any]:
        """Replay inputs one at a time, so every cell is its own execution.

        EXPERIMENTAL, auditable per-cell execution. ``evaluate_range`` remains
        the baseline span path; both exist so the two can be compared, and one
        of them is expected to be retired once there is evidence for which.

        ``evaluate_range`` hands a whole span to the kernel and gets one answer
        back. That is fewer round trips and it is the right thing when nobody
        needs to know what happened cell by cell. It also means a span has ONE
        execution identity: if the client dies halfway, what ran is not
        recoverable from the outside, and a user's abort has to be signalled
        through a file on disk because the kernel is busy for the whole span and
        cannot be asked anything.

        Driving the loop here costs a round trip per cell -- measured at about
        9 ms, which is noise beside any real cell -- and buys a request, a
        token, a correlation and an idempotency key for each one. That is what
        makes a replay resumable rather than merely repeatable.

        Cells are addressed by input ORDINAL, never by index: writing an output
        inserts a cell and shifts every index after it.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)

        listing = self.cells(notebook=notebook_id, limit=100000)
        if not listing.get("success"):
            return listing
        inputs = [c for c in listing.get("cells", [])
                  if c.get("style") in ("Input", "Code")]
        total = len(inputs)
        upper = total if last < 0 else min(last, total)
        if first < 1 or first > total:
            return {"success": False, "headless": True,
                    "error": f"first ordinal {first} is outside 1..{total}"}

        run_id = run or f"R{uuid.uuid4().hex[:10]}"
        started = time.time()

        # Content identity per input, so a reconnecting client can tell that
        # ordinal 7 still exists but is no longer the same science.
        digests = {}
        probe = self._call_with_session("MCPInputDigests", notebook_id, timeout=30)
        if probe.get("success"):
            digests = {d["ordinal"]: d["digest"] for d in probe.get("digests", [])}

        plan = [{"ordinal": n, "child_id": f"c{n}",
                 "idempotency_key": f"{run_id}.c{n}",
                 "input_digest": digests.get(n)}
                for n in range(first, upper + 1)]

        # Written and fsynced BEFORE the first child is submitted. A run whose
        # identity exists only in this process is lost by exactly the failure
        # that makes reconciliation necessary.
        manifest = None
        try:
            manifest = ReplayManifest.create(
                self._sessions[notebook_id].path, notebook_id, run_id, plan,
                execution_timeout_seconds=int(timeout))
        except OSError as exc:
            logger.warning("could not persist the replay manifest: %s", exc)

        # The same out-of-band channel the span path uses, and for the same
        # reason: while a cell is running, the kernel cannot be asked anything,
        # so a user's abort has to reach the helper by other means. Without it
        # the helper has no way to tell an abort it was sent from a cell that
        # called Abort[] itself, and reports the user's interruption as the
        # cell's own -- which is a false claim about what the science did.
        #
        # A supervisor-backed evaluator does not need this: it records the
        # control intent structurally. The file is what the direct backend has.
        from . import session as _session

        sentinel = os.path.join(tempfile.gettempdir(),
                                f"mcp-wstp-abort-{os.getpid()}-{notebook_id}-{run_id}")
        with contextlib.suppress(OSError):
            os.unlink(sentinel)
        _session.set_abort_sentinel(sentinel)
        cells: list[dict[str, Any]] = []
        executed = skipped = failed = 0
        stopped_at = None

        for ordinal in range(first, upper + 1):
            child = f"c{ordinal}"
            if manifest:
                # submit(), not mark(state=...): this transition is where the
                # absolute deadline comes from, and it exists in one place so a
                # child cannot acquire a deadline without a submission time.
                manifest.submit(ordinal)
            reply = self._call_with_session(
                "MCPEvaluateInput", notebook_id, int(ordinal), int(timeout),
                bool(write_outputs), sentinel,
                # Rides with the output cell and survives a save, so the
                # document can later say which child wrote what. Only what is
                # known before submission goes in here; the request id and token
                # are joined through the manifest.
                f"run={run_id};child={child};ordinal={ordinal};"
                f"input={(digests.get(ordinal) or '')[:16]}",
                timeout=timeout + 15,
                correlation={"parent": run_id, "child": child, "kind": "notebook_cell"},
                # Stable across a reconnect: the same cell of the same replay is
                # the same submission, so a client that loses its answer can ask
                # again without running the cell twice.
                idempotency_key=f"{run_id}.{child}",
            )
            entry = {"ordinal": ordinal, "child": child, "run": run_id,
                     "success": bool(reply.get("success"))}
            results = reply.get("results") or []
            if results:
                first_result = results[0]
                entry.update({k: first_result.get(k) for k in
                              ("index", "style", "output", "printed", "messages",
                               "timed_out", "aborted", "timing_ms", "reason")
                              if k in first_result})
            if not reply.get("success"):
                entry["error"] = reply.get("error")
            entry["request_id"] = reply.get("execution_id")
            entry["evaluation_token"] = reply.get("execution_token")
            cells.append(entry)
            if manifest:
                manifest.mark(ordinal, state="EXECUTED",
                              request_id=reply.get("execution_id"),
                              evaluation_token=reply.get("execution_token"),
                              backend=reply.get("execution_method"),
                              # Applied to the live session document -- which is
                              # not the file, and not durable until a save.
                              output_in_session=bool(write_outputs
                                                     and reply.get("outputs_written")))

            if entry.get("timed_out") or entry.get("aborted") or not entry["success"]:
                failed += 1
                if stop_on_error:
                    stopped_at = ordinal
                    break
            elif entry.get("reason"):
                skipped += 1
            else:
                executed += 1

        _session.set_abort_sentinel(None)
        with contextlib.suppress(OSError):
            os.unlink(sentinel)

        return {
            "success": stopped_at is None,
            "headless": True,
            "run": run_id,
            "manifest": manifest.path if manifest else None,
            "notebook": notebook_id,
            "summary": {"inputs": total, "attempted": len(cells), "executed": executed,
                        "skipped": skipped, "failed": failed,
                        "stopped_at_ordinal": stopped_at,
                        # The budget every cell ran under. A caller that did not
                        # pass one inherited a default, and the point of saying
                        # so here is that it stops being invisible: a notebook
                        # with hour-long cells replayed under the default would
                        # otherwise look like a notebook of failing cells.
                        "execution_timeout_seconds": int(timeout),
                        "seconds": round(time.time() - started, 2)},
            "cells": cells,
        }

    def reconcile_replay(
        self,
        manifest_path: str,
        notebook: str | None = None,
        evaluator_lookup=None,
    ) -> dict[str, Any]:
        """Work out what a replay actually achieved, after whatever interrupted it.

        Three questions, in this order, because the first can make the others
        moot:

        1. Is the notebook still the one this run planned to replay? Compared by
           stored-box digest, per child. Nothing else can answer this -- the
           payload a cell submission carries names the session and the ordinal
           and never the cell's content, so an execution ledger cannot tell two
           different cells at the same ordinal apart.

        2. For every child recorded as SUBMITTED, what became of it? The answer
           is looked up by the caller-chosen idempotency key, not by submitting
           anything again. A resubmission cannot recover it anyway: the payload
           embeds the submitting process's own pid, so a new client reproduces a
           different request and is rightly refused.

        3. Which children never ran at all?

        On divergence this stops and says so. It does not carry on to the later
        children, because one changed cell can alter definitions, assumptions
        and package state that everything after it depends on, and without a
        dependency graph the notebook layer cannot know otherwise. The completed
        prefix is preserved rather than discarded: what ran, ran.
        """
        from .evaluator import get_evaluator
        from .replay_manifest import ReplayManifest, past_deadline

        # A caller may pass its own lookup, but the usual case is that the
        # backend already knows how to answer. The direct backend does not and
        # cannot: its request ids are local bookkeeping, and if this process
        # died there is nobody left to ask. So the absence of a lookup is a
        # fact about the backend, and it is reported rather than hidden.
        lookup_backend = "caller-supplied" if evaluator_lookup else None
        if evaluator_lookup is None:
            evaluator = get_evaluator()
            evaluator_lookup = getattr(evaluator, "lookup", None)
            lookup_backend = evaluator.name if evaluator_lookup else None

        try:
            manifest = ReplayManifest.load(manifest_path)
        except (OSError, ValueError) as exc:
            return {"success": False, "headless": True,
                    "error": f"could not read the replay manifest: {exc}"}

        # Two stages, in this order, because the kernel may still be busy with
        # the very work being reconciled. Execution facts come from the ledger
        # and need no kernel at all; only the source check needs one. A client
        # that demanded the kernel first would be unable to find out what it was
        # busy with.
        notebook_id = self._resolve(notebook)
        current: dict[int, str] = {}
        source_check = "done"
        if notebook_id is None:
            source_check = "skipped: no open notebook session"
        else:
            try:
                probe = self._call_with_session("MCPInputDigests", notebook_id, timeout=30)
            except Exception as exc:                  # noqa: BLE001 - reported, not hidden
                probe = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            if probe.get("success"):
                current = {d["ordinal"]: d["digest"] for d in probe.get("digests", [])}
            else:
                source_check = f"deferred: {str(probe.get('error'))[:120]}"

        # A document with no inputs at all, for a run that planned some, is
        # almost never a notebook whose cells were deleted. A session can be
        # rebuilt in a fresh kernel by reopening the file, and a replay writes
        # its outputs into the SESSION document -- nothing reaches disk until
        # someone saves. So the likeliest reading is that this is a different
        # document from the one the run was about.
        #
        # Measured: reconciling after switching backends rebuilt the session
        # from an unsaved file and reported SOURCE_MISSING for every child of a
        # notebook that was entirely intact. A confident wrong answer about
        # whether the science still exists is worse than no answer, so this
        # refuses to judge and says which two possibilities remain.
        if source_check == "done" and not current and manifest.data["children"]:
            source_check = (
                "unavailable: the document presented has no input cells at all, while "
                f"this run planned {len(manifest.data['children'])}. Either every cell "
                "was deleted, or this is not the document that was replayed -- a session "
                "reopened from an unsaved file looks exactly like this. Reopen the "
                "notebook that was replayed, in the kernel that ran it, to judge the source.")
            current = {}

        # What the document says about who wrote its outputs. An output cell on
        # its own proves only that something wrote one: a person, an earlier
        # replay, or this run. The tag says which, and it survives a save.
        written: dict[str, int] = {}
        if source_check == "done":
            marks = self._call_with_session("MCPOutputProvenance", notebook_id, timeout=30)
            if marks.get("success"):
                for item in marks.get("provenance", []):
                    tag = item.get("child") or ""
                    for part in tag.split(";"):
                        if part.startswith("child="):
                            written[f'{tag.split(";")[0].removeprefix("run=")}.{part[6:]}'] = item["index"]

        children: list[dict[str, Any]] = []
        diverged = None
        for entry in manifest.data["children"]:
            ordinal = entry["ordinal"]
            record = {"ordinal": ordinal, "child_id": entry["child_id"],
                      "recorded_state": entry["state"],
                      "idempotency_key": entry["idempotency_key"],
                      # The policy travels with the state it explains. Without
                      # it a STILL_RUNNING verdict says a computation is alive
                      # but not until when, which is the one thing a caller
                      # deciding whether to wait actually needs.
                      "execution_timeout_seconds": entry.get("execution_timeout_seconds"),
                      "submitted_at_utc": entry.get("submitted_at_utc"),
                      "execution_deadline_utc": entry.get("execution_deadline_utc")}
            now = current.get(ordinal)
            if source_check != "done":
                pass                                   # cannot judge the source yet
            elif entry.get("input_digest") and now and now != entry["input_digest"]:
                record["verdict"] = "SOURCE_DIVERGED"
                record["detail"] = "the input under this ordinal is not the one that was planned"
                children.append(record)
                diverged = diverged or record
                continue
            elif now is None:
                record["verdict"] = "SOURCE_MISSING"
                record["detail"] = "there is no input at this ordinal any more"
                children.append(record)
                diverged = diverged or record
                continue

            if entry["state"] == "EXECUTED":
                record["verdict"] = "COMPLETE"
                record["request_id"] = entry.get("request_id")
                record["evaluation_token"] = entry.get("evaluation_token")
                record["output_in_session"] = entry.get("output_in_session")
                if source_check == "done" and entry.get("output_in_session"):
                    key = f'{manifest.run_id}.{entry["child_id"]}'
                    if key in written:
                        record["output"] = f"PRESENT at index {written[key]}"
                    else:
                        # It ran and its output was applied, and the document no
                        # longer carries that mark: someone has been here since.
                        record["output"] = "MISSING_OR_OVERWRITTEN"
            elif entry["state"] == "SUBMITTED":
                # The answer was lost, not the work. Ask by the one name that
                # outlives the client that chose it.
                found = evaluator_lookup(entry["idempotency_key"]) if evaluator_lookup else None
                if found and "state=RUNNING" in str(found):
                    # Still in the kernel, with nobody waiting for it. The
                    # caller decides whether to wait or take it over; this layer
                    # does not abort other people's science on its own.
                    record["verdict"] = "STILL_RUNNING"
                    record["found"] = found
                    # Reported, never acted on. Whether a computation past its
                    # deadline should be abandoned is the caller's decision;
                    # this layer does not end other people's science on a clock
                    # it merely wrote down.
                    expired = past_deadline(entry.get("execution_deadline_utc"))
                    if expired is not None:
                        record["past_deadline"] = expired
                elif found:
                    record["verdict"] = "RECOVERED"
                    record["found"] = found
                elif evaluator_lookup is None:
                    # Nobody could be asked. Not the same fact as having asked
                    # and found nothing, and collapsing the two would let a
                    # caller read "we checked" out of a reply that never
                    # checked anything.
                    record["verdict"] = "UNRESOLVED"
                    record["detail"] = (
                        "submitted, and there is no execution record to consult: the "
                        "backend in use keeps none that outlives this process. Whether "
                        "it ran cannot be established from here or anywhere else.")
                else:
                    record["verdict"] = "UNRESOLVED"
                    record["detail"] = (
                        f"submitted, and the {lookup_backend} ledger has no record under "
                        "its key; it was asked, and the answer is that nothing ran")
            else:
                record["verdict"] = "NEVER_SUBMITTED"
            children.append(record)

        if diverged:
            manifest.block("INPUT_DIGEST_MISMATCH", diverged["child_id"])

        verdicts: dict[str, int] = {}
        for record in children:
            verdicts[record["verdict"]] = verdicts.get(record["verdict"], 0) + 1

        return {
            "success": True,
            "headless": True,
            "run": manifest.run_id,
            "manifest": manifest.path,
            "blocked": manifest.blocked,
            "source_check": source_check,
            # Which of the two stages could actually be carried out. The source
            # check needs a kernel holding the document; the execution check
            # needs a ledger that outlived the client. They fail independently,
            # and a reply that reported only the verdicts would leave a caller
            # unable to tell "nothing ran" from "nothing could be asked".
            "execution_check": (f"done: {lookup_backend}" if lookup_backend
                                else "unavailable: the backend in use keeps no record "
                                     "that outlives this process"),
            "resumable_from": (None if diverged else
                               next((c["ordinal"] for c in children
                                     if c["verdict"] in ("NEVER_SUBMITTED", "UNRESOLVED")), None)),
            "summary": verdicts,
            "children": children,
            "choices": ([] if not diverged else [
                "restore the original cell and reconcile this run again",
                f"abandon run {manifest.run_id} at {diverged['child_id']}",
                "start a new replay run against the edited notebook",
            ]),
        }

    def verify_self(self, notebook: str | None = None) -> dict[str, Any]:
        """Check the open document's own cell labels, with no reference needed."""
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        return self._call_with_session("MCPVerifySelf", notebook_id, timeout=120)

    def find_defining(self, symbol: str, notebook: str | None = None) -> dict[str, Any]:
        """Find the cells of the open document that assign ``symbol``."""
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        return self._call_with_session("MCPFindDefining", notebook_id, symbol, timeout=60)

    _RECORDING_LOCKED = {
        "success": False,
        "error": "notebook is recording; only evaluate() can write to it",
        "headless": True,
    }

    def write_cell(
        self,
        content: str,
        style: str = "Input",
        notebook: str | None = None,
        position: str = "End",
        anchor: int = 0,
        record_tag: str = "",
    ) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        if notebook_id == self._recording_target:
            return dict(self._RECORDING_LOCKED)
        return self._call_with_session(
            "MCPWriteCell", notebook_id, content, style, position, int(anchor or 0), record_tag
        )

    def read_back(self, notebook: str | None = None) -> dict[str, Any]:
        """Full cell metadata for recorder verification.

        Returns every cell with its source digest, TaggingRules-based record
        tag, Evaluatable option, and CellTags. The recorder uses this to
        compare the notebook's actual state against the durable ledger.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        return self._call_with_session("MCPReadBack", notebook_id, timeout=30)

    def file_dependencies(self, notebook: str | None = None,
                          timeout: int = 120) -> dict[str, Any]:
        """What this notebook reads and writes, before running any of it.

        A notebook that loads a stored result is indistinguishable at runtime
        from one that computes it: both leave a value in a symbol and both
        report success. The difference shows only in the filesystem calls, and
        by the time a ``Get`` has silently returned ``$Failed`` the symptom is
        several cells downstream and looks like a failure of the science.

        Commented cells are included, and carry the finding: a commented
        computation directly above a live load is what a notebook shipped in
        "load the stored answer" mode looks like.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        raw = self._call_with_session("MCPFileDependencies", notebook_id, timeout=timeout)
        if not raw.get("success"):
            return raw

        ops = raw.get("operations") or []
        files: dict[str, dict[str, Any]] = {}
        for op in ops:
            entry = files.setdefault(os.path.basename(str(op.get("path", ""))), {
                "path": op.get("path"), "reads": [], "writes": [],
                "commented_reads": [], "commented_writes": []})
            bucket = ("commented_" if op.get("commented") else "") + \
                     ("reads" if op.get("kind") == "read" else "writes")
            entry[bucket].append(op.get("ordinal"))

        for name, e in files.items():
            live_r, live_w = e["reads"], e["writes"]
            if live_r and live_w and min(live_w) < min(live_r):
                e["verdict"] = "ROUND_TRIP"
                e["detail"] = ("written then read back inside the run; breaking "
                               "the write makes the read return $Failed silently")
            elif live_r and e["commented_writes"] and not live_w:
                e["verdict"] = "LOADS_A_STORED_RESULT"
                e["detail"] = ("read live, while the cell that would produce it is "
                               "commented out; the computation is usually the cell "
                               "just above that commented write")
            elif live_r and not live_w and not e["commented_writes"]:
                e["verdict"] = "EXTERNAL_INPUT"
                e["detail"] = "read but never written here; it must exist beforehand"
            elif live_w and not live_r:
                e["verdict"] = "WRITES_ONLY"
                e["detail"] = "this run overwrites it; check it is not something you need"
            else:
                e["verdict"] = "MIXED"
                e["detail"] = "read and written; inspect the ordinals"

        unresolved = [op for op in ops if str(op.get("path", "")).startswith("<unresolved")]
        return {"success": True, "headless": True, "id": notebook_id,
                "executable_cells": raw.get("executable_cells"),
                "files": files, "operations": ops,
                "unresolved": len(unresolved),
                "note": ("Paths that could not be resolved to a literal are reported "
                         "as <unresolved: ...> rather than guessed. A relative path "
                         "means whatever Directory[] is when that cell runs, which a "
                         "SetDirectory earlier in the notebook may have changed.")}

    def replace_cell(self, index: int, content: str,
                     notebook: str | None = None) -> dict[str, Any]:
        """Change one cell's content in place, keeping its style and options."""
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        if notebook_id == self._recording_target:
            return dict(self._RECORDING_LOCKED)
        return self._call_with_session("MCPReplaceCell", notebook_id, int(index), content)

    def delete_cell(self, index: int, notebook: str | None = None) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        if notebook_id == self._recording_target:
            return dict(self._RECORDING_LOCKED)
        return self._call_with_session("MCPDeleteCell", notebook_id, int(index))

    def execute_in_notebook(
        self,
        code: str,
        notebook: str | None = None,
        timeout: int = 60,
        style: str = "Input",
    ) -> dict[str, Any]:
        """Append a cell and evaluate it — the headless form of "run this in the notebook".

        Mirrors the addon's atomic write-then-evaluate so a caller gets one
        result rather than having to sequence two calls and reconcile them.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        if notebook_id == self._recording_target:
            return dict(self._RECORDING_LOCKED)
        written = self._call_with_session("MCPWriteCell", notebook_id, code, style, "End", 0)
        if not written.get("success"):
            return written
        index = max(0, int(written.get("cell_count", 1)) - 1)
        result = self._call_with_session(
            "MCPEvaluateCell", notebook_id, index, int(timeout), timeout=timeout + 15
        )
        result["cell_index"] = index
        result["written"] = True
        return result

    def save(self, notebook: str | None = None, path: str | None = None) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        target = os.path.abspath(os.path.expanduser(path)) if path else ""
        result = self._call_with_session("MCPSave", notebook_id, target)
        if result.get("success") and result.get("path"):
            with _registry_lock:
                if notebook_id in self._sessions:
                    self._sessions[notebook_id].path = result["path"]
        return result

    # -- recording --------------------------------------------------------

    def start_recording(self, notebook: str | None = None) -> dict[str, Any]:
        """Start recording every evaluate() call into a notebook."""
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        self._recording_target = notebook_id
        with _registry_lock:
            sess = self._sessions.get(notebook_id)
        return {
            "success": True,
            "recording": True,
            "notebook": notebook_id,
            "path": sess.path if sess else "",
            "headless": True,
        }

    def stop_recording(self) -> dict[str, Any]:
        was = self._recording_target
        self._recording_target = None
        return {
            "success": True,
            "recording": False,
            "was_recording": was,
            "headless": True,
        }

    @property
    def recording(self) -> str | None:
        return self._recording_target

    def record_input(self, code: str, style: str = "Input") -> dict[str, Any] | None:
        """Write a cell into the recording notebook, if one is active.

        Returns None when recording is off, the write result otherwise.
        Bypasses write_cell() so the recording lock cannot block it.
        Failures are logged but never block the caller's evaluation.
        """
        target = self._recording_target
        if target is None:
            return None
        try:
            return self._call_with_session(
                "MCPWriteCell", target, code, style, "End", 0)
        except Exception:
            logger.warning("recording cell write failed", exc_info=True)
            return {"success": False, "error": "recording write failed"}

    def finalize(self, notebook: str | None = None,
                 timeout: int = 600) -> dict[str, Any]:
        """Run NotebookEvaluate on the recorded notebook in a fresh kernel.

        Saves the notebook to disk first, then evaluates it via UsingFrontEnd
        so every cell gets native In[n]/Out[n] labels.
        """
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)

        saved = self.save(notebook=notebook_id)
        if not saved.get("success"):
            return saved
        nb_path = saved.get("path", "")
        if not nb_path:
            return {
                "success": False,
                "error": "notebook has no disk path; save it first",
                "headless": True,
            }

        return self._call_with_session(
            "MCPFinalize", notebook_id, nb_path, timeout=timeout,
        )

    # -- errors -----------------------------------------------------------

    def _no_session(self, notebook: str | None) -> dict[str, Any]:
        with _registry_lock:
            open_ids = sorted(self._sessions)
        if notebook:
            message = f"No headless notebook matches {notebook!r}"
        elif open_ids:
            message = "Several notebooks are open; name one with notebook="
        else:
            message = "No notebook is open"
        return {
            "success": False,
            "error": message,
            "open_notebooks": open_ids,
            "headless": True,
            "next_step": "notebooks(action='open', path='/path/to/file.nb')",
        }


_backend: HeadlessNotebooks | None = None


def get_headless_notebooks() -> HeadlessNotebooks:
    global _backend
    if _backend is None:
        _backend = HeadlessNotebooks()
    return _backend


def reset_headless_notebooks() -> None:
    """Drop the process-wide backend (tests)."""
    global _backend
    _backend = None
