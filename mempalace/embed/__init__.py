"""
mempalace.embed — embedding model and ChromaDB integration.

Embeddings are derived representations: ChromaDB is a backend, the log is
the source of truth. The reconciliation sweeper guarantees ChromaDB stays
consistent with the log even after restarts.
"""

from . import client, model, reconcile

__all__ = ["client", "model", "reconcile"]
