"""Minimal ctypes binding to WSTP, enough to prove the transport claims.

This is the seed of the link layer described in docs/architecture.md §3.1. It is
deliberately small: its job is to answer "can WSTP do the three things
wolframclient cannot", not to be the final API. Everything in
docs/measurements.md was measured with this file.

The three things, all verified on Mathematica 15.0.1 / Linux-x86-64:

  * abort      WSPutMessage(link, WSAbortMessage) interrupts a running
               evaluation. The evaluation returns $Aborted on the same link,
               the kernel keeps its pid, and kernel state survives intact.
  * liveness   A kernel killed mid-evaluation surfaces as WSError 1
               ("WSTP connection was lost") within ~0.3s, instead of hanging
               the caller forever.
  * latency    Round-trip floor is ~0.27ms, against ~30ms for the JSON-over-TCP
               addon transport in the current fork.

Two gotchas encoded here, both of which cost real time to find:

  * Every ctypes restype/argtypes must be declared. WSLINK and WSENV are
    pointers; leaving them at the default c_int truncates them on 64-bit and
    corrupts the link handle in ways that surface much later.
  * The poll interval in ev() *is* the measured latency floor. An early
    version used time.sleep(0.02) and produced a suspiciously flat "20.1ms"
    for every payload size. If you benchmark with this, shrink the sleep or
    switch to selecting on the link fd.
"""

import ctypes
import time

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
from mathematica_wstp.discovery import find_kernel, find_wstp_library
LIB = find_wstp_library()
KERNEL = find_kernel()

# From wstp.h, confirmed by compiling a printer against the shipped header
# rather than trusting documentation: these are interface-4 values.
RETURNPKT = 3
WSTerminateMessage, WSInterruptMessage, WSAbortMessage = 1, 2, 3

w = ctypes.CDLL(LIB)
w.WSInitialize.restype = ctypes.c_void_p
w.WSInitialize.argtypes = [ctypes.c_void_p]
w.WSOpenString.restype = ctypes.c_void_p
w.WSOpenString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
for _fn, _args, _res in [
    ("WSActivate", [ctypes.c_void_p], ctypes.c_int),
    ("WSPutFunction", [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
    ("WSPutString", [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_int),
    ("WSEndPacket", [ctypes.c_void_p], ctypes.c_int),
    ("WSFlush", [ctypes.c_void_p], ctypes.c_int),
    ("WSNextPacket", [ctypes.c_void_p], ctypes.c_int),
    ("WSNewPacket", [ctypes.c_void_p], ctypes.c_int),
    ("WSReady", [ctypes.c_void_p], ctypes.c_int),
    ("WSError", [ctypes.c_void_p], ctypes.c_int),
    ("WSPutMessage", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
    ("WSClose", [ctypes.c_void_p], None),
    ("WSGetString", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)], ctypes.c_int),
    ("WSReleaseString", [ctypes.c_void_p, ctypes.c_char_p], None),
]:
    _f = getattr(w, _fn)
    _f.argtypes = _args
    _f.restype = _res
w.WSErrorMessage.restype = ctypes.c_char_p
w.WSErrorMessage.argtypes = [ctypes.c_void_p]


class K:
    """One launched kernel on one WSTP link.

    Results come back as strings (ToString of the value) because that is all
    the experiments need. The real client sends and receives structured
    expressions -- see docs/architecture.md §3.1 on why that matters for the
    package-symbol-context problem.
    """

    def __init__(self, extra: str = ""):
        self.env = w.WSInitialize(None)
        err = ctypes.c_int(0)
        spec = f"-linkname '{KERNEL} -wstp{extra}' -linkmode launch".encode()
        self.l = w.WSOpenString(self.env, spec, ctypes.byref(err))
        if not self.l:
            raise RuntimeError(f"WSOpenString failed, err={err.value}")
        # NOTE: WSActivate blocks forever if the named binary does not exist --
        # measured, killed it manually after 3m37s. Production code must run
        # activation under a watchdog. See docs/measurements.md §7.
        if not w.WSActivate(self.l):
            raise RuntimeError("WSActivate failed")

    def ev(self, expr: str, timeout: float = 120, abort_after: float | None = None) -> str:
        """Evaluate expr, optionally firing WSAbortMessage after abort_after seconds."""
        w.WSPutFunction(self.l, b"EvaluatePacket", 1)
        w.WSPutFunction(self.l, b"ToString", 1)
        w.WSPutFunction(self.l, b"ToExpression", 1)
        w.WSPutString(self.l, expr.encode())
        w.WSEndPacket(self.l)
        w.WSFlush(self.l)

        t0 = time.time()
        sent_abort = False
        while time.time() - t0 < timeout:
            if abort_after is not None and not sent_abort and time.time() - t0 > abort_after:
                w.WSPutMessage(self.l, WSAbortMessage)
                w.WSFlush(self.l)
                sent_abort = True
            if w.WSReady(self.l):
                if w.WSError(self.l):
                    return f"<WSError: {w.WSErrorMessage(self.l).decode()}>"
                if w.WSNextPacket(self.l) == RETURNPKT:
                    s = ctypes.c_char_p()
                    if w.WSGetString(self.l, ctypes.byref(s)):
                        val = s.value.decode()
                        w.WSReleaseString(self.l, s)
                        w.WSNewPacket(self.l)
                        return val
                w.WSNewPacket(self.l)
            if w.WSError(self.l):
                return f"<WSError: {w.WSErrorMessage(self.l).decode()}>"
            time.sleep(0.0002)
        return "<TIMEOUT>"

    def abort(self) -> int:
        """Out-of-band abort. Safe to call while ev() is blocked in another thread."""
        rc = w.WSPutMessage(self.l, WSAbortMessage)
        w.WSFlush(self.l)
        return rc

    def close(self) -> None:
        w.WSClose(self.l)
