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
import time
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

# An abort whose outcome we could not verify. Kept as session state rather than
# returned once and forgotten: liveness is a point-in-time measurement, so a
# caller who reads "unverified" in one reply and moves on is exactly the failure
# the probe was added to prevent. A watchdog drives this to a definite answer.
_abort_uncertain: dict[str, Any] | None = None
_abort_watchdog_active = False
_abort_journal: list[dict[str, Any]] = []
JOURNAL_LIMIT = 50
UNCERTAINTY_DEADLINE = 30.0
UNCERTAINTY_POLL = 2.0


def _record(event: str, **details: Any) -> None:
    """Append to the abort journal, newest last, bounded."""
    _abort_journal.append({"event": event, "ts": time.time(), **details})
    if len(_abort_journal) > JOURNAL_LIMIT:
        del _abort_journal[:len(_abort_journal) - JOURNAL_LIMIT]


def abort_journal() -> list[dict[str, Any]]:
    """Every abort and how it resolved. Reconstructing one incident from a chat
    transcript took reading 11 MB of JSONL; this is the same story in one call."""
    return [dict(e) for e in _abort_journal]


def _clear_uncertainty(reason: str) -> None:
    global _abort_uncertain
    if _abort_uncertain is not None:
        _abort_uncertain = None
        _record("abort-reconciled", reason=reason)


def _start_uncertainty_watchdog() -> None:
    """Drive an unverified abort to a definite answer instead of leaving it open.

    Probes until the kernel answers or the deadline passes. A correct answer
    clears the fault; persistent silence resolves it to dead. Never indefinitely
    uncertain -- an unresolved warning is one people learn to ignore.
    """
    global _abort_watchdog_active
    if _abort_watchdog_active:
        return
    _abort_watchdog_active = True

    def run() -> None:
        global _abort_watchdog_active, _abort_uncertain
        deadline = time.monotonic() + UNCERTAINTY_DEADLINE
        try:
            while time.monotonic() < deadline:
                time.sleep(UNCERTAINTY_POLL)
                if _abort_uncertain is None:
                    return
                kernel = _kernel
                if kernel is None or not kernel.is_alive():
                    _abort_uncertain = None
                    _record("abort-resolved-dead", detail="kernel process is gone")
                    return
                verdict, detail = _verify_after_abort(kernel, 1.5)
                if verdict == "alive":
                    _clear_uncertainty("the kernel answered a probe")
                    return
                if verdict == "dead":
                    _abort_uncertain = None
                    _record("abort-resolved-dead", detail=detail)
                    return
            if _abort_uncertain is not None:
                _abort_uncertain = {**_abort_uncertain, "resolved": "no answer within "
                                    f"{UNCERTAINTY_DEADLINE:.0f}s -- treat the session as lost"}
                _record("abort-unresolved", detail=f"silent for {UNCERTAINTY_DEADLINE:.0f}s")
        finally:
            _abort_watchdog_active = False

    threading.Thread(target=run, name="abort-uncertainty-watchdog", daemon=True).start()


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
    #: An abort was requested while this evaluation was running, yet it still
    #: returned a value. See ``evaluate_wl``.
    abort_requested_during: bool = False
    aborted: bool = False
    execution_method: str = "wstp"
    extra: dict[str, Any] = field(default_factory=dict)


_kernel_change_notice: str | None = None


def take_kernel_change_notice() -> str | None:
    """Return and clear a pending 'your kernel was replaced' notice.

    Replacing a dead kernel is the right recovery, but doing it silently is not:
    the call that triggers it returns success against a brand-new empty session,
    and the caller has no way to tell unless they already know what the old
    kernel held. Observed costing a full replay -- the reply said success, every
    named result was gone, and only a deliberate spot-check revealed it.
    """
    global _kernel_change_notice
    notice, _kernel_change_notice = _kernel_change_notice, None
    return notice


def get_kernel(start: bool = True) -> Kernel:
    """The shared kernel, started on first use."""
    global _kernel, _generation, _kernel_change_notice
    with _kernel_lock:
        if _kernel is not None and not _kernel.is_alive():
            logger.warning("kernel %s is gone; discarding it", _kernel.pid)
            _kernel_change_notice = (
                f"The previous kernel (pid {_kernel.pid}, generation {_generation}) was "
                "gone and has been replaced by a fresh one. EVERY definition from the "
                "old session is lost -- anything you rely on must be recomputed or "
                "reloaded from disk. This reply comes from the new kernel."
            )
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


ABORT_PROBE_TIMEOUT = 5.0


