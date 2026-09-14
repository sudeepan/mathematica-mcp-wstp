"""Minimal ctypes WSTP client. Tests whether WSAbortMessage interrupts a
running evaluation without killing the kernel."""
import ctypes, time, os, sys

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
from mathematica_wstp.discovery import find_kernel, find_wstp_library
LIB = find_wstp_library()
KERNEL = find_kernel()

RETURNPKT, TEXTPKT, MESSAGEPKT, ILLEGALPKT = 3, 2, 5, 0
WSTerminateMessage, WSInterruptMessage, WSAbortMessage = 1, 2, 3

w = ctypes.CDLL(LIB)
w.WSInitialize.restype = ctypes.c_void_p
w.WSInitialize.argtypes = [ctypes.c_void_p]
w.WSOpenString.restype = ctypes.c_void_p
w.WSOpenString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
for fn, args, res in [
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
    f = getattr(w, fn); f.argtypes = args; f.restype = res
w.WSErrorMessage.restype = ctypes.c_char_p
w.WSErrorMessage.argtypes = [ctypes.c_void_p]

env = w.WSInitialize(None)
err = ctypes.c_int(0)
spec = f"-linkname '{KERNEL} -wstp' -linkmode launch".encode()
link = w.WSOpenString(env, spec, ctypes.byref(err))
print(f"open: link={link} err={err.value}", flush=True)
if not link:
    sys.exit("FAILED to open link")
print(f"activate: {w.WSActivate(link)}  (0 = failed)", flush=True)

def send(expr):
    w.WSPutFunction(link, b"EvaluatePacket", 1)
    w.WSPutFunction(link, b"ToString", 1)
    w.WSPutFunction(link, b"ToExpression", 1)
    w.WSPutString(link, expr.encode())
    w.WSEndPacket(link); w.WSFlush(link)

def read(timeout, abort_after=None):
    """Poll for a packet. Optionally send WSAbortMessage after N seconds."""
    t0 = time.time(); sent_abort = False
    while time.time() - t0 < timeout:
        if abort_after is not None and not sent_abort and time.time()-t0 > abort_after:
            rc = w.WSPutMessage(link, WSAbortMessage)
            w.WSFlush(link)
            print(f"  -> sent WSAbortMessage at t={time.time()-t0:.1f}s rc={rc}", flush=True)
            sent_abort = True
        if w.WSReady(link):
            pkt = w.WSNextPacket(link)
            if pkt == RETURNPKT:
                s = ctypes.c_char_p()
                if w.WSGetString(link, ctypes.byref(s)):
                    val = s.value.decode()
                    w.WSReleaseString(link, s)
                    w.WSNewPacket(link)
                    return time.time()-t0, val
            w.WSNewPacket(link)
        time.sleep(0.02)
    return time.time()-t0, "<TIMEOUT no packet>"

print("\n[1] kernel pid + set state marker", flush=True)
send('{$ProcessID, marker = 424242}')
print("   ", read(30), flush=True)

print("\n[2] long evaluation, abort after 2s", flush=True)
send('Do[qq = k, {k, 1, 10^12}]; "LOOP FINISHED NORMALLY"')
elapsed, val = read(25, abort_after=2.0)
print(f"    returned after {elapsed:.1f}s: {val!r}", flush=True)

print("\n[3] same link still usable? pid + marker still set?", flush=True)
send('{$ProcessID, marker, Head[qq]}')
print("   ", read(30), flush=True)

print(f"\nWSError={w.WSError(link)} msg={w.WSErrorMessage(link)}", flush=True)
w.WSClose(link)
