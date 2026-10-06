"""Recorder tests through the real server tool functions and a live kernel.

These call the functions behind the MCP tools (server.evaluate,
server.notebooks, ...) in-process, so a failure can be injected into the
recorder and its consequence checked in the kernel itself: whether a symbol
was ever created is the evidence that science did or did not run.

Needs the `mcp` package and a Wolfram kernel:

    .venv/bin/python tests/test_recorder_server.py
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import threading
import types
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp import notebooks, server, session
from mathematica_wstp.evaluator import evaluate_json, evaluate_text


def payload(result) -> dict:
    data = result.structured_content
    if not isinstance(data, dict):
        raise AssertionError(f"no structured content in {result!r}")
    return data


def fresh_symbol(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8].upper()


def symbol_exists(name: str) -> bool:
    """Ask the kernel by string, so the question itself creates no symbol."""
    out = evaluate_text(f'Names["Global`{name}"]', timeout=30)
    assert out.success, out.error
    return out.text.strip() != "{}"


@contextlib.contextmanager
def recording_session(label: str):
    tmp = tempfile.mkdtemp(prefix=f"rec-srv-{label}-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(tmp, "ledgers")
    notebooks.reset_headless_notebooks()
    ctx = types.SimpleNamespace(tmp=tmp, path=os.path.join(tmp, f"{label}.nb"),
                                nbid=None, nb=None)
    try:
        made = payload(server.notebooks(action="create", title=f"Server {label}",
                                        path=ctx.path, record=True))
        assert made["success"] and made["recording"] is True, made
        ctx.made = made
        ctx.nbid = made["id"]
        ctx.nb = notebooks.get_headless_notebooks()
        yield ctx
    finally:
        nb = notebooks.get_headless_notebooks()
        with contextlib.suppress(Exception):
            if nb.has_active_recorder:
                nb.stop_recording(force=True)
        with contextlib.suppress(Exception):
            if ctx.nbid:
                nb.close(ctx.nbid)
        notebooks.reset_headless_notebooks()
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_blocks_dispatch_when_recording_fails():
    """A failed pre-dispatch record means the code never reaches the kernel."""
    with recording_session("gate") as ctx:
        probe = fresh_symbol("mcpGateProbe")
        real_call = ctx.nb._call_with_session

        def readback_fails(fn, *args, **kwargs):
            if fn == "MCPReadBack":
                return {"success": False, "error": "injected read-back failure"}
            return real_call(fn, *args, **kwargs)

        ctx.nb._call_with_session = readback_fails
        try:
            reply = payload(server.evaluate(f"{probe} = 123"))
        finally:
            del ctx.nb._call_with_session
        assert reply["success"] is False, reply
        assert "recording integrity" in reply["error"], reply
        assert not symbol_exists(probe), "science ran although recording failed"

        again = payload(server.evaluate(f"{probe} = 456"))
        assert again["success"] is False and "faulted" in again["error"], again
        assert not symbol_exists(probe), "science ran on a faulted recorder"


def test_concurrent_evaluates_get_distinct_ordered_records():
    """Two evaluate() calls at once get separate, ordered, verified records."""
    with recording_session("conc") as ctx:
        barrier = threading.Barrier(2)
        replies: dict[str, dict] = {}

        def run(key: str, code: str) -> None:
            barrier.wait()
            try:
                replies[key] = payload(server.evaluate(code))
            except Exception as exc:
                replies[key] = {"success": False, "exception": repr(exc)}

        threads = [threading.Thread(target=run, args=(k, c)) for k, c in
                   (("a", fresh_symbol("mcpConcA") + " = 1"),
                    ("b", fresh_symbol("mcpConcB") + " = 2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert replies.get("a", {}).get("success") and replies.get("b", {}).get("success"), replies

        rec = ctx.nb._recorder
        assert not rec.is_faulted, rec.ledger.data.get("fault")
        records = rec.ledger.records
        assert [r["seq"] for r in records] == [1, 2], records
        assert len({r["record_tag"] for r in records}) == 2
        assert all(r["disposition"]["execution_outcome"] == "COMPLETED" for r in records)
        assert rec._verify_full()["verified"]


def test_public_finalize_uses_recorder_finalizer():
    """notebooks(action="finalize") runs the recorder's fresh-kernel finalizer."""
    with recording_session("fin") as ctx:
        section = payload(server.evaluate("Setup", style="Section"))
        assert section["success"] and section["scientific"] is False, section
        assert payload(server.evaluate(fresh_symbol("mcpFinX") + " = 2 + 3"))["success"]
        slow = payload(server.evaluate("Pause[3]", timeout=0.5))
        assert slow["success"] is False and slow["timed_out"] is True, slow
        assert not slow.get("recording_faulted"), slow
        flags = [(c["style"], c["evaluatable"])
                 for c in ctx.nb.read_back(notebook=ctx.nbid)["cells"]]
        # create() writes an unstamped title; everything the recorder wrote is stamped
        assert flags == [("Title", None), ("Section", False),
                         ("Input", True), ("Input", False)], flags
        saved = payload(server.notebooks(action="save"))
        assert saved["success"], saved

        fin = payload(server.notebooks(action="finalize"))
        assert fin["success"], fin
        assert fin["structural_verification"]["verified"] is True, fin
        run_id = ctx.nb._recorder.run_id
        assert fin["run_id"] == run_id
        expected = os.path.splitext(ctx.path)[0] + f"-{run_id}-finalized.nb"
        assert fin["finalized_path"] == expected and os.path.exists(expected)

        refused = payload(server.evaluate(fresh_symbol("mcpAfterSeal") + " = 1"))
        assert refused["success"] is False and "sealed" in refused["error"], refused

        closed = payload(server.notebooks(action="close"))
        assert closed["success"] and closed.get("recording_released") is True, closed
        assert not ctx.nb.has_active_recorder
        ctx.nbid = None


