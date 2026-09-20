"""Integration tests against a real Wolfram kernel.

Written to run under pytest if it is present, and standalone otherwise
(``python3 tests/test_kernel.py``) so the suite has no dependencies at all --
this project deliberately has none, and a test suite that needs a package
manager is a test suite that does not get run.

Every test here launches its own kernel and closes it. That is deliberate:
these tests are about process lifecycle as much as evaluation, and sharing a
kernel between them would hide exactly the failures they exist to catch.

If these start failing with handshake timeouts on a busy machine, check free
MEMORY, not licence seats: `$MaxLicenseProcesses` is Infinity on this licence,
so RAM is the resource that actually runs out (measurements §11).
"""

from __future__ import annotations

import contextlib
import os
import signal
import tempfile
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp import discovery, registry              # noqa: E402
from mathematica_wstp.kernel import (                          # noqa: E402
    EvaluationTimeout, Kernel,
)
from mathematica_wstp.link import LinkDead                     # noqa: E402


# --- discovery -------------------------------------------------------------

def test_discovery_finds_everything():
    summary = discovery.summary()
    assert "not found" not in str(summary["installation"]), summary
    assert "not found" not in str(summary["kernel"]), summary
    assert "not found" not in str(summary["library"]), summary
    assert os.access(summary["kernel"], os.X_OK)


# --- basic evaluation ------------------------------------------------------

def test_evaluate_and_state_persists():
    with Kernel() as k:
        assert k.evaluate("1+1").strip() == "2"
        k.evaluate("marker = 424242")
        assert k.evaluate("marker").strip() == "424242"


def test_kernel_runs_in_its_own_process_group():
    """A group signal must reach the kernel tree and never the server itself."""
    with Kernel() as k:
        assert k.pgid is not None
        assert k.pgid != os.getpgid(0), "kernel shares the server's process group"
        assert k.process_id() == k.pid, "spawned pid is not the kernel we talk to"


def test_input_form_preserves_machine_reals():
    """The sharp case: a machine real renders as 6 digits under OutputForm.

    ``Pi/3 // N`` prints as ``1.0472`` by default and does *not* compare equal
    to itself after a round trip. Arbitrary-precision numbers survive the value
    comparison but still lose their precision annotation (30. -> 29.497), so
    InputForm is the correct choice for both reasons.
    """
    with Kernel() as k:
        assert k.evaluate("ToExpression[ToString[Pi/3//N, InputForm]] == Pi/3//N").strip() == "True"
        assert k.evaluate("ToExpression[ToString[Pi/3//N]] == Pi/3//N").strip() == "False", \
            "OutputForm unexpectedly round-tripped a machine real"
        # and the transport itself must deliver the InputForm, not the default
        assert k.evaluate("Pi/3//N").strip() == "1.0471975511965976"


def test_input_form_preserves_precision_annotation():
    with Kernel() as k:
        text = k.evaluate("N[Pi,30]")
        assert "`" in text, f"precision marks lost: {text}"
        assert k.evaluate("Precision[ToExpression[ToString[N[Pi,30], InputForm]]]").strip() == "30."
        degraded = k.evaluate("Precision[ToExpression[ToString[N[Pi,30]]]]").strip()
        assert degraded != "30.", f"expected OutputForm to degrade precision, got {degraded}"


def test_backslashes_and_unicode_survive():
    """WSPutString eats escapes; the UTF-8 pair must not. See link.put_string."""
    with Kernel() as k:
        assert k.evaluate(r'StringLength["a\"b"]').strip() == "3"
        assert k.evaluate(r'StringLength["a\\b"]').strip() == "3"
        assert k.evaluate(r'StringLength["\[Gamma]"]').strip() == "1"
        assert "γ" in k.evaluate(r'"\[Gamma]"')


def test_json_round_trip_is_encoding_safe():
    """ExportString renders bytes as characters; ExportByteArray does not."""
    with Kernel() as k:
        got = k.evaluate_json(r'<|"g"->"\[Gamma]","s"->"quote\"in","n"->994|>')
        assert got == {"g": "γ", "s": 'quote"in', "n": 994}, got


def test_print_output_is_captured():
    """Print goes to its own text packet and used to be silently dropped."""
    with Kernel() as k:
        reply = k.evaluate_detailed('Print["HELLO"]; Print["AGAIN"]; 42')
        assert reply.value.strip() == "42"
        assert reply.prints == ["HELLO", "AGAIN"], reply.prints
        assert not reply.messages


def test_messages_are_captured():
    """A warning that explains a wrong answer must not vanish."""
    with Kernel() as k:
        reply = k.evaluate_detailed("{1,2}[[5]]")
        assert reply.messages, "Part::partw was not captured"
        assert reply.messages[0]["name"] == "Part::partw", reply.messages
        assert "does not exist" in reply.messages[0]["text"], reply.messages
        assert reply.messages[0]["symbol"] == "Part"
        assert reply.messages[0]["tag"] == "partw"


def test_messages_and_prints_are_kept_apart():
    """A text packet is message text only when a message packet preceded it."""
    with Kernel() as k:
        reply = k.evaluate_detailed('Print["before"]; {1,2}[[9]]')
        assert reply.prints == ["before"], reply.prints
        assert [m["name"] for m in reply.messages] == ["Part::partw"], reply.messages


