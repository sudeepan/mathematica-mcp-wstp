"""Locating the Wolfram installation, the kernel binary, and libWSTP.

Discovery is widest-first on purpose. A hardcoded version list stops finding
kernels the day the next major version ships, and the vendor default
(``/usr/local/Wolfram/Mathematica/<ver>``) is not where installations actually
live on shared boxes and in containers -- one install this was developed
against sits under a user's home directory and is reachable only by following
a symlink found on ``PATH``.

Order:

1. Explicit environment variables (ours first, then the ones wolframscript and
   other Wolfram tooling already use, so a working setup fixes this too).
2. The on-disk cache, which is what makes discovery survive the minimal
   environment an MCP client hands a server it spawns.
3. ``PATH``, resolving symlinks -- this is what finds a relocated install.
4. Glob patterns over the usual installation roots.

A successful discovery is cached, because scanning is only affordable once.
"""

from __future__ import annotations

import functools
import glob
import json
import logging
import os
import platform
import shutil
from pathlib import Path

logger = logging.getLogger("mathematica_wstp.discovery")

CACHE_DIR = Path(os.environ.get("MATHEMATICA_WSTP_HOME", Path.home() / ".mathematica-wstp"))
CACHE_FILE = CACHE_DIR / "paths.json"

# WolframKernel and MathKernel are the real binaries; `wolfram` and `math` are
# launcher scripts that exec them and work equally well as a WSTP target.
_KERNEL_BINARIES = ("WolframKernel", "MathKernel", "wolfram", "math")

_KERNEL_ENV_VARS = (
    "MATHEMATICA_WSTP_KERNEL",
    "MATHEMATICA_KERNEL_PATH",
    "WOLFRAMSCRIPT_KERNELPATH",
)
_INSTALL_ENV_VARS = (
    "MATHEMATICA_WSTP_INSTALL",
    "MATHEMATICA_INSTALL_DIR",
    "WOLFRAM_INSTALLATION_DIRECTORY",
)
_LIB_ENV_VARS = ("MATHEMATICA_WSTP_LIB",)

# Globs, not a version list.
_LINUX_ROOT_GLOBS = (
    "/usr/local/Wolfram/Mathematica/*",
    "/usr/local/Wolfram/Wolfram/*",
    "/usr/local/Wolfram/WolframEngine/*",
    "/usr/local/Wolfram/WolframDesktop/*",
    "/opt/Wolfram/Mathematica/*",
    "/opt/Wolfram/WolframEngine/*",
    "/opt/Mathematica/*",
    "/usr/share/Mathematica/*",
)
_DARWIN_ROOT_GLOBS = (
    "/Applications/Mathematica*.app/Contents",
    "/Applications/Wolfram*.app/Contents",
    "~/Applications/Mathematica*.app/Contents",
)


class DiscoveryError(RuntimeError):
    """Nothing usable was found. The message names what was tried."""


def system_id() -> str:
    """Wolfram's SystemID for this host, used in SystemFiles paths."""
    machine = platform.machine().lower()
    system = platform.system()
    if system == "Linux":
        if machine in ("x86_64", "amd64"):
            return "Linux-x86-64"
        if machine in ("aarch64", "arm64"):
            return "Linux-ARM64"
        return "Linux"
    if system == "Darwin":
        return "MacOSX-ARM64" if machine in ("arm64", "aarch64") else "MacOSX-x86-64"
    if system == "Windows":
        return "Windows-x86-64"
    return system


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        merged = _load_cache()
        merged.update(data)
        CACHE_FILE.write_text(json.dumps(merged, indent=2))
    except Exception as exc:  # a cache miss is recoverable; a crash here is not
        logger.debug("could not write discovery cache: %s", exc)


def _root_globs() -> tuple[str, ...]:
    system = platform.system()
    if system == "Darwin":
        return _DARWIN_ROOT_GLOBS
    if system == "Windows":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return (
            rf"{pf}\Wolfram Research\Mathematica\*",
            rf"{pf}\Wolfram Research\Wolfram Desktop\*",
            rf"{pf}\Wolfram Research\Wolfram Engine\*",
        )
    return _LINUX_ROOT_GLOBS


def _install_root_from_executable(exe: str) -> str | None:
    """Walk up from an executable to the installation root.

    Follows symlinks first: on this host every binary on PATH is a symlink into
    a relocated install, and the link target is the only thing that reveals it.
    """
    real = Path(exe).resolve()
    for parent in real.parents:
        if (parent / "SystemFiles").is_dir() and (parent / "Executables").is_dir():
            return str(parent)
    return None


