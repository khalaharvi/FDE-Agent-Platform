# Build the Claude Desktop bundle (.mcpb) for the FDE operator MCP server.
#
# The bundle carries the manifest, not the server: `manifest.json` launches
# `uv run --project <checkout> fde-mcp`, so Desktop runs the same code CI
# tests. See README.md in this directory for why it is packaged that way.
#
#   make validate   # check manifest.json against the MCPB v0.4 schema
#   make bundle     # stage + pack dist/fde-operator.mcpb
#   make clean

MCPB    ?= npx --yes @anthropic-ai/mcpb@2.1.2
BUILD   := build
DIST    := dist
BUNDLE  := $(DIST)/fde-operator.mcpb

.PHONY: all validate bundle clean
all: bundle

validate:
	$(MCPB) validate manifest.json

# Staged rather than packed in place so the artifact holds exactly the two
# files a host needs -- not this Makefile and not a previous dist/.
bundle: validate
	rm -rf $(BUILD)
	mkdir -p $(BUILD) $(DIST)
	cp manifest.json README.md $(BUILD)/
	$(MCPB) pack $(BUILD) $(BUNDLE)

clean:
	rm -rf $(BUILD) $(DIST)
