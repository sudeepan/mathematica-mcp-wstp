"""Acceptance tests for Phase 1: recorder foundation.

Pure-Python tests for RecorderLedger run without a kernel. Integration tests
for MCPReadBack and tagged MCPWriteCell need a live Wolfram kernel and follow
the same pattern as test_kernel.py: each test launches its own session and
cleans up afterwards.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mathematica_wstp.recorder_ledger import RecorderLedger, ledger_dir, SCHEMA


# --- Pure-Python: RecorderLedger -------------------------------------------

def test_ledger_create_and_load():
    """A ledger survives the process that created it."""
    d = tempfile.mkdtemp(prefix="rec-ledger-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        assert os.path.exists(ledger.path)
        assert ledger.run_id == run_id
        assert ledger.records == []
        assert ledger.next_seq == 1

        loaded = RecorderLedger.load(ledger.path)
        assert loaded.run_id == run_id
        assert loaded.data["schema"] == SCHEMA
        assert loaded.data["notebook"]["session_id"] == "hnb1"
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_ledger_append_and_sequence():
    """Appended records get monotonic sequence numbers and persist."""
    d = tempfile.mkdtemp(prefix="rec-ledger-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)

        tag1 = ledger.make_tag()
        assert tag1 == f"{run_id}/1"
        r1 = ledger.append("abc123", "x = 1 + 2", "Input", tag1)
        assert r1["seq"] == 1
        assert r1["record_tag"] == tag1

        tag2 = ledger.make_tag()
        assert tag2 == f"{run_id}/2"
        r2 = ledger.append("def456", "y = x^2", "Input", tag2)
        assert r2["seq"] == 2

        assert ledger.next_seq == 3
        assert len(ledger.records) == 2

        reloaded = RecorderLedger.load(ledger.path)
        assert len(reloaded.records) == 2
        assert reloaded.records[0]["source_digest"] == "abc123"
        assert reloaded.records[1]["source_digest"] == "def456"
        assert reloaded.next_seq == 3
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_ledger_lookup_by_seq_and_tag():
    d = tempfile.mkdtemp(prefix="rec-ledger-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        tag = ledger.make_tag()
        ledger.append("aaa", "code here", "Input", tag)

        assert ledger.record_by_seq(1) is not None
        assert ledger.record_by_seq(1)["record_tag"] == tag
        assert ledger.record_by_tag(tag) is not None
        assert ledger.record_by_tag(tag)["seq"] == 1
        assert ledger.record_by_seq(99) is None
        assert ledger.record_by_tag("nonexistent") is None
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_ledger_for_notebook():
    d = tempfile.mkdtemp(prefix="rec-ledger-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        r1 = RecorderLedger.create(nb_path, "hnb1", f"R{uuid.uuid4().hex[:10]}")
        r2 = RecorderLedger.create(nb_path, "hnb1", f"R{uuid.uuid4().hex[:10]}")

        found = RecorderLedger.for_notebook(nb_path)
        assert len(found) == 2
        assert r1.path in found or r2.path in found
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_ledger_atomic_write_produces_valid_json():
    d = tempfile.mkdtemp(prefix="rec-ledger-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        for i in range(5):
            tag = ledger.make_tag()
            ledger.append(f"digest{i}", f"cell {i}", "Input", tag)

        with open(ledger.path) as fh:
            raw = json.load(fh)
        assert raw["schema"] == SCHEMA
        assert len(raw["records"]) == 5
        assert raw["next_seq"] == 6
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


def test_ledger_source_preview_truncated():
    d = tempfile.mkdtemp(prefix="rec-ledger-")
    nb_path = os.path.join(d, "test.nb")
    os.environ["MATHEMATICA_WSTP_RECORDING_DIR"] = os.path.join(d, "ledgers")
    try:
        run_id = f"R{uuid.uuid4().hex[:10]}"
        ledger = RecorderLedger.create(nb_path, "hnb1", run_id)
        long_source = "x" * 500
        tag = ledger.make_tag()
        r = ledger.append("digest", long_source, "Input", tag)
        assert len(r["source_preview"]) == 200
    finally:
        os.environ.pop("MATHEMATICA_WSTP_RECORDING_DIR", None)
        shutil.rmtree(d, ignore_errors=True)


# --- Integration: MCPReadBack and tagged MCPWriteCell ----------------------
# These require a running Mathematica kernel.

def test_readback_returns_cell_metadata():
    """MCPReadBack surfaces digest, tag, evaluatable, and style for every cell."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"readback-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="ReadBack test", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        nb.write_cell("x = 1 + 2", style="Input", notebook=nbid)
        nb.write_cell("This is text", style="Text", notebook=nbid)

        result = nb.read_back(notebook=nbid)
        assert result.get("success"), result
        # create() inserts a Title cell, so total is 3 (Title + Input + Text)
        assert result["total"] == 3

        cells = result["cells"]
        assert cells[0]["style"] == "Title"

        inp = cells[1]
        assert inp["style"] == "Input"
        assert inp["executable"] is True
        assert isinstance(inp["source_digest"], str)
        assert len(inp["source_digest"]) == 64  # SHA-256 hex
        assert inp["source_chars"] > 0

        txt = cells[2]
        assert txt["style"] == "Text"
        assert txt["executable"] is False
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_write_cell_with_record_tag():
    """MCPWriteCell stamps TaggingRules when a record_tag is provided."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"tagged-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Tagged cells", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        tag = "Rtest123/1"
        result = nb.write_cell("a = 42", style="Input", notebook=nbid, record_tag=tag)
        assert result.get("success"), result
        assert result.get("record_tag") == tag

        readback = nb.read_back(notebook=nbid)
        assert readback.get("success"), readback
        # cells[0] is the Title from create(); cells[1] is our tagged Input
        cell = readback["cells"][1]
        assert cell["record_tag"] == tag
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_untagged_cell_has_empty_tag():
    """A cell written without a record_tag reads back with an empty tag."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"untagged-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Untagged", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        nb.write_cell("b = 99", style="Input", notebook=nbid)
        readback = nb.read_back(notebook=nbid)
        assert readback.get("success"), readback
        # cells[0] is the Title, cells[1] is our untagged Input
        assert readback["cells"][1]["record_tag"] == ""
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_digest_stable_across_save_reopen():
    """The source digest must not change when the notebook is saved and reopened.

    This is the acceptance criterion from the design review: save/reopen can
    change box serialization (a plain string becomes a RowBox), but boxText
    normalizes both to the same text, so the digest stays the same.
    """
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"digest-stable-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Digest stability", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        nb.write_cell("result = Integrate[x^2, x]", style="Input", notebook=nbid,
                       record_tag="Rstable/1")

        rb1 = nb.read_back(notebook=nbid)
        assert rb1.get("success"), rb1
        # cells[0] is the Title, cells[1] is our Input
        digest_before = rb1["cells"][1]["source_digest"]

        assert nb.save(nbid, path).get("success")

        notebooks.reset_headless_notebooks()
        nb2 = notebooks.get_headless_notebooks()
        reopened = nb2.open(path)
        assert reopened.get("success"), reopened
        rid = reopened["id"]

        rb2 = nb2.read_back(notebook=rid)
        assert rb2.get("success"), rb2
        digest_after = rb2["cells"][1]["source_digest"]

        assert digest_before == digest_after, (
            f"digest changed across save/reopen: {digest_before} -> {digest_after}")
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_digest_changes_on_edit():
    """Editing a cell's source must change its digest."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"digest-edit-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Digest edit", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        nb.write_cell("x = 1", style="Input", notebook=nbid, record_tag="Redit/1")
        rb1 = nb.read_back(notebook=nbid)
        # cells[0] is Title, cells[1] is our Input
        digest_before = rb1["cells"][1]["source_digest"]

        nb.replace_cell(1, "x = 2", notebook=nbid)
        rb2 = nb.read_back(notebook=nbid)
        digest_after = rb2["cells"][1]["source_digest"]

        assert digest_before != digest_after, "digest should change when source changes"
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_readback_detects_injected_cell():
    """MCPReadBack shows every cell, so injection is visible by count and tag."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"inject-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Injection detect", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        nb.write_cell("a = 1", style="Input", notebook=nbid, record_tag="Rinject/1")
        nb.write_cell("b = 2", style="Input", notebook=nbid, record_tag="Rinject/2")

        rb1 = nb.read_back(notebook=nbid)
        # Title + 2 Input cells
        assert rb1["total"] == 3

        nb.write_cell("injected = 999", style="Input", notebook=nbid,
                       position="After", anchor=1)
        rb2 = nb.read_back(notebook=nbid)
        assert rb2["total"] == 4

        tags = [c["record_tag"] for c in rb2["cells"]]
        # Title cell and injected cell both have empty tags
        assert tags.count("") == 2, f"expected 2 untagged cells (Title + injected), got {tags}"
        assert tags.count("Rinject/1") == 1
        assert tags.count("Rinject/2") == 1
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


