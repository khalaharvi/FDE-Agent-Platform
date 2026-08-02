# Multi-provider LLM support — design

**Date:** 2026-08-02 · **Status:** approved by user, pre-implementation
**Sub-project 1 of 2.** Sub-project 2 (one-click AWS deploy) is a separate spec; this design is deliberately "local-first, deploy-ready" so that spec can offer provider choice at launch time without rework here.

## Problem

The platform is Bedrock-only. Users should be able to configure ("log in to") other LLM providers — Anthropic API, OpenAI, Google Gemini, and any OpenAI-compatible endpoint (OpenCode Zen, Pi, Azure, local Ollama/LM Studio) — and run the full propose → gate → merge flow with them. Today that is impossible twice over: the model call is hardcoded to `BedrockModel` (`packages/fde-agents/src/fde_agents/common/runtime.py:392`), and agents can only run inside AgentCore (no local driver exists; `fde-agents-deploy invoke` targets the deployed data plane only).

## Goals

1. Any supported provider can author agent turns through the whole flow, locally, with zero AWS credentials.
2. "Login" is a guided, validated, one-time act per provider (`fde-providers login`), with env vars always overriding for CI/servers.
3. Existing Bedrock deployments are untouched by default (`FDE_MODEL_PROVIDER` defaults to `bedrock`).
4. Deploy-ready: deployed runtimes consume the same configuration purely via env vars; sub-project 2 only has to map Secrets Manager → env.

## Non-goals (v1)

- Wiring provider keys into AgentCore deploy / Secrets Manager (sub-project 2).
- Multi-provider support in `fde-training`'s judge/trace paths (`generate_traces.py`, `rival_grader.py` stay Bedrock; noted in docs).
- The Mac-tier local presets from the shelved local-Metal roadmap (its seam ships here via `openai-compat`; the presets stay shelved).
- Schema changes: embeddings remain `vector(1024)` (`db/001`, `db/003`).

## Design

### 1. Provider registry (`fde-agents`)

New `packages/fde-agents/src/fde_agents/common/providers.py`:

- `ProviderSpec` (frozen dataclass): provider key, lazy model-class factory, credential env var name, keychain account name, human setup hint.
- `PROVIDERS`: `bedrock` | `anthropic` | `openai` | `gemini` | `openai-compat`, each building the corresponding Strands model class (`BedrockModel`, `AnthropicModel`, `OpenAIModel`, `GeminiModel`; `openai-compat` = `OpenAIModel` with `base_url`). All non-Bedrock imports are function-local (`# noqa: PLC0415` convention) behind `strands-agents[anthropic,openai,gemini]` extras.
- `COMPAT_PRESETS`: named base URLs for `opencode-zen` (`https://opencode.ai/zen/v1`), `ollama` (`http://127.0.0.1:11434/v1`), `lmstudio` (`http://127.0.0.1:1234/v1`). Pi is documented via explicit `FDE_MODEL_BASE_URL` (its endpoint is user-configurable). `FDE_MODEL_COMPAT_PRESET` selects one; explicit `FDE_MODEL_BASE_URL` wins.
- `build_model(provider, model_id, settings) -> strands Model` — replaces the hardcoded construction at `runtime.py:392`. Nothing downstream changes (`streaming.py` only calls `agent.stream_async`).

Selection config (all in `fde_agents/common/config.py`, per docs/11 §10):

| Env var | Default | Meaning |
|---|---|---|
| `FDE_MODEL_PROVIDER` | `bedrock` | provider key from the registry |
| `FDE_MODEL_ID` | — | required for any non-Bedrock provider (fails loudly; `MODEL_PRESETS` stay Bedrock-only) |
| `FDE_MODEL_BASE_URL` | — | `openai-compat` endpoint |
| `FDE_MODEL_COMPAT_PRESET` | — | named compat preset; `FDE_MODEL_BASE_URL` overrides |

`resolve_model_id()` gains the rule: non-Bedrock provider + no explicit `FDE_MODEL_ID` → `ValueError` naming the fix (same fail-loud philosophy as the existing preset-typo error). The audit trail (`tracing.start_session(model_id=...)`) records the qualified `"<provider>/<model_id>"` (e.g. `anthropic/claude-sonnet-5`) for non-Bedrock providers so the authoring model is unambiguous; bare model id for `bedrock` (unchanged, keeps existing audit rows comparable).

### 2. Credentials + `fde-providers` CLI (`fde-mcp`)

Lives in `fde-mcp` because dependency direction already flows `fde-agents → fde-mcp` (shared config precedent: `fde_mcp.config.AgentSettings`).

New `packages/fde-mcp/src/fde_mcp/credentials.py`:

- `resolve_api_key(provider) -> str`: provider env var (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `FDE_MODEL_API_KEY` for compat) → OS keychain (`keyring`, service `"fde-platform"`, account = provider key) → raise `ConfigError` whose message names both the env var and `fde-providers login <provider>`.
- Servers/runtimes never touch the keychain in practice: env wins, which is the entire deploy-ready story.

New console script `fde-providers` (`packages/fde-mcp/src/fde_mcp/providers_cli.py`, hand-rolled `--help` per repo convention):