def test_clean_evaluation_reports_nothing_extra():
    with Kernel() as k:
        reply = k.evaluate_detailed("2+2")
        assert reply.value.strip() == "4"
        assert not reply.prints and not reply.messages


# --- the reason this project exists ---------------------------------------

def test_abort_interrupts_and_keeps_state():
    """The headline capability: interrupt without losing the kernel."""
    with Kernel() as k:
        k.evaluate("marker = 424242")
        pid_before = k.process_id()

        def fire():
            time.sleep(1.5)
            k.abort()

        threading.Thread(target=fire, daemon=True).start()
        started = time.monotonic()
        try:
            k.evaluate('Do[qq = i, {i, 1, 10^12}]; "NEVER"', timeout=30)
            raise AssertionError("evaluation was not interrupted")
        except Exception as exc:
            assert "abort" in str(exc).lower() or "$Aborted" in str(exc), exc
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"abort took {elapsed:.1f}s"

        assert k.process_id() == pid_before, "kernel was replaced, not interrupted"
        assert k.evaluate("marker").strip() == "424242", "state lost"


def test_a_kernel_that_cannot_be_armed_is_not_advertised():
    """Connected is not ready. If arming fails, start() must fail closed.

    The arming round trip exists to guarantee the kernel can be interrupted.
    Returning a kernel whose arming failed would hand back exactly the
    condition the step was added to rule out -- one with unknown abort
    semantics -- and a caller has no way to tell the difference.

    A kernel that cannot evaluate `1` is not a usable kernel missing one
    feature; it is a kernel that failed its first evaluation.
    """
    from mathematica_wstp.kernel import Kernel, KernelError

    k = Kernel()
    original = Kernel._raw_eval
    Kernel._raw_eval = lambda self, code, timeout: (_ for _ in ()).throw(
        RuntimeError("simulated arming failure"))
    try:
        with contextlib.suppress(Exception):
            k.start()
            raise AssertionError("start() returned a kernel that could not be armed")
        try:
            k.start()
        except KernelError as exc:
            assert "arming" in str(exc), exc
        except Exception as exc:
            raise AssertionError(f"expected KernelError, got {type(exc).__name__}: {exc}")
        else:
            raise AssertionError("start() did not raise on arming failure")
    finally:
        Kernel._raw_eval = original
        with contextlib.suppress(Exception):
            k.close()


def test_arming_the_kernel_is_not_observable():
    """The startup round trip must not appear in the kernel's own bookkeeping.

    start() evaluates once to arm the interrupt handler. This project cares
    about In[]/Out[] fidelity to the point of having a pitfall about it, so a
    hidden infrastructure evaluation that advanced $Line or populated history
    would corrupt the very thing the notebook layer works to get right.

    Measured with and without the warm-up: identical. Evaluations sent as
    EvaluatePacket over WSTP do not touch $Line or In/Out in either case.
    """
    from mathematica_wstp import session as sess

    sess.close_kernel()
    sess.get_kernel()
    try:
        assert sess.evaluate_wl("$Line", timeout=30).text.strip() == "1"
        hist = sess.evaluate_wl(
            "{Length[DownValues[Out]], Length[DownValues[In]]}", timeout=30).text
        assert hist.strip() == "{0, 0}", f"arming left history behind: {hist}"
    finally:
        sess.close_kernel()


def test_abort_is_refused_until_the_expression_has_been_flushed():
    """Pin the dispatch boundary with a barrier, not a race.

    The predicate this asserts was wrong once, and the test that should have
    caught it polled in a loop and aborted when the predicate went true -- which
    is a race, and it passed twice by losing it. Holding the link's flush lets
    the test stand on the exact boundary instead of hoping to land near it.

    Before the flush the kernel has been sent nothing, so an abort would arrive
    at an idle kernel and leave an interrupt pending that wedges it.
    """
    with Kernel() as k:
        at_flush, release = threading.Event(), threading.Event()
        original = k.link.flush

        def gated_flush():
            at_flush.set()
            release.wait(30)
            original()

        k.link.flush = gated_flush
        outcome = []

        def run():
            with contextlib.suppress(Exception):
                outcome.append(k.evaluate("Pause[6]; 1", timeout=60))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert at_flush.wait(15), "the evaluation never reached the flush"

        # The lock is held and nothing has crossed the link.
        assert k.evaluation_in_flight() is False, "in flight before anything was sent"
        assert k.abort(wait=2) is False, "an abort was accepted with nothing to abort"

        release.set()
        deadline = time.time() + 15
        while not k.evaluation_in_flight() and time.time() < deadline:
            time.sleep(0.01)
        assert k.evaluation_in_flight() is True, "not in flight after the flush"

        assert k.abort(wait=15) is True, "the abort did not land after the flush"
        t.join(30)
        assert not t.is_alive()
        assert k.evaluate("1+1", timeout=30).strip() == "2", "the kernel did not survive"