def test_readback_tag_survives_save():
    """The record tag in TaggingRules survives NotebookSave and reload."""
    from mathematica_wstp import notebooks, session

    path = os.path.join(tempfile.gettempdir(), f"tag-save-{uuid.uuid4().hex[:8]}.nb")
    try:
        nb = notebooks.get_headless_notebooks()
        made = nb.create(title="Tag persistence", path=path)
        assert made.get("success"), made
        nbid = made["id"]

        nb.write_cell("z = 42", style="Input", notebook=nbid, record_tag="Rsave/1")
        assert nb.save(nbid, path).get("success")

        notebooks.reset_headless_notebooks()
        nb2 = notebooks.get_headless_notebooks()
        reopened = nb2.open(path)
        rid = reopened["id"]

        rb = nb2.read_back(notebook=rid)
        assert rb.get("success"), rb
        # cells[0] is Title, cells[1] is our tagged Input
        assert rb["cells"][1]["record_tag"] == "Rsave/1"
    finally:
        with contextlib.suppress(Exception):
            notebooks.reset_headless_notebooks()
        with contextlib.suppress(OSError):
            os.unlink(path)
        session.close_kernel()


# --- Runner ----------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    pure_python = [
        test_ledger_create_and_load,
        test_ledger_append_and_sequence,
        test_ledger_lookup_by_seq_and_tag,
        test_ledger_for_notebook,
        test_ledger_atomic_write_produces_valid_json,
        test_ledger_source_preview_truncated,
    ]
    integration = [
        test_readback_returns_cell_metadata,
        test_write_cell_with_record_tag,
        test_untagged_cell_has_empty_tag,
        test_digest_stable_across_save_reopen,
        test_digest_changes_on_edit,
        test_readback_detects_injected_cell,
        test_readback_tag_survives_save,
    ]

    passed = failed = skipped = 0

    print("=== Pure-Python tests (RecorderLedger) ===")
    for fn in pure_python:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print("\n=== Integration tests (MCPReadBack, tagged MCPWriteCell) ===")
    for fn in integration:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except ImportError as exc:
            skipped += 1
            print(f"  SKIP  {fn.__name__} ({exc})")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
