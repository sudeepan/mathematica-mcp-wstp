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

import contextlib
import os
import shutil
import signal
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


def test_a_socket_file_is_not_evidence_of_a_supervisor():
    """A supervisor killed outright leaves its socket behind.

    The distinction decides whether starting a new one is correct or would
    steal a live supervisor's clients, so it is answered by connecting and
    asking rather than by looking at the filesystem.
    """
    from mathematica_wstp.supervisor import lifecycle

    absent = lifecycle.probe("/tmp/there-is-nothing-here.sock")
    assert absent.running is False and absent.stale_socket is False, absent

    leftover = os.path.join(tempfile.mkdtemp(), "left.sock")
    open(leftover, "w").close()
    info = lifecycle.probe(leftover)
    assert info.running is False, info
    assert info.stale_socket is True, "a dead socket was read as a live supervisor"


def test_a_started_supervisor_outlives_the_process_that_started_it():
    """The property the whole milestone exists for.

    A starter is spawned, told to start a supervisor, and then killed. If the
    supervisor were an ordinary child it would be reparented but its kernel
    would still be reachable; what is actually being checked is that neither
    the supervisor nor its kernel is in the starter's process group, so the
    signal that stops a server does not reach the laboratory.
    """
    from mathematica_wstp.supervisor import lifecycle

    workdir = tempfile.mkdtemp(prefix="sup-detach-")
    sock = os.path.join(workdir, "s.sock")
    starter_src = os.path.join(workdir, "starter.py")
    with open(starter_src, "w") as fh:
        fh.write(
            "import sys, time\n"
            f"sys.path.insert(0, {os.path.join(ROOT, 'src')!r})\n"
            "from mathematica_wstp.supervisor import SupervisorConfig, start\n"
            f"info = start(SupervisorConfig(sock={sock!r}, "
            f"audit={os.path.join(workdir, 'a.log')!r}, "
            f"spool={os.path.join(workdir, 'art')!r}))\n"
            "print(info.running, flush=True)\n"
            "time.sleep(600)\n")

    starter = subprocess.Popen([PYTHON, "-u", starter_src], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        assert starter.stdout.readline().strip() == "True", "supervisor never came up"
        before = lifecycle.probe(sock)
        assert before.running, before
        assert before.pid and before.pid != starter.pid, (before.pid, starter.pid)
        # Not merely a different pid: a different process group, which is what
        # makes a group signal miss it.
        assert os.getpgid(before.pid) != os.getpgid(starter.pid), "same process group"

        starter.kill()
        starter.wait(timeout=20)
        time.sleep(2)

        after = lifecycle.probe(sock)
        assert after.running, "the supervisor died with the process that started it"
        assert after.session == before.session, (before.session, after.session)
    finally:
        with contextlib.suppress(Exception):
            starter.kill()
        info = lifecycle.probe(sock)
        if info.running and info.pid:
            with contextlib.suppress(Exception):
                os.kill(info.pid, signal.SIGKILL)
        shutil.rmtree(workdir, ignore_errors=True)


def test_starting_twice_returns_the_one_that_is_running():
    """Starting is idempotent, or two laboratories fight over one socket."""
    from mathematica_wstp.supervisor import SupervisorConfig, lifecycle

    workdir = tempfile.mkdtemp(prefix="sup-twice-")
    config = SupervisorConfig(sock=os.path.join(workdir, "s.sock"),
                              audit=os.path.join(workdir, "a.log"),
                              spool=os.path.join(workdir, "art"))
    try:
        first = lifecycle.start(config)
        assert first.running, first
        second = lifecycle.start(config)
        assert second.running, second
        assert second.session == first.session, "a second supervisor was started"
        assert second.pid == first.pid, (first.pid, second.pid)
    finally:
        info = lifecycle.probe(config.sock)
        if info.running and info.pid:
            with contextlib.suppress(Exception):
                os.kill(info.pid, signal.SIGKILL)
        shutil.rmtree(workdir, ignore_errors=True)


def test_an_idle_laboratory_is_released_and_a_busy_one_is_not():
    """When a kernel nobody is using may be let go.

    Two absolutes first: work in flight and a result nobody collected are never
    destroyed, however long the wait. Only once neither holds does elapsed time
    get a say. The window is a day by default; it is seconds here so the rule
    can be observed rather than argued about.
    """
    lab = Lab().start(SUP_RECLAIM_AFTER="3", SUP_RECLAIM_CHECK_EVERY="1")
    kernel_pid = lab.kernel_pid
    try:
        # Busy: a long evaluation must hold the laboratory open well past the
        # window it would otherwise be reclaimed in.
        rid = lab.talk("SUBMIT slow t=60 Pause[8]; 1")
        assert rid.startswith("E"), rid
        time.sleep(5)                       # longer than the reclaim window
        kept = lab.talk("RECLAIM")
        assert kept.startswith("KEPT BUSY"), kept
        assert os.path.exists(f"/proc/{kernel_pid}"), "a busy kernel was reclaimed"

        reply = wait_terminal(lab, rid)
        assert "COMPLETED" in reply, reply

        # Collected, no clients, and past the window: now it may go.
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if not os.path.exists(f"/proc/{kernel_pid}"):
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"idle kernel was never released: {lab.talk('RECLAIM')}")

        assert lab.proc.wait(timeout=20) == 0, "the supervisor did not exit cleanly"
        lines = lab.audit_lines()
        assert any("RECLAIMING" in ln for ln in lines), lines[-3:]
        assert any("SUPERVISOR_DOWN" in ln and "idle" in ln for ln in lines), lines[-3:]
    finally:
        lab.stop()