def _verify_after_abort(kernel: Kernel, timeout: float) -> tuple[str, str]:
    """Round-trip the link and report what actually came back.

    ``Kernel.abort`` returning True means one thing only: the evaluation let go
    of the eval lock inside the wait window. A reader that died on a protocol
    error releases that lock exactly as a clean abort does, so the flag cannot
    tell the two apart -- and reporting the kernel as healthy on the strength of
    it is how a caller gets told "state is intact" about a process that no
    longer exists. The only way to know is to ask the kernel something and see
    if it answers.

    Returns (verdict, detail) where verdict is alive | dead | unverified.
    A timeout is deliberately NOT "dead": another queued call may simply hold
    the lock, and declaring a busy kernel dead would be the same overclaim in
    the other direction.
    """
    try:
        out = kernel.evaluate("1+1", timeout=timeout, abort_on_timeout=False)
    except EvaluationTimeout:
        return "unverified", "no answer to a probe within the window"
    except (LinkDead, WSTPError, KernelError) as exc:
        return "dead", str(exc)
    except Exception as exc:  # noqa: BLE001 -- an unexpected type is still not proof of death
        return "unverified", f"{type(exc).__name__}: {exc}"
    if out.strip() != "2":
        return "unverified", f"probe returned {out.strip()[:80]!r}"
    return "alive", ""


def verify_current_kernel(probe: float = 3.0) -> tuple[str, str]:
    """Public probe: is the kernel that just timed out still there?

    A timeout reports `aborted_cleanly`, which is the same lock-derived flag the
    abort path used, and it means the same limited thing: the evaluation let go.
    Timeouts are far more frequent than explicit aborts, so this is the path that
    most often had a caller told "intact" on no evidence.
    """
    global _abort_uncertain
    kernel = _kernel
    if kernel is None:
        return "none", "no kernel"
    if not kernel.is_alive():
        return "dead", "the kernel process is gone"
    verdict, detail = _verify_after_abort(kernel, probe)
    if verdict == "alive":
        _clear_uncertainty("a probe after a timeout answered")
    elif verdict == "unverified":
        _abort_uncertain = {"since": time.time(), "detail": detail, "pid": kernel.pid}
        _record("timeout-uncertain", pid=kernel.pid, detail=detail)
        _start_uncertainty_watchdog()
    else:
        _abort_uncertain = None
        _record("timeout-kernel-died", pid=kernel.pid, detail=detail)
    return verdict, detail


def _rss_mb(pids: list[int]) -> int:
    """Resident memory of these processes, in MB. Best effort."""
    total = 0
    for pid in pids:
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
        except OSError:
            continue
    return total // 1024


def close_parallel_kernels(timeout: float = 120.0) -> dict[str, Any]:
    """Release the subkernel pool, leaving the master kernel and its state alone.

    A pool outlives the work that needed it: once a replay has finished
    computing, its 20 subkernels sit idle holding gigabytes until the whole
    session is closed. Measured here, one finished replay's pool was 5.2 GB --
    69% of all Wolfram memory on the machine -- an hour after it had anything
    to do. Nothing reclaims it, because the master kernel is still perfectly
    healthy and still holds every definition the caller may want.

    So this is deliberately separate from restart: definitions survive, only
    the fan-out goes. Anything that needs parallelism again can call
    LaunchKernels[] and pay the startup cost then.
    """
    kernel = _kernel
    if kernel is None:
        return {"success": True, "closed": 0, "freed_mb": 0,
                "note": "no kernel is running, so there is no pool to close"}
    before = kernel.subkernel_pids()
    if not before:
        return {"success": True, "closed": 0, "freed_mb": 0,
                "subkernels": 0, "note": "no subkernels were open"}
    freed = _rss_mb(before)
    try:
        kernel.evaluate("CloseKernels[]; Length[Kernels[]]", timeout=timeout)
    except Exception as exc:
        return {"success": False, "error": f"could not close the pool: {exc}",
                "subkernels": len(before)}
    # CloseKernels[] returns when the request is sent, not when the processes
    # are gone -- asking /proc immediately still sees all of them and reports
    # "closed 0". Wait for them to actually leave, then report what is left.
    deadline = time.monotonic() + 15
    after = kernel.subkernel_pids()
    while after and time.monotonic() < deadline:
        time.sleep(0.2)
        after = kernel.subkernel_pids()
    # Report what /proc shows, not what the kernel claims: a subkernel that
    # survives CloseKernels[] is exactly the one worth knowing about.
    return {
        "success": True,
        "closed": len(before) - len(after),
        "still_open": len(after),
        "freed_mb": max(0, freed - _rss_mb(after)),
        "kernel": "alive",
        "note": ("The master kernel and every definition in it are untouched; "
                 "only the parallel pool was released. Call LaunchKernels[] "
                 "again if later work needs it."
                 + ("" if not after else
                    f" {len(after)} subkernel(s) did not exit and are still "
                    "holding memory; kernel(action='reap') can clear strays.")),
    }


