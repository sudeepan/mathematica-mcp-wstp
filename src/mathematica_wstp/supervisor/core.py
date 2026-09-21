"""A kernel that outlives the process that asked for the work.

The shipped server owns its kernel directly, which makes kernel lifetime equal
to client lifetime: measured, a kernel exits about a second after its owning
client is killed. For a cell that runs for minutes that is a nuisance. For one
that runs for days it means a dropped connection destroys the work.

This module is the other arrangement. A separate process owns the kernel and
accepts requests over a Unix socket, so a client may come and go while the
science continues. What it adds beyond survival is a record: every request has
an identity, every activation is linearised, and what happened to each one is
written down in a form that outlives the client that asked.

    session-uuid / K<kernel-gen> / V<eval-gen> / E<request>

A request moves ACCEPTED -> DISPATCHING -> RUNNING -> COMPLETED | ABORTED |
FAILED, or ACCEPTED -> CANCELLED, which is the one path that guarantees nothing
ran. Three things are tracked separately because collapsing them loses the
distinction that matters: what the execution did, whether an abort was
confirmed, and whether the kernel is answering at all.

``LOOKUP <key>`` is what makes reconnection work. A client that submits and
dies before hearing the answer cannot recover by resubmitting - the payload
embeds the client's own pid, so a new client reproduces a different payload and
is correctly refused as different work. The caller-chosen idempotency key is
the only name that survives the caller, so the ledger answers to it directly
without running anything.

This file was a standalone prototype through fifteen revisions, each one
answering a failure the previous one allowed. The request logic below is
carried across unchanged for that reason: it is not obvious code, and nearly
every branch is there because something was measured going wrong without it.
What changed on the way in is only the shape - nothing happens at import, the
configuration is explicit, and there is an entry point to call.
"""

from __future__ import annotations

import hashlib
import os
import re
import contextlib
import socket
import threading
import time
import uuid
from dataclasses import dataclass

from ..kernel import Kernel, KernelError, EvaluationAborted, EvaluationTimeout

__all__ = ["SupervisorConfig", "configure", "serve_forever", "main"]

# --- what cannot be configured ---------------------------------------------

TERMINAL = {"COMPLETED", "ABORTED", "FAILED", "CANCELLED"}
ACTIVATED = {"RUNNING", "COMPLETED", "ABORTED", "FAILED"}
LEGAL = {("ACCEPTED", "DISPATCHING"), ("ACCEPTED", "CANCELLED"),
         ("DISPATCHING", "RUNNING"), ("DISPATCHING", "CANCELLED"),
         ("RUNNING", "COMPLETED"), ("RUNNING", "ABORTED"), ("RUNNING", "FAILED")}
# Every fault has a code and a category. Authorisation reads the category, so
# rewording a human-readable message cannot silently change what is allowed.
FAULT_CATEGORY = {
    "MULTIPLE_RUNNING":             "identity",
    "TOKEN_ACTIVE_WITHOUT_RUNNING": "identity",
    "TOKEN_MISMATCH":               "identity",
    "CANCELLED_WITH_TOKEN":         "identity",
    "ACTIVATED_WITHOUT_TOKEN":      "identity",
    "TERMINAL_MARK_MISMATCH":       "invariant",
    "TERMINAL_BEFORE_START":        "invariant",
    "COMPLETED_WITHOUT_RESULT":     "invariant",
    "RUNNING_WITH_RESULT":          "invariant",
    "ILLEGAL_TRANSITION":           "invariant",
    "RECOVERY_FAILED":              "operational",
    "ARTIFACT_DIGEST_MISMATCH":     "operational",
}
POLICIES = ("PRESERVE", "RESTART_IF_UNRESPONSIVE")
FINGERPRINT_VERSION = "fp1"   # so a change in what is hashed cannot alias silently
CORR_MAX_PAIRS, CORR_MAX_KEY, CORR_MAX_VALUE = 8, 32, 64
CORR_OK = re.compile(r"^[A-Za-z0-9._:@+-]+$")   # cannot break the record format


# --- what can ---------------------------------------------------------------

