"""The process-wide kernel, and the ``evaluate_wl`` seam above it.

This is the only module that owns a :class:`~mathematica_wstp.kernel.Kernel`.
Everything else -- the notebook layer, the tool surface -- goes through
``evaluate_wl``/``evaluate_wl_bytes`` and never touches the link directly, so
there is exactly one place that knows about kernel lifecycle, restarts and
aborts.

Two behaviours here differ from the wolframclient-based server this replaces,
and both follow from the transport actually supporting them:

* **A timeout does not discard the session.** The old server had no choice: with
  no way to interrupt a running evaluation, a kernel that blew its deadline was
  unreachable and had to be thrown away, taking every definition with it. Here a
  timeout aborts, the kernel returns ``$Aborted``, and the state survives.
* **A dead kernel is an error, not a hang.** No cell-count heuristics, no
  Python-side deadline standing in for liveness the transport could not report.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
from dataclasses import dataclass, field
from typing import Any

from . import registry
from .kernel import EvaluationAborted, EvaluationTimeout, Kernel, KernelError
from .link import LinkDead, WSTPError

logger = logging.getLogger("mathematica_wstp.session")

DEFAULT_TIMEOUT = 60.0

_kernel: Kernel | None = None
_kernel_lock = threading.RLock()
_generation = 0


@dataclass
class WLResult:
    """One evaluation's outcome.

    ``text`` is InputForm source for expression results; ``data`` carries raw
    bytes when the caller asked for them. ``execution_method`` exists so callers
    copied from the previous server keep working unchanged.
    """

    success: bool
    text: str = ""
    data: bytes = b""
    messages: list[dict] = field(default_factory=list)
    prints: list[str] = field(default_factory=list)
    error: str = ""
    timed_out: bool = False
    aborted: bool = False
    execution_method: str = "wstp"
    extra: dict[str, Any] = field(default_factory=dict)


def get_kernel(start: bool = True) -> Kernel:
    """The shared kernel, started on first use."""
    global _kernel, _generation
    with _kernel_lock:
        if _kernel is not None and not _kernel.is_alive():
            logger.warning("kernel %s is gone; discarding it", _kernel.pid)
            try:
                _kernel.close()
            except Exception:
                pass
            _kernel = None
        if _kernel is None and start:
            _kernel = Kernel().start()
            _generation += 1
            logger.info("kernel generation %d up (pid %s)", _generation, _kernel.pid)
        if _kernel is None:
            raise KernelError("no kernel and start was not requested")
        return _kernel


def generation() -> int:
    """Increments on every kernel (re)start. Callers use it to spot a swap."""
    return _generation


def has_kernel() -> bool:
    with _kernel_lock:
        return _kernel is not None and _kernel.is_alive()


def restart_kernel() -> dict[str, Any]:
    """Shut the kernel down properly and start a fresh one.

    "Properly" is the whole point: ``CloseKernels[]`` first over a link that is
    still answerable, then a signal to the whole process group. The previous
    server terminated only the master, which is how a single restart stranded a
    20-way ``LaunchKernels[]`` fan-out.
    """
    global _kernel
    with _kernel_lock:
        old_pid = _kernel.pid if _kernel else None
        # Count before closing, or "nothing leaked" only means "nothing counted".
        subkernels = _kernel.observe_subkernels() if _kernel else []
        if _kernel is not None:
            _kernel.close()
            _kernel = None
        fresh = get_kernel()
        leaked = [p for p in subkernels if registry.pid_alive(p)]
        return {
            "success": True,
            "previous_pid": old_pid,
            "pid": fresh.pid,
            "generation": _generation,
            "subkernels_closed": len(subkernels) - len(leaked),
            "subkernels_leaked": leaked,
        }


def close_kernel() -> None:
    global _kernel
    with _kernel_lock:
        if _kernel is not None:
            _kernel.close()
            _kernel = None


def abort_current(wait: float = 5.0) -> dict[str, Any]:
    """Interrupt whatever the kernel is doing. Deliberately does not take the lock.

    Taking ``_kernel_lock`` here would deadlock against the evaluation we are
    trying to stop: the caller holding it is blocked on a read that only this
    abort can end.
    """
    kernel = _kernel
    if kernel is None or not kernel.is_alive():
        return {"success": False, "error": "no live kernel to abort"}
    confirmed = kernel.abort(wait=wait)
    return {
        "success": True,
        "confirmed": confirmed,
        "pid": kernel.pid,
        "note": (
            "Evaluation interrupted; kernel state is intact."
            if confirmed
            else "Abort sent; the kernel did not confirm within the wait window. "
                 "It may be inside a call that does not check for aborts (an external "
                 "process, or a long library call)."
        ),
    }


def _run(fn, *, timeout: float) -> WLResult:
    """Shared error handling for both evaluate entry points."""
    try:
        return fn()
    except EvaluationTimeout as exc:
        return WLResult(
            success=False,
            error=str(exc),
            timed_out=True,
            extra={"aborted_cleanly": exc.aborted_cleanly, "elapsed": round(exc.elapsed, 2)},
        )
    except EvaluationAborted as exc:
        return WLResult(success=False, error=str(exc), aborted=True)
    except LinkDead as exc:
        # The kernel is gone. Drop it so the next call builds a fresh one rather
        # than retrying a link that can never recover.
        close_kernel()
        return WLResult(success=False, error=f"kernel died: {exc}")
    except (KernelError, WSTPError) as exc:
        return WLResult(success=False, error=str(exc))


def evaluate_wl(code: str, timeout: float = DEFAULT_TIMEOUT) -> WLResult:
    """Evaluate Wolfram source, returning InputForm text."""
    def go() -> WLResult:
        kernel = get_kernel()
        reply = kernel.evaluate_detailed(code, timeout=timeout)
        return WLResult(success=True, text=reply.value,
                        messages=reply.messages, prints=reply.prints)
    return _run(go, timeout=timeout)


def evaluate_wl_bytes(code: str, timeout: float = DEFAULT_TIMEOUT) -> WLResult:
    """Evaluate an expression yielding a ByteArray, returning the raw bytes.

    Used wherever text has to survive intact -- JSON payloads, notebook content.
    ``ExportString`` would render the encoded bytes *as characters* and turn a
    gamma into two Latin-1 characters; bytes are unambiguous.
    """
    def go() -> WLResult:
        kernel = get_kernel()
        raw = kernel.evaluate_bytes(code, timeout)
        return WLResult(success=True, data=raw, text=raw.decode("utf-8", errors="replace"))
    return _run(go, timeout=timeout)


def evaluate_wl_json(code: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Evaluate an expression yielding an Association, decoded as Python data.

    Goes through the kernel's own JSON encoder and the byte path, so text with
    non-ASCII survives -- see evaluate_wl_bytes on why bytes rather than a
    string.
    """
    def go() -> WLResult:
        kernel = get_kernel()
        return WLResult(success=True, extra={"data": kernel.evaluate_json(code, timeout=timeout)})
    out = _run(go, timeout=timeout)
    if not out.success:
        return {"success": False, "error": out.error, "timed_out": out.timed_out}
    payload = out.extra.get("data")
    if isinstance(payload, dict):
        return {"success": True, **payload}
    return {"success": True, "value": payload}


