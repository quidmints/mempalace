"""
QVAC adapter tests — no sidecar required.

Mocks urllib at the transport layer so these tests run in CI without
Node.js or any QVAC infrastructure. Live integration tests against a
real sidecar live in `qvac-sidecar/tests/`.
"""

from __future__ import annotations

import base64
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from mempalace.qvac import (
    HyperdriveFetcher,
    OCRResult,
    QvacChatMessage,
    QvacClient,
    QvacConfig,
    QvacEmbedder,
    QvacLLMClient,
    QvacOCR,
    QvacUnavailable,
    fetch_model_via_sidecar,
)


def _mock_response(payload: dict) -> MagicMock:
    """Construct a context-manager mock that .read() returns JSON bytes."""
    body = json.dumps(payload).encode("utf-8")
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    mock.read.return_value = body
    return mock


class QvacClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = QvacConfig(base_url="http://test")
        self.client = QvacClient(self.config)

    @patch("urllib.request.urlopen")
    def test_healthz_returns_dict(self, urlopen):
        urlopen.return_value = _mock_response({"status": "ok"})
        result = self.client.healthz()
        self.assertEqual(result, {"status": "ok"})

    @patch("urllib.request.urlopen")
    def test_unavailable_when_connection_refused(self, urlopen):
        urlopen.side_effect = URLError("Connection refused")
        with self.assertRaises(QvacUnavailable):
            self.client.healthz()

    @patch("urllib.request.urlopen")
    def test_is_reachable_false_on_error(self, urlopen):
        urlopen.side_effect = URLError("no route")
        self.assertFalse(self.client.is_reachable())

    @patch("urllib.request.urlopen")
    def test_bearer_token_in_header(self, urlopen):
        urlopen.return_value = _mock_response({"status": "ok"})
        c = QvacClient(QvacConfig(base_url="http://test", api_token="secret"))
        c.healthz()
        sent_request = urlopen.call_args[0][0]
        self.assertEqual(
            sent_request.get_header("Authorization"), "Bearer secret",
        )

    @patch("urllib.request.urlopen")
    def test_embeddings_payload(self, urlopen):
        urlopen.return_value = _mock_response({
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "model": "qvac-local-embed",
        })
        self.client.embeddings("hello")
        body = urlopen.call_args[0][0].data
        decoded = json.loads(body)
        self.assertEqual(decoded["input"], "hello")
        self.assertEqual(decoded["model"], "qvac-local-embed")


class QvacEmbedderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = QvacClient(QvacConfig(base_url="http://test"))

    @patch("urllib.request.urlopen")
    def test_embed_returns_first_vector(self, urlopen):
        urlopen.return_value = _mock_response({
            "data": [{"index": 0, "embedding": [0.5, 0.6]}],
            "model": "qvac-local-embed",
        })
        emb = QvacEmbedder(client=self.client, dimension_override=2)
        vec = emb.embed("hi")
        self.assertEqual(vec, [0.5, 0.6])

    @patch("urllib.request.urlopen")
    def test_embed_batch_preserves_order(self, urlopen):
        urlopen.return_value = _mock_response({
            "data": [
                {"index": 1, "embedding": [1.0]},
                {"index": 0, "embedding": [0.0]},
            ],
            "model": "qvac-local-embed",
        })
        emb = QvacEmbedder(client=self.client, dimension_override=1)
        vectors = emb.embed_batch(["a", "b"])
        self.assertEqual(vectors, [[0.0], [1.0]])

    def test_embed_batch_empty_returns_empty(self):
        emb = QvacEmbedder(client=self.client)
        self.assertEqual(emb.embed_batch([]), [])


class QvacLLMClientTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_chat_parses_response(self, urlopen):
        urlopen.return_value = _mock_response({
            "id": "cmpl-xyz",
            "model": "qvac-local-llm",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello back"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        })
        llm = QvacLLMClient(client=QvacClient(QvacConfig(base_url="http://test")))
        resp = llm.complete("hi", system="be brief")
        self.assertEqual(resp.text, "hello back")
        self.assertEqual(resp.prompt_tokens, 4)
        self.assertEqual(resp.completion_tokens, 2)
        self.assertEqual(resp.finish_reason, "stop")


class QvacOCRTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_transcribe_parses_regions(self, urlopen):
        urlopen.return_value = _mock_response({
            "text": "hello world",
            "confidence": 0.9,
            "regions": [
                {"bbox": [0, 0, 50, 20], "text": "hello", "confidence": 0.95},
                {"bbox": [55, 0, 50, 20], "text": "world", "confidence": 0.85},
            ],
        })
        ocr = QvacOCR(client=QvacClient(QvacConfig(base_url="http://test")))
        result = ocr.transcribe(b"fake-bytes")
        self.assertEqual(result.text, "hello world")
        self.assertTrue(result.is_high_confidence)
        self.assertEqual(len(result.regions), 2)
        self.assertEqual(result.regions[0].text, "hello")

    @patch("urllib.request.urlopen")
    def test_low_confidence_flag(self, urlopen):
        urlopen.return_value = _mock_response({
            "text": "noisy", "confidence": 0.6, "regions": [],
        })
        ocr = QvacOCR(client=QvacClient(QvacConfig(base_url="http://test")))
        result = ocr.transcribe(b"x")
        self.assertFalse(result.is_high_confidence)


class HyperdriveFetcherTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_sha_mismatch_raises(self, urlopen):
        # Sidecar returns one sha, but bytes hash to another (corrupt).
        urlopen.side_effect = [
            _mock_response({"status": "ok"}),                       # healthz
            _mock_response({                                        # hyperdrive_fetch
                "bytes_b64": base64.b64encode(b"hello").decode("ascii"),
                "sha256": "BAD_SHA",
            }),
        ]
        with tempfile.TemporaryDirectory() as td:
            out_path = pathlib.Path(td) / "out.bin"
            client = QvacClient(QvacConfig(base_url="http://test"))
            with self.assertRaises(Exception):  # QvacError subclass
                fetch_model_via_sidecar(
                    "dk", "/file", out_path, client=client,
                )

    @patch("urllib.request.urlopen")
    def test_successful_fetch_writes_bytes(self, urlopen):
        import hashlib
        body = b"this is the model"
        sha = hashlib.sha256(body).hexdigest()
        urlopen.side_effect = [
            _mock_response({"status": "ok"}),
            _mock_response({
                "bytes_b64": base64.b64encode(body).decode("ascii"),
                "sha256": sha,
            }),
        ]
        with tempfile.TemporaryDirectory() as td:
            out_path = pathlib.Path(td) / "model.bin"
            client = QvacClient(QvacConfig(base_url="http://test"))
            result = fetch_model_via_sidecar(
                "dk", "/file", out_path, client=client,
                expected_sha256=sha,
            )
            self.assertEqual(result.sha256_hex, sha)
            self.assertEqual(result.bytes_written, len(body))
            self.assertEqual(out_path.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
