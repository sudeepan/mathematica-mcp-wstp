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

import os
import signal
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
            time.sleep(2.0)
            seen["during"] = sleepers()
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
