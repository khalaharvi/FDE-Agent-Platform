"""Provider-path tests for fde_mcp.embeddings against in-test HTTP stubs.

Module-level captures below grab the REAL functions during collection,
before conftest's session-scoped `_fake_bedrock` fixture replaces the
module attributes -- these tests exercise the actual HTTP provider paths.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from fde_mcp import embeddings
from fde_mcp.config import get_settings

_REAL_EMBED_BATCH = embeddings.embed_batch

DIMS = 1024


def _vec(seed: float) -> list[float]:
    return [seed] * DIMS


class _EmbedStub(BaseHTTPRequestHandler):
    """Answers POST like an OpenAI /v1/embeddings endpoint, or -- for paths
    containing `batchEmbedContents` -- like Gemini's batch embedding
    endpoint; records requests either way.
    """

    requests: list[dict[str, Any]] = []
    dims = DIMS
    fail_first_with: int | None = None
    _failed_once = False

    def do_POST(self) -> None:
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "goog_api_key": self.headers.get("x-goog-api-key"),
                "body": body,
            }
        )
        if cls.fail_first_with and not cls._failed_once:
            cls._failed_once = True
            self.send_response(cls.fail_first_with)
            self.end_headers()
            return
        if "batchEmbedContents" in self.path:
            reqs = body["requests"]
            payload: dict[str, Any] = {
                "embeddings": [{"values": _vec(0.5)[: cls.dims]} for _ in reqs]
            }
        else:
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            payload = {
                "data": [
                    {"embedding": _vec(0.5)[: cls.dims], "index": i} for i in range(len(texts))
                ]
            }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:  # silence test output
        return


@pytest.fixture
def stub_server() -> Any:
    _EmbedStub.requests = []
    _EmbedStub.dims = DIMS
    _EmbedStub.fail_first_with = None
    _EmbedStub._failed_once = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbedStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def _compat_env(monkeypatch: pytest.MonkeyPatch, stub_server: str) -> Any:
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "openai-compat")
    monkeypatch.setenv("FDE_EMBED_BASE_URL", stub_server)
    monkeypatch.setenv("FDE_EMBED_MODEL_ID", "test-embed")
    monkeypatch.setenv("FDE_EMBED_API_KEY", "k-compat")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_compat_env")
async def test_compat_path_shape_and_roundtrip() -> None:
    vecs = await _REAL_EMBED_BATCH(["alpha", "beta"], input_type="search_document")
    assert len(vecs) == 2
    assert all(len(v) == DIMS for v in vecs)
    req = _EmbedStub.requests[0]
    assert req["path"].endswith("/embeddings")
    assert req["auth"] == "Bearer k-compat"
    assert req["body"]["model"] == "test-embed"
    assert req["body"]["input"] == ["alpha", "beta"]


async def test_openai_path_sends_dimensions(
    monkeypatch: pytest.MonkeyPatch, stub_server: str
) -> None:
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("FDE_EMBED_MODEL_ID", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setattr(embeddings, "OPENAI_EMBED_URL", f"{stub_server}/embeddings")
    get_settings.cache_clear()
    try:
        await _REAL_EMBED_BATCH(["gamma"])
        assert _EmbedStub.requests[0]["body"]["dimensions"] == DIMS
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("_compat_env")
async def test_wrong_dims_raises_actionably() -> None:
    _EmbedStub.dims = 768
    with pytest.raises(ValueError, match="768"):
        await _REAL_EMBED_BATCH(["delta-768"])


@pytest.mark.usefixtures("_compat_env")
async def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_EMBED_BASE_BACKOFF", "0.01")
    get_settings.cache_clear()
    _EmbedStub.fail_first_with = 429
    vecs = await _REAL_EMBED_BATCH(["epsilon-retry"])
    assert len(vecs) == 1
    assert len(_EmbedStub.requests) == 2


async def test_gemini_path_batch_request_shape(
    monkeypatch: pytest.MonkeyPatch, stub_server: str
) -> None:
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "gemini")
    monkeypatch.setenv("FDE_EMBED_MODEL_ID", "gemini-embedding-001")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(embeddings, "GEMINI_EMBED_BASE", stub_server)
    get_settings.cache_clear()
    try:
        vecs = await _REAL_EMBED_BATCH(["zeta-gemini", "eta-gemini"])
        assert len(vecs) == 2
        assert all(len(v) == DIMS for v in vecs)
        req = _EmbedStub.requests[0]
        assert "batchEmbedContents" in req["path"]
        assert "key=" not in req["path"]
        assert req["goog_api_key"] == "g-key"
        item = req["body"]["requests"][0]
        assert item["output_dimensionality"] == DIMS
        assert item["model"] == "models/gemini-embedding-001"
        assert item["content"]["parts"][0]["text"] == "zeta-gemini"
    finally:
        get_settings.cache_clear()


async def test_gemini_batches_beyond_max_batch_size(
    monkeypatch: pytest.MonkeyPatch, stub_server: str
) -> None:
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "gemini")
    monkeypatch.setenv("FDE_EMBED_MODEL_ID", "gemini-embedding-001")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(embeddings, "GEMINI_EMBED_BASE", stub_server)
    monkeypatch.setattr(embeddings, "GEMINI_MAX_BATCH", 2)
    get_settings.cache_clear()
    try:
        texts = [f"theta-gemini-{i}" for i in range(5)]
        vecs = await _REAL_EMBED_BATCH(texts)
        assert len(vecs) == 5
        assert all(len(v) == DIMS for v in vecs)
        # 5 texts chunked at 2 per request -> 3 requests (2, 2, 1).
        assert len(_EmbedStub.requests) == 3
        assert [len(r["body"]["requests"]) for r in _EmbedStub.requests] == [2, 2, 1]
    finally:
        get_settings.cache_clear()