def _env(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


@dataclass
class SupervisorConfig:
    """Everything the supervisor needs to know before it starts.

    Read from the environment by default so the prototype's own exercisers
    still drive it, but passed explicitly so a caller does not have to set
    environment variables to start one.
    """

    sock: str = ""
    audit: str = ""
    spool: str = ""
    max_inline: int = 4096          # ByteCount, not text length
    max_inline_bytes: int = 4096    # raw, pre-base64
    grace: float = 5.0              # observation window, not a wait
    confirm: float = 0.0            # synchronous wait: none by default
    hold_before_activation: bool = False
    reclaim_after: float = 86400.0  # idle seconds before an unused kernel is released
    reclaim_check_every: float = 60.0

    @classmethod
    def from_env(cls) -> SupervisorConfig:
        return cls(
            sock=_env("SUP_SOCK", default_socket_path()),
            audit=_env("SUP_AUDIT", default_socket_path() + "-audit.log"),
            spool=_env("SUP_SPOOL", default_socket_path() + "-artifacts"),
            max_inline=int(_env("SUP_MAX_INLINE", "4096")),
            max_inline_bytes=int(_env("SUP_MAX_INLINE_BYTES", "4096")),
            grace=float(_env("SUP_GRACE", "5")),
            confirm=float(_env("SUP_CONFIRM", "0")),
            hold_before_activation=_env("SUP_HOLD_BEFORE_ACTIVATION", "") == "1",
            reclaim_after=float(_env("SUP_RECLAIM_AFTER", "86400")),
            reclaim_check_every=float(_env("SUP_RECLAIM_CHECK_EVERY", "60")),
        )


def default_socket_path() -> str:
    """Per-user, so two people on one machine do not share a laboratory."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"mathematica-wstp-supervisor-{os.getuid()}")


# Set by configure(). Declared here so the request logic below, which is
# carried across unchanged, finds the names it expects.
SOCK = AUDIT = SPOOL = ""
MAX_INLINE = MAX_INLINE_BYTES = 4096
GRACE, CONFIRM = 5.0, 0.0
SESSION = SHORT = STORE = ""
RECLAIM_AFTER, RECLAIM_CHECK_EVERY = 86400.0, 60.0
_configured = False

# A kernel is worth keeping because it holds state that cost something to
# build. It is worth releasing because holding it costs memory -- a stale
# parallel pool here has been measured at gigabytes. These two say when the
# second consideration is allowed to win.
clients = [0]                  # connected right now
last_activity = [0.0]          # any command from any client
shutdown_requested = threading.Event()


def configure(config: SupervisorConfig | None = None) -> SupervisorConfig:
    """Fix the configuration and mint this laboratory's identity.

    Separate from ``main`` so the module can be imported, inspected and tested
    without a socket being bound or a kernel started. The prototype did all of
    this at import, which is why it could only ever be run, never examined.
    """
    global SOCK, AUDIT, SPOOL, MAX_INLINE, MAX_INLINE_BYTES, GRACE, CONFIRM
    global RECLAIM_AFTER, RECLAIM_CHECK_EVERY
    global SESSION, SHORT, STORE, _configured
    config = config or SupervisorConfig.from_env()
    SOCK, AUDIT, SPOOL = config.sock, config.audit, config.spool
    MAX_INLINE, MAX_INLINE_BYTES = config.max_inline, config.max_inline_bytes
    GRACE, CONFIRM = config.grace, config.confirm
    RECLAIM_AFTER = config.reclaim_after
    RECLAIM_CHECK_EVERY = config.reclaim_check_every
    last_activity[0] = time.time()

    SESSION = str(uuid.uuid4())          # 128 bits, stored in full
    SHORT = "S" + SESSION[:8]            # what humans read
    STORE = os.path.join(SPOOL, SESSION)     # R1 from two sessions must not collide
    os.makedirs(STORE, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(AUDIT)) or ".", exist_ok=True)
    open(AUDIT, "w").close()
    if not config.hold_before_activation:
        hold.set()
    _configured = True
    return config


provenance = {"wolfram_version": "?", "system_id": "?"}


def maintenance_eval(purpose, code, timeout=20):
    """Run an expression in the live kernel that is NOT scientific work.

    Recorded in full -- purpose, expression, kernel generation, duration,
    outcome -- because it really did execute in the kernel, and an audit trail
    that hides its own housekeeping cannot be checked. It mints no request id
    and no evaluation generation, which is what fixes the meaning of Vn: the
    evaluation generation counts scientific evaluations, not every
    EvaluatePacket the kernel ever received.
    """
    started = time.monotonic()
    try:
        value = kern[0].evaluate(code, timeout=timeout).strip()
    except Exception as exc:
        event("MAINTENANCE_EVALUATION", purpose=purpose, expression=code[:60],
              generation=f"K{kernel_gen[0]}", ms=f"{(time.monotonic()-started)*1000:.0f}",
              outcome=type(exc).__name__, scientific="no")
        raise
    event("MAINTENANCE_EVALUATION", purpose=purpose, expression=code[:60],
          generation=f"K{kernel_gen[0]}", ms=f"{(time.monotonic()-started)*1000:.0f}",
          outcome="ok", scientific="no")
    return value


def read_provenance():
    """Ask the kernel what it is, once, so every artifact can record its producer."""
    try:
        # $Version, not $VersionNumber: a reproducibility record wants the build
        # string ("15.0.1 for Linux x86 (64-bit) (...)"), not the float 15.
        v = maintenance_eval("kernel_provenance", 'StringJoin[$Version, "||", $SystemID]')
        ver, _, sysid = v.strip().strip('"').partition("||")
        provenance["wolfram_version"] = ver.strip()
        provenance["system_id"] = sysid.strip()
    except Exception:
        pass


kern = [None]   # started at the bottom, once event() exists

ledger, by_key = {}, {}
lock = threading.RLock()
counter, eval_gen, kernel_gen = [0], [0], [1]
active = [None]
faulted = [None]
faulted_code = [None]
readiness = ["READY"]          # READY | HEALTH_UNVERIFIED | QUARANTINED | DEAD
health_note = [""]
poison_pending = [False]       # test-only: see INJECT POISON_BEFORE_PROBE
timers = {}
artifacts = {}

hold = threading.Event()   # released by configure(); see SupervisorConfig


seq = [0]
seq_lock = threading.Lock()


def event(kind, rid=None, **payload):
    """One numbered line in the durable record.

    The sequence number is supervisor-global rather than per evaluation, so the
    log can be reordered into a single true history across requests, kernel
    restarts and client disconnects.
    """
    with seq_lock:
        seq[0] += 1; n = seq[0]
    tok = ledger.get(rid, {}).get("token") if rid else None
    ident = f"{SESSION}/{tok}" if tok else (f"{SESSION}/-/-/{rid}" if rid else SESSION)
    body = " ".join(
        f'{k}="{str(v)}"' if isinstance(v, str) and " " in v else f"{k}={v}"
        for k, v in payload.items())
    with open(AUDIT, "a") as fh:
        fh.write(f"#{n} {time.time():.6f} {ident} {kind} {body}\n")
        fh.flush(); os.fsync(fh.fileno())


def fingerprint(code, bytes_mode, timeout, policy):
    """Everything that changes what the kernel will be asked to do.

    Versioned, so that adding a field later cannot make an old key match a new
    request by accident. Correlation is deliberately absent: it changes the
    record, not the execution, and is compared on its own.
    """
    canonical = (f"{FINGERPRINT_VERSION}\n"
                 f"source_sha256={hashlib.sha256(code.encode()).hexdigest()}\n"
                 f"result_mode={'bytes' if bytes_mode else 'text'}\n"
                 f"deadline={timeout if timeout else 'none'}\n"
                 f"recovery_policy={policy}\n")
    return hashlib.sha256(canonical.encode()).hexdigest()


def audit(*p):
    """Compatibility shim: positional lines become an event of the same kind."""
    parts = [str(x) for x in p]
    rid = parts[0] if parts and re.fullmatch(r"E\d+", parts[0]) else None
    kind = parts[1] if rid else parts[0]
    rest = parts[2:] if rid else parts[1:]
    payload, extra = {}, []
    for r in rest:
        k, eq, v = r.partition("=")
        (payload.update({k: v}) if eq else extra.append(r))
    if extra: payload["detail"] = " ".join(extra)
    event(kind, rid, **payload)


def fault(code, detail=""):
    """Latch a typed fault. The category, not the wording, drives authorisation."""
    if faulted[0] is None:
        faulted[0] = f"{code}: {detail}" if detail else code
        faulted_code[0] = code
        event("FAULT", code=code, category=FAULT_CATEGORY.get(code, "unknown"),
              detail=detail.replace(" ", "_") or "-")


def verify():
    """Invariants a snapshot can still contradict. Caller holds the lock."""
    running = [i for i, e in ledger.items() if e["state"] == "RUNNING"]
    if len(running) > 1:
        return "MULTIPLE_RUNNING", f"more than one RUNNING: {running}"
    if active[0] and not running:
        return "TOKEN_ACTIVE_WITHOUT_RUNNING", f"token {active[0]} is active with nothing RUNNING"
    if running and active[0] != ledger[running[0]]["token"]:
        return "TOKEN_MISMATCH", f"active token {active[0]} does not name the RUNNING request {running[0]}"
    for i, e in ledger.items():
        terminal = e["state"] in TERMINAL
        if bool(e.get("terminal_at")) != terminal:
            return "TERMINAL_MARK_MISMATCH", f"{i}: terminal_at present={bool(e.get('terminal_at'))} but state={e['state']}"
        if terminal and e["terminal_at"] < e["accepted_at"]:
            return "TERMINAL_BEFORE_START", f"{i}: terminal_at precedes accepted_at"
        if e["state"] == "CANCELLED" and e["token"]:
            return "CANCELLED_WITH_TOKEN", f"{i}: CANCELLED but carries token {e['token']}"
        if e["state"] in ACTIVATED and not e["token"]:
            return "ACTIVATED_WITHOUT_TOKEN", f"{i}: {e['state']} without a token"
        if e["state"] == "COMPLETED" and e["result"] is None:
            return "COMPLETED_WITHOUT_RESULT", f"{i}: COMPLETED with no result"
        if e["state"] == "RUNNING" and e["result"] is not None:
            return "RUNNING_WITH_RESULT", f"{i}: RUNNING but already carries a result"
    return None


def transition(rid, new, **fields):
    """The ONLY production path that changes a request's state."""
    with lock:
        e = ledger[rid]
        if (e["state"], new) not in LEGAL:
            fault("ILLEGAL_TRANSITION", f"{e['state']} -> {new} for {rid}"); return False
        e["state"] = new
        e["terminal_at"] = time.time() if new in TERMINAL else None
        e.update(fields)
        bad = verify()
        if bad:
            fault(*bad); return False
    audit(rid, new, *(f"{a}={b}" for a, b in fields.items() if a != "result"))
    return True


def activate(rid):
    """The linearization point: before it the kernel has not seen the request."""
    with lock:
        e = ledger[rid]
        if e["cancel_requested"]:
            transition(rid, "CANCELLED", execution="CANCELLED",
                       result="cancelled before dispatch; no Mathematica-side "
                              "execution of this request occurred")
            return None
        eval_gen[0] += 1
        token = f"K{kernel_gen[0]}/V{eval_gen[0]}/{rid}"
        # One sentinel per evaluation, 128 bits, structural rather than a bare
        # string: user output can carry the same head or a lookalike string and
        # still not be mistaken for it.
        e["sentinel_id"] = str(uuid.uuid4())
        # The text path compares against the expression; the byte path cannot --
        # embedding an expression containing quotes inside StringToByteArray["..."]
        # is broken Wolfram syntax. Keep the bare id for that path.
        e["sentinel"] = f'MCPAbortSentinel["{e["sentinel_id"]}"]'
        e["artifact_id"] = f"R{eval_gen[0]}"
        event("SENTINEL_MINTED", rid,
              sentinel=e["sentinel"].split('"')[1], artifact=e["artifact_id"])
        active[0] = token
        transition(rid, "RUNNING", token=token, activated_at=time.time(),
                   responsiveness="RESPONSIVE")
        if e["timeout"]:
            e["deadline"] = time.time() + e["timeout"]
            t = threading.Timer(e["timeout"], deadline_reached, args=(token, e["timeout"]))
            t.daemon = True; timers[rid] = t; t.start()
        return token


def kernel_busy():
    """Ask the transport, not the ledger, whether an evaluation is in flight.

    This reaches past the public API on purpose: the shipped Kernel has no
    predicate for it, and the whole point here is a second opinion that does not
    come from the state we already suspect.
    """
    lk = kern[0]._eval_lock
    if lk.acquire(blocking=False):
        lk.release(); return False
    return True


def abort_authorised(token):
    """May this abort be issued? Returns None if yes, else the reason it is not."""
    if active[0] is None:
        return "no active evaluation: nothing to abort"
    if token != active[0]:
        return f"token {token or '(none given)'} is not active ({active[0]})"
    if faulted[0]:
        if FAULT_CATEGORY.get(faulted_code[0]) == "identity":
            return (f"supervisor is faulted on identity state, so the token cannot "
                    f"authorise anything: {faulted[0]}")
        if not kernel_busy():
            return "supervisor is faulted and the kernel reports nothing in flight"
    return None


def request_abort(token, cause):
    """The only way an abort is ever issued.

    Issues and returns. Whether it worked is observed elsewhere -- by the worker,
    which reports the terminal outcome, and by check_landed, which reports the
    absence of one.
    """
    with lock:
        why = abort_authorised(token)
        if why:
            return f"REFUSED {why}"
        rid = token.rsplit("/", 1)[1]
        e = ledger[rid]
        if e["control"]["requested"]:
            return f"ALREADY_REQUESTED {token} cause={e['control']['cause']}"
        e["control"] = dict(requested=True, cause=cause)
        e["abort_confirmation"] = "PENDING"
    audit(rid, "ABORT_ISSUED", f"token={token}", f"cause={cause}")
    ok = kern[0].abort(wait=CONFIRM)
    with lock:
        if ok:
            e["abort_confirmation"] = "CONFIRMED"
    if not ok:
        t = threading.Timer(GRACE, check_landed, args=(token,)); t.daemon = True; t.start()
    return (f"ABORT_ISSUED {token} cause={cause} "
            f"confirmation={'CONFIRMED' if ok else 'PENDING'}")


def check_landed(token):
    """Report what is true at the end of the observation window. Change nothing else."""
    rid = token.rsplit("/", 1)[1]
    with lock:
        if active[0] != token:
            ledger[rid]["abort_confirmation"] = "CONFIRMED"
            audit(rid, "ABORT_LANDED", f"token={token}", f"within={GRACE}s")
            return
        e = ledger[rid]
        e["abort_confirmation"] = "UNCONFIRMED"
        e["responsiveness"] = "UNRESPONSIVE"
        policy = e["policy"]
    audit(rid, "ABORT_UNCONFIRMED", f"token={token}", f"after={GRACE}s",
          f"policy={policy}")
    if policy == "RESTART_IF_UNRESPONSIVE":
        forced_restart(token, rid)


def forced_restart(token, rid):
    """Destroy the laboratory to recover control, and say so in those words.

    Order matters. The request is terminalised BEFORE anything is torn down,
    because the evaluation worker is about to be woken by a dead link and would
    otherwise record this as an ordinary transport failure -- which is a true
    sentence about the link and a false one about what happened.
    """
    with lock:
        if active[0] != token:
            return
        t = timers.pop(rid, None)
        if t: t.cancel()
        active[0] = None
        # Not ABORTED: no abort was ever confirmed. The evaluation's own outcome
        # is unknown and stays unknown.
        transition(rid, "FAILED", execution="UNKNOWN",
                   failure_cause="FORCED_KERNEL_RESTART",
                   responsiveness="UNKNOWN",
                   result="kernel destroyed by recovery policy; the evaluation's "
                          "own outcome was never observed")
        old = kern[0]
    audit(rid, "FORCED_KERNEL_RESTART", f"token={token}",
          f"pid={old.pid}", "live_session_state=DESTROYED")

    pid = old.pid
    try:
        old.close(grace=1.0)
    except Exception as exc:
        audit(rid, "RECOVERY_CLOSE_FAILED", f"pid={pid}", f"{type(exc).__name__}")
    if not _dead(pid):
        try: os.kill(pid, 9)
        except OSError: pass
    audit(rid, "OLD_KERNEL_DEAD" if _dead(pid) else "OLD_KERNEL_SURVIVED", f"pid={pid}")

    try:
        new = Kernel(); new.start()
    except Exception as exc:
        # Recovery that cannot recover is a fault, not a silently dead thread.
        fault("RECOVERY_FAILED", f"could not start a replacement kernel: {type(exc).__name__}")
        return
    with lock:
        kern[0] = new
        kernel_gen[0] += 1
    read_provenance()
    audit(rid, "KERNEL_RESTART", f"generation=K{kernel_gen[0]}", f"pid={new.pid}")


def _dead(pid):
    """Zombies are not alive. /proc existence alone would say otherwise."""
    try:
        st = open(f"/proc/{pid}/stat").read()
    except OSError:
        return True
    return st[st.rindex(")") + 2:].split()[0] == "Z"


def deadline_reached(token, seconds):
    audit(token.rsplit("/", 1)[1], "DEADLINE", f"after={seconds}s")
    request_abort(token, "TIMEOUT")


ARTIFACT_RE = re.compile(r'^MCPArtifact\["([^"]+)", (\d+), (\d+), "([0-9a-f]{64})"\]$')


def evaluate_bytes_mode(rid, code, sentinel, art):
    """Evaluate an expression yielding a ByteArray and keep the bytes exact.

    The abort sentinel has to survive the byte path too, so it is returned AS
    bytes: a reply that is not a byte list would leave the link in the error
    state that a notebook replay has already been lost to once.
    """
    wrapped = ("Normal[CheckAbort[With[{mcpR = (" + code + ")},"
               " If[Head[mcpR] === ByteArray, mcpR,"
               ' StringToByteArray["MCP$NOT$BYTEARRAY:" <> ToString[Head[mcpR]]]]],'
               f' StringToByteArray["{sentinel}"]]]')
    try:
        raw = kern[0].evaluate_bytes(wrapped, timeout=3600)
    except EvaluationAborted:
        return "ABORTED", "INTERNAL_OR_EXTERNAL", "$Aborted", None
    except EvaluationTimeout as exc:
        return ("ABORTED" if exc.aborted_cleanly else "UNKNOWN"), None, str(exc)[:90], None
    except KernelError as exc:
        alive = kern[0].is_alive()
        return ("FAILED" if alive else "KERNEL_LOST"), None, f"{type(exc).__name__}: {str(exc)[:70]}", None
    except Exception as exc:
        return "FAILED", None, f"{type(exc).__name__}: {str(exc)[:70]}", None

    if raw == sentinel.encode():
        return "ABORTED", "INTERNAL", "an abort occurred inside the evaluation (sentinel returned)", None
    if raw.startswith(b"MCP$NOT$BYTEARRAY:"):
        return "FAILED", None, raw.decode("utf-8", "replace"), None

    with lock:
        ledger[rid]["result_kind"] = ("INLINE_BYTES" if len(raw) <= MAX_INLINE_BYTES
                                      else "BYTES_ARTIFACT")
    if len(raw) <= MAX_INLINE_BYTES:
        return "COMPLETED", None, __import__("base64").b64encode(raw).decode(), None
    tmp = os.path.join(STORE, f"{art}.{uuid.uuid4().hex[:8]}.bin.tmp")
    with open(tmp, "wb") as fh:
        fh.write(raw); fh.flush(); os.fsync(fh.fileno())
    return "COMPLETED", None, None, dict(
        tmp=tmp, byte_count=len(raw), file_bytes=len(raw),
        kernel_digest=hashlib.sha256(raw).hexdigest(), preview_tmp=None,
        rid=rid, token=ledger[rid]["token"], fmt="raw")


def evaluate(rid, code, sentinel, art):
    """Structured outcome, a result that may never cross the link, and the
    request's own output as numbered events."""
    stamp = uuid.uuid4().hex[:8]
    tmp = os.path.join(STORE, f"{art}.{stamp}.wxf.tmp")
    prev = os.path.join(STORE, f"{art}.{stamp}.preview.tmp")
    # Quiet+Check on the preview only: a preview that will not format must never
    # cost a scientific result that serialised and verified correctly.
    wrapped = (
        "CheckAbort[Module[{mcpR = (" + code + "), mcpN}, mcpN = ByteCount[mcpR];"
        f' If[mcpN > {MAX_INLINE},'
        f' Export["{tmp}", mcpR, "WXF"];'
        f' Quiet[Check[Export["{prev}", ToString[Short[mcpR, 3], InputForm], "Text"], $Failed]];'
        f' MCPArtifact["{tmp}", mcpN, FileByteCount["{tmp}"],'
        f' FileHash["{tmp}", "SHA256", "HexString"]],'
        " mcpR]], " + sentinel + "]")
    try:
        reply = kern[0].evaluate_detailed(wrapped, timeout=3600)
    except EvaluationAborted:
        return "ABORTED", "INTERNAL_OR_EXTERNAL", "$Aborted", None
    except EvaluationTimeout as exc:
        return ("ABORTED" if exc.aborted_cleanly else "UNKNOWN"), None, str(exc)[:90], None
    except KernelError as exc:
        alive = kern[0].is_alive()
        return ("FAILED" if alive else "KERNEL_LOST"), None, f"{type(exc).__name__}: {str(exc)[:70]}", None
    except Exception as exc:
        return "FAILED", None, f"{type(exc).__name__}: {str(exc)[:70]}", None

    # One ordered list, so the interleaving is the kernel's own and output_index
    # means the same thing across both kinds.
    for i, e in enumerate(reply.events):
        if e["kind"] == "print":
            event("PRINT", rid, output_index=i, text=e["text"][:200])
        else:
            event("MESSAGE", rid, output_index=i, name=e.get("name", "?"),
                  symbol=e.get("symbol", "?"), tag=e.get("tag", "?"),
                  text=(e.get("text", "") or "")[:200])
    with lock:
        ledger[rid]["prints"] = len(reply.prints)
        ledger[rid]["messages"] = [m.get("name", "?") for m in reply.messages]
        ledger[rid]["output_shape"] = [
            "print" if e["kind"] == "print" else e.get("name", "?") for e in reply.events]

    value = reply.value.strip()
    if value == sentinel:
        return "ABORTED", "INTERNAL", "an abort occurred inside the evaluation (sentinel returned)", None
    m = ARTIFACT_RE.match(value)
    if m:
        return "COMPLETED", None, None, dict(
            tmp=m.group(1), byte_count=int(m.group(2)),
            file_bytes=int(m.group(3)), kernel_digest=m.group(4),
            preview_tmp=prev, rid=rid, token=ledger[rid]["token"])
    return "COMPLETED", None, value, None


def publish_artifact(art, info):
    """Verify the digest ourselves, then publish by rename.

    ByteCount is an operational threshold and nothing else -- it decides whether
    to spool. The authoritative size is the WXF file's own byte count, and the
    authoritative identity is the hash of those exact bytes. Neither is a claim
    about mathematical sameness across Wolfram versions: a different
    serialisation SHOULD hash differently, because it is a different artifact.
    """
    h = hashlib.sha256()
    try:
        with open(info["tmp"], "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
            fh.flush()
            os.fsync(fh.fileno())      # durable before it is reachable by name
    except OSError as exc:
        return None, f"artifact {art} could not be read back: {exc}"
    mine = h.hexdigest()
    if mine != info["kernel_digest"]:
        fault("ARTIFACT_DIGEST_MISMATCH",
              f"{art}: kernel reported {info['kernel_digest'][:12]}, file hashes {mine[:12]}")
        return None, f"artifact {art} digest mismatch"
    if os.environ.get("SUP_NO_PUBLISH") == "1":
        return None, f"artifact {art} was written but not published (simulated crash)"

    fmt = info.get("fmt", "WXF")
    final = os.path.join(STORE, f"{art}." + ("bin" if fmt == "raw" else "wxf"))
    with lock:
        producer = dict(supervisor_session=SESSION,
                        kernel_generation=f"K{kernel_gen[0]}",
                        token=info.get("token"), request_id=info.get("rid"),
                        )
        asserted = dict(ledger.get(info.get("rid"), {}).get("correlation") or {})
    meta = dict(artifact=art, format=fmt, sha256=mine,
                file_bytes=info["file_bytes"], byte_count_hint=info["byte_count"],
                wolfram_version=provenance["wolfram_version"],
                system_id=provenance["system_id"],
                producer=producer,
                # What the caller said, kept apart from what we know. A buggy or
                # careless caller can label a result with the wrong cell; the
                # record should show that it was told so, not that it is true.
                caller_asserted=dict(correlation=asserted),
                preview_is_not_evidence=True,
                # kept flat as well, because an existing reader looks here
                supervisor_session=SESSION, kernel_generation=f"K{kernel_gen[0]}")
    preview = ""
    pv_final = os.path.join(STORE, f"{art}.preview.txt")
    try:
        if info.get("preview_tmp") and os.path.exists(info["preview_tmp"]):
            os.replace(info["preview_tmp"], pv_final)
            preview = open(pv_final).read().strip()[:160]
        else:
            preview = "(preview unavailable)"
    except OSError:
        preview = "(preview unavailable)"
    meta["preview"] = preview

    mtmp = os.path.join(STORE, f"{art}.meta.json.tmp")
    with open(mtmp, "w") as fh:
        fh.write(__import__("json").dumps(meta, indent=1)); fh.flush(); os.fsync(fh.fileno())
    os.replace(mtmp, os.path.join(STORE, f"{art}.meta.json"))
    os.replace(info["tmp"], final)
    fd = os.open(STORE, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

    rec = dict(id=art, path=final, format=fmt, sha256=mine, file_bytes=info["file_bytes"],
               byte_count=info["byte_count"], preview=preview,
               wolfram_version=provenance["wolfram_version"],
               system_id=provenance["system_id"])
    artifacts[art] = rec
    event("RESULT_ARTIFACT", artifact=art, format=fmt, sha256=mine,
          file_bytes=info["file_bytes"], byte_count_hint=info["byte_count"],
          wolfram_version=provenance["wolfram_version"], system_id=provenance["system_id"])
    return rec, None


STATE_OF = {"COMPLETED": "COMPLETED", "ABORTED": "ABORTED"}


def run_request(rid, code):
    if not transition(rid, "DISPATCHING"):
        return
    hold.wait(60)
    if activate(rid) is None:
        return
    with lock:
        sentinel = ledger[rid]["sentinel"]; art = ledger[rid]["artifact_id"]
    if ledger[rid]["bytes_mode"]:
        execution, internal_cause, result, artifact = evaluate_bytes_mode(
            rid, code, ledger[rid]["sentinel_id"], art)
    else:
        execution, internal_cause, result, artifact = evaluate(rid, code, sentinel, art)
    failure_cause = None
    if artifact:
        rec, why = publish_artifact(art, artifact)
        if rec is None:
            execution, failure_cause, result = "FAILED", "ARTIFACT_PUBLISH_FAILED", why
        else:
            result = (f"artifact {rec['id']} format={rec['format']} sha256={rec['sha256'][:16]}... "
                      f"file_bytes={rec['file_bytes']} byte_count={rec['byte_count']} "
                      f"preview={rec['preview']}")
    with lock:
        if ledger[rid]["state"] != "RUNNING":
            return                      # recovery policy already terminalised it
        active[0] = None
        t = timers.pop(rid, None)
        if t and os.environ.get("SUP_NO_TIMER_CANCEL") != "1":
            t.cancel()
        e = ledger[rid]
        if internal_cause and not e["control"]["requested"]:
            e["control"] = dict(requested=False, cause=internal_cause)
        if e["control"]["requested"]:
            if execution == "COMPLETED":
                # Measured: user code with its own CheckAbort absorbs the
                # interrupt and returns its own value. Completing normally is
                # therefore not evidence the abort missed, and not evidence it
                # landed. Say exactly that.
                e["abort_confirmation"] = "NOT_OBSERVED_AT_BOUNDARY"
            elif e["abort_confirmation"] == "UNCONFIRMED":
                e["abort_confirmation"] = "CONFIRMED_LATE"
                event("ABORT_OBSERVED_LATE", rid, token=e["token"])
            elif e["abort_confirmation"] != "CONFIRMED":
                e["abort_confirmation"] = "CONFIRMED"
        fields = dict(execution=execution, result=result, responsiveness="RESPONSIVE",
                      artifact=(artifact and art) or None)
        if failure_cause:
            fields["failure_cause"] = failure_cause
        if execution == "KERNEL_LOST":
            fields.update(execution="UNKNOWN", failure_cause="KERNEL_LOST",
                          responsiveness="UNKNOWN")
        transition(rid, STATE_OF.get(execution, "FAILED"), **fields)
    # Outside the lock: the probe talks to the kernel, and the request is
    # already terminal and will not be touched again.
    verify_health_after(rid)


def abort_history_is_ambiguous(e):
    """Could this request's abort have landed somewhere we did not see?

    Only when one was asked for and no abort was ever observed to take effect.
    An ordinary completion had no abort at all; a confirmed or late-confirmed
    abort was observed doing its job. Neither leaves the laboratory in doubt.
    """
    return (e["control"]["requested"]
            and e["abort_confirmation"] in ("NOT_OBSERVED_AT_BOUNDARY", "UNCONFIRMED"))


def health_probe(rid):
    """Decide whether the laboratory is safe to reuse. Boring on purpose.

    Two round trips, not one: a single reply proves the kernel answered once,
    and the question is whether it will keep answering. Cheap, because this runs
    only on the rare ambiguous path.
    """
    event("KERNEL_HEALTH_CHECK_STARTED", rid, generation=f"K{kernel_gen[0]}",
          probe_kind="TRIVIAL_ROUNDTRIP", probes_planned=2)
    for attempt in (1, 2):
        try:
            value = maintenance_eval(f"health_probe_{attempt}", "1", timeout=10)
        except Exception as exc:
            event("KERNEL_HEALTH_CHECK_FAILED", rid, attempt=attempt,
                  probe_kind="TRIVIAL_ROUNDTRIP", reason=type(exc).__name__)
            return False, f"{type(exc).__name__} on probe {attempt}"
        if value != "1":
            event("KERNEL_HEALTH_CHECK_FAILED", rid, attempt=attempt,
                  probe_kind="TRIVIAL_ROUNDTRIP", reason="wrong_value")
            return False, f"probe {attempt} returned {value!r}"
    # Two round trips is defensive redundancy on a rare path, not a measured
    # requirement: no case so far has been caught by the second that the first
    # missed.
    event("KERNEL_HEALTH_CHECK_PASSED", rid, probes=2, probe_kind="TRIVIAL_ROUNDTRIP")
    return True, ""


def verify_health_after(rid):
    """Called once a request is terminal. Never changes the request.

    Only once terminal, and that matters: UNCONFIRMED also describes a request
    that is still running, and probing then would put a maintenance evaluation
    in a queue behind the very evaluation whose fate is in doubt. A running
    request stays BUSY_UNRESPONSIVE; readiness is decided after it ends.
    """
    with lock:
        e = ledger[rid]
        if not abort_history_is_ambiguous(e):
            return
        readiness[0] = "HEALTH_UNVERIFIED"
        policy = e["policy"]
    event("KERNEL_HEALTH_UNVERIFIED", rid, abort=ledger[rid]["abort_confirmation"])

    if poison_pending[0]:
        # Fault injection, and the only honest way to reproduce the hazard: an
        # abort delivered to a kernel that is not evaluating. expect_reply
        # bypasses the in-flight guard, which is what the unobservable transport
        # gap does in the wild. It happens HERE, after the request is terminal,
        # because that is when the real race would already have poisoned it.
        poison_pending[0] = False
        event("INJECTED_POISON", rid, method="abort_while_idle")
        kern[0].abort(wait=1, expect_reply=True)

    ok, why = health_probe(rid)
    if ok:
        with lock:
            readiness[0] = "READY"; health_note[0] = ""
        return

    with lock:
        readiness[0] = "QUARANTINED"
        health_note[0] = why
    event("KERNEL_QUARANTINED", rid, reason=why.replace(" ", "_"),
          note="the_request_outcome_is_unchanged")
    if policy == "RESTART_IF_UNRESPONSIVE":
        recover_quarantined_kernel(rid)


def recover_quarantined_kernel(rid):
    """Destroy and replace the laboratory, recording what that cost."""
    with lock:
        old = kern[0]
    event("FORCED_KERNEL_RESTART", rid, pid=old.pid, live_session_state="DESTROYED",
          cause="HEALTH_CHECK_FAILED")
    pid = old.pid
    try:
        old.close(grace=1.0)
    except Exception:
        pass
    if not _dead(pid):
        try: os.kill(pid, 9)
        except OSError: pass
    try:
        new = Kernel(); new.start()
    except Exception as exc:
        fault("RECOVERY_FAILED", f"could not replace a quarantined kernel: {type(exc).__name__}")
        with lock: readiness[0] = "DEAD"
        return
    with lock:
        kern[0] = new; kernel_gen[0] += 1; active[0] = None
        readiness[0] = "READY"; health_note[0] = ""
    read_provenance()
    event("KERNEL_RESTART", rid, generation=f"K{kernel_gen[0]}", pid=new.pid)


def kernel_state():
    # Asked first, because every other answer presupposes it. A kernel killed
    # from outside -- by an operator, by the OOM killer, by a signal to its
    # process group -- leaves every recorded state untouched, so readiness
    # still says READY and a running request still says RUNNING. Measured: a
    # kernel reduced to a zombie was reported READY, IDLE and RESPONSIVE, which
    # is precisely the accurate-in-form, false-in-substance report this layer
    # exists to refuse.
    if kern[0] is None:
        return "NO_KERNEL"
    if _dead(kern[0].pid):
        running = [i for i, e in ledger.items() if e["state"] == "RUNNING"]
        lost = f" lost={running[0]}" if running else ""
        return f"DEAD pid={kern[0].pid} generation=K{kernel_gen[0]}{lost}"
    if faulted[0]:
        return f"FAULTED {faulted[0]}"
    for i, e in ledger.items():
        if e["state"] == "RUNNING":
            busy = "BUSY" if e["owner_alive"] else "BUSY_ORPHANED"
            if e["responsiveness"] == "UNRESPONSIVE":
                busy = "BUSY_UNRESPONSIVE"
            left = f" left={e['deadline']-time.time():.0f}s" if e["deadline"] else ""
            return (f"{busy} {i} token={e['token']} "
                    f"elapsed={time.time()-e['activated_at']:.0f}s{left} "
                    f"abort={e['abort_confirmation']}")
    un = [i for i, e in ledger.items()
          if e["state"] in TERMINAL and not e["owner_alive"] and not e["claimed"]]
    return f"COMPLETED_UNCLAIMED {un[0]}" if un else "IDLE"


def serve(conn):
    owned = []
    with lock:
        clients[0] += 1
        last_activity[0] = time.time()
    try:
        f = conn.makefile("rw")
        for line in f:
            with lock:
                last_activity[0] = time.time()
            cmd, _, arg = line.strip().partition(" ")
            mark = (lambda s: f"UNTRUSTED[{s}]") if faulted[0] else (lambda s: s)
            fire = None
            if cmd == "SUBMIT":
                key, _, code = arg.partition(" ")
                timeout, policy, corr, bytes_mode = None, "PRESERVE", {}, False
                while True:
                    if code.startswith("t="):
                        head, _, code = code.partition(" "); timeout = float(head[2:])
                    elif code.startswith("p="):
                        head, _, code = code.partition(" "); policy = head[2:]
                    elif code.startswith("bytes="):
                        head, _, code = code.partition(" "); bytes_mode = head[6:] == "1"
                    elif code.startswith("corr="):
                        head, _, code = code.partition(" ")
                        # Opaque to us. The caller decides what parent/child mean;
                        # the supervisor stores and echoes them and interprets
                        # nothing, so notebook replay is one caller among several
                        # rather than a concept the execution core knows about.
                        for pair in head[5:].split(","):
                            k2, _, v2 = pair.partition(":")
                            if k2: corr[k2] = v2
                    else:
                        break
                bad_corr = None
                if len(corr) > CORR_MAX_PAIRS:
                    bad_corr = f"at most {CORR_MAX_PAIRS} correlation pairs ({len(corr)} given)"
                for k2, v2 in corr.items():
                    if len(k2) > CORR_MAX_KEY or len(v2) > CORR_MAX_VALUE:
                        bad_corr = f"correlation {k2[:20]}: key<={CORR_MAX_KEY}, value<={CORR_MAX_VALUE} chars"
                    elif not CORR_OK.match(k2) or (v2 and not CORR_OK.match(v2)):
                        bad_corr = f"correlation {k2[:20]}: only [A-Za-z0-9._:@+-] is accepted"
                with lock:
                    if bad_corr:
                        res = f"REFUSED {bad_corr}"
                    elif policy not in POLICIES:
                        res = f"REFUSED unknown policy {policy}"
                    elif readiness[0] != "READY":
                        res = (f"REFUSED kernel readiness is {readiness[0]}"
                               + (f": {health_note[0]}" if health_note[0] else ""))
                    elif faulted[0]:
                        res = f"REFUSED faulted: {faulted[0]}"
                    elif key in by_key:
                        prior = ledger[by_key[key]]
                        fp = fingerprint(code, bytes_mode, timeout, policy)
                        if prior["fingerprint"] != fp:
                            # A reconnecting caller may repeat a submission it is
                            # unsure was accepted. It must not be able to change
                            # what that submission was -- in any respect that
                            # changes the execution, not only the source.
                            differs = [name for name, a, b in (
                                ("source", prior["code_sha256"], hashlib.sha256(code.encode()).hexdigest()),
                                ("result_mode", prior["bytes_mode"], bytes_mode),
                                ("deadline", prior["timeout"], timeout),
                                ("recovery_policy", prior["policy"], policy)) if a != b]
                            res = (f"REFUSED key {key} was admitted with a different execution "
                                   f"request (differs: {','.join(differs) or 'canonical form'}; "
                                   f"held {prior['fingerprint'][:12]}, offered {fp[:12]})")
                        elif prior["correlation"] != corr:
                            # Same execution, different story about whose it is.
                            res = (f"REFUSED key {key} was admitted with different correlation "
                                   f"(held {','.join(f'{a}:{b}' for a, b in prior['correlation'].items()) or 'none'}; "
                                   f"offered {','.join(f'{a}:{b}' for a, b in corr.items()) or 'none'}); "
                                   "provenance is not rewritten by a repeat submission")
                        else:
                            res = f"{by_key[key]} (idempotent: already submitted)"
                    elif any(e["state"] in ("ACCEPTED", "DISPATCHING", "RUNNING")
                             for e in ledger.values()):
                        # Refusal is the contract, so the refusal has to be
                        # actionable: the caller's only options are to wait or to
                        # abort what is running, and it cannot choose between them
                        # without knowing what it would be destroying.
                        b = next(i for i, e in ledger.items()
                                 if e["state"] in ("ACCEPTED", "DISPATCHING", "RUNNING"))
                        be = ledger[b]
                        if be["state"] == "RUNNING":
                            left = (f" deadline_in={be['deadline']-time.time():.0f}s"
                                    if be["deadline"] else " deadline=none")
                            res = (f"REFUSED kernel busy: {b} token={be['token']} "
                                   f"elapsed={time.time()-be['activated_at']:.0f}s{left} "
                                   f"owner={'live' if be['owner_alive'] else 'gone'} "
                                   f"abort_with=\"ABORT {be['token']}\" "
                                   f"code_sha256={be['code_sha256'][:12]} "
                                   f"code_preview_not_evidence={be['code'][:60]}"
                                   f"{'...' if len(be['code']) > 60 else ''} "
                                   f"full_record=\"REQUEST {b}\"")
                        else:
                            res = (f"REFUSED kernel busy: {b} is {be['state']} "
                                   f"(not yet in the kernel; CANCEL {b} costs nothing)")
                    else:
                        counter[0] += 1; rid = f"E{counter[0]}"
                        ledger[rid] = dict(state="ACCEPTED", code=code, key=key,
                                           token=None, accepted_at=time.time(),
                                           activated_at=None, terminal_at=None,
                                           execution=None, result=None,
                                           control=dict(requested=False, cause=None),
                                           abort_confirmation="NONE",
                                           responsiveness="UNKNOWN",
                                           failure_cause=None, policy=policy,
                                           sentinel=None, sentinel_id=None,
                                           artifact_id=None, artifact=None,
                                           prints=0, messages=[], output_shape=[],
                                           bytes_mode=bytes_mode, result_kind="INLINE_TEXT",
                                           correlation=corr,
                                           code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                                           fingerprint=fingerprint(code, bytes_mode, timeout, policy),
                                           cancel_requested=False,
                                           timeout=timeout, deadline=None,
                                           owner_alive=True, claimed=False)
                        by_key[key] = rid; owned.append(rid); res = rid
                if res.startswith("E") and "idempotent" not in res:
                    audit(res, "ACCEPTED", "key=" + key, f"policy={policy}",
                          *([f"corr.{k2}={v2}" for k2, v2 in corr.items()]), code[:40])
                    threading.Thread(target=run_request, args=(res, code), daemon=True).start()
            elif cmd == "STATUS":
                with lock: res = kernel_state()
            elif cmd == "TOKEN":
                with lock: res = active[0] or "NONE"
            elif cmd == "LOOKUP":
                # Answer about an existing request. Never creates one: a client
                # asking "what became of my key?" must not be the thing that
                # makes it exist.
                with lock:
                    rid = by_key.get(arg)
                    if rid is None:
                        res = f"UNKNOWN key {arg} was never admitted"
                    else:
                        e = ledger[rid]
                        c = e["control"]
                        res = mark(
                            f"{arg} -> {rid} state={e['state']} "
                            f"token={e['token'] or 'none'} "
                            f"full={SESSION}/{e['token'] or 'none'} "
                            f"execution={e['execution']} "
                            f"control={'requested' if c['requested'] else 'none'}/{c['cause']} "
                            f"abort={e['abort_confirmation']} "
                            f"kind={e['result_kind']} artifact={e['artifact'] or 'none'} "
                            f"claimed={e['claimed']} "
                            f"correlation_asserted={','.join(f'{a}:{b}' for a, b in e['correlation'].items()) or 'none'}")
            elif cmd == "SHUTDOWN":
                # Deliberate release, with the same absolute protections the
                # reaper obeys: work in flight and an uncollected result are
                # never destroyed on request, only reported.
                with lock:
                    state = kernel_state()
                    if not state.startswith("IDLE"):
                        res = f"REFUSED {state}"
                    else:
                        event("SUPERVISOR_DOWN", cause="requested")
                        res = "STOPPING"
                        shutdown_requested.set()
            elif cmd == "RECLAIM":
                # Why this laboratory is being kept, in the reaper's own words.
                # A client deciding whether to leave work here should be able to
                # see the rule that governs it rather than infer it.
                with lock:
                    reason = reclaimable(discount_clients=1)
                    res = (f"RECLAIMABLE idle_for={time.time()-last_activity[0]:.0f}s"
                           if not reason else
                           f"KEPT {reason} after={RECLAIM_AFTER:.0f}s "
                           f"clients={clients[0] - 1}")
            elif cmd == "READINESS":
                with lock:
                    # Readiness is recorded when something happens TO the
                    # kernel. Nothing happens to this record when the kernel
                    # simply ceases to exist, so the process is checked here
                    # rather than trusted from the last thing we wrote down.
                    if kern[0] is not None and _dead(kern[0].pid):
                        if readiness[0] != "DEAD":
                            readiness[0] = "DEAD"
                            health_note[0] = "the kernel process is gone"
                            event("KERNEL_FOUND_DEAD", pid=kern[0].pid,
                                  generation=f"K{kernel_gen[0]}")
                    res = f"{readiness[0]} generation=K{kernel_gen[0]} pid={kern[0].pid}" + (
                        f" note={health_note[0]}" if health_note[0] else "")
            elif cmd == "SESSION":
                res = (f"{SESSION} short={SHORT} kernel_generation=K{kernel_gen[0]} "
                       f"pid={os.getpid()} kernel_pid={kern[0].pid} events={seq[0]}")
            elif cmd == "LEDGER":
                with lock: res = mark(" | ".join(
                    f"{i}:{v['state']}[{v['token'] or 'no-token'}]"
                    f"{'' if v['owner_alive'] else '(no owner)'}" for i, v in ledger.items()))
            elif cmd == "RESULT":
                with lock:
                    e = ledger.get(arg)
                    if not e:
                        res = "UNKNOWN"
                    else:
                        e["claimed"] = True
                        c = e["control"]
                        label = ("TIMED_OUT" if e["state"] == "ABORTED"
                                 and c["cause"] == "TIMEOUT" else e["state"])
                        res = mark(f"{label} execution={e['execution']} "
                                   f"control={'requested' if c['requested'] else 'none'}"
                                   f"/{c['cause']} abort={e['abort_confirmation']} "
                                   f"responsiveness={e['responsiveness']} "
                                   f"cause={e['failure_cause']} "
                                   f"token={e['token'] or 'none'} "
                                   f"full={SESSION}/{e['token'] or 'none'} "
                                   f"kind={e['result_kind']} "
                                   f"artifact={e['artifact'] or 'none'} "
                                   f"prints={e['prints']} "
                                   f"output={'>'.join(e['output_shape']) or 'none'} "
                                   f"correlation_asserted={','.join(f'{a}:{b}' for a, b in e['correlation'].items()) or 'none'} "
                                   f"messages={','.join(e['messages']) or 'none'} "
                                   f"result={e['result']}")
            elif cmd == "CANCEL":
                with lock:
                    e = ledger.get(arg)
                    if not e:
                        res = "UNKNOWN"
                    elif e["state"] in TERMINAL:
                        res = f"REFUSED {arg} already {e['state']}"
                    elif e["state"] == "RUNNING":
                        fire = e["token"]; res = None
                    else:
                        e["cancel_requested"] = True
                        res = f"CANCEL_REQUESTED {arg} (pre-activation: will not enter the kernel)"
                        audit(arg, "CANCEL_REQUESTED")
            elif cmd == "ABORT":
                fire = arg; res = None
            elif cmd == "REQUEST":
                with lock:
                    e = ledger.get(arg)
                    if not e:
                        res = f"UNKNOWN request {arg}"
                    else:
                        res = mark(
                            f"{arg} state={e['state']} token={e['token'] or 'none'} "
                            f"full={SESSION}/{e['token'] or 'none'} policy={e['policy']} "
                            f"timeout={e['timeout']} owner={'live' if e['owner_alive'] else 'gone'} "
                            f"correlation_asserted={','.join(f'{a}:{b}' for a, b in e['correlation'].items()) or 'none'} "
                            f"output={'>'.join(e['output_shape']) or 'none'} "
                            f"artifact={e['artifact'] or 'none'} "
                            f"code_sha256={e['code_sha256']} "
                            f"fingerprint={e['fingerprint']} "
                            f"code={e['code']}")
            elif cmd == "ARTIFACT":
                a = artifacts.get(arg)
                res = (f"{a['id']} {a['path']} format={a['format']} sha256={a['sha256']} "
                       f"file_bytes={a['file_bytes']} byte_count_hint={a['byte_count']} "
                       f"wolfram_version={a['wolfram_version']} system_id={a['system_id']}"
                       if a else f"UNKNOWN artifact {arg}")
            elif cmd == "RELEASE":
                hold.set(); res = "released"
            elif cmd == "RESTART":
                with lock:
                    busy = [i for i, e in ledger.items() if e["state"] == "RUNNING"]
                if busy:
                    res = f"REFUSED {busy[0]} is RUNNING"
                else:
                    kern[0].close(); kern[0] = Kernel(); kern[0].start(); read_provenance()
                    with lock:
                        kernel_gen[0] += 1; active[0] = None
                    audit("KERNEL_RESTART", f"generation=K{kernel_gen[0]}", f"pid={kern[0].pid}")
                    res = f"K{kernel_gen[0]} pid={kern[0].pid}"
            elif cmd == "INJECT" and os.environ.get("SUP_ALLOW_INJECT") == "1":
                kind, _, rid = arg.partition(" ")
                with lock:                      # deliberately bypasses transition()
                    if kind == "DOUBLE_RUNNING":
                        for i in list(ledger)[:2]: ledger[i]["state"] = "RUNNING"
                    elif kind == "CANCELLED_WITH_TOKEN":
                        ledger[rid]["state"] = "CANCELLED"; ledger[rid]["token"] = "K1/V99/" + rid
                    elif kind == "STALE_ACTIVE":
                        active[0] = "K1/V99/ghost"
                    elif kind == "POISON_BEFORE_PROBE":
                        poison_pending[0] = True
                    elif kind == "RESULTLESS_COMPLETED":
                        ledger[rid]["result"] = None      # not an identity fault
                    bad = verify()
                    if bad: fault(*bad)
                    res = (f"injected {kind}; verify={bad[0]}: {bad[1]}" if bad
                           else f"injected {kind}; verify=passed")
            else:
                res = "?"
            if fire is not None:
                res = mark(request_abort(fire, "USER"))
            f.write(res + "\n"); f.flush()
    finally:
        with lock:
            for rid in owned: ledger[rid]["owner_alive"] = False
            clients[0] -= 1
            last_activity[0] = time.time()
        conn.close()
        if shutdown_requested.is_set():
            # After the reply has been written and the socket closed: a client
            # that asked for a shutdown is entitled to hear that it was granted.
            with contextlib.suppress(Exception):
                if kern[0]:
                    kern[0].stop()
            os._exit(0)


# --- letting go of one ------------------------------------------------------

def reclaimable(now: float | None = None, discount_clients: int = 0) -> str:
    """Why this laboratory may not be released yet, or "" when it may be.

    Deliberately asks ``kernel_state`` rather than forming its own opinion: a
    reaper with a private idea of "idle" would eventually disagree with what
    STATUS tells a client, and the disagreement would show up as a kernel that
    vanished while something still needed it.

    The order matters. Work in flight and an unclaimed result are absolute -- no
    amount of elapsed time makes it acceptable to destroy either. Only once
    neither holds does the clock get a say.
    """
    now = now if now is not None else time.time()
    state = kernel_state()
    if state.startswith("DEAD") or state.startswith("NO_KERNEL"):
        # Nothing to protect and nothing to wait for. Holding a supervisor
        # whose kernel is gone keeps a socket alive that answers every question
        # with a corpse.
        return ""
    if not state.startswith("IDLE"):
        return state.split()[0]
    # ``discount_clients`` is how a client asks about a laboratory without its
    # own question being the answer: reaching this command means holding a
    # connection, so an asker that counted itself would always be told the
    # laboratory is in use, by itself.
    connected = clients[0] - discount_clients
    if connected > 0:
        return f"CLIENTS_CONNECTED {connected}"
    idle_for = now - last_activity[0]
    if idle_for < RECLAIM_AFTER:
        return f"IDLE_ONLY_{idle_for:.0f}S"
    return ""


def reaper(stop: threading.Event) -> None:
    """Release a kernel nobody is using, and say so before doing it.

    A kernel that is merely unused still holds definitions someone paid for, so
    this is slow by design: the default window is a day, long enough that a
    multi-day evaluation is never at risk from it and short enough that an
    abandoned session does not hold memory indefinitely.
    """
    while not stop.wait(RECLAIM_CHECK_EVERY):
        with lock:
            reason = reclaimable()
            if reason:
                continue
            event("RECLAIMING", idle_for=round(time.time() - last_activity[0]),
                  kernel=kern[0].pid if kern[0] else None)
        try:
            if kern[0]:
                kern[0].stop()
        except Exception as exc:                  # noqa: BLE001 - recorded, not hidden
            event("RECLAIM_FAILED", detail=f"{type(exc).__name__}: {exc}")
        event("SUPERVISOR_DOWN", cause="idle")
        os._exit(0)


# --- running one ------------------------------------------------------------

def serve_forever(config: SupervisorConfig | None = None) -> None:
    """Start the kernel, bind the socket, and accept clients until killed.

    Prints two lines a parent can wait on: the kernel's pid once it exists, and
    READY once the socket will answer. A caller that waits for READY is waiting
    for the thing it actually needs, rather than sleeping and hoping.
    """
    config = configure(config)

    if os.path.exists(SOCK):
        # A socket file left by a supervisor that is gone. Binding over it is
        # correct; binding over a LIVE one would silently steal its clients,
        # which is why lifecycle ownership is decided before this runs.
        os.unlink(SOCK)

    kern[0] = Kernel()
    kern[0].start()
    read_provenance()
    print(f"KERNEL {kern[0].pid}", flush=True)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    event("SUPERVISOR_UP", pid=os.getpid(), kernel=kern[0].pid,
          spool=SPOOL, max_inline=MAX_INLINE, socket=SOCK,
          reclaim_after=RECLAIM_AFTER)
    stop = threading.Event()
    if RECLAIM_AFTER > 0:
        threading.Thread(target=reaper, args=(stop,), daemon=True).start()
    print("READY", flush=True)
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    finally:
        stop.set()
        srv.close()
        with contextlib.suppress(OSError):
            os.unlink(SOCK)


def main() -> None:
    serve_forever()
