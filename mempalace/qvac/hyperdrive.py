"""
Hyperdrive model fetcher.

Two fetch paths, in order of preference:

  1. **Via sidecar** (`fetch_model_via_sidecar`): the sidecar must be running.
     One HTTP call returns the file bytes inline.

  2. **Via CLI** (`fetch_model_via_cli`): spawns `node fetch-model.js`
     directly. Used during bootstrap, before the sidecar can start
     (chicken-and-egg: the sidecar can't load models it doesn't have).

# Why both

The sidecar is the high-level path. But the sidecar needs an LLM model
loaded to be useful — and the model has to land on disk before that
happens. So we need a one-shot bootstrap fetch that runs without the
sidecar, then the sidecar fetches subsequent models normally.

# Hyperdrive trust model

`drive_key` is a 32-byte public key that uniquely identifies a Hyperdrive.
Anyone with the key can read; only the holder of the corresponding secret
key can write. For mempalace's purposes, drive keys are pinned in
configuration after the operator chooses which model bundle to trust.
The fetched file is hashed (SHA-256) and the caller compares against a
known-good hash before loading. The hash check is the actual trust
boundary; the drive key just discovers the bytes.

# Where this fits

  - Initial bootstrap: operator picks an LLM model, runs `fetch_model_via_cli`
    to land it on disk, starts the sidecar.
  - Operator-tuned models: when a palace fine-tunes its own embedder on
    local substrate, it publishes the resulting weights to a Hyperdrive,
    other federated palaces fetch via `fetch_model_via_sidecar` and pin
    the hash. (Publishing flow isn't in this MVP — fetch-only.)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable

from mempalace.qvac.client import (
    QvacClient,
    QvacError,
    QvacUnavailable,
    get_default_client,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Sidecar path
# =============================================================================

@dataclass
class FetchResult:
    """Outcome of a Hyperdrive fetch."""
    output_path: pathlib.Path
    sha256_hex: str
    bytes_written: int


def fetch_model_via_sidecar(
    drive_key: str,
    file_path: str,
    output_path: str | pathlib.Path,
    *,
    client: QvacClient | None = None,
    expected_sha256: str | None = None,
) -> FetchResult:
    """Fetch a single file via the running QVAC sidecar.

    Raises `QvacUnavailable` if the sidecar isn't responding;
    `ValueError` if `expected_sha256` is provided and doesn't match.
    """
    c = client or get_default_client()
    if not c.is_reachable():
        raise QvacUnavailable("sidecar not reachable for hyperdrive fetch")
    resp = c.hyperdrive_fetch(drive_key, file_path)
    bytes_b64 = resp.get("bytes_b64")
    sha = resp.get("sha256")
    if not bytes_b64:
        raise QvacError("sidecar returned no bytes")
    raw = base64.b64decode(bytes_b64)
    actual_sha = hashlib.sha256(raw).hexdigest()
    if sha and sha != actual_sha:
        # sidecar's reported sha should match ours; if not the bytes are corrupt
        raise QvacError(
            f"sidecar sha mismatch: server={sha} local={actual_sha}",
        )
    if expected_sha256 and expected_sha256 != actual_sha:
        raise ValueError(
            f"expected sha {expected_sha256}; got {actual_sha} for {drive_key}/{file_path}",
        )
    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return FetchResult(
        output_path=out,
        sha256_hex=actual_sha,
        bytes_written=len(raw),
    )


# =============================================================================
# CLI path (bootstrap)
# =============================================================================

DEFAULT_CLI_SCRIPT = "qvac-sidecar/fetch-model.js"


def fetch_model_via_cli(
    drive_key: str,
    file_path: str,
    output_path: str | pathlib.Path,
    *,
    cli_script: str = DEFAULT_CLI_SCRIPT,
    expected_sha256: str | None = None,
    timeout_seconds: float = 600.0,
    cwd: str | None = None,
) -> FetchResult:
    """Fetch via the standalone `fetch-model.js` CLI script.

    Useful for bootstrap — runs without the sidecar. Requires Node 20+
    and that the QVAC SDK is installed (`npm install` in qvac-sidecar/).
    """
    node = shutil.which("node")
    if not node:
        raise RuntimeError("`node` binary not on PATH; install Node 20+")
    out = pathlib.Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [node, cli_script, drive_key, file_path, str(out)]
    logger.info("running hyperdrive fetch CLI: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"fetch-model.js failed: exit={e.returncode} stderr={e.stderr}",
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"fetch-model.js timed out after {timeout_seconds}s") from e

    reported_sha = result.stdout.strip()
    actual_sha = _hash_file(out)
    if reported_sha and reported_sha != actual_sha:
        raise RuntimeError(
            f"sha mismatch from CLI: stdout={reported_sha} file={actual_sha}",
        )
    if expected_sha256 and expected_sha256 != actual_sha:
        raise ValueError(
            f"expected sha {expected_sha256}; got {actual_sha} for {drive_key}/{file_path}",
        )
    return FetchResult(
        output_path=out,
        sha256_hex=actual_sha,
        bytes_written=out.stat().st_size,
    )


def _hash_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# =============================================================================
# High-level orchestrator
# =============================================================================

class HyperdriveFetcher:
    """Coordinates sidecar-vs-CLI fetch choice.

    Try sidecar first (already-loaded models, faster). Fall back to CLI
    if the sidecar isn't reachable (bootstrap path).
    """

    def __init__(
        self,
        *,
        cli_script: str = DEFAULT_CLI_SCRIPT,
        cli_cwd: str | None = None,
    ) -> None:
        self._cli_script = cli_script
        self._cli_cwd = cli_cwd

    def fetch(
        self,
        drive_key: str,
        file_path: str,
        output_path: str | pathlib.Path,
        *,
        expected_sha256: str | None = None,
    ) -> FetchResult:
        try:
            return fetch_model_via_sidecar(
                drive_key, file_path, output_path,
                expected_sha256=expected_sha256,
            )
        except QvacUnavailable:
            logger.info(
                "sidecar unreachable; falling back to CLI for hyperdrive fetch",
            )
            return fetch_model_via_cli(
                drive_key, file_path, output_path,
                cli_script=self._cli_script,
                cwd=self._cli_cwd,
                expected_sha256=expected_sha256,
            )
