"""ctypes binding to libWSTP.

This is the layer that makes abort possible, and abort is the reason this
project exists. Everything above it is ordinary Python; everything below it is
the C library that ships with Mathematica.

Three things here are load-bearing and easy to get wrong:

1. **Every restype and argtypes is declared.** WSLINK and WSENV are pointers.
   ctypes defaults a return type to ``c_int``, which truncates a 64-bit pointer
   to 32 bits. The link then "works" for a while and corrupts much later, far
   from the cause.

2. **WSPutMessage is the abort channel, and it is out of band.** It may be
   called while another thread is blocked reading the same link. That is how
   the front end's Abort Evaluation reaches a busy kernel, and it is the one
   thing wolframclient cannot do at all.

3. **Strings returned by WSGetString are owned by WSTP** and must be handed
   back with WSReleaseString, or the kernel link leaks memory for the life of
   the process.

The library exposes no file descriptor accessor, so waiting is a poll on
WSReady. The interval is the latency floor -- keep it small. At 0.2ms the
measured round trip is 0.27ms; at 20ms every payload appears to take 20ms.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

from .discovery import find_wstp_library

logger = logging.getLogger("mathematica_wstp.link")

# --- packet types (from wstp.h, interface 4) -------------------------------
ILLEGALPKT = 0
TEXTPKT = 2
RETURNPKT = 3
RETURNTEXTPKT = 4
MESSAGEPKT = 5
CALLPKT = 7
INPUTNAMEPKT = 8
OUTPUTNAMEPKT = 9
SYNTAXPKT = 10

# --- out-of-band messages --------------------------------------------------
WSTerminateMessage = 1
WSInterruptMessage = 2
WSAbortMessage = 3
WSEndPacketMessage = 4
WSSynchronizeMessage = 5
WSImDyingMessage = 6
WSMarkTopLevelMessage = 8

# --- expression token types (WSGetType) ------------------------------------
# Taken from wstp.h by compiling a printer against it, not guessed from the
# mnemonic characters. WSTKSTR is 34 ('"'), NOT 83 ('S') -- 83 is WSTKOLDSTR,
# the pre-interface-3 form. Guessing 'S' here silently misclassified every
# string token and made message capture look impossible.
WSTKFUNC = 70     # 'F'
WSTKSTR = 34      # '"'
WSTKSYM = 35      # '#'
WSTKINT = 43      # '+'
WSTKREAL = 42     # '*'
WSTKOLDSTR = 83   # 'S'
WSTKOLDSYM = 89   # 'Y'
WSTKERROR = 0

STRING_TOKENS = frozenset({WSTKSTR, WSTKOLDSTR})
SYMBOL_TOKENS = frozenset({WSTKSYM, WSTKOLDSYM})

DEFAULT_POLL_INTERVAL = 0.0002


class WSTPError(RuntimeError):
    """A WSTP-level failure. ``code`` is the WSError value."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class LinkDead(WSTPError):
    """The link is gone -- the kernel died, or the connection dropped.

    Distinct from WSTPError because it is the one condition that must never be
    retried on the same link: recovery is to discard it and launch again.
    """