def test_close_before_finalize_refused():
    """Closing the recording notebook early would silently drop the audit trail."""
    with recording_session("earlyclose") as ctx:
        assert payload(server.evaluate(fresh_symbol("mcpEarly") + " = 1"))["success"]
        closed = payload(server.notebooks(action="close"))
        assert closed["success"] is False and "before finalization" in closed["error"], closed
        assert ctx.nb.has_active_recorder
        stopped = payload(server.notebooks(action="stop_recording"))
        assert stopped["success"] is False, stopped
        forced = payload(server.notebooks(action="stop_recording", force=True))
        assert forced["success"] and forced["abandoned"] is True, forced


def test_dispatch_exception_faults_as_outcome_unknown():
    """If evaluation breaks after the record exists, the run is faulted, not guessed."""
    with recording_session("gap") as ctx:
        real_eval = server.evaluate_text

        def evaluate_then_break(code, timeout=60):
            real_eval(code, timeout=timeout)
            raise RuntimeError("link dropped before the reply was read")

        server.evaluate_text = evaluate_then_break
        try:
            reply = payload(server.evaluate(fresh_symbol("mcpGap") + " = 1"))
        finally:
            server.evaluate_text = real_eval
        assert reply["success"] is False and reply["outcome_unknown"] is True, reply
        assert reply["recording_faulted"] is True, reply
        assert reply["recording_fault"]["phase"] == "POST_DISPATCH", reply
        record = ctx.nb._recorder.ledger.records[0]
        assert "disposition" not in record, "no outcome may be invented"
        again = payload(server.evaluate(fresh_symbol("mcpGapNext") + " = 2"))
        assert again["success"] is False and "faulted" in again["error"], again


def test_outcome_exception_is_reported_in_reply():
    """A crash while storing the outcome is visible in the reply that ran the science."""
    from mathematica_wstp import recorder as recorder_mod
    with recording_session("outcome") as ctx:
        real_extract = recorder_mod.extract_disposition

        def broken(*args, **kwargs):
            raise ValueError("disposition axes unreadable")

        recorder_mod.extract_disposition = broken
        try:
            reply = payload(server.evaluate(fresh_symbol("mcpOutcome") + " = 3"))
        finally:
            recorder_mod.extract_disposition = real_extract
        assert reply["success"] is True and reply["output"].strip() == "3", reply
        assert reply["recording_faulted"] is True, reply
        assert reply["recording_fault"]["phase"] == "POST_EVAL", reply


def test_bad_explicit_finalize_target_rejected():
    """An explicit notebook name that does not resolve must not finalize anything."""
    with recording_session("badtarget") as ctx:
        fin = payload(server.notebooks(action="finalize", notebook="no-such-notebook"))
        assert fin["success"] is False and "not found" in fin["error"], fin
        assert ctx.nb.has_active_recorder
        assert "finalization" not in ctx.nb._recorder.ledger.data