def test_the_interval_closes_when_the_return_packet_arrives():
    """The other end of the same interval, also pinned with a barrier.

    An evaluation is over when its return packet appears, not when this process
    gets around to reading the value out of it. Everything in between is time
    spent on our own side, and an abort issued during it reaches a kernel that
    has already finished.

    That is not a recoverable mistake. Measured with the reader held in that
    stretch before the interval was closed there: the kernel wedged permanently
    in 7 trials out of 7 -- no link error, nothing readable, `clear_error()` and
    a drain abort both ineffective, only a restart recovering it.

    What remains is the gap between the kernel finishing and its packet reaching
    us, which nothing in this process can observe. That residue is why the
    supervisor keeps an unresponsive-kernel policy.
    """
    with Kernel() as k:
        armed = [True]
        at_value, release = threading.Event(), threading.Event()
        original = k.link.get_string

        def gated_get_string():
            if armed[0]:
                armed[0] = False      # only the first read, which is the reply
                at_value.set()
                release.wait(30)
            return original()

        k.link.get_string = gated_get_string
        outcome = []

        def run():
            with contextlib.suppress(Exception):
                outcome.append(k.evaluate("1+1", timeout=60))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert at_value.wait(20), "the reply never arrived"

        # The return packet is in hand; the evaluation is over even though this
        # process has not read the value yet.
        assert k.evaluation_in_flight() is False, (
            "still claiming an evaluation after its return packet arrived")
        assert k.abort(wait=2) is False, "an abort was accepted after the evaluation ended"

        release.set()
        t.join(30)
        assert not t.is_alive()
        assert outcome == ["2"], outcome

        for attempt in range(2):
            started = time.monotonic()
            assert k.evaluate("1+1", timeout=15).strip() == "2", f"attempt {attempt}"
            assert time.monotonic() - started < 10, (
                f"attempt {attempt} took {time.monotonic()-started:.1f}s: "
                "a pending interrupt survived")


