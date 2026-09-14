"""Front-end rendering: typesetting, rasterisation and export, headlessly.

This restores the capability the headless fork dropped -- screenshots and
typeset output -- without needing a display. ``UsingFrontEnd`` starts
``WolframNB`` with ``-platform offscreen`` on demand, measured at ~1.8s cold,
and it is torn down with the kernel. Because our kernels run in their own
process group, a front end the kernel spawned is inside that group and is
reaped along with everything else.

**What this module will not do: evaluate.** The front end is a rendering
service here and nothing more. Dispatching work to it with ``SelectionEvaluate``
does not run when an external WSTP client owns the kernel's main link -- a job
measured at 5.1s in the kernel had not completed after 200s, and had not started
after 40s of total link silence. Small expressions appear to succeed because
they are serviced inside the ``UsingFrontEnd`` block itself, which makes the
failure look flaky instead of absolute. Evaluation belongs on the kernel link,
where abort and liveness both work; see design/measurements.md §5.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import session
from .notebooks import get_headless_notebooks

logger = logging.getLogger("mathematica_wstp.render")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_DPI = 96
MAX_DPI = 600


def _wl_str(value: str) -> str:
    """Quote a Python string as Wolfram source.

    Backslashes first, then quotes -- the other order double-escapes.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _helper_call(function: str, args: str, timeout: float) -> Any:
    """Invoke a helper function that returns either raw bytes or a JSON error.

    The two are never ambiguous: PNG starts with 0x89, PDF with ``%PDF``, and
    the helper's JSON error objects start with ``{``.
    """
    notebooks = get_headless_notebooks()
    helper = _wl_str(notebooks._helper_path())
    code = (
        "Normal[Module[{},"
        f"  If[!TrueQ[$MCPHeadlessNotebookLoaded],"
        f"    If[Get[{helper}] =!= $Failed, $MCPHeadlessNotebookLoaded = True]];"
        f"  MCPHeadlessNotebook`{function}[{args}]"
        "]]"
    )
    result = session.evaluate_wl_bytes(code, timeout=timeout)
    if not result.success:
        return {"success": False, "error": result.error, "timed_out": result.timed_out}
    raw = result.data
    if raw.startswith(PNG_MAGIC) or raw.startswith(b"%PDF"):
        return raw
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"success": False, "error": "unrecognised reply from the render helper",
                "bytes": len(raw), "head": repr(raw[:40])}


def available(timeout: float = 120.0) -> dict[str, Any]:
    """Can a headless front end be started? First call pays the ~1.8s launch."""
    out = _helper_call("MCPFrontEndAvailable", "", timeout)
    if isinstance(out, dict):
        return out
    return {"success": False, "error": "unexpected binary reply from availability probe"}


def _resolve(notebook: str | None) -> str | None:
    return get_headless_notebooks()._resolve(notebook)


def render_cell(index: int, notebook: str | None = None, dpi: int = DEFAULT_DPI,
                timeout: float = 180.0) -> bytes | dict[str, Any]:
    """Rasterise one notebook cell to PNG bytes, with real typesetting."""
    nb_id = _resolve(notebook)
    if nb_id is None:
        return {"success": False, "error": "no open notebook; call notebooks(action='open') first"}
    dpi = max(24, min(int(dpi), MAX_DPI))
    return _helper_call("MCPRenderCell", f"{_wl_str(nb_id)}, {int(index)}, {dpi}", timeout)


def render_expression(code: str, dpi: int = DEFAULT_DPI,
                      timeout: float = 180.0) -> bytes | dict[str, Any]:
    """Rasterise a Wolfram expression to PNG bytes.

    The expression is evaluated by the *kernel* inside the front-end block, not
    dispatched to the front end for evaluation.
    """
    dpi = max(24, min(int(dpi), MAX_DPI))
    return _helper_call("MCPRenderExpression", f"{_wl_str(code)}, {dpi}", timeout)


# Headless export paginates correctly and scales: 994 simple cells -> 31 pages,
# and 200 cells each holding a large expanded polynomial -> 40 pages. An earlier
# note here claimed export was "first-page-only" on a headless host. That was
# wrong, and it was wrong because it generalised from a single notebook.
#
# What actually shortens an export is closed cell groups. Export renders what is
# VISIBLE, exactly as printing from the GUI would, so a collapsed section
# exports collapsed. One real notebook measured here had 158 of its 335 groups
# Closed; opening them
# multiplies the exported text 27x (54 -> 1479 characters).
#
# Caveat kept deliberately: even fully opened, that particular notebook exports
# to 2 pages, which is fewer than its 994 cells suggest. Notebook options and
# group state are ruled out. The residual cause is unidentified and specific to
# that file, so `open_groups` is offered rather than promised as a fix.
_EXPORT_NOTE = (
    "Export renders what is VISIBLE, as printing from the GUI would: collapsed "
    "cell groups export collapsed. Pass open_groups=True to expand every group "
    "first if you want the whole document."
)


def export_notebook(path: str, notebook: str | None = None,
                    open_groups: bool = True,
                    tex_math: bool = False,
                    paper: tuple[int, int] | None = None,   # None -> A4 portrait
                    fit_width: bool = False,
                    timeout: float = 300.0) -> dict[str, Any]:
    """Export the open notebook through the front end.

    Format follows the file extension. ``open_groups`` forces every cell group
    Open before rendering; see the note above on why that matters.
    """
    nb_id = _resolve(notebook)
    if nb_id is None:
        return {"success": False, "error": "no open notebook; call notebooks(action='open') first"}
    if path.lower().endswith((".md", ".markdown")):
        # Not Export[..., "Markdown"]: that rasterises every Output cell into a
        # sibling img/ directory, takes no options to stop it, and emits its own
        # unevaluated internals when no front end is attached. Our generator
        # keeps the results as text in one self-contained file.
        tex = "True" if tex_math else "False"
        out = _helper_call(
            "MCPExportMarkdown", f"{_wl_str(nb_id)}, {_wl_str(path)}, {tex}", timeout)
        return out if isinstance(out, dict) else {"success": True, "path": path}
    flag = "True" if open_groups else "False"
    # A4 portrait unless told otherwise: a defined page is what makes clipping
    # detectable at all. With no paper size the front end picks one and the
    # server has nothing to measure "too wide" against.
    w, h = (paper or (595, 842))
    out = _helper_call(
        "MCPExportNotebook",
        f"{_wl_str(nb_id)}, {_wl_str(path)}, {flag}, {int(w)}, {int(h)}, "
        f"{'True' if fit_width else 'False'}", timeout)
    if isinstance(out, bytes):
        return {"success": False, "error": "unexpected binary reply from export"}
    if isinstance(out, dict) and out.get("success"):
        out["groups_opened"] = open_groups
        if not open_groups:
            out["note"] = _EXPORT_NOTE
    return out