def rebuild_parallel(kernel: Kernel, count: int, timeout: float = 240.0) -> dict[str, Any]:
    """Close the subkernels and start the same number again.

    Offered, never automatic. Nothing here can tell whether an interrupted
    parallel evaluation actually left the subkernels inconsistent -- and closing
    twenty of them unasked, when most aborts involve no parallel work at all,
    would be its own unannounced side effect.
    """
    try:
        out = kernel.evaluate(f"CloseKernels[]; Length[LaunchKernels[{int(count)}]]",
                              timeout=timeout)
        return {"rebuilt": True, "closed": count, "relaunched": out.strip()}
    except Exception as exc:  # noqa: BLE001
        return {"rebuilt": False, "error": f"{type(exc).__name__}: {exc}"}


# A cell-range evaluation registers a path here for the duration of the call.
# abort_current touches it, which is the only way the kernel can tell a user's
# abort() from a cell calling Abort[] itself: both arrive as user-initiated
# aborts and CheckAbort absorbs them identically, so without this marker a
# contained abort either always stops the range (breaking self-aborting cells)
# or never does (making abort() useless on a long replay).
_abort_sentinel: str | None = None
_sentinel_lock = threading.Lock()


def set_abort_sentinel(path: str | None) -> None:
    """Register (or clear) the file abort_current should touch."""
    global _abort_sentinel
    with _sentinel_lock:
        _abort_sentinel = path


def _mark_user_abort() -> None:
    with _sentinel_lock:
        path = _abort_sentinel
    if not path:
        return
    try:
        with open(path, "w") as fh:
            fh.write("1")
    except OSError:
        # Best effort: failing to mark means the range behaves as it did before
        # (contained abort, replay continues), which is a degradation, not a break.
        logger.warning("could not write the abort sentinel %s", path)


# Counts abort requests, so an evaluation can tell whether one landed while it
# was running. Never reset: callers compare a before/after reading, not a total.
_abort_requests = 0


def abort_request_count() -> int:
    return _abort_requests


