"""A supervised Wolfram kernel on a WSTP link.

This is where the transport and the process supervision meet, and the two are
deliberately not separable: owning a kernel means owning its process tree.

**Why we do not use ``-linkmode launch``.** It is the obvious way to start a
kernel and the prototype used it, but WSTP forks and execs the child inside the
C library, so we never see the fork. A parent cannot change the process group of
a child that has already exec'd, which means the kernel and every subkernel it
later spawns stay in *our* process group -- and a group signal aimed at the
kernel would hit the server itself. Instead we spawn the kernel ourselves with
``start_new_session=True`` and have it *connect back* to a link we are already
listening on. That costs one extra handshake and buys:

  * a real pid and process group we own from the start;
  * ``LaunchKernels[]`` subkernels inheriting that group, so one ``killpg``
    reaches the whole fan-out that previously leaked;
  * control over stdout, which wolframclient left as an unread pipe that
    blocked the kernel in ``write()`` past 64 KB.

Results come back as text. Nothing is deserialized into Python objects, because
nothing downstream is Python -- results travel to a language model as text.
``ToString[expr, InputForm]`` round-trips exactly and the kernel is the
reference implementation for printing Wolfram expressions. ``OutputForm``, the
default, silently truncates machine reals; do not use it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import registry
from .discovery import find_kernel
from .link import (
    Link, LinkDead, MESSAGEPKT, RETURNPKT, STRING_TOKENS, SYMBOL_TOKENS,
    TEXTPKT, WSTPError, listen_link,
)

logger = logging.getLogger("mathematica_wstp.kernel")

DEFAULT_TIMEOUT = 60.0
ACTIVATE_TIMEOUT = 45.0


class KernelError(RuntimeError):
    pass


class EvaluationAborted(RuntimeError):
    """The evaluation was interrupted.

    Raised only when $Aborted came back over the link, so the kernel was
    answering at that point. That is evidence, not a guarantee about now --
    session.abort_current() probes before making any claim about survival.
    """


class EvaluationTimeout(RuntimeError):
    """The deadline passed. Carries whatever the abort recovered.

    Note that unlike the previous server, a timeout does not cost you the
    session: we abort, the kernel returns $Aborted, and all state survives.
    """

    def __init__(self, message: str, elapsed: float, aborted_cleanly: bool):
        super().__init__(message)
        self.elapsed = elapsed
        self.aborted_cleanly = aborted_cleanly


@dataclass
class Reply:
    """One evaluation's full result, including everything printed alongside it.

    The kernel does not send only an answer. ``Print`` output arrives as its own
    text packet, and every message (``Part::partw``, ``Power::infy``, ...)
    arrives as a message packet naming the symbol and tag, followed by a text
    packet holding the rendered text. A reader that keeps only the return packet
    silently discards all of it -- which is how a warning that explains a wrong
    answer disappears before anyone sees it.

    ``events`` is the source of truth and is in packet order, so the interleaving
    of prints and messages survives:

        Print["one"]; 1/0; Print["two"]  ->  print one, message Power::infy, print two

    ``prints`` and ``messages`` are filtered views of it. They were separate
    lists until a supervisor needed to render a cell's output back into a
    document and found the relative order had been discarded at this layer --
    once two lists have been built, no later code can recover which came first.
    """

    value: str = ""
    events: list[dict] = field(default_factory=list)

    @property
    def messages(self) -> list[dict]:
        return [{k: v for k, v in e.items() if k != "kind"}
                for e in self.events if e["kind"] == "message"]

    @property
    def prints(self) -> list[str]:
        return [e["text"] for e in self.events if e["kind"] == "print"]


def _tidy_message(text: str, name: str) -> str:
    """Collapse the kernel's rendered message text onto one line.

    The wire form carries line-wrapping padding, and for messages that quote
    two-dimensional typeset input the parts arrive on separate lines --
    ``1/0`` reaches us as ``'1\nPower::infy: Infinite expression - encountered.\n  0'``
    because the numerator and denominator straddle the division bar.

    Whitespace is collapsed and nothing else. An earlier version trimmed
    everything before the ``Symbol::tag:`` marker, which tidied that example by
    throwing away the ``1`` -- part of the very expression the message is about.
    The clean identifier is already available separately as ``name``, so this
    field stays faithful rather than pretty.
    """
    return " ".join(text.split())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Kernel:
    """One kernel process, its link, and its process tree."""

    def __init__(self, kernel_path: str | None = None, *, protocol: str = "TCPIP",
                 stdout_to_devnull: bool = True, extra_args: tuple[str, ...] = ()):
        self.kernel_path = kernel_path or find_kernel()
        self.protocol = protocol
        self._stdout_to_devnull = stdout_to_devnull
        self._extra_args = extra_args
        self.link: Link | None = None
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.pgid: int | None = None
        self._eval_lock = threading.RLock()
        self._closed = False
        self._abort_requested = threading.Event()
        # Set once an expression has actually been written to the link and
        # flushed, cleared when its reply has been consumed. NOT the same as
        # "the evaluation lock is held": the lock is taken first, and an abort
        # sent in the gap reaches an idle kernel, which wedges it.
        self._in_flight = threading.Event()
        self.subkernel_pids_cached: list[int] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = ACTIVATE_TIMEOUT) -> Kernel:
        if self.link is not None:
            return self

        linkname = str(_free_port()) if self.protocol == "TCPIP" else f"mcp{os.getpid()}x{random.randint(1000, 9999)}"
        link = listen_link(linkname, protocol=self.protocol)

        argv = [
            self.kernel_path, "-wstp",
            "-linkmode", "connect",
            "-linkprotocol", self.protocol,
            "-linkname", linkname,
            *self._extra_args,
        ]
        devnull = subprocess.DEVNULL if self._stdout_to_devnull else None
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=devnull,
                stderr=devnull,
                start_new_session=True,   # its own session + process group
                close_fds=True,
                env=self._child_env(),
            )
        except OSError as exc:
            link.close()
            raise KernelError(f"could not spawn kernel {self.kernel_path}: {exc}") from exc

        self.pid = self.proc.pid
        try:
            self.pgid = os.getpgid(self.pid)
        except (ProcessLookupError, PermissionError):
            self.pgid = self.pid

        try:
            link.activate(timeout=timeout)
        except (WSTPError, LinkDead) as exc:
            link.close()
            self._kill_now()
            raise KernelError(
                f"kernel started (pid {self.pid}) but the WSTP handshake failed: {exc}"
            ) from exc

        self.link = link
        registry.record(self.pid, self.pgid or self.pid, self.kernel_path)

        # One throwaway evaluation before reporting ready, because a kernel that
        # has answered the handshake is not yet ABORTABLE. Connected is not
        # ready: ready means connected AND armed.
        #
        # Measured: on a kernel whose first evaluation is the one you want to
        # stop, an out-of-band abort does not merely go unconfirmed -- it has no
        # effect at all. `Pause[20]; "NEVER"` returned "NEVER" after the abort,
        # with confirmed=False reported 23s later. The same abort against the
        # same expression on a kernel that had already evaluated `1+1` was
        # confirmed in 0.0s and returned $Aborted.
        #
        # So the interrupt handler is not armed by the link handshake. Arming it
        # costs one trivial round trip at startup; not arming it means abort --
        # the property this whole transport exists to provide -- silently does
        # nothing on the first evaluation of every fresh kernel.
        # Failing this is fatal, not a warning. A kernel that cannot evaluate `1`
        # is not a usable kernel missing one feature -- it is a kernel that
        # failed its first evaluation. Advertising it would hand back something
        # whose abort semantics are unknown, which is the exact condition the
        # arming step exists to rule out.
        try:
            self._raw_eval("1", timeout=min(30.0, timeout))
        except Exception as exc:
            self.link = None
            link.close()
            self._kill_now()
            raise KernelError(
                f"kernel {self.pid} connected but failed its arming evaluation "
                f"({exc}); refusing to report it ready, because a kernel that has "
                "not completed a round trip cannot be interrupted"
            ) from exc

        logger.info("kernel up: pid=%s pgid=%s link=%s", self.pid, self.pgid, linkname)
        return self

    def _child_env(self) -> dict[str, str]:
        """Environment for the spawned kernel.

        A user's ``$UserBaseDirectory/Kernel/init.m`` is loaded by every kernel,
        and on a machine with the older socket-based MCP addon installed that
        init file does two unwanted things to a kernel we launch: it prints its
        banner onto our link, and it starts a *second* MCP socket server on the
        shared default port. The banner is harmless (the read loop skips
        non-return packets) but a competing listener is not.

        ``MATHEMATICA_MCP_CHILD`` is the flag that addon already checks to
        decline starting a server in a kernel spawned by a server. Setting it
        costs nothing where the addon is absent.
        """
        env = dict(os.environ)
        env["MATHEMATICA_MCP_CHILD"] = "1"
        env["MATHEMATICA_WSTP_CHILD"] = "1"
        return env

    def is_alive(self) -> bool:
        if self._closed or self.link is None or self.pid is None:
            return False
        if self.proc is not None and self.proc.poll() is not None:
            return False
        return registry.pid_alive(self.pid)

    def close(self, grace: float = 5.0) -> None:
        """Shut down in three stages: CloseKernels, SIGTERM group, SIGKILL.

        The graceful stage is the one that matters and the one the old transport
        could not rely on: over WSTP the link stays answerable, so we can ask the
        kernel to close its own subkernels before anything is signalled.
        """
        if self._closed:
            return
        self._closed = True
        pid = self.pid
        # Note the helpers before anything is signalled -- once the master is
        # gone we can no longer ask who its children were.
        helpers = self.observe_subkernels() if pid else []

        if self.link is not None and self.is_alive():
            try:
                self._raw_eval('Quiet[If[Length[Kernels[]] > 0, CloseKernels[]]; "ok"]', timeout=grace)
            except Exception as exc:
                logger.debug("CloseKernels failed (continuing to signals): %s", exc)
            try:
                self._raw_eval('Quit[]', timeout=0.5)
            except Exception:
                pass  # Quit never replies -- that is the point

        if self.link is not None:
            self.link.close()
            self.link = None

        if pid is not None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                # Reap our own child as we go. Without this the kernel exits,
                # becomes a zombie, and keeps answering kill(pid, 0) -- so the
                # loop below would run out the full grace on a process that has
                # already gone, and then signal a corpse.
                if self.proc is not None:
                    try:
                        if self.proc.poll() is not None:
                            break
                    except Exception:
                        pass
                if not registry.pid_alive(pid):
                    break
                time.sleep(0.02)

            if registry.pid_alive(pid):
                self._kill_now()

            # Wait for the helpers too, not just the master. They die from the
            # group signal a moment after it does, so a caller that checks
            # immediately sees a full fan-out still alive and reports a leak
            # that is not there -- measured: 4 helpers "leaked" at t+0.0s, all
            # gone by t+0.5s. close() promises to take the tree down, so it
            # should not return until the tree is down.
            if helpers:
                tree_deadline = time.monotonic() + grace
                while time.monotonic() < tree_deadline:
                    if not any(registry.pid_alive(h) for h in helpers):
                        break
                    time.sleep(0.05)
                survivors = [h for h in helpers if registry.pid_alive(h)]
                if survivors:
                    logger.warning("subkernels outlived the kernel: %s", survivors)

            registry.forget(pid)

        if self.proc is not None:
            try:
                self.proc.wait(timeout=1.0)
            except Exception:
                pass

    def _kill_now(self) -> None:
        """Signal the whole group. Falls back to the bare pid if we have no group."""
        if self.pid is None:
            return
        entry = {
            "pid": self.pid,
            "pgid": self.pgid or 0,
            "starttime": registry.proc_starttime(self.pid) or 0,
            # Count now, from /proc. The cache is only as good as the last
            # caller who asked, and shutdown is exactly when nobody has.
            "subkernels": self.observe_subkernels(),
        }
        # starttime may be None if the process is already gone; terminate_tree
        # re-checks ownership and simply does nothing in that case.
        registry.terminate_tree(entry)

    def __enter__(self) -> Kernel:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- evaluation --------------------------------------------------------

    def _raw_eval(self, code: str, timeout: float) -> Reply:
        """Send ToString[ToExpression[code], InputForm] and read one result."""
        link = self.link
        if link is None:
            raise KernelError("kernel is not started")
        with self._eval_lock:
            self._abort_requested.clear()
            link.put_function("EvaluatePacket", 1)
            link.put_function("ToString", 2)
            link.put_function("ToExpression", 1)
            link.put_string(code)
            link.put_symbol("InputForm")
            link.end_packet()
            link.flush()
            self._in_flight.set()
            try:
                return self._read_reply(timeout)
            finally:
                self._in_flight.clear()

    def _eval_string(self, code: str, timeout: float) -> str:
        """Evaluate an expression that already yields a String, and read it raw.

        No ``ToString``/``InputForm`` layer, so nothing is escaped on the way
        back. Wrapping a string result in InputForm renders its *source form*
        -- quotes become ``\\"`` -- and a consumer that then unescapes is
        guessing at an encoding it does not control. For JSON and other text
        payloads, ask for the string itself.
        """
        link = self.link
        if link is None:
            raise KernelError("kernel is not started")
        with self._eval_lock:
            self._abort_requested.clear()
            link.put_function("EvaluatePacket", 1)
            link.put_function("ToExpression", 1)
            link.put_string(code)
            link.end_packet()
            link.flush()
            return self._read_result(timeout)

    def evaluate_bytes(self, code: str, timeout: float) -> bytes:
        """Evaluate an expression yielding a ByteArray, and read the raw bytes.

        The only encoding-safe way to move text out of the kernel.
        ``ExportString[..., "RawJSON"]`` returns the *serialized bytes rendered
        as characters*, so a gamma arrives as two Latin-1 characters and decodes
        to mojibake -- and no ``CharacterEncoding`` option changes that
        (measured: UTF8 and PrintableASCII give byte-identical output).
        ``ExportByteArray`` keeps bytes as bytes, and the decode happens once,
        here, where we know the encoding.
        """
        link = self.link
        if link is None:
            raise KernelError("kernel is not started")
        with self._eval_lock:
            self._abort_requested.clear()
            link.put_function("EvaluatePacket", 1)
            link.put_function("ToExpression", 1)
            link.put_string(code)
            link.end_packet()
            link.flush()
            self._in_flight.set()
            started = time.monotonic()
            try:
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0 or not link.wait_ready(remaining):
                        raise TimeoutError("no reply within the deadline")
                    if link.next_packet() == RETURNPKT:
                        try:
                            return link.get_bytes()
                        finally:
                            link.new_packet()
                    link.new_packet()
            finally:
                self._in_flight.clear()

    def _read_reply(self, timeout: float) -> Reply:
        """Read until the return packet, keeping everything that arrives first.

        Packet order for a message is MESSAGEPKT (symbol + tag) then TEXTPKT
        (the rendered text); ``Print`` output is a bare TEXTPKT. So a text packet
        is message text when a message packet immediately preceded it, and
        Print output otherwise -- that adjacency is the only thing separating
        them, which is why the pending-message state is tracked across packets
        rather than decided per packet.
        """
        link = self.link
        assert link is not None
        reply = Reply()
        pending: dict | None = None
        started = time.monotonic()

        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0 or not link.wait_ready(remaining):
                raise TimeoutError("no reply within the deadline")

            packet = link.next_packet()

            if packet == RETURNPKT:
                try:
                    reply.value = link.get_string()
                finally:
                    link.new_packet()
                return reply

            if packet == MESSAGEPKT:
                try:
                    symbol = link.get_symbol()
                    tag = ""
                    if link.get_type() in STRING_TOKENS:
                        tag = link.get_string()
                    elif link.get_type() in SYMBOL_TOKENS:
                        tag = link.get_symbol()
                    pending = {"kind": "message", "symbol": symbol, "tag": tag,
                               "name": f"{symbol}::{tag}" if tag else symbol, "text": ""}
                    reply.events.append(pending)
                except (WSTPError, LinkDead):
                    pending = None
                finally:
                    link.new_packet()
                continue

            if packet == TEXTPKT:
                try:
                    text = link.get_string()
                    if pending is not None:
                        pending["text"] = _tidy_message(text, pending["name"])
                    else:
                        reply.events.append({"kind": "print", "text": text.rstrip("\n")})
                except (WSTPError, LinkDead):
                    pass
                finally:
                    pending = None
                    link.new_packet()
                continue

            pending = None
            link.new_packet()

    def _read_result(self, timeout: float) -> str:
        """Value only. Kept for callers that do not care about messages."""
        return self._read_reply(timeout).value

    def evaluate(self, code: str, timeout: float = DEFAULT_TIMEOUT,
                 abort_on_timeout: bool = True) -> str:
        """Evaluate Wolfram source, returning the result as InputForm text.

        Messages and ``Print`` output are discarded by this form; use
        :meth:`evaluate_detailed` when they matter, which is most of the time.
        """
        return self.evaluate_detailed(code, timeout, abort_on_timeout).value

    def evaluate_detailed(self, code: str, timeout: float = DEFAULT_TIMEOUT,
                          abort_on_timeout: bool = True) -> Reply:
        """Evaluate, returning the value together with messages and Print output.

        On timeout the evaluation is aborted rather than abandoned, so the kernel
        and every definition in it survive. That is only possible because the
        abort actually reaches the kernel; it is the single behavioural
        difference that motivated this transport.
        """
        started = time.monotonic()
        try:
            reply = self._raw_eval(code, timeout)
        except TimeoutError:
            elapsed = time.monotonic() - started
            if not abort_on_timeout:
                raise EvaluationTimeout(
                    f"evaluation exceeded {timeout}s and was left running", elapsed, False
                ) from None
            recovered = self.abort(wait=min(10.0, max(2.0, timeout * 0.1)),
                                   expect_reply=True)
            raise EvaluationTimeout(
                f"evaluation exceeded {timeout}s; aborted "
                f"({'the evaluation stopped; kernel not probed' if recovered else 'kernel did not confirm the abort'})",
                elapsed, recovered,
            ) from None
        if self._abort_requested.is_set() and reply.value.strip() == "$Aborted":
            # Unlike the timeout path above, this is evidenced: $Aborted came back
            # over the link, so the kernel was answering when it sent it.
            raise EvaluationAborted(
                "evaluation aborted on request; the kernel returned $Aborted, so it "
                "was answering at that point"
            )
        return reply

    def evaluate_json(self, code: str, timeout: float = DEFAULT_TIMEOUT):
        """Evaluate an expression that yields an Association, returned as Python data.

        The kernel does the encoding (``RawJSON``) and Python's stdlib does the
        decoding. This is the structured path -- used for control envelopes such
        as success flags, message lists and cell counts -- and it needs no
        expression deserializer.
        """
        raw = self.evaluate_bytes(
            f'Normal[ExportByteArray[{code}, "RawJSON", "Compact" -> True]]', timeout
        )
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise KernelError(
                f"expected JSON from the kernel, got {raw[:200]!r}"
            ) from exc

    def evaluation_in_flight(self) -> bool:
        """Has an expression been sent to the kernel whose reply is still owed?

        Deliberately narrow. It answers yes or no about the transport, for a
        caller that needs a second opinion independent of its own bookkeeping --
        the case that motivated it is a supervisor deciding whether an emergency
        abort is defensible when its own records have become contradictory.

        It does NOT say which evaluation, or whose. The link has no notion of
        request identity, so this must not be used as one.

        The first version answered "is the evaluation lock held", which is not
        the same question and is wrong in the way that matters. The lock is
        taken before the expression is written to the link, so there is a window
        in which the lock is held and the kernel has been sent nothing at all.
        Measured: a caller polling this predicate and aborting the moment it went
        true caught that window every time -- 3 trials, in_flight true at 0ms --
        and the abort, arriving at an idle kernel, left an interrupt pending that
        wedged it. The evaluation that followed never returned, and the abort was
        still unconfirmed 30s later.
        """
        return self.link is not None and self._in_flight.is_set()

    def abort(self, wait: float = 5.0, expect_reply: bool = False) -> bool:
        """Interrupt whatever is running. Returns True if the kernel confirmed.

        Safe to call from another thread while :meth:`evaluate` is blocked --
        the WSTP message channel is out of band, which is exactly what the
        front end's Abort Evaluation uses.

        Does nothing when no evaluation is in flight. Sending an abort to an
        idle kernel is not harmless: the interrupt stays pending and wedges the
        kernel. Measured -- after one bare abort against an idle kernel, two
        successive ``1+1`` evaluations each timed out at 10s. This was invisible
        until start() began arming the interrupt handler, because before that an
        abort to a fresh kernel did nothing at all.

        ``expect_reply`` is the timeout path, which has already released the
        lock but IS owed a ``$Aborted`` and must still drain it.
        """
        link = self.link
        if link is None:
            return False
        if not expect_reply and not self.evaluation_in_flight():
            logger.debug("abort ignored: no evaluation in flight")
            return False
        self._abort_requested.set()
        link.abort()

        # An aborted evaluation still sends its $Aborted back. Somebody has to
        # read it, or it sits on the link and is handed to the *next* caller as
        # their result -- a timeout would silently poison the following
        # evaluation. Whoever is positioned to read is the one who should:
        #
        #   * If an evaluate() is blocked in another thread, it holds the lock
        #     and will consume the reply itself. Draining here would steal it.
        #   * If nobody holds the lock (the timeout path, which released it on
        #     the way out), the reply is ours to clear.
        acquired = self._eval_lock.acquire(blocking=False)
        if not acquired:
            # Somebody is mid-evaluation and will consume the $Aborted itself.
            # Do NOT poll link.ready() here: that races the reader, which
            # normally wins, and we would report "not confirmed" for an abort
            # that in fact worked. Wait on the lock instead -- the reader
            # releases it when the evaluation ends, which is precisely the
            # signal we want.
            if self._eval_lock.acquire(timeout=wait):
                self._eval_lock.release()
                return True
            return False

        try:
            self._read_result(wait)
            return True
        except (TimeoutError, LinkDead, WSTPError):
            return False
        finally:
            self._eval_lock.release()

    # -- introspection -----------------------------------------------------

    def process_id(self) -> int:
        return int(self.evaluate("$ProcessID", timeout=15))

    def observe_subkernels(self) -> list[int]:
        """Subkernels as /proc sees them, asking the kernel nothing.

        This is the census that can always run. It costs no link round trip, so
        it is safe to call while an evaluation is in flight, and it still works
        when the kernel is too wedged to answer -- which is the moment the count
        actually matters. Filtering on the master's own executable name keeps
        the front end (a ``WolframNB`` child of the kernel) out of the count.
        """
        if self.pid is None:
            return []
        pids = registry.proc_children(self.pid, comm=registry.proc_comm(self.pid))
        self._record_subkernels(pids)
        return pids

    def _record_subkernels(self, pids: list[int]) -> None:
        self.subkernel_pids_cached = pids
        if self.pid is not None and pids:
            registry.update_subkernels(self.pid, pids)

    def subkernel_pids(self) -> list[int]:
        """Every subkernel this master owns, by both accounts.

        /proc says what is running; ``Kernels[]`` says what the kernel believes
        it owns. They normally agree, and the union is the safe answer when they
        do not: a process missing from the first was never ours to kill, and one
        missing from the second is exactly the kind that gets stranded.

        The link query is skipped when the kernel is busy, so this never blocks
        behind a running evaluation -- it just falls back to the /proc count.
        """
        observed = self.observe_subkernels()
        reported: list[int] = []
        if self._eval_lock.acquire(blocking=False):
            try:
                raw = self.evaluate(
                    'Quiet[If[Length[Kernels[]] > 0, '
                    'ToString /@ (# ["ProcessID"] & /@ Kernels[]), {}]]', timeout=20)
            except Exception:
                raw = ""
            finally:
                self._eval_lock.release()
            for token in raw.strip("{} ").split(","):
                token = token.strip().strip('"')
                if token.isdigit():
                    reported.append(int(token))
        pids = sorted(set(observed) | set(reported))
        self._record_subkernels(pids)
        return pids

    def health(self) -> dict:
        return {
            "alive": self.is_alive(),
            "pid": self.pid,
            "pgid": self.pgid,
            "kernel_path": self.kernel_path,
            "protocol": self.protocol,
            "link_error": self.link.error() if self.link else None,
        }
