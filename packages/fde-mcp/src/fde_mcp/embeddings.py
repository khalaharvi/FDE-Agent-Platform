"""embeddings.py -- multi-provider embedding client shared by the MCP server
and the embedder backfill worker.

Four providers are supported, selected by `FDE_EMBED_PROVIDER` (default
"bedrock"): `bedrock` (AWS Bedrock Titan/Cohere, see below), `openai`
(OpenAI's `/v1/embeddings` API), `gemini` (Google's Generative Language
API `batchEmbedContents` endpoint), and `openai-compat` (any self-hosted
server presenting the OpenAI `/v1/embeddings` wire shape -- Ollama, LM
Studio, vLLM -- at `FDE_EMBED_BASE_URL`). Every provider must emit vectors
matching 003_vectors_hnsw.sql's fixed `kg.embedding` `vector(1024)` domain;
`_check_dims` enforces this per-vector for every provider, with no silent
truncation, padding, or fallback.

Within the `bedrock` provider, two model families are supported, selected
by `FDE_EMBED_MODEL_ID` (default `amazon.titan-embed-text-v2:0`), because
both families can be configured to emit exactly 1024 dims:

  * amazon.titan-embed-text-v2:0  -- body: {"inputText", "dimensions":1024,
    "normalize": true}. One text per invoke_model call; Titan has no batch
    endpoint on Bedrock.
  * cohere.embed-v4 (and the v3 family, same wire shape) -- body:
    {"texts": [...], "input_type": ..., "output_dimension": 1024,
    "embedding_types": ["float"]}. Up to 96 texts per call; `input_type`
    matters for Cohere's asymmetric embeddings ("search_query" for what the
    user asks, "search_document" for what gets indexed -- get this backwards
    and recall quietly drops).

Everything below is written against the documented Bedrock and Gemini
request/response shapes. It has NOT been exercised against a real Bedrock
or Gemini endpoint in this environment (no live AWS credentials / network
egress to bedrock-runtime, no live Gemini API key) -- see the top-level
report for what was stubbed versus verified. The `openai` and
`openai-compat` HTTP paths are exercised against an in-test HTTP stub
(test_embeddings_providers.py), which verifies request/response shape but
is likewise not a live-API guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import OrderedDict
from operator import itemgetter
from typing import Any, Protocol

from fde_mcp import credentials
from fde_mcp.config import get_settings
from fde_mcp.logging import get_logger

log = get_logger(__name__)


class _BedrockRuntimeClient(Protocol):
    """The one method this module calls on a boto3 bedrock-runtime client.

    boto3 clients are generated at runtime from a service JSON model, not
    from a hand-written class, so botocore's own stubs only type the
    generic `BaseClient` (no `invoke_model` attribute at all) -- typing
    the client as `BaseClient` would make every call site an
    `attr-defined` error, and typing it as bare `Any` would leak an
    untyped value through every function that touches it. This Protocol
    is the explicit, narrow boundary: `_get_client()` is the one place
    that takes boto3's effectively-`Any` return value and asserts it
    satisfies this shape.
    """

    # modelId/contentType (not model_id/content_type) because these are boto3's
    # actual wire parameter names -- the call site passes them as kwargs.
    def invoke_model(
        self,
        *,
        modelId: str,  # noqa: N803
        body: str,
        accept: str,
        contentType: str,  # noqa: N803
    ) -> dict[str, Any]: ...


TITAN_MODEL_ID = "amazon.titan-embed-text-v2:0"
COHERE_MODEL_PREFIX = "cohere."
COHERE_MAX_BATCH = 96

OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
GEMINI_EMBED_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MAX_BATCH = 100

_THROTTLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
}

_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

_1024_DIM_ALTERNATIVES = (
    "known 1024-dim choices: text-embedding-3-small/-large (dimensions=1024), "
    "gemini-embedding-001 (output_dimensionality=1024), mxbai-embed-large, bge-m3"
)

InputType = str  # "search_query" | "search_document"; Bedrock/Cohere-only.

# ---------------------------------------------------------------------------
# In-process LRU cache. Keyed on (provider, model_id, input_type,
# sha256(text)) exactly as specified: an RL rollout that asks the same
# question of the same model many times in one process should not re-bill
# the provider for it. `provider` is part of the key so switching
# FDE_EMBED_PROVIDER never serves a vector produced by a different
# provider/model pairing. This is intentionally NOT shared across
# processes -- it is a hot-path cost optimisation, not a correctness
# dependency (a cache miss just re-embeds).
# ---------------------------------------------------------------------------
_cache: OrderedDict[tuple[str, str, str, str], list[float]] = OrderedDict()
_cache_lock = asyncio.Lock()


def _cache_key(
    provider: str, model_id: str, input_type: str, text: str
) -> tuple[str, str, str, str]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return (provider, model_id, input_type, digest)


async def _cache_get(key: tuple[str, str, str, str]) -> list[float] | None:
    async with _cache_lock:
        val = _cache.get(key)
        if val is not None:
            _cache.move_to_end(key)
        return val


async def _cache_put(key: tuple[str, str, str, str], value: list[float]) -> None:
    async with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        cache_size = get_settings().embedding.cache_size
        while len(_cache) > cache_size:
            _cache.popitem(last=False)


def cache_stats() -> dict[str, int]:
    return {"entries": len(_cache), "max_entries": get_settings().embedding.cache_size}


# ---------------------------------------------------------------------------
# Bedrock client (lazy singleton -- boto3 clients are thread-safe but not
# cheap to construct, and we don't want an import-time dependency on
# credentials being present for code paths that never call Bedrock).
# ---------------------------------------------------------------------------
_client: _BedrockRuntimeClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> _BedrockRuntimeClient:
    global _client  # noqa: PLW0603 -- lazy process-wide singleton, see module docstring
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            import boto3  # noqa: PLC0415 -- keep boto3 optional until first use

            region = get_settings().embedding.bedrock_region
            # boto3.client(...) is typed `Any` here (no per-service stub package
            # installed) -- assigning it to a `_BedrockRuntimeClient`-annotated
            # name is the narrowing cast described on that Protocol.
            _client = boto3.client("bedrock-runtime", region_name=region)
    return _client


async def _invoke_with_retry(model_id: str, body: str) -> dict[str, Any]:
    from botocore.exceptions import ClientError  # noqa: PLC0415 -- local import, same rationale

    settings = get_settings().embedding
    client = await _get_client()
    attempt = 0
    while True:
        try:
            resp = await asyncio.to_thread(
                client.invoke_model,
                modelId=model_id,
                body=body,
                accept="application/json",
                contentType="application/json",
            )
            payload: dict[str, Any] = json.loads(resp["body"].read())
            return payload
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            attempt += 1
            if code in _THROTTLE_CODES and attempt <= settings.max_retries:
                delay = min(
                    settings.base_backoff_seconds * (2 ** (attempt - 1)),
                    settings.max_backoff_seconds,
                ) + random.uniform(0, 0.25)  # noqa: S311 -- jitter, not security-sensitive
                log.warning(
                    "bedrock_throttled",
                    model_id=model_id,
                    code=code,
                    attempt=attempt,
                    max_retries=settings.max_retries,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                continue
            raise


def _titan_body(text: str) -> str:
    dims = get_settings().embedding.dimensions
    return json.dumps({"inputText": text, "dimensions": dims, "normalize": True})


def _cohere_body(texts: list[str], input_type: InputType) -> str:
    dims = get_settings().embedding.dimensions
    return json.dumps(
        {
            "texts": texts,
            "input_type": input_type,
            "embedding_types": ["float"],
            "output_dimension": dims,
        }
    )


def _extract_cohere_embeddings(payload: dict[str, Any]) -> list[list[float]]:
    """Cohere-on-Bedrock has returned embeddings under a couple of shapes
    across model versions ({"embeddings": [[...]]} for v3 float-only calls,
    {"embeddings": {"float": [[...]]}} for v3/v4 multi-type calls). Handle
    both rather than assume one.
    """
    emb = payload.get("embeddings")
    if isinstance(emb, dict):
        if "float" in emb:
            return list(emb["float"])
        return list(next(iter(emb.values())))
    if isinstance(emb, list):
        return emb
    msg = f"unrecognised cohere embed response shape: keys={list(payload)}"
    raise ValueError(msg)


def _is_cohere(model_id: str) -> bool:
    return model_id.startswith(COHERE_MODEL_PREFIX)


def _check_dims(vec: list[float], model_id: str) -> list[float]:
    """Enforce the per-vector dimension contract every provider must meet:
    003_vectors_hnsw.sql's `kg.embedding` is a fixed `vector(1024)` domain,
    so a wrong-sized vector must fail loudly here, at the source, rather
    than at the pgvector insert (`to_pgvector_literal`) with no idea which
    provider/model produced it.
    """
    dims = get_settings().embedding.dimensions
    if len(vec) != dims:
        msg = (
            f"{model_id} returned a {len(vec)}-dim embedding, expected {dims} "
            f"(kg.embedding is a fixed vector({dims}) domain) -- {_1024_DIM_ALTERNATIVES}"
        )
        raise ValueError(msg)
    return vec


async def _http_post_with_retry(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    """POST once, retrying on 429/500/502/503/504 with the same
    exponential-backoff-plus-jitter shape as `_invoke_with_retry` (Bedrock's
    throttle retry loop), governed by the same `max_retries`/
    `base_backoff_seconds`/`max_backoff_seconds` knobs. Raises `ValueError`
    -- never silently returns a partial or garbage payload -- once retries
    are exhausted or the status is not retryable.
    """
    import httpx  # noqa: PLC0415 -- keep httpx import local, same rationale as boto3 above

    settings = get_settings().embedding
    attempt = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code < 300:
                payload: dict[str, Any] = resp.json()
                return payload
            attempt += 1
            if resp.status_code in _RETRYABLE_HTTP_STATUS and attempt <= settings.max_retries:
                delay = min(
                    settings.base_backoff_seconds * (2 ** (attempt - 1)),
                    settings.max_backoff_seconds,
                ) + random.uniform(0, 0.25)  # noqa: S311 -- jitter, not security-sensitive
                log.warning(
                    "embed_http_retried",
                    url=url,
                    status=resp.status_code,
                    attempt=attempt,
                    max_retries=settings.max_retries,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                continue
            snippet = resp.text[:500]
            msg = f"embedding request to {url} failed: status={resp.status_code} body={snippet!r}"
            raise ValueError(msg)


def _embed_api_key() -> str:
    """Resolve the API key for the configured HTTP embedding provider.

    `FDE_EMBED_API_KEY` wins outright when set (a deployment may want the
    embedding client on a different key than the chat-completion model).
    Otherwise fall through to `credentials.resolve_api_key`: for
    openai/gemini a missing key is a hard error (there is no keyless
    OpenAI/Gemini deployment); for openai-compat a missing key is expected
    for keyless local servers (Ollama, LM Studio), so `CredentialError`
    there is swallowed into the conventional placeholder `"local"` --
    matching Task 3's `build_model` `openai-compat` handling.
    """
    settings = get_settings().embedding
    if settings.api_key:
        return settings.api_key
    if settings.provider == "openai-compat":
        try:
            return credentials.resolve_api_key(settings.provider)
        except credentials.CredentialError:
            return "local"
    return credentials.resolve_api_key(settings.provider)


async def _openai_style_uncached(
    texts: list[str],
    model_id: str,
    *,
    url: str,
    api_key: str,
    send_dimensions: bool,
) -> list[list[float]]:
    """Shared by `openai` and `openai-compat`: both speak the OpenAI
    `/v1/embeddings` wire shape. `send_dimensions` is False for
    openai-compat because a self-hosted server's `dimensions` support is
    unknown -- its configured model must already emit 1024 dims, and
    `_check_dims` catches it if not.
    """
    dims = get_settings().embedding.dimensions
    body: dict[str, Any] = {"model": model_id, "input": texts}
    if send_dimensions:
        body["dimensions"] = dims
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = await _http_post_with_retry(url, headers, body)
    data = sorted(payload["data"], key=itemgetter("index"))
    vecs = [_check_dims(d["embedding"], model_id) for d in data]
    if len(vecs) != len(texts):
        msg = f"{model_id} returned {len(vecs)} embeddings for {len(texts)} inputs"
        raise ValueError(msg)
    return vecs


async def _gemini_uncached(texts: list[str], model_id: str) -> list[list[float]]:
    """Google's Generative Language API `batchEmbedContents` endpoint,
    chunked to `GEMINI_MAX_BATCH` texts per call (Gemini caps batch size,
    unlike OpenAI's single-call `/v1/embeddings`). `output_dimensionality`
    is sent per-request-item, mirroring Bedrock Cohere's `output_dimension`
    and OpenAI's `dimensions` -- every provider is told explicitly to emit
    1024 dims, `_check_dims` verifies it did.
    """
    dims = get_settings().embedding.dimensions
    api_key = _embed_api_key()
    headers = {"x-goog-api-key": api_key}
    vecs: list[list[float]] = []
    for start in range(0, len(texts), GEMINI_MAX_BATCH):
        chunk = texts[start : start + GEMINI_MAX_BATCH]
        url = f"{GEMINI_EMBED_BASE}/models/{model_id}:batchEmbedContents"
        body = {
            "requests": [
                {
                    "model": f"models/{model_id}",
                    "content": {"parts": [{"text": t}]},
                    "output_dimensionality": dims,
                }
                for t in chunk
            ]
        }
        payload = await _http_post_with_retry(url, headers, body)
        chunk_vecs = payload["embeddings"]
        if len(chunk_vecs) != len(chunk):
            msg = f"{model_id} returned {len(chunk_vecs)} embeddings for {len(chunk)} inputs"
            raise ValueError(msg)
        vecs.extend(_check_dims(e["values"], model_id) for e in chunk_vecs)
    return vecs


async def _bedrock_uncached_fill(
    resolved_model_id: str,
    input_type: InputType,
    texts: list[str],
    uncached_idx: list[int],
    *,
    results: list[list[float] | None],
    keys: list[tuple[str, str, str, str]],
) -> None:
    """The `bedrock` provider's dispatch for `embed_batch`'s uncached
    slots: Titan (one `invoke_model` per text -- no batch endpoint) or
    Cohere (batched up to `COHERE_MAX_BATCH`). Verbatim pre-multi-provider
    behaviour, only relocated out of `embed_batch` to keep that function's
    branch/statement count within lint limits; mutates `results` and the
    module cache in place exactly as `embed_batch` did inline before.
    """
    if resolved_model_id == TITAN_MODEL_ID:
        # No batch endpoint: one invoke_model per text.
        for i in uncached_idx:
            payload = await _invoke_with_retry(resolved_model_id, _titan_body(texts[i]))
            vec = payload["embedding"]
            results[i] = vec
            await _cache_put(keys[i], vec)
    elif _is_cohere(resolved_model_id):
        for start in range(0, len(uncached_idx), COHERE_MAX_BATCH):
            chunk_idx = uncached_idx[start : start + COHERE_MAX_BATCH]
            chunk_texts = [texts[i] for i in chunk_idx]
            payload = await _invoke_with_retry(
                resolved_model_id, _cohere_body(chunk_texts, input_type)
            )
            vecs = _extract_cohere_embeddings(payload)
            if len(vecs) != len(chunk_idx):
                msg = f"cohere returned {len(vecs)} embeddings for {len(chunk_idx)} inputs"
                raise ValueError(msg)
            for i, vec in zip(chunk_idx, vecs, strict=True):
                results[i] = vec
                await _cache_put(keys[i], vec)
    else:
        msg = (
            f"unsupported FDE_EMBED_MODEL_ID={resolved_model_id!r}; expected "
            f"{TITAN_MODEL_ID!r} or a {COHERE_MODEL_PREFIX!r}-prefixed model"
        )
        raise ValueError(msg)


async def _http_provider_uncached(
    provider: str, texts: list[str], model_id: str
) -> list[list[float]]:
    """The `openai`/`openai-compat`/`gemini` dispatch for `embed_batch`'s
    uncached slots -- separated out (alongside `_bedrock_uncached_fill`)
    purely to keep `embed_batch` under lint's branch/statement ceiling.
    """
    if provider == "gemini":
        return await _gemini_uncached(texts, model_id)
    settings = get_settings().embedding
    if provider == "openai":
        url, send_dims = OPENAI_EMBED_URL, True
    else:
        if not settings.base_url:
            msg = "FDE_EMBED_PROVIDER=openai-compat requires FDE_EMBED_BASE_URL"
            raise ValueError(msg)
        url, send_dims = settings.base_url.rstrip("/") + "/embeddings", False
    return await _openai_style_uncached(
        texts, model_id, url=url, api_key=_embed_api_key(), send_dimensions=send_dims
    )


async def embed(
    text: str, *, input_type: InputType = "search_query", model_id: str | None = None
) -> list[float]:
    """Embed a single piece of text.

    `input_type` is Bedrock/Cohere-only: "search_query" for a user/agent
    question, "search_document" for text being indexed (node/edge
    verbalisations, evidence chunks). Titan v2 ignores it (symmetric
    embeddings) but it is still threaded through so switching
    FDE_EMBED_MODEL_ID to a Cohere model requires no call-site changes.
    The HTTP providers (openai/gemini/openai-compat) have no such field
    and ignore this argument entirely.
    """
    resolved_model_id = model_id or get_settings().embedding.model_id
    provider = get_settings().embedding.provider
    key = _cache_key(provider, resolved_model_id, input_type, text)
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    vectors = await embed_batch([text], input_type=input_type, model_id=resolved_model_id)
    return vectors[0]


def to_pgvector_literal(vec: list[float]) -> str:
    """Render a Python float list as pgvector's text input format:
    '[v1,v2,...]'. Shared by the MCP server (query-side probes) and the
    embedder worker (index-side writes) so both go through one dimension
    check against `kg.embedding`'s fixed vector(1024) domain -- see
    003_vectors_hnsw.sql.
    """
    dims = get_settings().embedding.dimensions
    if len(vec) != dims:
        msg = (
            f"embedding has {len(vec)} dims, expected {dims} "
            "(kg.embedding is a fixed vector(1024) domain -- check "
            "FDE_EMBED_MODEL_ID and FDE_EMBED_DIMENSIONS)"
        )
        raise ValueError(msg)
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def embed_batch(
    texts: list[str],
    input_type: InputType = "search_document",
    model_id: str | None = None,
) -> list[list[float]]:
    """Embed many texts, batching per-model and de-duplicating via cache.

    Preserves input order in the output regardless of cache hits/misses or
    how the uncached texts get chunked into provider batches.
    """
    resolved_model_id = model_id or get_settings().embedding.model_id
    if not texts:
        return []

    provider = get_settings().embedding.provider
    results: list[list[float] | None] = [None] * len(texts)
    keys = [_cache_key(provider, resolved_model_id, input_type, t) for t in texts]

    uncached_idx: list[int] = []
    for i, key in enumerate(keys):
        cached = await _cache_get(key)
        if cached is not None:
            results[i] = cached
        else:
            uncached_idx.append(i)

    if uncached_idx:
        if provider == "bedrock":
            await _bedrock_uncached_fill(
                resolved_model_id, input_type, texts, uncached_idx, results=results, keys=keys
            )
        elif provider in {"openai", "openai-compat", "gemini"}:
            uncached_texts = [texts[i] for i in uncached_idx]
            vecs = await _http_provider_uncached(provider, uncached_texts, resolved_model_id)
            for i, vec in zip(uncached_idx, vecs, strict=True):
                results[i] = vec
                await _cache_put(keys[i], vec)
        else:
            msg = (
                f"FDE_EMBED_PROVIDER={provider!r} invalid; choose one of "
                "['bedrock', 'gemini', 'openai', 'openai-compat']"
            )
            raise ValueError(msg)

    return _assert_filled(results)


def _assert_filled(results: list[list[float] | None]) -> list[list[float]]:
    """Narrow `list[list[float] | None]` to `list[list[float]]`.

    By this point every slot has been filled by either a cache hit or a
    provider call above -- `None` only ever appears as the initial
    placeholder. Raising here (rather than silently filtering) turns "a
    provider call returned fewer vectors than requested" into a loud bug
    instead of a quietly shorter result list that desyncs from its input.
    """
    filled: list[list[float]] = []
    for r in results:
        if r is None:
            raise AssertionError("embed_batch: a slot was never filled")
        filled.append(r)
    return filled
