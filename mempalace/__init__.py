"""MemPalace — Give your AI a memory. No API key required.

```python
from mempalace import Palace

palace = Palace.create()
result = palace.capture(transcript="something happened today")
hits = palace.search("what was happening")
palace.close()
```

The package is decomposed into focused subsystems. Most callers will
go through the `Palace` facade above; advanced callers can import
subsystems directly:

  - mempalace.log         — append-only event log (source of truth)
  - mempalace.schema      — event taxonomy + identifiers + kinds
  - mempalace.views       — derived state (graph, current, topology)
  - mempalace.derived     — the master DAG of view computations
  - mempalace.handle      — handle protocol (cluster patterns, frames,
                            search policy, walk driver)
  - mempalace.retrieve    — handle lifecycle (mem_allocate / refine /
                            resolve / close), scope, fidelity, gather,
                            rhyme, substrate verification
  - mempalace.canonicalizer — normalization + reversibility
  - mempalace.miner       — Class 1/2/3 mining passes
  - mempalace.drawer      — drawer capture, facets, secure read,
                            collision detection, burn
  - mempalace.embed       — embedding model, reconciliation
  - mempalace.features    — feature pipeline + computation
  - mempalace.rank        — ranker stack, isolation, signed loader,
                            dispatch, quorum
  - mempalace.stack       — Step / AttestedStep / TrustedAggregator
                            primitives
  - mempalace.federate    — federation matching layers, anchor
                            boundary, RHYME, discourse patterns
  - mempalace.resolve     — resolution-side execution (inference
                            steps, evidence verify)
  - mempalace.secure      — phone secure element, key manager, burn
                            flow, phone-off graceful degradation
  - mempalace.signatures  — palace signatures (R3 §8 narrowed)
  - mempalace.switchboard — oracle SDK (node listener, resolution
                            job, decloak watcher, chain observer)
  - mempalace.multiplex   — multi-consumer coordination
  - mempalace.query       — bi-directional queries, question qualifier
  - mempalace.migrate     — schema migrations
  - mempalace.mcp         — MCP integration (planned)

"""

import logging

from __future__ import annotations

# The single load-bearing import: the Palace facade.
from .palace import Palace, PalaceConfig
from .version import __version__  # noqa: E402

# chromadb telemetry: posthog capture() was broken in 0.6.x causing noisy stderr
# warnings ("capture() takes 1 positional argument but 3 were given"). In 1.x the
# posthog client is a no-op stub, so this is now harmless — kept as a guard in
# case future chromadb versions re-introduce real telemetry calls.
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

# NOTE: the previous block set ``ORT_DISABLE_COREML=1`` on macOS arm64 as a
# supposed workaround for the #74 ARM64 segfault.  Two problems:
#
# 1. ONNX Runtime does not read that env var -- it has no global way to
#    disable a single execution provider, so the setdefault was a no-op.
# 2. #74 is a null-pointer crash in ``chromadb_rust_bindings.abi3.so``, not
#    an ONNX issue, so disabling CoreML would not have fixed it anyway.
#
# #521 has since traced the actual macOS arm64 crashes (both in mine and
# search paths) to the 0.x chromadb hnswlib binding.  Filtering
# CoreMLExecutionProvider at the ONNX layer leaves the hnswlib C++ crash
# intact, so the real fix is upgrading chromadb to 1.5.4+, which #581
# proposes.  See #397 for the history of this line.

__all__ = [
    "Palace",
    "PalaceConfig",
    "__version__"
]
