#!/usr/bin/env python3
"""Structural checks for the fde-operator plugin.

There is no pytest harness for Claude plugins in this repo, so this is the
gate: every JSON file parses, every command and skill carries frontmatter that
parses as YAML with the fields Claude Code reads, the expected files exist, and
-- the check that actually rots -- every repository path cited in any plugin
file still exists. A command that points at a moved file is a command that
sends a fresh session somewhere that is not there.

    python3 claude-plugin/fde-operator/scripts/check_plugin.py

Exits 0 if everything holds, 1 with one "FAIL <what>: <why>" line per problem.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parent.parent

JSON_FILES = (".claude-plugin/plugin.json", ".mcp.json", "mcpb/manifest.json")
COMMANDS = ("ingest-interview", "run-agent", "playbook")
EXPECTED = (
    ".claude-plugin/plugin.json",
    ".mcp.json",
    "README.md",
    "skills/fde-operator/SKILL.md",
    "mcpb/manifest.json",
    "mcpb/Makefile",
    "mcpb/README.md",
)

# A repo path cited in prose: `packages/...`, `db/008_retrieval.sql`,
# `docs/05-mcp-surface.md`. Anchored to the top-level directories the repo
# actually has, so ordinary backticked prose ("`anchor_keys`") is not mistaken
# for a path. Extend the alternation when the plugin starts citing a new one.
CITED_PATH = re.compile(r"`((?:packages|docs|docs-site|db|infra|tests|\.github)/[\w./-]+)`")

failures: list[str] = []


def fail(what: str, why: str) -> None:
    failures.append(f"FAIL {what}: {why}")


def check_json() -> None:
    for rel in JSON_FILES:
        path = PLUGIN / rel
        if not path.is_file():
            fail(rel, "missing")
            continue
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            fail(rel, f"does not parse: {exc}")


def check_expected_files() -> None:
    for rel in EXPECTED:
        if not (PLUGIN / rel).is_file():
            fail(rel, "missing")
    for name in COMMANDS:
        if not (PLUGIN / "commands" / f"{name}.md").is_file():
            fail(f"commands/{name}.md", "missing")


def frontmatter(path: Path) -> dict[str, object] | None:
    """The YAML block between the leading `---` fences, or None if the file
    has no frontmatter at all (which is itself a failure for these files).
    """
    text = path.read_text()
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    block = text[4 : end + 1]
    loaded = yaml.safe_load(block)
    return loaded if isinstance(loaded, dict) else None


def check_frontmatter() -> None:
    targets = [(PLUGIN / "commands" / f"{n}.md", ("description",)) for n in COMMANDS]
    targets.append((PLUGIN / "skills/fde-operator/SKILL.md", ("name", "description")))
    for path, required in targets:
        rel = path.relative_to(PLUGIN)
        if not path.is_file():
            continue  # already reported by check_expected_files
        try:
            meta = frontmatter(path)
        except yaml.YAMLError as exc:
            fail(str(rel), f"frontmatter is not valid YAML: {exc}")
            continue
        if meta is None:
            fail(str(rel), "no YAML frontmatter block")
            continue
        for field in required:
            if not str(meta.get(field, "")).strip():
                fail(str(rel), f"frontmatter is missing `{field}`")


def check_plugin_name() -> None:
    manifest = PLUGIN / ".claude-plugin/plugin.json"
    if not manifest.is_file():
        return
    plugin = json.loads(manifest.read_text())
    name = plugin.get("name")
    if name != PLUGIN.name:
        fail(".claude-plugin/plugin.json", f"name {name!r} != directory {PLUGIN.name!r}")

    # Three files declare this plugin's version -- plugin.json, the MCPB
    # manifest, and the marketplace entry (checked in check_marketplace). They
    # ship as one artifact, so a bumped version that reaches only one of them
    # means Claude Code and Claude Desktop advertise different releases of the
    # same thing.
    bundle = PLUGIN / "mcpb/manifest.json"
    if bundle.is_file():
        bundled = json.loads(bundle.read_text()).get("version")
        if bundled != plugin.get("version"):
            fail(
                "mcpb/manifest.json",
                f"version {bundled!r} != plugin.json {plugin.get('version')!r}",
            )


def check_marketplace() -> None:
    """The repo-root marketplace entry must point at this plugin and agree with
    its manifest. Without the entry there is no `/plugin install` command; with
    a stale one, the install resolves to nothing.
    """
    path = REPO / ".claude-plugin/marketplace.json"
    if not path.is_file():
        fail(".claude-plugin/marketplace.json", "missing -- nothing can install this plugin")
        return
    try:
        market = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        fail(".claude-plugin/marketplace.json", f"does not parse: {exc}")
        return

    entry = next((p for p in market.get("plugins", []) if p.get("name") == PLUGIN.name), None)
    if entry is None:
        fail(".claude-plugin/marketplace.json", f"has no entry named {PLUGIN.name!r}")
        return

    source = (REPO / entry.get("source", "")).resolve()
    if source != PLUGIN:
        fail(
            ".claude-plugin/marketplace.json", f"source {entry.get('source')!r} is not this plugin"
        )
    if not (source / ".claude-plugin/plugin.json").is_file():
        fail(".claude-plugin/marketplace.json", "source has no .claude-plugin/plugin.json")

    manifest = PLUGIN / ".claude-plugin/plugin.json"
    if manifest.is_file():
        declared = json.loads(manifest.read_text()).get("version")
        if entry.get("version") != declared:
            fail(
                ".claude-plugin/marketplace.json",
                f"version {entry.get('version')!r} != plugin.json {declared!r}",
            )


def check_cited_paths() -> None:
    """Every `repo/relative/path` in any plugin file resolves in the repo."""
    for path in sorted(PLUGIN.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".json"}:
            continue
        if "build/" in str(path) or "dist/" in str(path):
            continue
        rel = path.relative_to(PLUGIN)
        for cited in sorted(set(CITED_PATH.findall(path.read_text()))):
            if not (REPO / cited).exists():
                fail(str(rel), f"cites `{cited}`, which does not exist")


def check_no_absolute_paths() -> None:
    """No machine-specific paths or email addresses committed."""
    banned = re.compile(r"/Users/[\w.-]+|/home/[\w.-]+|[\w.+-]+@[\w-]+\.[\w.]+")
    allowed = {"noreply@anthropic.com"}
    for path in sorted(PLUGIN.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".json", ".py"}:
            continue
        if "build/" in str(path) or "dist/" in str(path):
            continue
        rel = path.relative_to(PLUGIN)
        for hit in sorted(set(banned.findall(path.read_text()))):
            if hit in allowed:
                continue
            fail(str(rel), f"contains a machine-specific path or address: {hit}")


def main() -> int:
    check_json()
    check_expected_files()
    check_frontmatter()
    check_plugin_name()
    check_marketplace()
    check_cited_paths()
    check_no_absolute_paths()

    # sys.stdout.write rather than print(), matching the repo's other
    # operator-facing entrypoints (fde_agents/local_runner.py) -- it keeps this
    # file clean under the workspace ruff config without needing a T20 ignore.
    if failures:
        for line in failures:
            sys.stdout.write(line + "\n")
        sys.stdout.write(f"\n{len(failures)} problem(s)\n")
        return 1
    for ok in (
        f"json parses ({len(JSON_FILES)} files)",
        "expected files present",
        "marketplace entry resolves to this plugin",
        "version agrees across plugin.json / mcpb / marketplace",
        "command + skill frontmatter parses as YAML",
        "every cited repo path exists",
        "no absolute paths or email addresses",
    ):
        sys.stdout.write(f"ok  {ok}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
