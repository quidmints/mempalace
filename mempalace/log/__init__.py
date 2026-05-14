"""
mempalace.log — Python-side log client and subscription registry.

The log itself is implemented in Rust (mempalace_core) for performance and
DDflow integration. This package provides the Python-facing client interface
and a subscriber registry for consumers (derived representations, miner
passes, etc.) that pull forward through the log.
"""

from . import client, subscriber

__all__ = ["client", "subscriber"]