def test_create_with_record_but_no_path_reports_failure():
    """create(record=True) without a path creates the notebook but not a recording."""
    tmp = tempfile.mkdtemp(prefix="rec-srv-nopath-")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(tmp, "ledgers")
    notebooks.reset_headless_notebooks()
    try:
        reply = payload(server.notebooks(action="create", title="No path", record=True))
        assert reply["success"] is True, reply
        assert reply["recording"] is False, reply
        assert "disk path" in reply["recording_error"], reply
        assert not notebooks.get_headless_notebooks().has_active_recorder
        notebooks.get_headless_notebooks().close(reply["id"])
    finally:
        notebooks.reset_headless_notebooks()
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_evaluate_reply_surfaces_post_eval_fault():
    """The same reply that ran the science reports that recording faulted."""
    with recording_session("surface") as ctx:
        real_eval = server.evaluate_text

        def evaluate_then_tamper(code, timeout=60):
            out = real_eval(code, timeout=timeout)
            cells = ctx.nb.read_back(notebook=ctx.nbid)["cells"]
            index = next(c["index"] for c in cells if c["record_tag"])
            ctx.nb._call_with_session("MCPReplaceCell", ctx.nbid, index, "mcpTampered = 0")
            return out

        server.evaluate_text = evaluate_then_tamper
        try:
            reply = payload(server.evaluate(fresh_symbol("mcpSurface") + " = 7"))
        finally:
            server.evaluate_text = real_eval
        assert reply["success"] is True and reply["output"].strip() == "7", reply
        assert reply["recording_faulted"] is True, reply
        assert reply["recording_fault"]["phase"] == "POST_EVAL", reply


def test_state_changing_tools_blocked_while_recording():
    """Tools that change kernel state outside the recorder are refused."""
    with recording_session("blocked") as ctx:
        name = fresh_symbol("mcpBlocked")
        via_get = fresh_symbol("mcpViaGet")
        via_render = fresh_symbol("mcpViaRender")
        via_verify = fresh_symbol("mcpViaVerify")
        attempts = {
            "evaluate_cells": lambda: server.evaluate_cells(index=0),
            "replay run": lambda: server.replay(action="run"),
            "vars set": lambda: server.vars(action="set", name=name, value="1"),
            "vars clear": lambda: server.vars(action="clear", name=name),
            "vars clear_all": lambda: server.vars(action="clear_all"),
            "vars get": lambda: server.vars(action="get", name=f"{via_get} = 5"),
            "kernel restart": lambda: server.kernel(action="restart"),
            "kernel stop": lambda: server.kernel(action="stop"),
            "kernel close_subkernels": lambda: server.kernel(action="close_subkernels"),
            "render expression": lambda: server.render(action="expression", code=f"{via_render} = 1"),
            "verify_derivation": lambda: server.verify_derivation([f"{via_verify} = 1", "1"]),
            "supervisor use": lambda: server.supervisor(action="use"),
            "supervisor use_direct": lambda: server.supervisor(action="use_direct"),
        }
        for label, attempt in attempts.items():
            reply = payload(attempt())
            assert reply["success"] is False, (label, reply)
            assert "blocked during integrity recording" in reply["error"], (label, reply)
        for probe in (name, via_get, via_render, via_verify):
            assert not symbol_exists(probe), probe
        from mathematica_wstp.evaluator import get_evaluator
        assert get_evaluator().name == "direct"
        assert ctx.nb.has_active_recorder and not ctx.nb._recorder.is_faulted


def test_named_characters_survive_real_save_and_finalize():
    """Cells written with \[...] escapes finalize although the front end saves glyphs."""
    with recording_session("named") as ctx:
        cells = [fresh_symbol("mcpElem") + " = (pz \\[Element] Reals) && (\\[Alpha] > 0)",
                 fresh_symbol("mcpRule") + " = {a \\[Rule] 1}"]
        for code in cells:
            reply = payload(server.evaluate(code))
            assert reply["success"] and not reply.get("recording_faulted"), reply
        saved = payload(server.notebooks(action="save"))
        assert saved["success"], saved
        on_disk = payload(server.read_notebook_file(path=ctx.path, mode="wolfram"))
        text = " ".join(c["source"] for c in on_disk["code"])
        assert "\\[Element]" not in text and "∈" in text, text

        fin = payload(server.notebooks(action="finalize"))
        assert fin["success"], fin
        assert fin["structural_verification"]["verified"] is True, fin


