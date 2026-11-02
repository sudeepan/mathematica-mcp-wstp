"""A kernel that outlives the process that asked for the work."""

from .backend import SupervisorEvaluator, SupervisorUnavailable, connect
from .core import SupervisorConfig, configure, main, serve_forever
from .lifecycle import SupervisorInfo, probe, start, stop, talk

__all__ = ["SupervisorConfig", "SupervisorEvaluator", "SupervisorInfo",
           "SupervisorUnavailable", "configure", "connect", "main", "probe",
           "serve_forever", "start", "stop", "talk", "use", "use_direct"]


def use(socket_path: str | None = None) -> SupervisorEvaluator:
    """Run notebook cells in a supervisor's kernel instead of this process's.

    Refuses if no supervisor is running rather than starting one: a call that
    quietly leaves a long-lived process on the machine is not something to do
    as a side effect of choosing a backend.

    Everything that evaluates goes through the evaluator seam: ``evaluate``,
    ``vars`` and notebook replay all reach the supervisor's kernel once it is
    selected, so a symbol defined through one is there for the others.
    ``abort()`` does not: it reaches this process's kernel, and the supervisor
    tool's own running and abort actions are the ones to use.

    A notebook already open lives in the kernel that opened it -- the document
    is held in that kernel, not in this process -- so switching strands it, and
    the next call against it fails with no such session. Open notebooks after
    choosing a backend, not before. ``stranded_notebooks`` on the returned
    evaluator says how many were left behind, because a silent orphan here
    looks exactly like a notebook that was never opened.
    """
    from ..evaluator import set_evaluator
    from ..notebooks import get_headless_notebooks

    evaluator = connect(socket_path)
    open_now = (get_headless_notebooks().list() or {}).get("notebooks", [])
    set_evaluator(evaluator)
    evaluator.stranded_notebooks = [n.get("id") for n in open_now]
    return evaluator


def use_direct():
    """Put notebook execution back in this process's own kernel."""
    from ..evaluator import DirectSessionEvaluator, set_evaluator

    evaluator = DirectSessionEvaluator()
    set_evaluator(evaluator)
    return evaluator
