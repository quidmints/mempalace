# mempalace QVAC sidecar

A Node.js process that runs QVAC's local-inference engines (`@qvac/sdk`)
and exposes them as OpenAI-compatible HTTP endpoints, so the Python
mempalace substrate can use local LLM completion, embeddings, OCR, and
Hyperdrive model distribution without going through the JS SDK directly.

## Why a sidecar

The Python side of mempalace doesn't have a clean way to call into
QVAC's JS engines. The two viable shapes are subprocess-per-call (bad
latency for high-frequency embeddings) or a long-running HTTP server
(this). HTTP also has the side benefit of being OpenAI-compatible: the
existing `mempalace/llm_client.py` works against this sidecar with zero
code changes, just a base-URL configuration pointing at `127.0.0.1:11434`.

The sidecar is one operator → one process. It's not a multi-tenant
service. Run it on the same machine as the Python mempalace daemon
(usually the cloud-box, not the phone — phone uses a different code
path in the React Native voice agent).

## Install

```bash
cd qvac-sidecar
npm install
```

Requires Node 20+.

## Prepare models

The sidecar lazily loads three models on first request to their
respective endpoints. By default it looks for them in `~/.qvac/models/`:

- `~/.qvac/models/llm.gguf`      — chat-completion model
- `~/.qvac/models/embed.gguf`    — embedding model
- `~/.qvac/models/ocr.onnx`      — OCR model

Override paths with `QVAC_LLM_MODEL_PATH` / `QVAC_EMBED_MODEL_PATH` /
`QVAC_OCR_MODEL_PATH` env vars.

Suggested starting models (small enough to run on a 16GB box):

- LLM: Llama-3.1-8B-Instruct Q4_K_M (~5GB) or Mistral-7B-Instruct Q5_K_M
- Embedding: BGE-small or all-MiniLM-L6-v2 in GGUF (~100MB)
- OCR: PaddleOCR mobile detection + recognition in ONNX (~50MB)

You can also fetch models via Hyperdrive using `npm run fetch-model`:

```bash
npm run fetch-model -- <drive_key> /llama-8b.gguf ~/.qvac/models/llm.gguf
```

## Run

```bash
npm start                  # production (logs to stdout)
npm run start:dev          # dev mode, restarts on file change
```

The server binds to `127.0.0.1:11434` by default. Override with
`QVAC_PORT` and `QVAC_HOST` env vars. Use `0.0.0.0` only if you trust
the network — there's no built-in TLS termination.

## Authentication

Set `QVAC_API_TOKEN=somelongstring` to require `Authorization: Bearer
somelongstring` on every request except `/healthz`. The Python adapter
reads `MEMPALACE_QVAC_API_TOKEN` and sets the header automatically.

## Endpoints

| Method | Path                       | Purpose                                  |
|--------|----------------------------|------------------------------------------|
| GET    | `/healthz`                 | Liveness probe                           |
| GET    | `/v1/models`               | List loaded models                       |
| POST   | `/v1/embeddings`           | OpenAI-compatible embeddings             |
| POST   | `/v1/chat/completions`     | OpenAI-compatible chat                   |
| POST   | `/v1/ocr`                  | Image → text                             |
| POST   | `/v1/hyperdrive/fetch`     | Fetch model bytes from a Hyperdrive      |

`/v1/embeddings` and `/v1/chat/completions` are wire-compatible with
the OpenAI v1 API — point any OpenAI client at the base URL.

## Limitations

This is a minimum-viable sidecar:

- No streaming responses. Chat returns the full completion in one body.
- One model per kind. Multi-model routing (different LLMs for different
  market kinds, etc.) requires extending `engines` in `server.js`.
- No metrics endpoint. Add OpenTelemetry/Prometheus when you need it.
- Hyperdrive write is intentionally not exposed — model distribution
  is currently fetch-only. Publishing comes later when there's a
  governance flow for promoting a fine-tuned model.

## Cost-of-running notes

- LLM completion at 8B Q4 needs ~6GB RAM and runs at ~20 tok/s on CPU,
  ~80 tok/s on Apple Silicon, ~200 tok/s on a 24GB GPU.
- Embedding model is small enough to keep loaded permanently.
- OCR model loads quickly but inference is slow on CPU (~2s/page).
- Sidecar memory steady-state with all three engines loaded: ~7GB.

## Stopping

Ctrl-C in the foreground, or `kill -TERM <pid>` if backgrounded. The
sidecar doesn't currently flush state on shutdown — engines just stop.
