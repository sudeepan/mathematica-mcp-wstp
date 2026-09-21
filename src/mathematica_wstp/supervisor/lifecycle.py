"""Finding, starting and stopping a supervisor, from the outside.

A supervisor is only useful if it outlives the thing that started it, which
makes three questions unavoidable: is one already running, how do I start one
that will survive me, and how do I stop it on purpose.

The first is the one worth being careful about. A socket file on disk is not
evidence that anything is listening -- a supervisor killed with SIGKILL leaves
its socket behind, and a stale file looks exactly like a live one. So "is one
running" is answered by connecting to it and asking, never by ``os.path.exists``.
That is the same rule the rest of this project keeps arriving at: a component's
trace is not proof of the component.

Nothing here starts a supervisor on its own. Starting a process that outlives
the caller is not something to do as a side effect of an ordinary evaluation,
so it happens only when someone asks for it.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

from .core import SupervisorConfig, default_socket_path

__all__ = ["SupervisorInfo", "probe", "start", "stop", "talk"]


@dataclass
class SupervisorInfo:
    """What could be established about a supervisor at a socket path."""

    socket_path: str
    running: bool
    session: str | None = None
    pid: int | None = None
    kernel_state: str | None = None
    stale_socket: bool = False
    detail: str = ""


def talk(socket_path: str, message: str, timeout: float = 10.0) -> str:
    """One request, one reply. Raises OSError if nothing is listening."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(socket_path)
        f = s.makefile("rw")
        f.write(message + "\n")
        f.flush()
        return f.readline().strip()
    finally:
        with contextlib.suppress(OSError):
            s.close()


def probe(socket_path: str | None = None) -> SupervisorInfo:
    """Ask whether a supervisor is there, by talking to it.

    A socket file with nothing behind it is reported as stale rather than as
    running, because the difference decides whether starting a new one is
    correct or would steal a live supervisor's clients.
    """
    path = socket_path or default_socket_path()
    if not os.path.exists(path):
        return SupervisorInfo(path, running=False, detail="no socket")
    try:
        session = talk(path, "SESSION")
        state = talk(path, "STATUS")
    except OSError as exc:
        return SupervisorInfo(path, running=False, stale_socket=True,
                              detail=f"socket present but not answering: {exc}")
    pid = None
    for field in session.split():
        if field.startswith("pid="):
            with contextlib.suppress(ValueError):
                pid = int(field[4:])
    return SupervisorInfo(path, running=True, session=session, pid=pid,
                          kernel_state=state)


def start(config: SupervisorConfig | None = None, wait: float = 180.0,
          log: str | None = None) -> SupervisorInfo:
    """Start a supervisor that will outlive this process.

    ``start_new_session`` puts it in its own session and process group, so a
    signal sent to this process's group -- the ordinary way a server is stopped
    -- does not reach it. That is the entire point: the kernel must not die
    because the thing that asked for the work did.

    Output goes to a file rather than a pipe. A pipe nobody drains fills and
    blocks the writer, which here would mean a supervisor wedged behind its own
    startup messages.
    """
    config = config or SupervisorConfig.from_env()
    existing = probe(config.sock)
    if existing.running:
        return existing
    if existing.stale_socket:
        # Nothing answered, so this file is a leftover. Removing it is safe for
        # exactly that reason, and the supervisor itself refuses to bind over a
        # socket it has not established is dead.
        with contextlib.suppress(OSError):
            os.unlink(config.sock)

    log_path = log or (config.audit + ".startup")
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    handle = open(log_path, "w")
    env = {
        **os.environ,
        "SUP_SOCK": config.sock,
        "SUP_AUDIT": config.audit,
        "SUP_SPOOL": config.spool,
        "SUP_RECLAIM_AFTER": str(config.reclaim_after),
        "SUP_RECLAIM_CHECK_EVERY": str(config.reclaim_check_every),
    }
    subprocess.Popen(
        [sys.executable, "-u", "-m", "mathematica_wstp.supervisor"],
        stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env)
    handle.close()

    # Wait for the socket to answer, not for the process to exist. A process
    # that has started is not a laboratory that will take work.
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        info = probe(config.sock)
        if info.running:
            return info
        time.sleep(0.2)
    detail = ""
    with contextlib.suppress(OSError):
        detail = open(log_path).read()[-400:]
    return SupervisorInfo(config.sock, running=False,
                          detail=f"did not answer within {wait:.0f}s. {detail}")


def stop(socket_path: str | None = None, wait: float = 30.0) -> SupervisorInfo:
    """Ask a supervisor to shut down, and confirm that it did.

    Refuses while the kernel is busy or holds a result nobody has collected.
    Stopping is a deliberate act, and taking work down with it silently would
    make it a destructive one.
    """
    path = socket_path or default_socket_path()
    info = probe(path)
    if not info.running:
        return info
    state = info.kernel_state or ""
    if not state.startswith("IDLE"):
        return SupervisorInfo(path, running=True, session=info.session,
                              pid=info.pid, kernel_state=state,
                              detail=f"refused: {state}")
    try:
        talk(path, "SHUTDOWN")
    except OSError:
        pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not probe(path).running:
            return SupervisorInfo(path, running=False, detail="stopped")
        time.sleep(0.2)
    return SupervisorInfo(path, running=True, detail="still answering after SHUTDOWN")