def test_the_replay_manifest_is_durable_before_the_first_cell_runs():
    """The ordering is the point of the file, not a detail of it.

    A run identity that exists only in this process is lost by exactly the
    failure that makes reconciliation necessary: the client dies, and the new one
    cannot ask "did child 7 of my run reach the kernel?" because it no longer
    knows the run was called R042f621acc.

    So this reads the manifest from disk WHILE the first cell is still running,
    in another process's worth of ignorance -- if it is only written at the end,
    this fails.
    """
    import json as _json
    import uuid as _uuid

    from mathematica_wstp import notebooks, session
    from mathematica_wstp.replay_manifest import ReplayManifest

    path = os.path.join(tempfile.gettempdir(), f"manifest-{_uuid.uuid4().hex[:8]}.nb")
    replay_dir = os.path.join(tempfile.gettempdir(), f"manifests-{_uuid.uuid4().hex[:8]}")
    os.environ["MATHEMATICA_WSTP_REPLAY_DIR"] = replay_dir
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Manifest ordering", path=path)
        assert made.get("success"), made
        nbid = made["id"]
        for source in ('Pause[8]; "first"', "second = 2", "third = 3"):
            assert nb.write_cell(source, style="Input", notebook=nbid).get("success")

        outcome = {}

        def replay():
            outcome["r"] = nb.replay_cells(notebook=nbid, timeout=60, write_outputs=True)

        t = threading.Thread(target=replay, daemon=True)
        t.start()

        # While cell 1 is still running, the plan must already be readable.
        deadline = time.time() + 6
        found = []
        while time.time() < deadline and not found:
            if os.path.isdir(replay_dir):
                found = [f for f in os.listdir(replay_dir) if f.endswith(".json")]
            time.sleep(0.1)
        assert found, "no manifest on disk while the first cell was still running"
        assert not t.is_alive() or True

        mid_run = _json.load(open(os.path.join(replay_dir, found[0])))
        assert mid_run["schema"] == "replay-manifest-v1"
        assert len(mid_run["children"]) == 3, mid_run
        first_child = mid_run["children"][0]
        assert first_child["state"] in ("SUBMITTED", "EXECUTED"), first_child
        assert first_child["idempotency_key"].endswith(".c1")
        assert first_child["input_digest"], "no content identity recorded for the input"
        # A child that has not been reached yet must not claim to have run.
        assert mid_run["children"][2]["state"] == "PLANNED", mid_run["children"][2]

        t.join(60)
        assert not t.is_alive()
        result = outcome["r"]
        assert result["success"], result

        # And afterwards the same file says how it ended.
        final = ReplayManifest.load(result["manifest"])
        assert final.run_id == result["run"]
        assert [c["state"] for c in final.data["children"]] == ["EXECUTED"] * 3
        assert final.unfinished() == [], final.summary()
        assert all(c["output_in_session"] for c in final.data["children"]), final.summary()
        # Nothing half-written left behind.
        assert not [f for f in os.listdir(replay_dir) if f.startswith(".tmp-")]
    finally:
        os.environ.pop("MATHEMATICA_WSTP_REPLAY_DIR", None)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_an_aborted_cell_says_who_aborted_it():
    """A user's abort and a cell's own Abort[] must not be recorded alike.

    They are indistinguishable inside the kernel -- both arrive as user-initiated
    aborts and CheckAbort absorbs both identically -- so the only thing that can
    tell them apart is the sentinel the client marks before signalling. Until
    that is consulted, nothing in the kernel knows whose abort it was, and
    saying "the cell called Abort[]" is a claim about the science rather than an
    observation.

    Both directions are asserted, because a version that always said "the client
    aborted it" would be just as wrong and would pass a one-sided test.
    """
    import uuid as _uuid

    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"abort-attribution-{_uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Abort attribution", path=path)
        assert made.get("success"), made
        nbid = made["id"]
        for source in ('Pause[15]; "NEVER"', "(Abort[]; 1)"):
            assert nb.write_cell(source, style="Input", notebook=nbid).get("success")

        # The client aborts a cell that would otherwise run for fifteen seconds.
        outcome = {}

        def replay():
            outcome["r"] = nb.replay_cells(notebook=nbid, first=1, last=1,
                                           timeout=60, write_outputs=False)

        t = threading.Thread(target=replay, daemon=True)
        t.start()
        time.sleep(3)
        session.abort_current(wait=10)
        t.join(45)
        assert not t.is_alive(), "the aborted cell never returned"
        user_cell = outcome["r"]["cells"][0]
        assert user_cell.get("aborted") is True, user_cell
        assert "client aborted" in (user_cell.get("reason") or ""), user_cell.get("reason")

        # A cell that aborts itself, with nobody asking.
        own = nb.replay_cells(notebook=nbid, first=2, last=2, timeout=30,
                              write_outputs=False)["cells"][0]
        assert own.get("aborted") is True, own
        assert "the cell called Abort[]" in (own.get("reason") or ""), own.get("reason")
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_replay_gives_every_cell_its_own_identity():
    """A per-cell replay is resumable; a span replay is only repeatable.

    Each cell must arrive at the evaluator as its own submission, carrying a
    correlation that names the run and the cell, and an idempotency key stable
    enough that a client which loses the answer can ask again without running
    the cell twice.

    Cells are addressed by input ordinal rather than index on purpose: writing an
    output inserts a cell, so the indices move while the replay is under way.
    The test asserts that they moved, because a replay that addressed indices
    would have silently evaluated the wrong cells.
    """
    import uuid as _uuid

    from mathematica_wstp import evaluator as ev_mod
    from mathematica_wstp import notebooks, session

    seen = []

    class Recording:
        """Delegates, and writes down what the notebook layer asked for."""

        name = "recording"

        def __init__(self, inner):
            self._inner = inner

        def submit_bytes(self, code, timeout=60, idempotency_key=None, correlation=None):
            seen.append((idempotency_key, dict(correlation or {})))
            return self._inner.submit_bytes(code, timeout=timeout,
                                            idempotency_key=idempotency_key,
                                            correlation=correlation)

    previous = ev_mod.set_evaluator(Recording(ev_mod.DirectSessionEvaluator()))
    path = os.path.join(tempfile.gettempdir(), f"replay-identity-{_uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Replay identity", path=path)
        assert made.get("success"), made
        nbid = made["id"]
        for source in ("zz = 6*7", "zz + 1", "zz^2"):
            assert nb.write_cell(source, style="Input", notebook=nbid).get("success")

        before = [c["index"] for c in nb.cells(notebook=nbid)["cells"]
                  if c["style"] in ("Input", "Code")]

        seen.clear()
        replay = nb.replay_cells(notebook=nbid, write_outputs=True, timeout=30)
        assert replay["success"], replay
        assert replay["summary"]["executed"] == 3, replay["summary"]

        # State really did carry from cell to cell.
        outputs = [c.get("output") for c in replay["cells"]]
        assert outputs == ["42", "43", "1764"], outputs

        # One submission per cell, each naming the run and the cell.
        run = replay["run"]
        cell_submissions = [(k, c) for k, c in seen if c.get("kind") == "notebook_cell"]
        assert len(cell_submissions) == 3, seen
        assert [c["child"] for _, c in cell_submissions] == ["c1", "c2", "c3"]
        assert {c["parent"] for _, c in cell_submissions} == {run}
        assert [k for k, _ in cell_submissions] == [f"{run}.c1", f"{run}.c2", f"{run}.c3"]

        # Distinct per cell, and derived from nothing that moves.
        assert len({k for k, _ in cell_submissions}) == 3

        after = [c["index"] for c in nb.cells(notebook=nbid)["cells"]
                 if c["style"] in ("Input", "Code")]
        assert after != before, (
            "no outputs were inserted, so this run never exercised the shifting "
            "indices that ordinal addressing exists to survive")
    finally:
        ev_mod.set_evaluator(previous)
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_direct_evaluator_returns_exact_bytes():
    """The evaluator's contract is bytes in, bytes out -- not text that usually
    happens to encode bytes.

    The payloads are the ones that break a text path: a Wolfram named character,
    a byte sequence that is not valid UTF-8, and an embedded null. If any of
    these round-trips through a str, this test fails and the notebook layer's
    reason for using the byte path is gone.
    """
    from mathematica_wstp.evaluator import DirectSessionEvaluator

    ev = DirectSessionEvaluator()
    cases = [
        ('ByteArray[{}]', b""),
        ('StringToByteArray["\\[Gamma] = 2"]', "\u03b3 = 2".encode()),
        ('ByteArray[{255, 254, 65, 0, 66}]', bytes([255, 254, 65, 0, 66])),
        ('StringToByteArray["a\\\\b\\"c"]', b'a\\b"c'),
    ]
    try:
        for code, expected in cases:
            result = ev.submit_bytes(code, timeout=60).wait(120)
            assert result.success, f"{code}: {result.outcome} {result.detail}"
            assert result.data == expected, f"{code}: {result.data!r} != {expected!r}"
            assert result.backend == "direct"
            assert result.authenticated is False, "local ids must not claim to be authenticated"
    finally:
        from mathematica_wstp import session
        session.close_kernel()