def abort_current(wait: float = 5.0, probe: float = ABORT_PROBE_TIMEOUT,
                  rebuild_parallel_kernels: bool = False) -> dict[str, Any]:
    """Interrupt whatever the kernel is doing. Deliberately does not take the lock.

    Taking ``_kernel_lock`` here would deadlock against the evaluation we are
    trying to stop: the caller holding it is blocked on a read that only this
    abort can end.

    Whatever the abort achieved, this verifies it before saying anything about
    the kernel. Claiming an unchecked "state is intact" once cost a caller an
    hour of accumulated results: they were told nothing was lost, relayed that,
    and found out 23 seconds later that the kernel had died with everything in
    it.
    """
    kernel = _kernel
    if kernel is None:
        return {"success": False, "error": "no kernel is running", "kernel": "none",
                "generation": _generation}
    # Before the signal, not after: the kernel may act on the abort immediately.
    global _abort_requests
    _abort_requests += 1
    _mark_user_abort()
    if not kernel.is_alive():
        # Same verdict vocabulary as the post-probe answer below. Two different
        # shapes for "your kernel is gone" is how a caller checking one of them
        # misses the other.
        _record("abort-on-dead-kernel", pid=kernel.pid)
        return {"success": False, "error": "the kernel is already gone", "kernel": "dead",
                "state": "lost", "generation": _generation,
                "note": "Nothing to abort -- the kernel had already died and every "
                        "definition with it. The next call will start a fresh one."}
    confirmed = kernel.abort(wait=wait)
    pid = kernel.pid
    result: dict[str, Any] = {
        "success": True,
        "confirmed": confirmed,
        "pid": pid,
        "generation": _generation,
    }

    # Probe whether or not the abort was confirmed. `confirmed` answers "did the
    # evaluation stop?", which is a different question from "is the kernel still
    # there?", and an unconfirmed abort is the case where you most want the
    # second answer. On an idle kernel there is nothing to confirm and the flag
    # is False, so gating the probe on it would report UNVERIFIED for the
    # cheapest, safest call there is.
    verdict, detail = _verify_after_abort(kernel, probe)
    global _abort_uncertain
    if verdict == "dead" or not kernel.is_alive():
        _abort_uncertain = None
        _record("abort-kernel-died", pid=pid, detail=detail, confirmed=confirmed)
        result["success"] = False
        result["kernel"] = "dead"
        result["state"] = "lost"
        result["error"] = f"the kernel did not survive the abort: {detail or 'link is gone'}"
        result["note"] = (
            "The evaluation stopped, but the kernel is GONE and every definition "
            "with it. Nothing has been restarted -- the next call will start a "
            "fresh kernel with an empty session. Anything not written to disk is "
            "lost. This is a Wolfram-side fault, not something the abort could "
            "have avoided; checkpoint long runs to disk before interrupting them."
        )
        return result

    if verdict == "unverified":
        _abort_uncertain = {"since": time.time(), "detail": detail, "pid": pid}
        _record("abort-uncertain", pid=pid, detail=detail, confirmed=confirmed)
        _start_uncertainty_watchdog()
        result["kernel"] = "unverified"
        result["note"] = (
            f"The kernel could not be verified ({detail}). It is most likely busy "
            "with another queued call rather than gone. Do not assume the session "
            "survived until something round-trips."
        )
        return result

    _clear_uncertainty("a later abort verified the kernel")
    _record("abort-verified", pid=pid, confirmed=confirmed)
    result["kernel"] = "alive"
    result["state"] = "intact"
    result["note"] = (
        "The kernel answered a probe, so the session and its definitions are intact. "
        + ("The evaluation was interrupted."
           if confirmed
           else "Nothing stopped within the wait window, though -- either the kernel "
                "was already idle, or it is inside a call that does not check for "
                "aborts (an external process, or a long library call).")
    )

    # The probe proves the kernel answers. It proves nothing about whether an
    # interrupted DistributeDefinitions or a cut-short gather left the
    # subkernels holding a consistent set of definitions, and no check here can:
    # a probe verifies the transport, never the algebra. So say that the risk
    # exists and leave the decision with the caller.
    subs = kernel.observe_subkernels()
    if subs:
        result["parallel_subkernels"] = len(subs)
        result["parallel_state"] = "unverified"
        result["parallel_note"] = (
            f"{len(subs)} subkernels are live. If the interrupted evaluation was "
            "parallel, they may hold a partial set of definitions -- the probe "
            "above cannot tell, and neither can anything else in this server. "
            "Rebuild with CloseKernels[]; LaunchKernels[] before computing "
            "anything you intend to keep, or pass rebuild_parallel_kernels=True "
            "to have this call do it. Note this risk is reasoned, not measured: "
            "no corruption of that kind has actually been observed here."
        )
        if rebuild_parallel_kernels:
            result["parallel_rebuild"] = rebuild_parallel(kernel, len(subs))
            result["parallel_state"] = (
                "rebuilt" if result["parallel_rebuild"].get("rebuilt") else "rebuild failed")
    return result


def _run(fn, *, timeout: float) -> WLResult:
    """Shared error handling for both evaluate entry points."""
    try:
        result = fn()
        # A call that came back is better evidence than any probe we could run.
        if _abort_uncertain is not None:
            _clear_uncertainty("an evaluation completed normally")
        return result
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
    """Evaluate Wolfram source, returning InputForm text.

    An evaluation that returns a value after an abort was asked for is marked.
    Whether an out-of-band abort unwinds the whole expression or only the
    innermost one is version-dependent: on 15.0.1 ``Do[...]; "NEVER"`` aborts
    entirely, while on 14.0.0 the Do is interrupted and the CompoundExpression
    carries on and returns "NEVER" -- a partial execution reported as a clean
    result. Rather than detect the version, notice the situation: an abort was
    requested, and a value came back anyway.
    """
    before = abort_request_count()

    def go() -> WLResult:
        kernel = get_kernel()
        reply = kernel.evaluate_detailed(code, timeout=timeout)
        return WLResult(success=True, text=reply.value,
                        messages=reply.messages, prints=reply.prints)

    result = _run(go, timeout=timeout)
    if result.success and abort_request_count() != before:
        result.abort_requested_during = True
    return result


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
        # A fault that only appeared in one abort reply is a fault nobody sees.
        # And link_health has to consult the LINK: reporting "connected" off the
        # abort flag alone said connected while link_error was 3, which is the
        # same unchecked assertion this module exists to stop making.
        if health.get("link_error"):
            health["link_health"] = "error"
            health["lifecycle"] = "faulted"
            health["note"] = (
                f"WSTP link error {health['link_error']} is set. Reads on this link "
                "will fail and the next call will discard the kernel and start a "
                "fresh one -- every definition in the current session is already "
                "unreachable."
            )
        elif _abort_uncertain is not None:
            health["link_health"] = "uncertain"
            health["lifecycle"] = "faulted"
            health["abort_uncertain"] = dict(_abort_uncertain)
        else:
            health["link_health"] = "connected"
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