def test_finalize_accepts_messages_given_when_recorded():
    """Cells that gave messages when recorded finalize when they give the same again."""
    with recording_session("msgs") as ctx:
        infy = payload(server.evaluate(fresh_symbol("mcpInfy") + " = 1/0"))
        assert infy.get("message_names") == ["Power::infy"], infy
        part = payload(server.evaluate("{1, 2}[[5]]"))
        assert part.get("message_names") == ["Part::partw"], part
        clean = payload(server.evaluate(fresh_symbol("mcpClean") + " = 3"))
        assert clean["success"] and not clean.get("messages"), clean
        records = ctx.nb._recorder.ledger.records
        assert [r["message_names"] for r in records] == [
            ["Power::infy"], ["Part::partw"], []], records
        assert payload(server.notebooks(action="save"))["success"]

        fin = payload(server.notebooks(action="finalize"))
        assert fin["success"], fin
        assert fin["structural_verification"]["verified"] is True, fin
        assert [(m["seq"], m["messages"]) for m in fin["repeated_messages"]] == [
            (1, ["Power::infy"]), (2, ["Part::partw"])], fin


def test_finalize_refuses_message_not_given_when_recorded():
    """A cell that leaned on a definition made before recording fails in the fresh kernel."""
    warm = fresh_symbol("mcpWarm")
    assert evaluate_text(f"{warm} = True", timeout=30).success
    with recording_session("warm") as ctx:
        reply = payload(server.evaluate(f"If[TrueQ[{warm}], 1, 1/0]"))
        assert reply["success"] and not reply.get("messages"), reply
        assert payload(server.notebooks(action="save"))["success"]

        fin = payload(server.notebooks(action="finalize"))
        assert fin["success"] is False, fin
        assert fin["unexpected_messages"] == [
            {"seq": 1, "record_tag": ctx.nb._recorder.ledger.records[0]["record_tag"],
             "messages": ["Power::infy"], "recorded": []}], fin
        assert not ctx.nb._recorder.is_sealed


def cell_forms(path: str) -> list[list[str]]:
    """[style, head of the content] for every cell of the notebook file on disk."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    got = evaluate_json(f'<|"cells" -> Cases[Get["{escaped}"], '
                        'Cell[c_, s_String, ___] :> {s, ToString[Head[c]]}, Infinity]|>',
                        timeout=60)
    assert got.get("success"), got
    return got["cells"]


def test_narrative_cells_are_stored_as_text():
    """Headings and prose are stored as plain text, as typed; Input cells as code boxes."""
    with recording_session("text") as ctx:
        for text, style in [("Diagrams", "Chapter"), ("Common setup", "Section"),
                            ("A note on the gauge choice.", "Text")]:
            assert payload(server.evaluate(text, style=style))["success"]
        assert payload(server.evaluate(fresh_symbol("mcpText") + " = 1"))["success"]
        assert payload(server.notebooks(action="save"))["success"]
        assert cell_forms(ctx.path) == [
            ["Title", "String"], ["Chapter", "String"], ["Section", "String"],
            ["Text", "String"], ["Input", "BoxData"]], cell_forms(ctx.path)

        fin = payload(server.notebooks(action="finalize"))
        assert fin["success"], fin
        assert fin["structural_verification"]["verified"] is True, fin


def test_replacing_a_heading_keeps_it_text():
    """Replacing a heading's content keeps it plain text; an Input cell stays code."""
    notebooks.reset_headless_notebooks()
    tmp = tempfile.mkdtemp(prefix="rec-srv-replace-")
    path = os.path.join(tmp, "replace.nb")
    nb = notebooks.get_headless_notebooks()
    nbid = None
    try:
        made = nb.create(title="Replace", path=path)
        assert made["success"], made
        nbid = made["id"]
        assert nb.write_cell("Old heading", style="Section", notebook=nbid)["success"]
        assert nb.write_cell("x = 1", style="Input", notebook=nbid)["success"]
        assert nb.replace_cell(1, "New heading", notebook=nbid)["success"]
        assert nb.replace_cell(2, "x = 2", notebook=nbid)["success"]
        assert nb.save(notebook=nbid)["success"]
        assert cell_forms(path) == [
            ["Title", "String"], ["Section", "String"], ["Input", "BoxData"]], cell_forms(path)
        with open(path) as fh:
            assert '"New heading"' in fh.read()
    finally:
        with contextlib.suppress(Exception):
            if nbid:
                nb.close(nbid)
        notebooks.reset_headless_notebooks()
        shutil.rmtree(tmp, ignore_errors=True)


