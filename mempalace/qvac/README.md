# `mempalace.qvac` — local-inference adapters

This module bridges mempalace to the QVAC sidecar (`qvac-sidecar/`).
Four adapters mirror the four QVAC capabilities mempalace adopts:

| Adapter             | Maps to                              | Replaces            |
|---------------------|--------------------------------------|---------------------|
| `QvacEmbedder`      | sidecar `/v1/embeddings`             | `_ColdStartEmbedder` in `embed/model.py` |
| `QvacLLMClient`     | sidecar `/v1/chat/completions`       | optional — `llm_client.py` works against sidecar with zero changes |
| `QvacOCR`           | sidecar `/v1/ocr`                    | OCR backend for `diary_ingest.py` |
| `HyperdriveFetcher` | sidecar `/v1/hyperdrive/fetch` + CLI | (new — no prior implementation) |

## Quick wiring

Run the sidecar (one-time install + start):

```bash
cd qvac-sidecar && npm install && npm start &
```

Point mempalace at QVAC embeddings:

```python
from mempalace.embed.model import EmbeddingService, set_default_service
from mempalace.qvac import QvacEmbedder

set_default_service(EmbeddingService(embedder=QvacEmbedder()))
```

Point `llm_client.py` (in mempalace-develop) at QVAC by configuring its
base URL to `http://127.0.0.1:11434/v1`. No code change required —
QVAC's HTTP API is OpenAI-compatible.

Use OCR in the diary ingest path:

```python
from mempalace.qvac import QvacOCR

ocr = QvacOCR()
result = ocr.transcribe(image_bytes)
if result.is_high_confidence:
    record_diary_text(result.text)
else:
    flag_for_human_review(result)
```

Fetch a fine-tuned model bundle from another palace:

```python
from mempalace.qvac import HyperdriveFetcher

fetcher = HyperdriveFetcher()
result = fetcher.fetch(
    drive_key="abc123...",
    file_path="/llama-8b-mempalace-tuned.gguf",
    output_path="~/.qvac/models/llm.gguf",
    expected_sha256="def456...",
)
```

## Configuration

All env vars:

| Variable                    | Default                        | Purpose                       |
|-----------------------------|--------------------------------|-------------------------------|
| `MEMPALACE_QVAC_BASE_URL`   | `http://127.0.0.1:11434`       | Sidecar URL                   |
| `MEMPALACE_QVAC_API_TOKEN`  | (none)                         | Bearer token if sidecar requires |
| `MEMPALACE_QVAC_TIMEOUT`    | `60.0`                         | HTTP request timeout (sec)    |
| `MEMPALACE_QVAC_LLM_MODEL`  | `qvac-local-llm`               | Model id for chat completion  |
| `MEMPALACE_QVAC_EMBED_MODEL`| `qvac-local-embed`             | Model id for embeddings       |

The sidecar reads `QVAC_*` env vars for its own configuration; see
`qvac-sidecar/README.md`.

## Error handling

All adapters raise from a small hierarchy:

```
QvacError                       # base
 ├─ QvacUnavailable             # sidecar not reachable — caller falls back
 ├─ QvacBadRequest              # 4xx — programming error, fix the call
 └─ QvacServerError             # 5xx — sidecar runtime failure (model load, OOM)
```

Caller patterns:

```python
# Fallback pattern: try QVAC, fall back to remote API
try:
    embedder = QvacEmbedder()
    vectors = embedder.embed_batch(texts)
except QvacUnavailable:
    embedder = AnthropicEmbedder()    # or whatever your fallback is
    vectors = embedder.embed_batch(texts)
```

```python
# Required pattern: QVAC must work, otherwise error up
embedder = QvacEmbedder()
if not embedder._client.is_reachable():
    raise RuntimeError("QVAC sidecar required but not running")
vectors = embedder.embed_batch(texts)
```

## Federation considerations

`QvacEmbedder.info().weights_hash` reflects the sidecar's currently-
loaded model. Federation comparisons in `federate/manifold_index.py`
gate cross-palace embedding compatibility on matching `weights_hash`.
Two palaces running different LLM models *will not* be able to compare
embeddings — by design.

When palaces want to compare, they:
1. Both fetch the same model via `HyperdriveFetcher` with a pinned
   sha256.
2. Verify both sidecars report the same `weights_hash` via
   `embedder.info()`.
3. Then federation embedding queries work.

This is the deliberate cost of operator-owned models: shared
embeddings require shared model bytes, fetched and pinned.

## Testing

`mempalace/tests/test_qvac.py` covers:

- Client transport (mock urllib)
- Embedder dimension caching
- LLM response parsing
- OCR region parsing
- Hyperdrive sha mismatch detection

None of those tests require the sidecar to be running — they mock the
HTTP layer. Live integration tests against a real sidecar live in
`qvac-sidecar/tests/` and require Node 20+ plus a loaded model.
