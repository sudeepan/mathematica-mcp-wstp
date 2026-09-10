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

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass
class HeadlessNotebooks:
    """Front-end-free notebook sessions backed by the persistent kernel."""

    _sessions: dict[str, _Session] = field(default_factory=dict)
    _counter: int = 0

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _helper_path() -> str:
        return str(Path(__file__).parent / "helpers" / "headless_notebook.wl")

    def _call(self, function: str, *args: Any, timeout: int = 60) -> dict[str, Any]:
        """Invoke one helper function and parse its JSON reply.

        The helper is loaded behind an in-kernel sentinel, so a restarted kernel
        reloads it on the next call instead of failing with an undefined symbol.
        """
        from .session import evaluate_wl_bytes

        helper = _wl_string(self._helper_path())
        arglist = ", ".join(_wl_arg(a) for a in args)
        # Normal[] unwraps the helper's ByteArray into a byte list the link can
        # carry. The helper returns bytes rather than a string so that notebook
        # content -- \[Gamma] and every other non-ASCII character a physics
        # notebook is full of -- survives the trip intact.
        code = (
            "Normal[Module[{},"
            f"  If[!TrueQ[$MCPHeadlessNotebookLoaded],"
            f"    If[Get[{helper}] =!= $Failed, $MCPHeadlessNotebookLoaded = True]];"
            f"  MCPHeadlessNotebook`{function}[{arglist}]"
            "]]"
        )
        result = evaluate_wl_bytes(code, timeout=timeout)
        if not result.success:
            return {
                "success": False,
                "error": result.error or "kernel evaluation failed",
                "timed_out": result.timed_out,
                "headless": True,
            }
        text = (result.text or "").strip()
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
            parsed.setdefault("execution_method", result.execution_method)
            return parsed
        return {"success": False, "error": "Unexpected reply shape", "raw": text[:2000], "headless": True}

    def _call_with_session(self, function: str, notebook_id: str, *args: Any, timeout: int = 60) -> dict[str, Any]:
        """``_call`` that transparently reopens a session the kernel has lost.

        A silent kernel swap is the failure mode most likely to waste a long
        workflow: every later call fails on state that used to be there. Cell
        *contents* come from the file, so reopening restores everything this
        layer owns — but variables the notebook defined are genuinely gone, so
        the caller is told the replay has to restart.
        """
        result = self._call(function, notebook_id, *args, timeout=timeout)
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
        return self._call_with_session(
            "MCPEvaluateCell", notebook_id, int(index), int(timeout), timeout=timeout + 15
        )

    def evaluate_range(
        self,
        start: int = 0,
        end: int = -1,
        notebook: str | None = None,
        timeout: int = 60,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        # The per-cell timeout bounds each cell; the transport has to outlast the
        # whole span, so give it room for every cell in the range to use its budget.
        span = max(1, (end - start + 1) if end >= 0 else 64)
        return self._call_with_session(
            "MCPEvaluateRange",
            notebook_id,
            int(start),
            int(end),
            int(timeout),
            bool(stop_on_error),
            timeout=timeout * span + 30,
        )

    def write_cell(
        self,
        content: str,
        style: str = "Input",
        notebook: str | None = None,
        position: str = "End",
        anchor: int = 0,
    ) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
        return self._call_with_session("MCPWriteCell", notebook_id, content, style, position, int(anchor))

    def delete_cell(self, index: int, notebook: str | None = None) -> dict[str, Any]:
        notebook_id = self._resolve(notebook)
        if notebook_id is None:
            return self._no_session(notebook)
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