def test_finalized_prints_and_outputs_are_typeset():
    """Print cells keep their formatting and characters; outputs are typeset."""
    cells = ['Print[Style["bold", Bold], " ", Hyperlink["a link", "https://example.org"], '
             '" \\[Bullet] \\[Gamma]"]',
             fresh_symbol("mcpFrac") + " = a^2/b + Sqrt[c]",
             "Plot[Sin[t], {t, 0, 1}]",
             "$PrePrint = TraditionalForm;",
             "a^2/b"]
    try:
        with recording_session("typeset") as ctx:
            for code in cells:
                assert payload(server.evaluate(code))["success"], code
            assert payload(server.notebooks(action="save"))["success"]
            fin = payload(server.notebooks(action="finalize"))
            assert fin["success"], fin
            path = fin["finalized_path"].replace("\\", "\\\\").replace('"', '\\"')
            got = evaluate_json(
                '<|"prints" -> Cases[Get["' + path + '"], Cell[b_, "Print", ___] :> {'
                'ToString[Head[b]], !FreeQ[b, FontWeight -> Bold], !FreeQ[b, "HyperlinkURL"], '
                '!FreeQ[b, s_String /; StringContainsQ[s, FromCharacterCode[{8226, 32, 947}]]]}, '
                'Infinity], '
                '"outputs" -> Cases[Get["' + path + '"], Cell[b_, "Output", ___] :> Which['
                '!FreeQ[b, FormBox[_, TraditionalForm]], "TraditionalForm", '
                '!FreeQ[b, GraphicsBox], "Graphics", '
                'MatchQ[b, _BoxData], "StandardForm", True, ToString[Head[b]]], Infinity]|>',
                timeout=60)
            assert got.get("success"), got
            assert got["prints"] == [["BoxData", True, True, True]], got
            assert got["outputs"] == ["StandardForm", "Graphics", "TraditionalForm"], got
    finally:
        evaluate_text("$PrePrint =.", timeout=30)


def test_record_start_reports_existing_definitions():
    """Definitions made before recording are reported and kept in the ledger."""
    name = fresh_symbol("recWarn")
    assert evaluate_text(f"{name} = 3", timeout=30).success
    try:
        with recording_session("warn") as ctx:
            made = ctx.made
            assert made.get("run_id") == ctx.nb._recorder.run_id, made
            assert made.get("kernel_not_fresh") is True, made
            assert name in made["kernel_at_start"]["user_symbols"], made
            assert "restart" in made["warning"], made
            assert not any(n.startswith(("mcp", "$MCP"))
                           for n in made["kernel_at_start"]["user_symbols"]), made
            stored = ctx.nb._recorder.ledger.data["kernel_at_start"]
            assert name in stored["user_symbols"], stored
    finally:
        evaluate_text(f"Remove[{name}]", timeout=30)


def test_fresh_kernel_records_without_a_warning():
    """A recording started in a fresh kernel reports nothing the server made itself."""
    session.close_kernel()
    with recording_session("clean") as ctx:
        assert "kernel_not_fresh" not in ctx.made, ctx.made
        stored = ctx.nb._recorder.ledger.data["kernel_at_start"]
        assert stored == {"user_symbols": [], "packages": []}, stored


def test_multi_statement_cell_with_a_message_finalizes():
    """A message from the second statement of a recorded cell belongs to that cell."""
    with recording_session("multi") as ctx:
        reply = payload(server.evaluate(fresh_symbol("mcpMultiA") + " = 1\n"
                                        + fresh_symbol("mcpMultiB") + " = 1/0"))
        assert reply.get("message_names") == ["Power::infy"], reply
        assert payload(server.notebooks(action="save"))["success"]
        fin = payload(server.notebooks(action="finalize", timeout=900))
        assert fin["success"], fin
        assert [m["seq"] for m in fin["repeated_messages"]] == [1], fin
        assert fin["timeout"] == 900 and fin["estimated_seconds"] < 900, fin


if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    print("=== Recorder tests through the server (live kernel) ===")
    try:
        for fn in tests:
            try:
                fn()
                passed += 1
                print(f"  PASS  {fn.__name__}")
            except Exception:
                failed += 1
                print(f"  FAIL  {fn.__name__}")
                traceback.print_exc()
    finally:
        with contextlib.suppress(Exception):
            session.close_kernel()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
