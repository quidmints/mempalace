#!/usr/bin/env node
/**
 * Standalone Hyperdrive model fetcher.
 *
 * Usage:
 *   node fetch-model.js <drive_key> <file_path> <output_path>
 *
 * Fetches a single file from a Hyperdrive and writes it to disk. Used by
 * the Python `mempalace/qvac/hyperdrive.py` adapter when the full sidecar
 * isn't running (e.g., one-shot model bootstrap before first inference).
 *
 * Exits 0 on success with the file's SHA-256 on stdout.
 * Exits 1 on failure with an error message on stderr.
 */

import fs from 'fs';
import crypto from 'crypto';

const [,, driveKey, filePath, outputPath] = process.argv;

if (!driveKey || !filePath || !outputPath) {
  console.error('Usage: node fetch-model.js <drive_key> <file_path> <output_path>');
  process.exit(1);
}

try {
  const sdk = await import('@qvac/sdk');
  const drive = await sdk.hyperdrive.connect({ driveKey });
  const bytes = await drive.get(filePath);
  if (!bytes) {
    console.error(`file ${filePath} not found in drive ${driveKey}`);
    process.exit(1);
  }
  fs.writeFileSync(outputPath, bytes);
  const sha = crypto.createHash('sha256').update(bytes).digest('hex');
  console.log(sha);
  process.exit(0);
} catch (e) {
  console.error(`fetch failed: ${e.message || e}`);
  process.exit(1);
}
