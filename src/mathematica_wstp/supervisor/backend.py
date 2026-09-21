"""The notebook layer, evaluating in a kernel this process does not own.

``evaluator.py`` defines what the rest of the server may ask of a backend, and
was written for this case. Swapping the backend is the whole of the change: the
notebook layer submits an expression and gets bytes back, and whether the
kernel lives in this process or another one does not appear in its code.

What does differ is what an identity is worth. The direct backend mints request
ids for its own bookkeeping and marks them unauthenticated, because nothing
outside the process can corroborate them: if it dies, the ids die with it and
there is nobody left to ask. A supervisor's identities are issued by a process
that survives the client, recorded in a ledger, and answerable by
``LOOKUP`` afterwards -- so they are marked authenticated, and that flag is the
difference between a record and a claim.

This never starts a supervisor. Selecting a backend is an ordinary call and
must not leave a process running on the machine as a side effect; starting one
is a separate, deliberate act.
"""

from __future__ import annotations

import base64
import contextlib
import socket
import time
import uuid

from ..evaluator import BytesResult
from .core import default_socket_path

__all__ = ["SupervisorEvaluator", "SupervisorUnavailable", "connect"]

TERMINAL_STATES = ("COMPLETED", "ABORTED", "TIMED_OUT", "FAILED", "CANCELLED")


class SupervisorUnavailable(RuntimeError):
    """No supervisor is listening where one was expected."""


def _field(line: str, key: str, default: str | None = None) -> str | None:
    for part in line.split():
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return default


class _SupervisorExecution:
    """A handle on one request the supervisor accepted."""

    def __init__(self, evaluator: SupervisorEvaluator, request_id: str):
        self._ev = evaluator
        self.request_id = request_id
        self.token: str | None = None
        #: The supervisor issued this, and can still be asked about it after
        #: this process is gone.
        self.authenticated = True

    def wait(self, timeout: float = 3600) -> BytesResult:
        """Poll until the request reaches a state it cannot leave.

        Keyed on the terminal set rather than on "not the states I thought of":
        DISPATCHING is neither pending nor running, and a loop written the
        other way reads a request that has not started as one that has
        finished.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._ev.talk(f"RESULT {self.request_id}")
            self.token = self.token or _field(line, "token")
            if line.split()[0] in TERMINAL_STATES:
                return self._ev.result_from(line, self.request_id)
            time.sleep(0.1)
        # The evaluation is still running over there. This is the caller's wait
        # expiring, not the science being abandoned, and the distinction is why
        # nothing is aborted here: the request remains in the ledger under its
        # key, and can be collected later.
        return BytesResult(data=None, outcome="TIMED_OUT", request_id=self.request_id,
                           backend="supervisor", token=self.token, authenticated=True,
                           detail=f"no answer within {timeout}s; the request is still "
                                  f"recorded and can be collected by its key")

    def abort(self) -> str:
        """Interrupt this execution, named by its token.

        Control is scoped to the token the supervisor issued, never to
        "whatever is running". A request that never activated has no token, and
        refusing is right: there is nothing of this request in the kernel to
        interrupt, and an abort aimed at an idle kernel wedges it.
        """
        line = self._ev.talk(f"REQUEST {self.request_id}")
        token = _field(line, "token")
        if not token or token == "none":
            return f"REFUSED {self.request_id} has no active evaluation"
        self.token = token
        return self._ev.talk(f"ABORT {token}")

    def status(self) -> str:
        line = self._ev.talk(f"REQUEST {self.request_id}")
        state = _field(line, "state")
        if state is None and "state=" in line:
            state = line.split("state=")[1].split()[0]
        return state or "UNKNOWN"


class SupervisorEvaluator:
    """A supervisor, reached over its socket."""

    name = "supervisor"

    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or default_socket_path()

    def talk(self, message: str, timeout: float = 30.0) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.socket_path)
        except OSError as exc:
            raise SupervisorUnavailable(
                f"no supervisor answering at {self.socket_path}: {exc}") from exc
        try:
            f = s.makefile("rw")
            f.write(message + "\n")
            f.flush()
            reply = f.readline().strip()
            f.close()          # makefile duplicates the descriptor; both must go
            return reply
        finally:
            with contextlib.suppress(OSError):
                s.close()

    def submit_bytes(self, code: str, timeout: float = 60,
                     idempotency_key: str | None = None,
                     correlation: dict[str, str] | None = None) -> _SupervisorExecution:
        key = idempotency_key or uuid.uuid4().hex[:12]
        parts = [f"SUBMIT {key}", f"t={timeout}"]
        if correlation:
            parts.append("corr=" + ",".join(f"{k}:{v}" for k, v in correlation.items()))
        parts.append("bytes=1")
        reply = self.talk(" ".join(parts) + " " + code)
        request_id = reply.split()[0] if reply else ""
        if not request_id.startswith("E"):
            raise SupervisorUnavailable(f"submission refused: {reply}")
        return _SupervisorExecution(self, request_id)

    def lookup(self, idempotency_key: str) -> str | None:
        """What became of a key, without submitting anything.

        Returns None when the key was never admitted. Asking must never be the
        thing that makes a request exist.
        """
        reply = self.talk(f"LOOKUP {idempotency_key}")
        return None if reply.startswith("UNKNOWN") else reply

    def result_from(self, line: str, request_id: str) -> BytesResult:
        state = line.split()[0]
        kind = _field(line, "kind", "INLINE_TEXT")
        token = _field(line, "token")
        artifact = _field(line, "artifact")
        data = None
        if state == "COMPLETED":
            if kind == "INLINE_BYTES" and "result=" in line:
                data = base64.b64decode(line.split("result=", 1)[1])
            elif artifact and artifact != "none":
                # Large results are spooled rather than inlined. The supervisor
                # names the file; the digest it publishes alongside is what makes
                # reading it back evidence rather than trust.
                path = self.talk(f"ARTIFACT {artifact}").split()[1]
                with open(path, "rb") as fh:
                    data = fh.read()
        return BytesResult(data=data, outcome=state, request_id=request_id,
                           backend="supervisor", token=token, authenticated=True,
                           detail="" if state == "COMPLETED" else line[:200])


def connect(socket_path: str | None = None) -> SupervisorEvaluator:
    """A backend for the supervisor that is already running, or an error.

    Deliberately refuses rather than starting one. A call that quietly leaves a
    long-lived process on the machine is not something the notebook layer
    should be able to do by accident.
    """
    from .lifecycle import probe

    info = probe(socket_path)
    if not info.running:
        raise SupervisorUnavailable(
            f"no supervisor at {info.socket_path} ({info.detail}). "
            f"Start one deliberately before selecting this backend.")
    return SupervisorEvaluator(info.socket_path)