- `login <provider>` — hidden prompt (`getpass`), then a **live validation ping** using the provider's cheapest authenticated call (models-list style, via httpx) *before* storing; a bad key fails at login, not mid-agent-turn. Stores via `keyring`.
- `logout <provider>` — removes the stored key.
- `status` — one row per provider: source (`env` / `keychain` / `none`) and validation result; the `bedrock` row reports the ambient AWS credential chain instead of a key.

Dependencies: `keyring` + explicit `httpx` on fde-mcp (`uv add --package fde-mcp`; httpx is already transitive via `mcp`).

### 3. Embeddings providers (`fde-mcp`)

`EmbeddingSettings` (`config.py`) gains:

| Env var | Default | Meaning |
|---|---|---|
| `FDE_EMBED_PROVIDER` | `bedrock` | `bedrock` \| `openai` \| `gemini` \| `openai-compat` |
| `FDE_EMBED_BASE_URL` | — | `openai-compat` endpoint |
| `FDE_EMBED_API_KEY` | — | compat key override (else `credentials.resolve_api_key`) |

`embeddings.py` dispatches on provider before the existing Titan/Cohere model-id logic (which remains the `bedrock` path, unchanged):

- `openai`: POST `https://api.openai.com/v1/embeddings` with `dimensions: 1024` (`text-embedding-3-small`/`-large`).
- `gemini`: `embedContent` with `output_dimensionality: 1024` (`gemini-embedding-001`).
- `openai-compat`: POST `{base_url}/embeddings`; `input_type` is ignored on this path (no such API field — docstring notes it).

All paths reuse the existing retry/backoff knobs and **hard-validate every returned vector is exactly `settings.dimensions` (1024)** with an error naming the model and known-good alternatives; `to_pgvector_literal` remains the backstop. Chat and embedding providers are independent — necessarily, since Anthropic and OpenCode Zen have no embeddings API. Docs give pairings (e.g. Anthropic chat + OpenAI embeddings; Gemini chat + Gemini embeddings). Explicit selection only; no fake or silent fallback (see the fake-embeddings incident, docs/99 §7). Retrieval SQL is untouched, so `test_parity.py` and its whitelist are untouched.

### 4. Local driver: `fde-agents-local` (`fde-agents`)

New `packages/fde-agents/src/fde_agents/local_runner.py` + console script:

- `fde-agents-local <engagement|workflow|development> --task <task> --engagement-id <id> [--input '<json>'] [--json]` — imports that agent module's module-level `handler` (shape per `create_app()`, `runtime.py:440-469`), builds the `TaskPayload` (`{"task", "engagement_id", "input"}`), drives the async generator via `asyncio.run`, prints events human-readably (`--json` for raw). With `FDE_GATEWAY_URL` unset, tools arrive over the existing local stdio MCP subprocess — already zero-AWS.
- `fde-agents-local doctor` — preflight: DB reachable via `FDE_DB_DSN`; chat provider credential resolves and validates; embed provider configured; warns if `FDE_GATEWAY_URL` is set. Actionable messages throughout.

### 5. Errors, testing, docs

**Error posture:** every misconfiguration fails loudly with the fix in the message — missing key → names env var + login command; invalid key → caught at `login`/`doctor` time; embeddings on a provider without them → error listing valid `FDE_EMBED_PROVIDER` values; non-1024 vector → error naming the model. No silent fallbacks anywhere.

**Testing (all network-free in CI):**
- `packages/fde-agents/tests/test_providers.py` — registry: each provider builds the right class with the right args (SDK classes monkeypatched); non-Bedrock-without-model-id raises; compat preset vs base-url precedence; qualified audit id.
- `packages/fde-mcp/tests/test_credentials.py` — resolution order env > keychain > error, using an in-memory `keyring` backend; `fde-providers` login/status against a stub HTTP validator.
- `packages/fde-mcp/tests/test_embeddings_providers.py` — per-provider request shapes + 1024-dim enforcement against an in-test `ThreadingHTTPServer`.
- `packages/fde-agents/tests/test_local_runner.py` — driver against a fake handler.
- `test_runtime.py`'s `_patch_agent_and_model` fixture adapts to patch `build_model`.
- Test module basenames unique across all packages' tests dirs (verified before creation). mypy `--strict` holds for fde-agents/fde-mcp; ruff clean; `uv lock --check` clean; the eight privilege-denial invariants are untouched (no DB changes at all).

**Docs:** new `docs/12-providers.md` (provider matrix incl. embeddings availability, login walkthrough, pairing guidance, compat presets incl. OpenCode Zen/Pi/local servers, honesty labels); README gains a "Bring your own model provider" collapsible; CLAUDE.md one line. Honesty rule: provider HTTP paths are stub-tested in CI and live-validated only when run with real keys — labeled accordingly, matching the AWS honesty convention.

**Workflow:** implementation lands on `feat/multi-provider-llm` → PR → CI green → **user merges** (user retains sole merge control).

## Decision log

- Architecture: native Strands provider classes + registry (chosen over LiteLLM — heavy dep in a strict tree — and over OpenAI-compat-shims-for-everything — degraded tool calling for the 21-tool loop).
- Login UX: keychain-backed `fde-providers login` with env override (chosen over env-only and plaintext config file).
- Scope: local-first, deploy-ready (Secrets-Manager wiring deferred to sub-project 2).
- OpenCode Zen and Pi ride the `openai-compat` provider as presets/documentation, not bespoke code.
