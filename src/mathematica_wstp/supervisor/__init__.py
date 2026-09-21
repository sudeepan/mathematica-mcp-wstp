"""A kernel that outlives the process that asked for the work."""

from .core import SupervisorConfig, configure, main, serve_forever
from .lifecycle import SupervisorInfo, probe, start, stop, talk

__all__ = ["SupervisorConfig", "SupervisorInfo", "configure", "main",
           "probe", "serve_forever", "start", "stop", "talk"]