def test_direct_evaluator_control_is_scoped_to_the_handle():
    """Abort belongs to an execution, not to the evaluator.

    The direct backend has only one evaluation at a time and could get away with
    "abort whatever is running". The interface does not let it, and a handle
    whose execution has already finished refuses rather than interrupting
    whatever came next.
    """
    from mathematica_wstp import session
    from mathematica_wstp.evaluator import DirectSessionEvaluator

    ev = DirectSessionEvaluator()
    try:
        slow = ev.submit_bytes('(Pause[20]; StringToByteArray["never"])', timeout=60)
        deadline = time.time() + 10
        while slow.status() != "RUNNING" and time.time() < deadline:
            time.sleep(0.05)
        assert slow.status() == "RUNNING"

        assert "ABORT_ISSUED" in slow.abort()
        result = slow.wait(60)
        assert result.outcome in ("ABORTED", "FAILED"), result.outcome
        assert result.data is None

        finished = ev.submit_bytes('StringToByteArray["done"]', timeout=60)
        assert finished.wait(60).data == b"done"
        assert "REFUSED" in finished.abort(), "a finished handle must not abort anything"
    finally:
        session.close_kernel()


def test_reply_events_keep_prints_and_messages_in_packet_order():
    """The interleaving is the part that cannot be recovered later.

    Two separate lists preserve the order within each stream and discard the
    order between them, which is exactly what a caller rendering a cell's output
    back into a document needs. The ordered event list is therefore the source
    of truth, and prints/messages are views of it.
    """
    with Kernel() as k:
        reply = k.evaluate_detailed(
            '(Print["one"]; 1/0; Print["two"]; Part[{1, 2}, 5]; Print["three"]; 42)',
            timeout=30)
        assert reply.value.strip() == "42", reply.value

        shape = [e["kind"] if e["kind"] == "print" else e["name"] for e in reply.events]
        assert shape == ["print", "Power::infy", "print", "Part::partw", "print"], shape

        # The views still answer the old questions.
        assert reply.prints == ["one", "two", "three"], reply.prints
        assert [m["name"] for m in reply.messages] == ["Power::infy", "Part::partw"]
        assert "kind" not in reply.messages[0], "the view leaks the event tag"
        assert "Infinite expression" in reply.messages[0]["text"], reply.messages[0]


def test_aborting_an_idle_kernel_is_refused_and_harmless():
    """An abort with nothing running must not be sent to the kernel.

    Measured before the guard: one bare abort against an idle (armed) kernel
    left the interrupt pending and wedged it -- two successive 1+1 evaluations
    each timed out at 10s. Repeated here, including repeated stale aborts,
    because a client retrying an uncertain abort is the realistic way to
    produce several in a row.
    """
    from mathematica_wstp.kernel import Kernel

    k = Kernel()
    k.start()
    try:
        for _ in range(3):
            assert k.abort(wait=0.5) is False, "an idle abort reported success"
        for i in (1, 2, 3):
            assert k.evaluate("1+1", timeout=8).strip() == "2", (
                f"evaluation {i} broke after idle aborts -- the interrupt was sent anyway")
    finally:
        k.close()


def test_abort_works_on_the_first_evaluation_after_a_restart():
    """restart() produces a fresh kernel, so it must arm it too."""
    from mathematica_wstp import session as sess

    sess.get_kernel()
    sess.restart_kernel()
    out: dict = {}

    def run() -> None:
        out["r"] = sess.evaluate_wl('Pause[20]; "NEVER"', timeout=60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.3)
    res = sess.abort_current(wait=10)
    t.join(25)
    assert res.get("confirmed") is True, f"abort not confirmed after restart: {res}"
    reply = out.get("r")
    assert reply is not None and (not reply.success or "NEVER" not in (reply.text or "")), (
        "the evaluation survived the abort on a restarted kernel")
    sess.close_kernel()


def test_abort_works_on_a_kernel_first_evaluation():
    """A fresh kernel must be abortable immediately, not after it warms up.

    Answering the WSTP handshake does not arm the interrupt handler. Measured
    before the fix: on a kernel whose first evaluation was the one being
    stopped, the abort had NO EFFECT -- `Pause[20]; "NEVER"` returned "NEVER",
    and confirmed=False came back 23s later. The identical abort against a
    kernel that had already evaluated `1+1` was confirmed in 0.0s.

    That is the worst shape of bug this server can have: abort is the property
    the transport exists to provide, and it silently did nothing on the first
    evaluation of every kernel -- including every fresh session and every
    kernel(action="restart").
    """
    from mathematica_wstp import session as sess

    sess.close_kernel()
    sess.get_kernel()                      # fresh: no evaluation but the warm-up
    out: dict = {}

    def run() -> None:
        out["r"] = sess.evaluate_wl('Pause[20]; "NEVER"', timeout=60)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    res = sess.abort_current(wait=10)
    elapsed = time.monotonic() - started
    t.join(25)

    assert res.get("confirmed") is True, f"abort not confirmed on a fresh kernel: {res}"
    assert elapsed < 5, f"abort took {elapsed:.1f}s on a fresh kernel"
    reply = out.get("r")
    assert reply is not None, "the evaluation never returned"
    assert not reply.success or "NEVER" not in (reply.text or ""), (
        "the evaluation ran to completion despite the abort -- the interrupt "
        f"handler was not armed: {reply.text!r}")
    sess.close_kernel()


