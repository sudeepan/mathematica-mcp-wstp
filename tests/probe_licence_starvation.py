"""What does running out of licence seats look like over WSTP?

Not part of the test suite -- it deliberately consumes licence seats, so it is
run by hand, not on every change.

The question it answers: under `wolframscript`, a refused seat exits with
**empty output and no error**, which reads downstream as a code bug and has cost
whole sessions of debugging. WSTP ought to do better, because the handshake is
explicit: a kernel that cannot take a seat should fail to activate and leave a
real message behind. That was a hypothesis in the architecture doc, not a
measurement. This turns it into one.

Safe by construction:

* kernels are opened one at a time and the run stops at the FIRST failure;
* a hard cap bounds it regardless;
* free memory is checked each round and the run stops before the machine is
  squeezed, because exhausting RAM would prove nothing about licences;
* every kernel is closed in a ``finally``, including on Ctrl-C.

Usage:  .venv/bin/python tests/probe_licence_starvation.py [max_kernels]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mathematica_wstp.kernel import Kernel, KernelError   # noqa: E402
from mathematica_wstp.link import WSTPError               # noqa: E402

DEFAULT_CAP = 32
MIN_FREE_MB = 3000


def free_mb() -> int:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 1 << 30  # unknown: do not let this be the thing that stops us


def main() -> int:
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAP
    kernels: list[Kernel] = []
    verdict = "cap reached without a refusal"
    print(f"cap={cap}, MemAvailable={free_mb()} MB\n")

    try:
        while len(kernels) < cap:
            avail = free_mb()
            if avail < MIN_FREE_MB:
                verdict = f"stopped early: only {avail} MB free (memory, not licences)"
                break
            started = time.monotonic()
            try:
                k = Kernel().start(timeout=90)
                kernels.append(k)
                answer = k.evaluate("$LicenseID", timeout=30).strip()
                print(f"  #{len(kernels):3d}  up in {time.monotonic()-started:5.1f}s  "
                      f"pid={k.pid}  $LicenseID={answer[:40]}")
            except (KernelError, WSTPError) as exc:
                print(f"\n  #{len(kernels)+1} REFUSED after {time.monotonic()-started:.1f}s")
                print(f"     {type(exc).__name__}: {exc}")
                verdict = "explicit, typed failure at the WSTP handshake"
                break
            except Exception as exc:
                print(f"\n  #{len(kernels)+1} failed unexpectedly: {type(exc).__name__}: {exc}")
                verdict = f"unexpected failure mode: {type(exc).__name__}"
                break
    finally:
        print(f"\nclosing {len(kernels)} kernel(s)...")
        for k in kernels:
            try:
                k.close(grace=2.0)
            except Exception as exc:
                print(f"  close failed for pid {k.pid}: {exc}")
        print("closed.")

    print(f"\nVERDICT: {verdict}")
    print(f"kernels held at peak: {len(kernels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
