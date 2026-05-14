/**
 * Mempalace QVAC sidecar.
 *
 * Hosts QVAC's local-inference engines behind an OpenAI-compatible HTTP API
 * so the Python `mempalace/qvac/` module can talk to them without going
 * through the JS SDK directly. Endpoints:
 *
 *   POST /v1/embeddings           — OpenAI-compatible embeddings
 *   POST /v1/chat/completions     — OpenAI-compatible chat completion
 *   POST /v1/ocr                  — { image_base64 } -> { text, confidence }
 *   POST /v1/hyperdrive/fetch     — { drive_key, file_path } -> { bytes_b64, sha256 }
 *   GET  /v1/models               — model registry
 *   GET  /healthz                 — liveness
 *
 * Configuration via env vars:
 *   QVAC_PORT                 (default: 11434)
 *   QVAC_LLM_MODEL_PATH       (default: ~/.qvac/models/llm.gguf)
 *   QVAC_EMBED_MODEL_PATH     (default: ~/.qvac/models/embed.gguf)
 *   QVAC_OCR_MODEL_PATH       (default: ~/.qvac/models/ocr.onnx)
 *   QVAC_API_TOKEN            (optional: requires Bearer <token> on requests)
 *
 * Why a sidecar and not direct Python bindings to llama.cpp?
 *   - QVAC's value is the unified, batteries-included wrapping. Re-implementing
 *     that wrapping in Python defeats the point.
 *   - OpenAI-compatible HTTP lets `llm_client.py` work with zero changes —
 *     just point its base URL at this server.
 *   - Process isolation: the sidecar can crash without taking the substrate
 *     down. The Python side retries.
 *
 * NOT IN SCOPE for this MVP:
 *   - Multi-tenancy (one sidecar = one operator).
 *   - Rate limiting beyond what Fastify gives us.
 *   - Streaming responses (chat completion returns the full message).
 */

import Fastify from 'fastify';
import path from 'path';
import os from 'os';

// QVAC imports — lazy so the server can boot and respond to /healthz
// even if a model fails to load.
let qvacSdk = null;
async function loadQvac() {
  if (qvacSdk) return qvacSdk;
  qvacSdk = await import('@qvac/sdk');
  return qvacSdk;
}

const fastify = Fastify({
  logger: { level: process.env.QVAC_LOG_LEVEL || 'info' },
  bodyLimit: 50 * 1024 * 1024,   // 50MB — OCR images / batch embeddings
});

const config = {
  port: parseInt(process.env.QVAC_PORT || '11434', 10),
  host: process.env.QVAC_HOST || '127.0.0.1',
  apiToken: process.env.QVAC_API_TOKEN || null,
  llmModelPath: process.env.QVAC_LLM_MODEL_PATH
    || path.join(os.homedir(), '.qvac/models/llm.gguf'),
  embedModelPath: process.env.QVAC_EMBED_MODEL_PATH
    || path.join(os.homedir(), '.qvac/models/embed.gguf'),
  ocrModelPath: process.env.QVAC_OCR_MODEL_PATH
    || path.join(os.homedir(), '.qvac/models/ocr.onnx'),
};

// Engine handles — initialized lazily. First call to each endpoint loads
// its engine. Once loaded, the engine is reused for subsequent calls.
const engines = {
  llm: null,
  embed: null,
  ocr: null,
};

async function getLlm() {
  if (engines.llm) return engines.llm;
  const sdk = await loadQvac();
  // QVAC SDK 0.8.2 API: sdk.llm.create({ modelPath, ... })
  // The exact name may shift across versions — adjust here when upgrading.
  engines.llm = await sdk.llm.create({
    modelPath: config.llmModelPath,
    contextSize: 4096,
  });
  fastify.log.info({ model: config.llmModelPath }, 'LLM engine loaded');
  return engines.llm;
}

async function getEmbed() {
  if (engines.embed) return engines.embed;
  const sdk = await loadQvac();
  engines.embed = await sdk.embed.create({
    modelPath: config.embedModelPath,
  });
  fastify.log.info({ model: config.embedModelPath }, 'Embedding engine loaded');
  return engines.embed;
}

async function getOcr() {
  if (engines.ocr) return engines.ocr;
  const sdk = await loadQvac();
  engines.ocr = await sdk.ocr.create({
    modelPath: config.ocrModelPath,
  });
  fastify.log.info({ model: config.ocrModelPath }, 'OCR engine loaded');
  return engines.ocr;
}

// =================================================================
// Auth hook (optional bearer token)
// =================================================================
fastify.addHook('onRequest', async (request, reply) => {
  if (!config.apiToken) return;                          // open
  if (request.url === '/healthz') return;                // always reachable
  const auth = request.headers['authorization'];
  if (!auth || auth !== `Bearer ${config.apiToken}`) {
    reply.code(401).send({ error: 'unauthorized' });
  }
});

// =================================================================
// Health
// =================================================================
fastify.get('/healthz', async () => ({
  status: 'ok',
  engines: {
    llm: engines.llm ? 'loaded' : 'lazy',
    embed: engines.embed ? 'loaded' : 'lazy',
    ocr: engines.ocr ? 'loaded' : 'lazy',
  },
}));