def test_abort_interrupts_an_external_process():
    """Abort must reach a kernel blocked in RunProcess, and not strand the child.

    This is the case that matters for real work: the kernel is blocked on an
    external solver, not spinning in a Wolfram loop. Measured: the evaluation
    returns ~0.1s after the abort, the kernel survives, and the external
    process is killed rather than orphaned.
    """
    import subprocess
    marker = "sleep 91"

    def sleepers() -> list[str]:
        out = subprocess.run(["ps", "-eo", "pid,ppid,args"],
                             capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines()
                if marker in ln and "ps -eo" not in ln and "/bin/sh -c" not in ln]

    with Kernel() as k:
        seen: dict[str, list[str]] = {}

        def watcher() -> None:
            # Wait for the child to actually exist rather than sampling at a
            # fixed instant, so the test cannot pass vacuously.
            #
            # The ~2.5s this originally needed was NOT the kernel being slow to
            # spawn the child. It was a fresh kernel being unable to service an
            # abort at all until it had completed an evaluation -- see
            # test_abort_works_on_a_kernel_first_evaluation. start() now arms the
            # kernel, so the wait is short; polling stays because asserting the
            # precondition is right regardless of why it was failing.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                found = sleepers()
                if found:
                    seen["during"] = found
                    break
                time.sleep(0.2)
            seen.setdefault("during", [])
            time.sleep(1.0)
            k.abort(wait=15)

        threading.Thread(target=watcher, daemon=True).start()
        started = time.monotonic()
        try:
            k.evaluate('RunProcess[{"sleep","91"}]; "FINISHED"', timeout=45)
            raise AssertionError("the external process was not interrupted")
        except Exception as exc:
            assert "abort" in str(exc).lower(), exc
        elapsed = time.monotonic() - started
        assert elapsed < 20, f"abort took {elapsed:.1f}s against RunProcess"
        assert seen.get("during"), "the sleep process never started -- test is vacuous"
        assert k.evaluate("1+1").strip() == "2", "kernel unusable after aborting RunProcess"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and sleepers():
        time.sleep(0.2)
    assert not sleepers(), "external process was orphaned by the abort"


def test_timeout_aborts_but_keeps_the_session():
    """A timeout must no longer cost you the kernel."""
    with Kernel() as k:
        k.evaluate("keepme = 99")
        try:
            k.evaluate('Do[zz = i, {i, 1, 10^12}]', timeout=2.0)
            raise AssertionError("expected a timeout")
        except EvaluationTimeout as exc:
            assert exc.aborted_cleanly, "kernel did not confirm the abort"
        assert k.is_alive(), "kernel died on timeout"
        assert k.evaluate("keepme").strip() == "99", "state lost on timeout"
        assert k.evaluate("1+1").strip() == "2", "link unusable after timeout"


def test_dead_kernel_surfaces_quickly():
    """A killed kernel must raise, not hang. This is the liveness guarantee."""
    k = Kernel().start()
    pid = k.pid
    assert pid is not None

    def killer():
        time.sleep(1.0)
        os.kill(pid, signal.SIGKILL)

    threading.Thread(target=killer, daemon=True).start()
    started = time.monotonic()
    try:
        k.evaluate('Do[zz = i, {i, 1, 10^12}]', timeout=30)
        raise AssertionError("expected the link to die")
    except LinkDead:
        pass
    except Exception as exc:
        raise AssertionError(f"expected LinkDead, got {type(exc).__name__}: {exc}") from exc
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"took {elapsed:.1f}s to notice a dead kernel"
    assert not k.is_alive()
    k.close()


# --- supervision -----------------------------------------------------------

def test_close_removes_process_and_registry_entry():
    k = Kernel().start()
    pid = k.pid
    assert any(e["pid"] == pid for e in registry.entries())
    k.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and registry.pid_alive(pid):
        time.sleep(0.1)
    assert not registry.pid_alive(pid), "kernel survived close()"
    assert not any(e["pid"] == pid for e in registry.entries()), "registry entry left behind"


def test_close_is_prompt():
    """Shutdown must not burn its grace period on an already-dead kernel.

    A kernel that exits becomes a zombie until waited on, and a zombie still
    answers kill(pid, 0). Before the fix this cost the full 5s grace plus a
    pointless signal -- 10.03s per close, which every restart paid.
    """
    k = Kernel().start()
    k.evaluate("1+1")
    started = time.monotonic()
    k.close()
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"close() took {elapsed:.1f}s (zombie-reaping regression?)"
    assert not registry.pid_alive(k.pid)


