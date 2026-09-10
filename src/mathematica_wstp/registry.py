"""A durable record of every kernel process we launched, so we can clean up.

Why this exists: the previous server's restart path terminated only the master
kernel. Subkernels from ``LaunchKernels[]`` are separate OS processes on their
own links, so they survived and reparented to init. A snapshot of the machine
this was written on found 37 kernels, 7 orphaned, ~23.9 GB resident, including
19 stranded subkernels at ~400 MB each under a single dead parent.

**The registry keys on pid, never on a command line.** Matching
``pkill -f WolframKernel`` also matches the shell running the command and other
users' kernels; that mistake cost several self-inflicted failures during the
investigation, including killing the investigating shell twice. It also matched
another session's subkernels, which very nearly got attributed -- and killed --
as ours.

Pid alone is not safe either, because pids are reused. Every entry therefore
records the process start time from ``/proc/<pid>/stat``, which is unique per
pid incarnation. Before signalling anything we check that the start time still
matches; a mismatch means the pid was recycled and the process is somebody
else's. A command-line sanity check runs on top of that, as a second gate --
never as the primary key.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("mathematica_wstp.registry")

REGISTRY_DIR = Path(os.environ.get("MATHEMATICA_WSTP_HOME", Path.home() / ".mathematica-wstp"))
REGISTRY_FILE = REGISTRY_DIR / "kernels.json"


@dataclass
class KernelRecord:
    pid: int
    pgid: int
    starttime: int          # /proc/<pid>/stat field 22 -- guards against pid reuse
    owner_pid: int          # the server process that launched it
    launched_at: float
    kernel_path: str
    subkernels: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def proc_starttime(pid: int) -> int | None:
    """Start time in clock ticks since boot, or None if the pid is gone.

    Field 22 of /proc/<pid>/stat. Parsed from the last ')' because the comm
    field can itself contain spaces and parentheses.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        after_comm = raw[raw.rindex(")") + 2:]
        return int(after_comm.split()[19])
    except (ValueError, IndexError):
        return None


def proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


def proc_ppid(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[1])
    except Exception:
        return None


def proc_comm(pid: int) -> str | None:
    """The executable name from /proc/<pid>/stat, between the parentheses."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.index("(") + 1:raw.rindex(")")]
    except Exception:
        return None


def proc_children(pid: int, comm: str | None = None) -> list[int]:
    """Live children of ``pid``, read from /proc rather than asked of anyone.

    The kernel can tell you its own subkernels via ``Kernels[]``, but only while
    it is idle enough to answer -- which is never the case when it is busy with
    the fan-out you are trying to count, and least of all when it is wedged.
    Reading /proc needs nothing from the kernel and works in both cases.

    ``comm`` filters to children with that executable name, which is how the
    front end (``WolframNB``, also a child of the kernel) stays out of a
    subkernel count.
    """
    kids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return kids
    for name in entries:
        if not name.isdigit():
            continue
        child = int(name)
        if proc_ppid(child) != pid:
            continue
        if comm is not None and proc_comm(child) != comm:
            continue
        if pid_alive(child):
            kids.append(child)
    return sorted(kids)


def proc_state(pid: int) -> str | None:
    """Single-letter process state from /proc/<pid>/stat ('Z' for zombie)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[0]
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    """True only for a process that still exists AND is not a zombie.

    The zombie check is load-bearing, not a nicety. A child that has exited but
    has not been waited on still answers ``os.kill(pid, 0)``, so a bare signal
    probe reports a dead kernel as alive. That made shutdown burn its entire
    grace period waiting for a process that had already gone, then signal a
    corpse -- measured at 10s per kernel, of which 5s was this. It also matters
    for the reaper, which would otherwise count zombies as live orphans.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return proc_state(pid) != "Z"


def _read() -> list[dict]:
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("registry unreadable (%s); starting empty", exc)
        return []


def _write(entries: list[dict]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.replace(REGISTRY_FILE)  # atomic


@contextlib.contextmanager
def _locked():
    """Serialise registry updates across server instances."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_DIR / "kernels.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # best effort: a missing lock degrades to last-writer-wins
        yield
    finally:
        os.close(fd)


def record(pid: int, pgid: int, kernel_path: str) -> KernelRecord:
    """Register a freshly launched kernel."""
    entry = KernelRecord(
        pid=pid,
        pgid=pgid,
        starttime=proc_starttime(pid) or 0,
        owner_pid=os.getpid(),
        launched_at=time.time(),
        kernel_path=kernel_path,
    )
    with _locked():
        entries = _read()
        entries = [e for e in entries if e.get("pid") != pid]
        entries.append(entry.to_json())
        _write(entries)
    return entry


def update_subkernels(pid: int, subkernels: list[int]) -> None:
    """Record the subkernels a master has spawned, so shutdown can reach them."""
    with _locked():
        entries = _read()
        for entry in entries:
            if entry.get("pid") == pid:
                entry["subkernels"] = sorted(set(subkernels))
        _write(entries)


