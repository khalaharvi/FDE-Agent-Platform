# Bring your own model provider

Bedrock stays the default and needs nothing new. This document is for the
other path: authoring agent turns with the Anthropic API, OpenAI, Google
Gemini, or any OpenAI-compatible endpoint — including OpenCode Zen, Pi, and
a local Ollama or LM Studio server — and running the whole propose → gate →
merge flow with zero AWS credentials.

---

## 1. Provider matrix

| Provider | `FDE_MODEL_PROVIDER` | Chat | Embeddings | Credential env var |
|---|---|---|---|---|
| Bedrock | `bedrock` (default) | ✓ | ✓ Titan / Cohere | ambient AWS credential chain |
| Anthropic API | `anthropic` | ✓ | ✗ — pair with another provider's embeddings | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | ✓ | ✓ `text-embedding-3-small` / `-large` @ `dimensions=1024` | `OPENAI_API_KEY` |
| Google Gemini | `gemini` | ✓ | ✓ `gemini-embedding-001` @ `output_dimensionality=1024` | `GEMINI_API_KEY` (falls back to `GOOGLE_API_KEY`) |
| OpenAI-compatible | `openai-compat` | ✓ | ✓ if the server serves `/v1/embeddings` — 1024-dim models: `mxbai-embed-large`, `bge-m3`, `snowflake-arctic-embed-l` | `FDE_MODEL_API_KEY` (unset is fine for keyless local servers) |

Chat and embeddings are selected independently (`FDE_MODEL_PROVIDER` vs.
`FDE_EMBED_PROVIDER`) — necessarily, since Anthropic has no embeddings API
and OpenCode Zen (an `openai-compat` preset, see §4) is chat-only. Pick a
pairing: Anthropic chat + OpenAI embeddings, or one provider for both —
Bedrock, OpenAI and Gemini each cover chat and embeddings unaided, and an
`openai-compat` server does too once it serves `/v1/embeddings` with one of
the 1024-dim models above. What sets Bedrock apart is reach rather than
pairing: it is the only provider the offline training pipeline can use at
all (§5). The 1024-dim figure is not a preference — `kg.embedding` is a
fixed `vector(1024)` domain (`db/001`, `db/003`), and every embedding path
hard-validates the returned vector length against it with no silent
truncation, padding, or fallback.

---

## 2. Login walkthrough

`fde-providers` (installed with `fde-mcp`) manages API keys in the OS
keychain so they never land in a config file:

```bash
uv run fde-providers login anthropic     # hidden prompt, validates live before storing
uv run fde-providers status              # one row per provider: source + validation
```

`login` makes one authenticated GET against the provider (Anthropic/OpenAI
`/v1/models`, Gemini's `models` endpoint, or `{FDE_MODEL_BASE_URL}/models`
for `openai-compat`) before writing anything, so a bad key fails at login
time, not mid-agent-turn. `logout <provider>` removes the stored key;
`status --no-validate` skips the live check (useful offline).

Environment variables always win over the keychain — `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `FDE_MODEL_API_KEY`
for `openai-compat`. That is the entire deploy-ready story: a server or CI
job sets the env var and never touches a keychain; only a developer laptop
runs `login` once and forgets about it. `bedrock` has no key here at all —
it uses the ambient AWS credential chain, and `status` reports whether one
looks present (`AWS_ACCESS_KEY_ID` / `AWS_PROFILE` / `AWS_ROLE_ARN`).

---

## 3. Run the full flow locally

This is the exact sequence for an Anthropic-chat, OpenAI-embeddings run,
from a clean checkout, no AWS account:

```bash
# database -- README's Quick start section (compose + rebuild.sh), then:
export FDE_DB_DSN="<the DSN from that section>"

# credentials
uv run fde-providers login anthropic
uv run fde-providers login openai

# provider selection
export FDE_MODEL_PROVIDER=anthropic FDE_MODEL_ID=claude-sonnet-5
export FDE_EMBED_PROVIDER=openai FDE_EMBED_MODEL_ID=text-embedding-3-small

# preflight, then a real task
uv run fde-agents-local doctor
uv run fde-agents-local engagement --task ingest_interview \
  --engagement-id <id> --input '{"material": "..."}'