def _bind(lib: ctypes.CDLL) -> None:
    """Declare every signature we use. See note 1 in the module docstring."""
    P = ctypes.c_void_p
    sigs = [
        ("WSInitialize", [P], P),
        ("WSDeinitialize", [P], None),
        ("WSOpenString", [P, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)], P),
        ("WSActivate", [P], ctypes.c_int),
        ("WSClose", [P], None),
        ("WSPutFunction", [P, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
        ("WSPutString", [P, ctypes.c_char_p], ctypes.c_int),
        ("WSPutUTF8String", [P, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
        ("WSGetUTF8String", [P, ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                             ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        ("WSReleaseUTF8String", [P, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int], None),
        ("WSPutSymbol", [P, ctypes.c_char_p], ctypes.c_int),
        ("WSPutInteger64", [P, ctypes.c_int64], ctypes.c_int),
        ("WSPutReal64", [P, ctypes.c_double], ctypes.c_int),
        ("WSEndPacket", [P], ctypes.c_int),
        ("WSFlush", [P], ctypes.c_int),
        ("WSReady", [P], ctypes.c_int),
        ("WSNextPacket", [P], ctypes.c_int),
        ("WSNewPacket", [P], ctypes.c_int),
        ("WSGetType", [P], ctypes.c_int),
        ("WSGetString", [P, ctypes.POINTER(ctypes.c_char_p)], ctypes.c_int),
        ("WSGetSymbol", [P, ctypes.POINTER(ctypes.c_char_p)], ctypes.c_int),
        ("WSReleaseSymbol", [P, ctypes.c_char_p], None),
        ("WSReleaseString", [P, ctypes.c_char_p], None),
        ("WSGetInteger64", [P, ctypes.POINTER(ctypes.c_int64)], ctypes.c_int),
        ("WSGetReal64", [P, ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
        ("WSGetInteger8List", [P, ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                               ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        ("WSReleaseInteger8List", [P, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int], None),
        ("WSPutMessage", [P, ctypes.c_int], ctypes.c_int),
        ("WSError", [P], ctypes.c_int),
        ("WSClearError", [P], ctypes.c_int),
    ]
    for name, argtypes, restype in sigs:
        fn = getattr(lib, name)
        fn.argtypes = argtypes
        fn.restype = restype
    lib.WSErrorMessage.argtypes = [P]
    lib.WSErrorMessage.restype = ctypes.c_char_p


_lib: ctypes.CDLL | None = None
_env: int | None = None
_env_lock = threading.Lock()


def _library() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        path = find_wstp_library()
        logger.debug("loading WSTP from %s", path)
        lib = ctypes.CDLL(path)
        _bind(lib)
        _lib = lib
    return _lib


def _environment() -> int:
    """The process-wide WSENV. WSInitialize is called once."""
    global _env
    with _env_lock:
        if _env is None:
            env = _library().WSInitialize(None)
            if not env:
                raise WSTPError("WSInitialize failed -- WSTP could not start")
            _env = env
    return _env


class Link:
    """One WSTP link.

    Not thread-safe for concurrent evaluation: one writer at a time, guarded by
    ``lock``. The deliberate exception is :meth:`put_message`, which is designed
    to be called from another thread while a read is outstanding -- that is the
    whole point of the message channel.
    """

    def __init__(self, spec: str):
        self._lib = _library()
        self.spec = spec
        self.lock = threading.RLock()
        self._closed = False
        err = ctypes.c_int(0)
        self._link = self._lib.WSOpenString(_environment(), spec.encode(), ctypes.byref(err))
        if not self._link:
            raise WSTPError(f"WSOpenString failed (err={err.value}) for spec: {spec}", err.value)

    # -- lifecycle ---------------------------------------------------------

    def activate(self, timeout: float = 30.0) -> None:
        """Complete the handshake.

        Runs on a worker thread with a deadline because WSActivate against a
        nonexistent binary blocks forever -- measured, killed manually after
        3m37s. This is the one place WSTP is worse than a subprocess call, and
        it has to be handled rather than trusted.
        """
        result: list[int] = []
        error: list[BaseException] = []

        def _run() -> None:
            try:
                result.append(self._lib.WSActivate(self._link))
            except BaseException as exc:  # pragma: no cover - defensive
                error.append(exc)

        worker = threading.Thread(target=_run, daemon=True, name="wstp-activate")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise WSTPError(
                f"WSActivate did not return within {timeout}s for spec: {self.spec}. "
                "The kernel binary is probably missing or not executable."
            )
        if error:
            raise WSTPError(f"WSActivate raised: {error[0]}")
        if not result or not result[0]:
            raise WSTPError(f"WSActivate failed: {self.error_message()}", self.error())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lib.WSClose(self._link)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("WSClose raised: %s", exc)

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Link:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- errors ------------------------------------------------------------

    def error(self) -> int:
        return self._lib.WSError(self._link)

    def error_message(self) -> str:
        msg = self._lib.WSErrorMessage(self._link)
        return msg.decode(errors="replace") if msg else "<no message>"

    def clear_error(self) -> bool:
        return bool(self._lib.WSClearError(self._link))

    def _check(self) -> None:
        """Raise LinkDead if the link has failed. Called after every I/O step."""
        code = self.error()
        if code:
            raise LinkDead(f"WSTP error {code}: {self.error_message()}", code)

    # -- writing -----------------------------------------------------------

    def put_function(self, head: str, argc: int) -> None:
        self._lib.WSPutFunction(self._link, head.encode(), argc)

    def put_string(self, value: str) -> None:
        """Send a string with no escape interpretation.

        Uses WSPutUTF8String rather than WSPutString. ``WSPutString`` takes the
        older "character string" format, in which a backslash introduces an
        escape -- so it does not round-trip arbitrary text. Measured: sending
        the seven characters ``StringLength["a\\\\b"]`` through WSPutString
        makes the kernel report 2 instead of 3, because one backslash is eaten
        and ``\\b`` becomes a backspace; sending ``StringLength["a\\"b"]``
        yields ``$Failed`` outright, the quote having been unescaped into a
        syntax error.

        That is not a corner case for this server. Wolfram source is full of
        backslashes -- every ``\\[Gamma]``, every escaped quote, every ``\\n``
        in a string literal -- and silent corruption of them is exactly the
        class of bug the notebook layer exists to avoid.
        """
        raw = value.encode("utf-8")
        self._lib.WSPutUTF8String(self._link, raw, len(raw))

    def put_symbol(self, name: str) -> None:
        self._lib.WSPutSymbol(self._link, name.encode())

    def put_int(self, value: int) -> None:
        self._lib.WSPutInteger64(self._link, value)

    def put_real(self, value: float) -> None:
        self._lib.WSPutReal64(self._link, value)

    def end_packet(self) -> None:
        self._lib.WSEndPacket(self._link)

    def flush(self) -> None:
        self._lib.WSFlush(self._link)

    # -- reading -----------------------------------------------------------

    def ready(self) -> bool:
        return bool(self._lib.WSReady(self._link))

    def next_packet(self) -> int:
        pkt = self._lib.WSNextPacket(self._link)
        self._check()
        return pkt

    def new_packet(self) -> None:
        self._lib.WSNewPacket(self._link)

    def get_type(self) -> int:
        return self._lib.WSGetType(self._link)

    def get_string(self) -> str:
        """Read a string with no escape interpretation (the WSPutString mirror)."""
        buf = ctypes.POINTER(ctypes.c_ubyte)()
        nbytes = ctypes.c_int(0)
        nchars = ctypes.c_int(0)
        if not self._lib.WSGetUTF8String(
            self._link, ctypes.byref(buf), ctypes.byref(nbytes), ctypes.byref(nchars)
        ):
            self._check()
            raise WSTPError(f"WSGetUTF8String failed: {self.error_message()}", self.error())
        try:
            return bytes(bytearray(buf[i] for i in range(nbytes.value))).decode(
                "utf-8", errors="replace"
            )
        finally:
            self._lib.WSReleaseUTF8String(self._link, buf, nbytes)  # see note 3

    def get_symbol(self) -> str:
        """Read a symbol token as its name."""
        buf = ctypes.c_char_p()
        if not self._lib.WSGetSymbol(self._link, ctypes.byref(buf)):
            self._check()
            raise WSTPError(f"WSGetSymbol failed: {self.error_message()}", self.error())
        try:
            return buf.value.decode(errors="replace") if buf.value else ""
        finally:
            self._lib.WSReleaseSymbol(self._link, buf)

    def get_int(self) -> int:
        out = ctypes.c_int64(0)
        if not self._lib.WSGetInteger64(self._link, ctypes.byref(out)):
            raise WSTPError(f"WSGetInteger64 failed: {self.error_message()}", self.error())
        return out.value

    def get_real(self) -> float:
        out = ctypes.c_double(0.0)
        if not self._lib.WSGetReal64(self._link, ctypes.byref(out)):
            raise WSTPError(f"WSGetReal64 failed: {self.error_message()}", self.error())
        return out.value

    def get_bytes(self) -> bytes:
        """Pull a byte list (e.g. Normal[BinarySerialize[...]] or raw image data).

        If what arrived is not a byte list -- the evaluation returned $Failed, or
        an unevaluated expression, or anything else -- WSGetInteger8List fails
        and sets the link's error state. WSTP keeps that state set until it is
        cleared, so leaving it makes every later call on this link fail too:
        one wrong reply turns into a dead kernel and a silently replaced
        session. Clear it here so the caller gets a diagnosable error on a link
        that still works.
        """
        arr = ctypes.POINTER(ctypes.c_ubyte)()
        count = ctypes.c_int(0)
        if not self._lib.WSGetInteger8List(self._link, ctypes.byref(arr), ctypes.byref(count)):
            code, msg = self.error(), self.error_message()
            self.clear_error()
            raise WSTPError(
                f"WSGetInteger8List failed: {msg} -- the reply was not a byte list. "
                "The link error has been cleared; the kernel should still be usable.",
                code,
            )
        try:
            return bytes(bytearray(arr[i] & 0xFF for i in range(count.value)))
        finally:
            self._lib.WSReleaseInteger8List(self._link, arr, count)

    # -- out of band -------------------------------------------------------

    def put_message(self, message: int) -> bool:
        """Send an out-of-band message. Safe while another thread is reading.

        This is the abort channel. See note 2 in the module docstring.
        """
        rc = self._lib.WSPutMessage(self._link, message)
        try:
            self._lib.WSFlush(self._link)
        except Exception:  # pragma: no cover - flush is best effort here
            pass
        return bool(rc)

    def abort(self) -> bool:
        """Interrupt the running evaluation. Returns $Aborted on the read side."""
        return self.put_message(WSAbortMessage)

    def interrupt(self) -> bool:
        return self.put_message(WSInterruptMessage)

    def terminate_message(self) -> bool:
        return self.put_message(WSTerminateMessage)

    # -- waiting -----------------------------------------------------------

    def wait_ready(self, timeout: float, poll: float = DEFAULT_POLL_INTERVAL) -> bool:
        """Block until a packet is available, the link dies, or timeout.

        Returns True if data is ready. Raises LinkDead the moment the kernel
        goes away -- which is the whole liveness story: a dead kernel surfaces
        in ~0.3s instead of hanging the caller forever.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.ready():
                return True
            code = self.error()
            if code:
                raise LinkDead(f"WSTP error {code}: {self.error_message()}", code)
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)


def launch_link(kernel_path: str, extra_args: str = "", protocol: str | None = None) -> Link:
    """Open a link that launches its own kernel."""
    args = f"{kernel_path} -wstp"
    if extra_args:
        args += f" {extra_args}"
    spec = f"-linkname '{args}' -linkmode launch"
    if protocol:
        spec += f" -linkprotocol {protocol}"
    return Link(spec)


def listen_link(name: str, protocol: str = "TCPIP") -> Link:
    """Open a listening link for something else to connect to."""
    return Link(f"-linkmode listen -linkprotocol {protocol} -linkname {name}")


def connect_link(name: str, protocol: str = "TCPIP") -> Link:
    """Connect to an existing listening link."""
    return Link(f"-linkmode connect -linkprotocol {protocol} -linkname {name}")
