"""Whether this machine can run the tests that need a Wolfram kernel.

The recorder suites mix pure-Python tests with integration tests that start a
kernel. On a machine without Mathematica, such as a CI runner, the integration
tests are skipped with this reason instead of failing on discovery.
"""

from __future__ import annotations


def missing_wolfram() -> str | None:
    """The reason the integration tests cannot run here, or None if they can."""
    from mathematica_wstp.discovery import DiscoveryError, find_kernel
    try:
        find_kernel()
    except DiscoveryError:
        return "no Wolfram installation found"
    return None