# review what it proposed
FDE_GATE_DEV_PRINCIPAL=sme@example.com uv run fde-gate-dev   # → http://127.0.0.1:8787/ui
```

`doctor` runs five checks — DB reachable via `FDE_DB_DSN`, the chat
provider's credential resolves and validates live, `FDE_MODEL_ID` resolves,
the embeddings provider is configured, and a warning if `FDE_GATEWAY_URL`
is set — and prints `ok <name>` or `FAIL <name>: <problem>` per check.
Local runs need `FDE_GATEWAY_URL` unset: with it unset, `fde-agents-local`
talks to the same MCP tool surface a deployed runtime uses over a local
stdio subprocess, so the task exercises the identical code path a
production invocation does. `FDE_MODEL_ID` is required for every
non-Bedrock chat provider (`MODEL_PRESETS` name Bedrock model ids, which
mean nothing elsewhere) — `resolve_model_id()` raises rather than guessing.
`FDE_MODEL_MAX_TOKENS` (default 8192) applies to every provider and is
required by Anthropic's API specifically.

Engagement tasks: `map_process`, `score_opportunities`,
`detect_bottlenecks`, `ingest_interview`. `fde-agents-local` also runs
`workflow` and `development` agents the same way. Every proposal this
produces still goes through the same human gates as a Bedrock-authored one
— nothing about provider choice changes what `hitl.merge_proposal` accepts.

**The pairing rule, stated plainly:** if your chat provider is Anthropic or
an OpenCode-Zen-backed `openai-compat` endpoint, you must set a *different*
`FDE_EMBED_PROVIDER` — neither has an embeddings API, and `doctor`'s
embeddings check will fail loudly (naming `fde-providers login <provider>`)
rather than silently falling back to anything.

---

## 4. Compat presets

`FDE_MODEL_PROVIDER=openai-compat` talks to any server that speaks the
OpenAI chat-completions wire shape. A base URL is required and nothing is
guessed: set `FDE_MODEL_BASE_URL`, or name one of the three presets below
with `FDE_MODEL_COMPAT_PRESET`. With neither set there is nothing to
resolve, and `doctor`'s model-provider check fails saying so. An explicit
`FDE_MODEL_BASE_URL` always overrides the preset. The value ends where the
preset URLs end — at the API version segment, `.../v1` — because the chat
path appends `/chat/completions` to it and login validation appends
`/models`:

| Preset | Base URL | Notes |
|---|---|---|
| `opencode-zen` | `https://opencode.ai/zen/v1` | get a key at opencode.ai; chat-only, no embeddings |
| `ollama` | `http://127.0.0.1:11434/v1` | local, keyless |
| `lmstudio` | `http://127.0.0.1:1234/v1` | local, keyless |

Pi is not a named preset — its endpoint is account-configurable, so set
`FDE_MODEL_BASE_URL` explicitly to whatever Pi issues you.

Keyless local servers (Ollama, LM Studio, most self-hosted `vLLM`
deployments) work without `fde-providers login` at all: if no credential
resolves for `openai-compat`, `build_model` sends the literal string
`"local"` as the API key rather than failing, since these servers do not
check it. A hosted `openai-compat` endpoint that does require a key still
needs one — via `FDE_MODEL_API_KEY` or `fde-providers login openai-compat`.

`FDE_EMBED_PROVIDER=openai-compat` needs its own base URL, and here the
requirement is unconditional: there is no embeddings preset table, so
`FDE_EMBED_BASE_URL` is the only way to name the server and both `doctor`
and the embedder itself refuse without it. It also needs that server to
serve `/v1/embeddings` with a 1024-dim model — `mxbai-embed-large`,
`bge-m3`, and `snowflake-arctic-embed-l` are known-good choices.

---

## 5. Caveats, honestly labeled

- **Stub-tested, not live-validated.** Every non-Bedrock HTTP path (chat
  model construction and every embeddings provider) is exercised in CI
  against in-test HTTP stubs (`test_providers.py`, `test_credentials.py`,
  `test_embeddings_providers.py`) that verify request/response *shape*.
  None of it has run against a live Anthropic, OpenAI, Gemini, or
  third-party endpoint in this environment — the same honesty rule this
  repo applies to Bedrock/AgentCore (see README "Status and honesty").
  `fde-providers login` and `doctor`'s live validation call are the first
  point any of this touches a real provider, and only when you run them
  with real keys.
- **Switching embedding provider or model mid-graph strands old vectors.**
  `kg.embedding` does not record which provider produced a vector; if you
  ingest under `FDE_EMBED_PROVIDER=openai` and later switch to `gemini`,
  older rows are not re-embedded automatically and `kg.hybrid_search`
  will silently mix vector spaces. Pick a provider and model before
  ingesting, or explicitly re-enqueue affected nodes/edges/chunks through
  the embedder worker after a change.
- **`fde-training`'s judge and trace paths remain Bedrock-only.**
  `generate_traces.py` and `rival_grader.py` were out of scope for this
  work and still call Bedrock directly; multi-provider selection here only
  covers the three AgentCore agent runtimes and the embedder, not the
  training pipeline.
- **1024 dimensions is a database constraint, not a preference.**
  `kg.embedding`'s column type is `vector(1024)` (`db/001`, with HNSW
  indexes added in `db/003`). Every embeddings provider path
  hard-validates each returned vector against `settings.dimensions`
  (default 1024) and raises, naming the model and known-good
  alternatives, rather than truncating, padding, or silently degrading
  retrieval quality.