def forget(pid: int) -> None:
    """Drop an entry after a clean shutdown."""
    with _locked():
        _write([e for e in _read() if e.get("pid") != pid])


def entries() -> list[dict]:
    return _read()


def is_still_ours(entry: dict) -> bool:
    """True only if this pid is the same process we recorded.

    Two independent gates. Start time catches pid reuse; the command-line check
    catches a registry that has gone stale in some way we did not anticipate.
    Both must pass before we signal anything.
    """
    pid = entry.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    recorded = entry.get("starttime") or 0
    if recorded and proc_starttime(pid) != recorded:
        logger.debug("pid %s was recycled -- not ours", pid)
        return False
    cmdline = proc_cmdline(pid).lower()
    if cmdline and not any(tok in cmdline for tok in ("wolframkernel", "mathkernel", "wolfram")):
        logger.debug("pid %s no longer looks like a kernel -- not ours", pid)
        return False
    return True


def terminate_tree(entry: dict, grace: float = 5.0) -> list[int]:
    """SIGTERM then SIGKILL a recorded kernel and its process group.

    Note that the SIGTERM stage does nothing to Wolfram kernels: 26 of them
    across three separate sets ignored it outright and every one needed
    SIGKILL, so this path always pays the full grace before escalating. The
    stage is kept deliberately anyway -- the process group can also hold the
    front end and any external solver a package shells out to, and those do
    shut down cleanly on SIGTERM. Paying
    five seconds on a fallback path is cheaper than killing those abruptly.

    The normal path never gets here: close() asks the kernel to quit over the
    WSTP link, which works. Signals are for when the link is already gone.

    Returns the pids actually signalled. Refuses to act on anything that fails
    :func:`is_still_ours`.
    """
    if not is_still_ours(entry):
        return []

    pid = entry["pid"]
    pgid = entry.get("pgid") or 0
    signalled: list[int] = []

    targets: list[tuple[str, int]] = []
    # Signalling the group reaches subkernels; guard against pgid 0 or 1, which
    # would mean "every process in my group" or init.
    if pgid > 1:
        targets.append(("group", pgid))
    else:
        targets.append(("proc", pid))
        targets.extend(("proc", sk) for sk in entry.get("subkernels", []) if isinstance(sk, int))

    for kind, target in targets:
        try:
            os.killpg(target, signal.SIGTERM) if kind == "group" else os.kill(target, signal.SIGTERM)
            signalled.append(target)
        except (ProcessLookupError, PermissionError) as exc:
            logger.debug("SIGTERM %s %s: %s", kind, target, exc)

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.05)

    if pid_alive(pid):
        for kind, target in targets:
            try:
                os.killpg(target, signal.SIGKILL) if kind == "group" else os.kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # Reap our own child so it does not linger as a zombie.
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)

    return signalled


def reap_orphans(include_live_owners: bool = False) -> list[dict]:
    """Clean up kernels whose owning server is gone.

    Called at startup. Returns the entries acted on.
    """
    reaped: list[dict] = []
    with _locked():
        entries_now = _read()
        survivors: list[dict] = []
        for entry in entries_now:
            pid = entry.get("pid")
            if not isinstance(pid, int):
                continue
            if not pid_alive(pid):
                continue  # already gone; drop the record
            owner = entry.get("owner_pid")
            owner_gone = not (isinstance(owner, int) and pid_alive(owner))
            if owner_gone or include_live_owners:
                if terminate_tree(entry):
                    reaped.append(entry)
                    continue
                if not is_still_ours(entry):
                    continue  # recycled pid: forget it, never signal it
            survivors.append(entry)
        _write(survivors)
    if reaped:
        logger.info("reaped %d orphaned kernel(s)", len(reaped))
    return reaped


def registered_kernels() -> list[dict]:
    """Every live kernel this machine has recorded, orphan or not."""
    out = []
    for entry in _read():
        pid = entry.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            continue
        owner = entry.get("owner_pid")
        out.append({
            "pid": pid,
            "pgid": entry.get("pgid"),
            "ppid": proc_ppid(pid),
            "owner_pid": owner,
            "owner_alive": isinstance(owner, int) and pid_alive(owner),
            "still_ours": is_still_ours(entry),
            "age_seconds": round(time.time() - entry.get("launched_at", time.time()), 1),
            "subkernels": entry.get("subkernels", []),
        })
    return out


def orphan_report() -> list[dict]:
    """Only the kernels that are genuinely orphaned -- owner process gone.

    This used to return every live registered kernel, so `status` listed the
    server's own healthy kernel under "orphans". A diagnostic that cries wolf
    on the normal case is worse than none.
    """
    return [k for k in registered_kernels() if not k.get("owner_alive")]