// =================================================================
// /v1/models — minimal model registry
// =================================================================
fastify.get('/v1/models', async () => ({
  object: 'list',
  data: [
    { id: 'qvac-local-llm',   object: 'model', owned_by: 'qvac', path: config.llmModelPath },
    { id: 'qvac-local-embed', object: 'model', owned_by: 'qvac', path: config.embedModelPath },
    { id: 'qvac-local-ocr',   object: 'model', owned_by: 'qvac', path: config.ocrModelPath },
  ],
}));

// =================================================================
// /v1/embeddings — OpenAI-compatible
// Request:  { input: string | string[], model?: string }
// Response: { object: "list", data: [{ embedding: [...], index, object: "embedding" }], model, usage }
// =================================================================
fastify.post('/v1/embeddings', async (request, reply) => {
  const { input, model = 'qvac-local-embed' } = request.body || {};
  if (!input) return reply.code(400).send({ error: { message: 'input required' }});
  const texts = Array.isArray(input) ? input : [input];
  try {
    const engine = await getEmbed();
    const vectors = await engine.embedBatch(texts);
    return {
      object: 'list',
      data: vectors.map((v, i) => ({ object: 'embedding', index: i, embedding: Array.from(v) })),
      model,
      usage: { prompt_tokens: texts.reduce((s, t) => s + t.length, 0), total_tokens: 0 },
    };
  } catch (e) {
    request.log.error({ err: e }, 'embedding failed');
    return reply.code(500).send({ error: { message: String(e) }});
  }
});

// =================================================================
// /v1/chat/completions — OpenAI-compatible
// Request:  { messages: [{ role, content }], model?, max_tokens?, temperature? }
// Response: { id, object, choices: [{ message: { role, content }, finish_reason }], model, usage }
// =================================================================
fastify.post('/v1/chat/completions', async (request, reply) => {
  const { messages, model = 'qvac-local-llm', max_tokens = 512, temperature = 0.7 } = request.body || {};
  if (!Array.isArray(messages) || messages.length === 0) {
    return reply.code(400).send({ error: { message: 'messages required' }});
  }
  try {
    const engine = await getLlm();
    // Naive prompt assembly — for production prompt format hew to the
    // model's chat template (Llama-3-Instruct, Mistral, etc.). This is
    // a no-frills concat that's good enough for an MVP.
    const prompt = messages
      .map(m => `${m.role.toUpperCase()}: ${m.content}`)
      .join('\n') + '\nASSISTANT:';
    const out = await engine.complete(prompt, { maxTokens: max_tokens, temperature });
    return {
      id: 'cmpl-' + Date.now().toString(36),
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [{ index: 0, message: { role: 'assistant', content: out.text }, finish_reason: out.stopReason || 'stop' }],
      usage: { prompt_tokens: out.promptTokens || 0, completion_tokens: out.completionTokens || 0, total_tokens: 0 },
    };
  } catch (e) {
    request.log.error({ err: e }, 'completion failed');
    return reply.code(500).send({ error: { message: String(e) }});
  }
});

// =================================================================
// /v1/ocr — diary ingestion / scanned-document path
// Request:  { image_base64: string, language?: string }
// Response: { text: string, confidence: number, regions?: [{ bbox, text }] }
// =================================================================
fastify.post('/v1/ocr', async (request, reply) => {
  const { image_base64, language = 'en' } = request.body || {};
  if (!image_base64) return reply.code(400).send({ error: { message: 'image_base64 required' }});
  try {
    const engine = await getOcr();
    const bytes = Buffer.from(image_base64, 'base64');
    const out = await engine.transcribe(bytes, { language });
    return {
      text: out.text,
      confidence: out.confidence ?? 0.0,
      regions: out.regions || [],
    };
  } catch (e) {
    request.log.error({ err: e }, 'ocr failed');
    return reply.code(500).send({ error: { message: String(e) }});
  }
});

// =================================================================
// /v1/hyperdrive/fetch — model-weight distribution
// Request:  { drive_key: string, file_path: string }
// Response: { bytes_b64: string, sha256: string }
//
// Fetches a single file from a Hyperdrive given its public key. Mempalace
// uses this for distributing locally-trained models across operator boxes
// without putting them on public infra. The drive_key is the recipient's
// content-addressed pointer to the model bundle.
// =================================================================
fastify.post('/v1/hyperdrive/fetch', async (request, reply) => {
  const { drive_key, file_path } = request.body || {};
  if (!drive_key || !file_path) {
    return reply.code(400).send({ error: { message: 'drive_key and file_path required' }});
  }
  try {
    const sdk = await loadQvac();
    // QVAC SDK exposes lib-dl-hyperdrive as sdk.hyperdrive
    const drive = await sdk.hyperdrive.connect({ driveKey: drive_key });
    const bytes = await drive.get(file_path);
    if (!bytes) return reply.code(404).send({ error: { message: 'file not found in drive' }});
    const crypto = await import('crypto');
    const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
    return { bytes_b64: bytes.toString('base64'), sha256 };
  } catch (e) {
    request.log.error({ err: e }, 'hyperdrive fetch failed');
    return reply.code(500).send({ error: { message: String(e) }});
  }
});

// =================================================================
// Boot
// =================================================================
const start = async () => {
  try {
    await fastify.listen({ port: config.port, host: config.host });
    fastify.log.info({ port: config.port, host: config.host }, 'mempalace QVAC sidecar listening');
  } catch (err) {
    fastify.log.error(err);
    process.exit(1);
  }
};

start();
