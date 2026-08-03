# Claude Desktop bundle (MCPB)

One-click install of the FDE MCP server into Claude Desktop. Build it with
`make bundle`, then drag `dist/fde-operator.mcpb` onto Claude Desktop.

At install Desktop asks for two things:

- **Database DSN** — the `fde_agent` Postgres DSN, stored in the OS keychain
  (`sensitive: true`). Provisioned by whoever runs the platform.
- **Platform checkout** — the directory holding this repository, chosen with a
  native folder picker.

## Why the checkout is a second setting

The bundle launches `uv run --project <checkout> --all-packages --frozen
fde-mcp`. It ships the manifest, not a vendored runtime. Two alternatives were
tried and rejected:

**Vendoring the server and its dependencies** (`uv pip install --target`, the
recipe in Anthropic's `build-mcpb` skill) produces a directory pinned to one
CPython minor version and one OS/architecture — the build here emitted
`pydantic_core/_pydantic_core.cpython-312-darwin.so`, which fails to import on
the same machine's Python 3.13 with `ModuleNotFoundError`. Desktop does not
guarantee which `python3` it resolves, so that bundle breaks silently on any
host whose interpreter differs from the build machine's.

**Shipping the server source with its own lock file** avoids the ABI problem
but creates a second dependency set that nothing in CI resolves or tests. The
workspace lock is a checked gate (`uv sync --all-packages --frozen`); a
bundle-local lock would drift from it unnoticed.

Launching from the checkout costs one folder picker and buys the guarantee that
Desktop runs byte-identical code to the test suite. It is also not a real extra
burden in v1: the DSN only exists because someone applied the `db/` migrations,
and that someone has the repository.

`uv` must be on PATH — https://docs.astral.sh/uv/.

## Verified here / not verified here

- `manifest.json` passes `mcpb validate` against the v0.4 schema, and
  `make bundle` produces a signed-nothing `.mcpb` archive locally.
- The launch command in `mcp_config` was run directly and starts the server.
- **Not verified here:** installing the built `.mcpb` into Claude Desktop. No
  Desktop install was exercised against this bundle; the install flow is
  written against the MCPB manifest spec, not validated here.
