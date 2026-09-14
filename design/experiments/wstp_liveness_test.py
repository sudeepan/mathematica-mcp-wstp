"""Does WSTP report a kernel that died mid-evaluation, or hang like wolframclient?"""
import ctypes, time, os, signal, subprocess, sys
exec(open("/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad/wstp_abort_test.py").read().split('env = w.WSInitialize')[0])

env = w.WSInitialize(None)
err = ctypes.c_int(0)
link = w.WSOpenString(env, f"-linkname '{KERNEL} -wstp' -linkmode launch".encode(), ctypes.byref(err))
w.WSActivate(link)

def send(expr):
    w.WSPutFunction(link, b"EvaluatePacket", 1)
    w.WSPutFunction(link, b"ToString", 1)
    w.WSPutFunction(link, b"ToExpression", 1)
    w.WSPutString(link, expr.encode()); w.WSEndPacket(link); w.WSFlush(link)

def read(timeout, kill_pid_after=None, pid=None):
    t0=time.time(); killed=False
    while time.time()-t0 < timeout:
        if kill_pid_after is not None and not killed and time.time()-t0 > kill_pid_after:
            os.kill(pid, signal.SIGKILL)
            print(f"  -> SIGKILL kernel pid {pid} at t={time.time()-t0:.1f}s", flush=True)
            killed=True
        if w.WSReady(link):
            pkt = w.WSNextPacket(link)
            e = w.WSError(link)
            if e:
                return time.time()-t0, f"<WSError {e}: {w.WSErrorMessage(link).decode()}>"
            if pkt == RETURNPKT:
                s=ctypes.c_char_p()
                if w.WSGetString(link, ctypes.byref(s)):
                    v=s.value.decode(); w.WSReleaseString(link,s); w.WSNewPacket(link); return time.time()-t0, v
            w.WSNewPacket(link)
        e = w.WSError(link)
        if e:
            return time.time()-t0, f"<WSError {e}: {w.WSErrorMessage(link).decode()}>"
        time.sleep(0.02)
    return time.time()-t0, "<TIMEOUT: hung, no error reported>"

send('$ProcessID'); t,v = read(30); print(f"[1] kernel pid = {v} ({t:.1f}s)", flush=True)
pid = int(v)

print("\n[2] start long eval, SIGKILL the kernel after 2s", flush=True)
send('Do[k2 = k, {k, 1, 10^12}]; "FINISHED"')
t, v = read(30, kill_pid_after=2.0, pid=pid)
print(f"    read returned after {t:.1f}s: {v}", flush=True)
print(f"\n[3] WSReady={w.WSReady(link)} WSError={w.WSError(link)} -> {w.WSErrorMessage(link).decode()}", flush=True)