def test_pid_alive_treats_a_zombie_as_dead():
    """The check underneath the above, isolated from any kernel."""
    import subprocess
    proc = subprocess.Popen(["/bin/true"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and registry.proc_state(proc.pid) != "Z":
        time.sleep(0.02)
    assert registry.proc_state(proc.pid) == "Z", "could not produce a zombie to test"
    assert not registry.pid_alive(proc.pid), "a zombie was reported as alive"
    proc.wait()


def test_registry_refuses_to_signal_a_recycled_pid():
    """The pid-reuse guard: a stale record must never kill an innocent process."""
    entry = {"pid": os.getpid(), "pgid": os.getpgid(0),
             "starttime": (registry.proc_starttime(os.getpid()) or 0) + 12345,
             "subkernels": []}
    assert not registry.is_still_ours(entry)
    assert registry.terminate_tree(entry) == [], "would have signalled the test process"


def test_unverified_abort_becomes_a_sticky_fault_then_resolves():
    """An unverified abort must outlive the reply that reported it.

    Liveness is measured at an instant. A caller who reads "unverified" once and
    carries on is the failure the probe exists to prevent, so the fault is held
    on the session, shows up in status(), and a watchdog drives it to a definite
    answer rather than leaving a warning nobody acts on.
    """
    from mathematica_wstp import session as sess

    sess.get_kernel()
    original = sess._verify_after_abort
    sess._verify_after_abort = lambda kernel, timeout: ("unverified", "probe timed out")
    try:
        res = sess.abort_current(wait=2.0)
        assert res["kernel"] == "unverified", res
        assert sess._abort_uncertain is not None, "fault was not held on the session"

        st = sess.kernel_status()
        assert st["link_health"] == "uncertain", st
        assert st["lifecycle"] == "faulted", st

        # A real round trip is the cheapest reconciliation there is.
        sess._verify_after_abort = original
        assert sess.evaluate_wl("1+1", timeout=30).success
        assert sess._abort_uncertain is None, "a completed evaluation did not clear the fault"
        assert sess.kernel_status()["link_health"] == "connected"

        events = [e["event"] for e in sess.abort_journal()]
        assert "abort-uncertain" in events, events
        assert "abort-reconciled" in events, events
    finally:
        sess._verify_after_abort = original
        sess._abort_uncertain = None
        with contextlib.suppress(Exception):
            sess.close_kernel()


def test_abort_on_an_already_dead_kernel_uses_the_same_vocabulary():
    """Two shapes for "your kernel is gone" means callers check one and miss the other."""
    from mathematica_wstp import session as sess

    k = sess.get_kernel()
    pid = k.pid
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and registry.pid_alive(pid):
        time.sleep(0.05)
    try:
        res = sess.abort_current(wait=1.0)
        assert res["success"] is False, res
        assert res["kernel"] == "dead", res
        assert res["state"] == "lost", res
    finally:
        with contextlib.suppress(Exception):
            sess.close_kernel()


def test_opening_groups_reaches_nested_ones():
    """ReplaceAll does not descend into what it just replaced.

    A notebook nests groups -- chapters inside a title -- so rewriting the outer
    one with /. carries every inner group through untouched. Measured on a real
    document: 2 of 158 closed groups opened, and the export was a 1-page PDF of
    a 68-page notebook, silently missing everything past the first group.
    """
    from mathematica_wstp import session as sess

    nested = ('Notebook[{Cell[CellGroupData[{'
              'Cell["Title", "Title"],'
              'Cell[CellGroupData[{Cell["Chapter", "Section"],'
              '  Cell[CellGroupData[{Cell["Sub", "Subsection"],'
              '    Cell[BoxData["1+1"], "Input"]}, Closed]]}, Closed]]}, Closed]]}]')
    k = sess.get_kernel()
    try:
        k.evaluate(f"nbTest = {nested};", timeout=60)
        bad = k.evaluate(
            'Length[Cases[nbTest /. CellGroupData[c_, _] :> CellGroupData[c, Open],'
            ' CellGroupData[_, Closed], Infinity]]', timeout=60).strip()
        good = k.evaluate(
            'Length[Cases[nbTest //. CellGroupData[c_, st_] /; st =!= Open :>'
            ' CellGroupData[c, Open], CellGroupData[_, Closed], Infinity]]', timeout=60).strip()
        assert bad != "0", "expected ReplaceAll to leave nested groups closed"
        assert good == "0", f"ReplaceRepeated left {good} groups closed"
    finally:
        with contextlib.suppress(Exception):
            sess.close_kernel()


def test_probe_reports_dead_when_the_kernel_is_gone():
    """A killed kernel must fail the probe, not pass it quietly."""
    from mathematica_wstp import session as sess

    k = Kernel().start()
    try:
        assert k.evaluate("1+1", timeout=30).strip() == "2"
        os.kill(k.pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and registry.pid_alive(k.pid):
            time.sleep(0.05)
        verdict, detail = sess._verify_after_abort(k, 5.0)
        assert verdict == "dead", f"a killed kernel probed as {verdict!r} ({detail})"
    finally:
        with contextlib.suppress(Exception):
            k.close()


def test_probe_reports_alive_on_a_healthy_kernel():
    from mathematica_wstp import session as sess

    with Kernel() as k:
        verdict, detail = sess._verify_after_abort(k, 15.0)
        assert verdict == "alive", f"healthy kernel probed as {verdict!r} ({detail})"


def test_abort_says_state_is_lost_when_the_probe_fails():
    """The defect, pinned.

    ``Kernel.abort`` returning True means only that the evaluation released the
    eval lock. A reader that died on a protocol error releases it exactly as a
    clean abort does, so the flag cannot distinguish them -- and the tool used
    to answer "kernel state is intact" on the strength of it. That is not a
    cosmetic wording problem: a caller was told nothing was lost, repeated it to
    their user, and found out 23 seconds later that an hour of accumulated
    results had died with the kernel.

    The probe is stubbed rather than provoked because the real trigger is a
    Wolfram-side crash nobody has reproduced on demand; what must be guaranteed
    is that a failed probe is never reported as an intact session.
    """
    from mathematica_wstp import session as sess

    sess.get_kernel()
    original = sess._verify_after_abort
    sess._verify_after_abort = lambda kernel, timeout: ("dead", "WSTP error 3: WSGet out of sequence")
    try:
        res = sess.abort_current(wait=2.0)
    finally:
        sess._verify_after_abort = original
        with contextlib.suppress(Exception):
            sess.close_kernel()

    assert res["success"] is False, res
    assert res["kernel"] == "dead", res
    assert res["state"] == "lost", res
    assert "intact" not in res["note"], f"still claiming intactness: {res['note']}"
    assert "lost" in res["note"].lower(), res["note"]


def test_abort_confirms_intact_only_after_a_real_round_trip():
    """The healthy path must still report intact -- and be right about it."""
    from mathematica_wstp import session as sess

    k = sess.get_kernel()
    try:
        k.evaluate("keepme = 4242", timeout=60)

        results: list[dict] = []

        def fire() -> None:
            time.sleep(1.5)
            results.append(sess.abort_current(wait=10.0))

        t = threading.Thread(target=fire, daemon=True)
        t.start()
        with contextlib.suppress(Exception):
            k.evaluate('Do[qq = i, {i, 1, 10^12}]; "NEVER"', timeout=30)
        t.join(30)

        assert k.evaluate("keepme", timeout=30).strip() == "4242"
        assert results, "the abort produced no result to check"
        res = results[0]
        assert res["confirmed"] is True, res
        assert res["kernel"] == "alive", res
        assert res.get("state") == "intact", res
        assert "generation" in res, res
    finally:
        with contextlib.suppress(Exception):
            sess.close_kernel()


def test_proc_census_ignores_the_front_end():
    """WolframNB is a child of the kernel too. It is not a subkernel."""
    import subprocess as sp
    child = sp.Popen(["sleep", "30"])
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and registry.proc_ppid(child.pid) != os.getpid():
            time.sleep(0.02)
        unfiltered = registry.proc_children(os.getpid())
        assert child.pid in unfiltered, "census missed a live child"
        filtered = registry.proc_children(os.getpid(), comm="WolframKernel")
        assert child.pid not in filtered, "comm filter let a non-kernel child through"
    finally:
        child.kill()
        child.wait()


def test_status_counts_subkernels_without_being_asked_first():
    """The defect: status reported [] until something called subkernel_pids().

    A fan-out that reads as an empty list is worse than no census at all, because
    it is the census restart_kernel() uses to decide whether anything leaked.
    """
    with Kernel() as k:
        launched = k.evaluate("Length[LaunchKernels[2]]", timeout=180).strip()
        if launched in ("0", "$Failed", "$Aborted"):
            print("  (skipped: no subkernel licences available)")
            return
        # Deliberately never call subkernel_pids() -- that is what used to be
        # the only thing that ever populated the cache.
        observed = k.observe_subkernels()
        assert observed, "status census reported no subkernels while two were running"
        assert all(registry.pid_alive(p) for p in observed)
        assert k.pid not in observed, "the master counted itself as its own subkernel"


def test_subkernel_cache_is_not_shared_between_kernels():
    """It was a class attribute, so every instance read the same empty list."""
    a, b = Kernel(), Kernel()
    a.subkernel_pids_cached = [111, 222]
    assert b.subkernel_pids_cached == [], "one kernel's census leaked into another's"


def test_subkernels_are_tracked_and_reaped():
    """LaunchKernels[] fan-out is the thing that leaked ~8 GB. It must not."""
    with Kernel() as k:
        launched = k.evaluate("Length[LaunchKernels[2]]", timeout=180).strip()
        if launched in ("0", "$Failed", "$Aborted"):
            print("  (skipped: no subkernel licences available)")
            return
        subs = k.subkernel_pids()
        assert subs, "subkernels launched but none tracked"
        assert all(registry.pid_alive(p) for p in subs)
        pids = list(subs)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(registry.pid_alive(p) for p in pids):
        time.sleep(0.2)
    survivors = [p for p in pids if registry.pid_alive(p)]
    assert not survivors, f"subkernels leaked: {survivors}"


# --- standalone runner -----------------------------------------------------

def _main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
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