def kernel_status() -> dict[str, Any]:
    with _kernel_lock:
        if _kernel is None:
            return {"running": False, "generation": _generation}
        health = _kernel.health()
        # Count from /proc every time. Reporting the cache meant status showed
        # no subkernels until someone happened to ask for them explicitly, so a
        # 20-way fan-out sat there for hours reading as an empty list.
        health.update({"running": health["alive"], "generation": _generation,
                       "subkernels": _kernel.observe_subkernels()})
        return health


def reap_on_startup() -> list[dict]:
    """Clean up kernels stranded by a previous server process."""
    try:
        return registry.reap_orphans()
    except Exception as exc:
        logger.warning("startup reap failed: %s", exc)
        return []


@atexit.register
def _shutdown() -> None:
    try:
        close_kernel()
    except Exception:
        pass


def _terminate_handler(signum: int, _frame) -> None:
    """Close the kernel on a signal, then die the way the signal intended.

    ``atexit`` does not run on SIGTERM -- the default disposition kills the
    process outright -- so without this a ``kill`` of the server leaves its
    kernel and any subkernels to be found later by the startup reaper. The
    reaper is a backstop, not a substitute: between the kill and the next
    start, that memory is simply gone.

    Worth knowing when reading this: **WolframKernel itself ignores SIGTERM.**
    Measured on three orphaned kernels -- SIGTERM left all three running, and
    only SIGKILL removed them. That is why ``Kernel.close`` asks over the link
    first and escalates to SIGKILL, and why this handler calls close() rather
    than just forwarding the signal to the process group.

    After cleaning up we restore the default disposition and re-raise, so the
    exit status still reports the signal. Swallowing it would make the server
    look like it exited normally to whatever supervises it.
    """
    try:
        close_kernel()
    except Exception:
        logger.warning("kernel close failed during signal shutdown", exc_info=True)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_signal_handlers() -> list[str]:
    """Install the shutdown handler. Returns the signals actually hooked.

    Only from the main thread -- ``signal.signal`` raises anywhere else, and
    the server is imported by tests that run off-thread.
    """
    installed: list[str] = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _terminate_handler)
            installed.append(name)
        except (ValueError, OSError, RuntimeError):
            pass  # not the main thread, or the platform disallows it
    return installed