@functools.lru_cache(maxsize=1)
def find_installation() -> str:
    """The Wolfram installation root ($InstallationDirectory)."""
    tried: list[str] = []

    for var in _INSTALL_ENV_VARS:
        val = os.environ.get(var)
        if val:
            tried.append(f"${var}={val}")
            if (Path(val) / "SystemFiles").is_dir():
                return val

    cached = _load_cache().get("installation")
    if cached and (Path(cached) / "SystemFiles").is_dir():
        return cached
    if cached:
        tried.append(f"cache={cached} (stale)")

    for name in _KERNEL_BINARIES + ("wolframscript",):
        exe = shutil.which(name)
        if exe:
            root = _install_root_from_executable(exe)
            tried.append(f"PATH:{name}={exe}")
            if root:
                _save_cache({"installation": root})
                return root

    for pattern in _root_globs():
        for candidate in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            tried.append(f"glob={candidate}")
            if (Path(candidate) / "SystemFiles").is_dir():
                _save_cache({"installation": candidate})
                return candidate

    raise DiscoveryError(
        "No Wolfram installation found. Set $MATHEMATICA_WSTP_INSTALL to the "
        "installation directory (the one containing SystemFiles/ and "
        f"Executables/). Tried: {'; '.join(tried) or 'nothing on PATH, no globs matched'}"
    )


@functools.lru_cache(maxsize=1)
def find_kernel() -> str:
    """Path to a kernel binary usable as a WSTP launch target."""
    for var in _KERNEL_ENV_VARS:
        val = os.environ.get(var)
        if val and os.access(val, os.X_OK):
            return val

    cached = _load_cache().get("kernel")
    if cached and os.access(cached, os.X_OK):
        return cached

    root = Path(find_installation())
    for name in _KERNEL_BINARIES:
        for candidate in (root / "Executables" / name,
                          root / "SystemFiles" / "Kernel" / "Binaries" / system_id() / name):
            if os.access(candidate, os.X_OK):
                _save_cache({"kernel": str(candidate)})
                return str(candidate)

    for name in _KERNEL_BINARIES:
        exe = shutil.which(name)
        if exe:
            _save_cache({"kernel": exe})
            return exe

    raise DiscoveryError(
        f"No Wolfram kernel binary found under {root}. Set $MATHEMATICA_WSTP_KERNEL."
    )


@functools.lru_cache(maxsize=1)
def find_wstp_library() -> str:
    """Path to libWSTP for this platform.

    Two locations ship it: the DeveloperKit (which also carries wstp.h) and
    SystemFiles/Libraries. Either works -- we only need the shared object. The
    ``i4`` in the filename is the interface version; glob rather than hardcode it
    so a future interface 5 is picked up without a code change.
    """
    for var in _LIB_ENV_VARS:
        val = os.environ.get(var)
        if val and Path(val).is_file():
            return val

    cached = _load_cache().get("library")
    if cached and Path(cached).is_file():
        return cached

    root = Path(find_installation())
    sysid = system_id()
    suffix = {"Darwin": "*.dylib", "Windows": "*.dll"}.get(platform.system(), "*.so")
    patterns = [
        root / "SystemFiles" / "Links" / "WSTP" / "DeveloperKit" / sysid / "CompilerAdditions" / f"libWSTP*{suffix}",
        root / "SystemFiles" / "Links" / "WSTP" / "DeveloperKit" / sysid / "SystemAdditions" / f"libWSTP*{suffix}",
        root / "SystemFiles" / "Libraries" / sysid / f"libWSTP*{suffix}",
    ]
    for pattern in patterns:
        # Prefer the highest interface version when several are present.
        matches = sorted(glob.glob(str(pattern)), reverse=True)
        for match in matches:
            if Path(match).is_file():
                _save_cache({"library": match})
                return match

    raise DiscoveryError(
        f"libWSTP not found under {root} for SystemID {sysid}. "
        "Set $MATHEMATICA_WSTP_LIB to the shared library path. Looked in: "
        + "; ".join(str(p) for p in patterns)
    )


def find_frontend() -> str | None:
    """Path to the front-end binary, or None. Optional -- rendering only."""
    root = Path(find_installation())
    for name in ("WolframNB", "Mathematica"):
        candidate = root / "Executables" / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def clear_cache() -> None:
    """Forget cached paths. For tests, and after a Mathematica upgrade."""
    find_installation.cache_clear()
    find_kernel.cache_clear()
    find_wstp_library.cache_clear()
    try:
        CACHE_FILE.unlink()
    except FileNotFoundError:
        pass


def summary() -> dict[str, str | None]:
    """Everything discovery found, for diagnostics."""
    out: dict[str, str | None] = {"system_id": system_id()}
    for key, fn in (("installation", find_installation), ("kernel", find_kernel),
                    ("library", find_wstp_library)):
        try:
            out[key] = fn()
        except DiscoveryError as exc:
            out[key] = f"<not found: {exc}>"
    out["frontend"] = find_frontend()
    return out