def test_an_unclaimed_result_is_not_thrown_away_by_the_clock():
    """An answer nobody has collected outranks any idle window.

    This is the case that makes the policy worth stating separately: the kernel
    is not busy, no client is connected, and the elapsed time says release --
    but a result computed for someone who has not come back for it is exactly
    what the supervisor exists to hold.
    """
    lab = Lab().start(SUP_RECLAIM_AFTER="3", SUP_RECLAIM_CHECK_EVERY="1")
    kernel_pid = lab.kernel_pid
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(lab.sock)
        f = sock.makefile("rw")
        f.write("SUBMIT orphan 6*7\n")
        f.flush()
        rid = f.readline().strip()
        assert rid.startswith("E"), rid
        time.sleep(1)
        # Both, and in this order: makefile() duplicates the descriptor, so
        # closing the socket alone leaves the connection open from the
        # supervisor's side and the client looks like it never left.
        f.close()
        sock.close()                        # never collects the answer

        time.sleep(6)                       # twice the window
        kept = lab.talk("RECLAIM")
        assert kept.startswith("KEPT COMPLETED_UNCLAIMED"), kept
        assert os.path.exists(f"/proc/{kernel_pid}"), "an uncollected result was destroyed"
        assert lab.talk("LOOKUP orphan").startswith("orphan ->"), lab.talk("LOOKUP orphan")
    finally:
        lab.stop()


def test_stopping_refuses_while_the_kernel_is_busy():
    """Deliberate release is still not permission to destroy running work."""
    from mathematica_wstp.supervisor import lifecycle

    lab = Lab().start()
    try:
        rid = lab.talk("SUBMIT slow2 t=60 Pause[6]; 1")
        assert rid.startswith("E"), rid
        time.sleep(1.5)
        refused = lifecycle.stop(lab.sock)
        assert refused.running, refused
        assert "refused" in refused.detail, refused.detail
        assert lab.proc.poll() is None, "it stopped anyway"

        wait_terminal(lab, rid)
        lab.talk(f"RESULT {rid}")           # collect it
        stopped = lifecycle.stop(lab.sock)
        assert stopped.running is False, stopped
        assert lab.proc.wait(timeout=20) == 0, "did not exit cleanly on request"
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
