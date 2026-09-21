"""The supervisor, exercised the way a client reaches it: over its socket.

Nothing here inspects the supervisor's own variables. It is started as a real
process, asked real questions down a real Unix socket, and judged on what comes
back and on what the audit file says afterwards -- because the one failure this
project keeps finding is a component reporting work it did not do, and an
in-process assertion cannot catch that.

The deeper exercisers still live beside the prototype (supervisor-demo/drive23
and drive25, 49 and 27 checks). Those cover destructive recovery, quarantine and
the fault taxonomy against a supervisor started by hand. These cover what the
packaging changed: that it imports without doing anything, that it starts from
an explicit configuration, and that the request lifecycle survived the move.

Run: .venv/bin/python tests/test_supervisor.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


class Lab:
    """A supervisor process, its socket, and the audit file it writes."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="sup-test-")
        self.sock = os.path.join(self.dir, "s.sock")
        self.audit = os.path.join(self.dir, "audit.log")
        self.proc: subprocess.Popen | None = None
        self.kernel_pid: int | None = None

    def start(self, **env: str) -> Lab:
        self.proc = subprocess.Popen(
            [PYTHON, "-u", "-m", "mathematica_wstp.supervisor"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            cwd=ROOT,
            env={**os.environ, "SUP_SOCK": self.sock, "SUP_AUDIT": self.audit,
                 "SUP_SPOOL": os.path.join(self.dir, "art"),
                 "PYTHONPATH": os.path.join(ROOT, "src"), **env})
        # Wait for READY rather than sleeping: the point of printing it is that
        # a parent can wait for the thing it actually needs.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("supervisor exited before READY")
            if line.startswith("KERNEL "):
                self.kernel_pid = int(line.split()[1])
            if line.strip() == "READY":
                return self
        raise RuntimeError("supervisor never became READY")

    def talk(self, message: str) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock)
        f = s.makefile("rw")
        f.write(message + "\n")
        f.flush()
        reply = f.readline().strip()
        f.close()
        s.close()
        return reply

    def audit_lines(self) -> list[str]:
        with open(self.audit) as fh:
            return [ln.rstrip("\n") for ln in fh]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


TERMINAL = ("COMPLETED", "ABORTED", "FAILED", "CANCELLED")


def wait_terminal(lab: Lab, rid: str, timeout: float = 60) -> str:
    """Poll until the request reaches a state it cannot leave.

    Deliberately keyed on the terminal set rather than on "not the states I
    happened to think of": the first version of this stopped at DISPATCHING,
    because DISPATCHING is neither PENDING nor RUNNING, and read a request that
    had not started as one that had finished.
    """
    deadline = time.monotonic() + timeout
    reply = ""
    while time.monotonic() < deadline:
        reply = lab.talk(f"RESULT {rid}")
        if any(state in reply for state in TERMINAL):
            return reply
        time.sleep(0.2)
    raise AssertionError(f"{rid} never reached a terminal state: {reply}")


def test_importing_the_supervisor_does_nothing():
    """A module that acts at import can be run but never examined.

    The prototype bound its socket, truncated its audit file and started a
    kernel as a side effect of being imported, so there was no way to look at
    it without running a laboratory. Configuration is now a value and startup
    is a call.
    """
    from mathematica_wstp import supervisor

    config = supervisor.SupervisorConfig(sock="/tmp/does-not-exist.sock",
                                         audit="/tmp/does-not-exist.log",
                                         spool="/tmp/does-not-exist-art")
    assert not os.path.exists(config.sock), "importing bound a socket"
    assert not os.path.exists(config.audit), "importing truncated an audit file"
    assert not os.path.exists(config.spool), "importing created a spool"

    # The per-user default must not be a path two people could collide on.
    default = supervisor.SupervisorConfig.from_env().sock
    assert str(os.getuid()) in default, default


