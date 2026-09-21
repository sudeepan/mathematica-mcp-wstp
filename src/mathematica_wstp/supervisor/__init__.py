"""A kernel that outlives the process that asked for the work."""

from .core import SupervisorConfig, configure, main, serve_forever

__all__ = ["SupervisorConfig", "configure", "main", "serve_forever"]
