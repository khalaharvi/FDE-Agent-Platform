# fde-operator

A Claude plugin that makes the FDE Agent Platform operable from Claude Code and
Claude Desktop: ingest interviews as anchored evidence, launch agent tasks,
export workflow playbooks into a vault.

## Why this is safe to hand to a model

**The plugin runs as `fde_agent`. It can search, register evidence, and propose
— it structurally cannot write the graph, decide gates, or merge. Approvals stay
in the console under the operator's JWT.**

That is a property of the database, not of the prompt. The only path that writes
`kg.*` is `hitl.merge_proposal`, and the `fde_agent` role this server connects
as was never granted it. CI asserts the denials on every build ("core invariants
must hold", `.github/workflows/ci.yml`). So there is no jailbreak, tool
sequence, or unlucky context that turns a proposing model into a writing one —
the model's worst case is a bad proposal a human declines.

**What that claim covers.** It is a statement about the tool surface this plugin
adds, and it rests on one thing you control: `FDE_DB_DSN` must name the
`fde_agent` role. Point it at a superuser and you have removed the boundary
yourself. It is also not a claim about the whole session — a Claude Code session
has shell access with or without this plugin, so the guarantee is about what the
plugin hands the model, not about everything the operator's terminal can reach.

## What you get

Four commands, one skill, 23 MCP tools:

| Surface | What it does |
|---|---|
| `/ingest-interview` | Register a transcript, chunk it, anchor the chunks to real nodes, report coverage |
| `/run-agent` | Launch an engagement or workflow agent task, then hand the proposal to the console |
| `/playbook` | Export a published workflow as Markdown and file it in the vault |
| `/dashboard` | Export the decision dashboard — how work is flowing through the gates — and file it in the vault |
| `fde-operator` skill | The discipline behind all four — loads automatically when a session touches this platform |

The skill is the substantive part. It encodes the one thing that is easy to get
wrong and impossible to notice: **ingestion accepts chunks with no
`anchor_keys`, and retrieval discards them** (`db/008_retrieval.sql`), so a
naive ingest produces evidence the graph will never surface. The skill's §2
walks register → chunk → anchor → ingest → coverage check → enrichment loop.

## Install — Claude Code

The plugin lives inside the repository and launches the MCP server from it, so
the checkout is the install:

```bash
git clone https://github.com/khalaharvi/FDE-Agent-Platform ~/FDE-Agent-Platform
cd ~/FDE-Agent-Platform
uv sync --all-packages --frozen
createdb fde && ./db/rebuild.sh fde --with-demo
export FDE_DB_DSN=postgresql:///fde
```

Verify the server starts before going further — this is the command `.mcp.json`
runs, and it prints the tool count:

```bash
uv run --all-packages --frozen fde-mcp --help
```

Then install the plugin from this checkout. The repository is its own plugin
marketplace (`.claude-plugin/marketplace.json` at the repo root), so in Claude
Code:

```
/plugin marketplace add ~/FDE-Agent-Platform
/plugin install fde-operator@fde-agent-platform
```

Restart the session afterwards so the MCP server and commands load.

**Run Claude Code from the checkout.** Installing copies the plugin into
`~/.claude/plugins/cache/…`, well away from the repository, so `.mcp.json`
locates the workspace as `${CLAUDE_PROJECT_DIR}` — your session's project root
— rather than by walking up from the plugin. That is why the `cd` above
matters: the project root has to be the repository, or `uv` has no workspace to
run the server from.

### Settings

| Variable | Required | What it is |
|---|---|---|
| `FDE_DB_DSN` | yes | Postgres DSN for the `fde_agent` role, with `db/` applied. Passed through to the server. |
| `FDE_VAULT_DIR` | no | Where `/playbook` writes. Unset, the skill asks once per session. |

`.mcp.json` defaults the DSN to `postgresql:///fde` when the variable is unset,
so the plugin works against a local database with no configuration. Note the
consequence for a deployed database: `FDE_DB_DSN` **wins over**
`FDE_DB_SECRET_ARN` and `FDE_DB_IAM_AUTH` in the server's own resolution
(`packages/fde-mcp/src/fde_mcp/config.py`), so the default silently takes
precedence over Secrets Manager or IAM auth. Point `FDE_DB_DSN` at the real
database, or edit the default out of `.mcp.json`, before using either.

There is no model configuration here: the plugin uses whatever model your Claude
session runs. `FDE_MODEL_*` configures the platform's own agents, which are a
different thing (`docs/03-agent-workflow.md`).

### Verified / not verified

- **Verified:** the launch command starts the server and reports its tool count;
  it was exercised from an unrelated working directory with the plugin copied to
  a directory outside the repository, mirroring the layout `/plugin install`
  produces, to confirm it does not depend on the plugin sitting in the checkout.
- **Not verified:** a live `/plugin marketplace add` + `/plugin install` into a
  real Claude Code installation. Nothing here was installed into a user's
  `~/.claude`, so the install flow is written against the documented behaviour
  and the on-disk layout of already-installed plugins, not exercised end to end.

## Install — Claude Desktop

`mcpb/` builds a `.mcpb` bundle for one-click install. See `mcpb/README.md` for
the build, the two install-time settings, and why the bundle launches from a
checkout instead of vendoring a Python runtime.

## What it deliberately does not do

- **Approve, merge, or publish anything.** Those are console actions under the
  operator's own principal. The plugin produces links to them and stops.
- **Write to `kg.*`.** See above — it cannot.
- **Run SQL.** The MCP tools are the whole surface.

## Where the boundary is documented

- `docs/05-mcp-surface.md` — tool-by-tool contracts
- `docs/10-prodops-runbook.md` — the operator runbook: running a workflow, the
  review and drift queues, escalation, and the reviewer roster (§8)
- `docs-site/first-merge.mdx` — the propose → gate → approve → merge loop by
  hand, including the privilege denial you can reproduce in `psql`