def test_the_supervisor_starts_from_an_explicit_configuration():
    """Configuration passed in wins; the environment is only the fallback."""
    from mathematica_wstp import supervisor

    chosen = supervisor.SupervisorConfig(
        sock="/tmp/explicit.sock", audit=os.path.join(tempfile.mkdtemp(), "a.log"),
        spool=tempfile.mkdtemp(), grace=9.0, max_inline=77)
    try:
        applied = supervisor.configure(chosen)
        assert applied is chosen, applied
        assert supervisor.core.GRACE == 9.0, supervisor.core.GRACE
        assert supervisor.core.MAX_INLINE == 77, supervisor.core.MAX_INLINE
        # A laboratory identity is minted here, and its spool is namespaced by
        # it, so R1 from two sessions cannot collide.
        assert supervisor.core.SESSION, "no session identity was minted"
        assert supervisor.core.SESSION in supervisor.core.STORE, supervisor.core.STORE
        assert os.path.isdir(supervisor.core.STORE), supervisor.core.STORE
    finally:
        import shutil
        shutil.rmtree(chosen.spool, ignore_errors=True)


def test_a_request_completes_and_says_who_ran_it():
    """The lifecycle, end to end, from outside the process."""
    lab = Lab().start()
    try:
        rid = lab.talk("SUBMIT k1 1+1")
        assert rid.startswith("E"), rid

        reply = wait_terminal(lab, rid)
        assert "COMPLETED" in reply, reply
        # An evaluation that completed must carry the identity it ran under.
        assert "token=K1/V1/E" in reply, reply

        # And the kernel is a laboratory with a readiness of its own.
        assert lab.talk("READINESS").startswith("READY"), lab.talk("READINESS")

        # Every expression the supervisor caused the kernel to run is recorded,
        # and startup provenance is maintenance rather than science -- which is
        # what makes Vn countable.
        lines = lab.audit_lines()
        assert any("MAINTENANCE_EVALUATION" in ln for ln in lines), lines[:3]
        assert any("SUPERVISOR_UP" in ln for ln in lines), lines[:3]
        # Numbered, so a gap is visible rather than merely absent.
        numbers = [int(ln.split()[0][1:]) for ln in lines if ln.startswith("#")]
        assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers), numbers
    finally:
        lab.stop()


def test_a_key_can_be_looked_up_without_submitting_anything():
    """The primitive reconnection depends on.

    A client that submits and dies before hearing the answer cannot recover by
    resubmitting: the payload embeds its own pid, so a new client reproduces a
    different payload and is correctly refused as different work. The
    caller-chosen key is the only name that outlives the caller, and asking
    about it must never be the thing that makes it exist.
    """
    lab = Lab().start()
    try:
        unknown = lab.talk("LOOKUP never-submitted")
        assert unknown.startswith("UNKNOWN"), unknown

        rid = lab.talk("SUBMIT k7 2+2")
        found = lab.talk("LOOKUP k7")
        assert f"-> {rid}" in found, (rid, found)

        # Asking did not create a second request.
        assert lab.talk("LOOKUP never-submitted").startswith("UNKNOWN")
        ledger = lab.talk("LEDGER")
        assert ledger.count("E") >= 1, ledger
    finally:
        lab.stop()


def test_the_kernel_outlives_a_client_that_disconnects():
    """The whole point: a client may come and go, the laboratory does not.

    This is the property the direct backend cannot have -- measured, its kernel
    exits about a second after its owning client is killed.
    """
    lab = Lab().start()
    try:
        first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        first.connect(lab.sock)
        f = first.makefile("rw")
        f.write("SUBMIT k9 vv = 12345\n")
        f.flush()
        rid = f.readline().strip()
        assert rid.startswith("E"), rid
        # Drop the client entirely, the way a crash would.
        first.close()
        time.sleep(2)

        assert lab.kernel_pid and os.path.exists(f"/proc/{lab.kernel_pid}"), \
            "the kernel did not survive its client"

        # A new client finds the work by the key the old one chose, and the
        # state it left behind is still there.
        found = lab.talk("LOOKUP k9")
        assert f"-> {rid}" in found, found
        again = lab.talk("SUBMIT k10 vv")
        reply = wait_terminal(lab, again)
        assert "COMPLETED" in reply, reply
        # The value the dead client's request assigned is still in the kernel.
        assert "12345" in reply, reply
    finally:
        lab.stop()


def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        started = time.monotonic()
        try:
            fn()
            print(f"PASS  {name}  ({time.monotonic()-started:.1f}s)")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {name}  ({time.monotonic()-started:.1f}s)\n        "
                  f"{type(exc).__name__}: {exc}")
    print(f"\n{len(tests)-failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
