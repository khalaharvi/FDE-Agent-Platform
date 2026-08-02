"""Tests for the deploy CLI's artifact naming, stdio defaults, and CodeZip
staging.

Each case here pins a drift that had already shipped:

* the container URI was built as `fde-{agent}-agent` while the Dockerfile,
  the CI image build, and the README all name it `fde-{agent}` -- a
  mismatch only discoverable as an ImageNotFound at provisioning time;
* the dev stdio defaults pointed at `/app/mcp/server.py`, a path from the
  pre-workspace flat layout that exists nowhere, so stdio mode could only
  ever fail;
* the `code` artifact mode expected an S3 prefix containing a flattened
  `agent.py`, and nothing in the repo produced one.

Staging is exercised with `install_deps=False`. Vendoring wheels needs a
network round trip and a cross-platform pip resolve, which is a packaging
step, not a unit under test; what IS under test is the layout AgentCore
reads.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from fde_agents.common import mcp_tools
from fde_agents.common.config import GatewaySettings, get_agent_runtime_settings
from fde_agents.deploy import cli as deploy_cli
from fde_agents.deploy import codezip, runtimes
from fde_agents.engagement import agent as engagement_agent

REPO_ROOT = Path(__file__).resolve().parents[3]


# ===========================================================================
# Container / code artifacts
# ===========================================================================
def _artifact_args(**overrides: Any) -> argparse.Namespace:
    defaults = {
        "artifact_mode": "container",
        "ecr_registry": "123456789012.dkr.ecr.us-west-2.amazonaws.com",
        "image_tag": "v1",
        "code_bucket": "fde-code",
        "code_prefix_root": "fde-agents",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.mark.parametrize("agent_name", ["engagement", "workflow", "development"])
def test_container_uri_has_no_agent_suffix(agent_name: str) -> None:
    artifact = runtimes._build_artifact(agent_name, _artifact_args())
    uri = artifact["containerConfiguration"]["containerUri"]
    assert uri.endswith(f"/fde-{agent_name}:v1")
    assert "-agent:" not in uri


def test_container_uri_matches_the_image_ci_actually_builds() -> None:
    """The CI workflow and the Dockerfile are the other two places this name
    is written; if they move, this is the assertion that notices."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "fde-${{ matrix.agent }}" in ci or "fde-engagement" in ci
    artifact = runtimes._build_artifact("engagement", _artifact_args())
    assert "fde-engagement:v1" in artifact["containerConfiguration"]["containerUri"]


def test_code_artifact_prefix_matches_what_codezip_uploads_to() -> None:
    """`runtimes --artifact-mode code` and `codezip` have to agree on the
    key layout, or the runtime points at an empty prefix."""
    artifact = runtimes._build_artifact("engagement", _artifact_args(artifact_mode="code"))
    code = artifact["codeConfiguration"]
    assert code["runtime"] == "PYTHON_3_12"
    assert code["entryPoint"] == ["agent.py"]
    prefix = code["code"]["s3"]["prefix"]
    assert prefix == "fde-agents/engagement"
    assert codezip.s3_key("fde-agents", "engagement", "agent.py") == f"{prefix}/agent.py"


def test_python_version_pin_matches_the_codezip_runtime() -> None:
    """`.python-version` exists so the local interpreter, the container base
    image, and AgentCore's `PYTHON_3_12` cannot drift apart silently."""
    pinned = (REPO_ROOT / ".python-version").read_text().strip()
    assert pinned == codezip.PYTHON_VERSION == "3.12"


# ===========================================================================
# Gateway stdio settings
# ===========================================================================
def test_stdio_defaults_point_at_the_real_module_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("FDE_MCP_SERVER_CMD", "FDE_MCP_SERVER_ARGS", "FDE_MCP_SERVER_CWD"):
        monkeypatch.delenv(var, raising=False)
    get_agent_runtime_settings.cache_clear()

    settings = GatewaySettings.from_env()
    assert settings.stdio_command == "python3"
    assert settings.stdio_args == ("-m", "fde_mcp")
    assert settings.stdio_cwd is None


def test_stdio_settings_remain_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_MCP_SERVER_CMD", "/usr/bin/python3.12")
    monkeypatch.setenv("FDE_MCP_SERVER_ARGS", "-m fde_mcp --transport stdio")
    monkeypatch.setenv("FDE_MCP_SERVER_CWD", "/srv/app")
    get_agent_runtime_settings.cache_clear()

    settings = GatewaySettings.from_env()
    assert settings.stdio_command == "/usr/bin/python3.12"
    assert settings.stdio_args == ("-m", "fde_mcp", "--transport", "stdio")
    assert settings.stdio_cwd == "/srv/app"


def test_stdio_transport_passes_a_none_cwd_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """`StdioServerParameters.cwd` is `str | Path | None`; the default must
    reach it as None (inherit) rather than as an empty string."""
    for var in ("FDE_MCP_SERVER_CMD", "FDE_MCP_SERVER_ARGS", "FDE_MCP_SERVER_CWD"):
        monkeypatch.delenv(var, raising=False)
    get_agent_runtime_settings.cache_clear()

    captured: dict[str, Any] = {}

    class _Params:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(mcp_tools, "StdioServerParameters", _Params)
    mcp_tools._stdio_transport(GatewaySettings.from_env())
    assert captured["cwd"] is None
    assert captured["args"] == ["-m", "fde_mcp"]


# ===========================================================================
# CodeZip
# ===========================================================================
@pytest.mark.parametrize("agent_name", ["engagement", "workflow", "development"])
def test_entrypoint_shim_imports_the_agents_own_app(agent_name: str) -> None:
    shim = codezip.build_entrypoint_shim(agent_name)
    assert f"from fde_agents.{agent_name}.agent import app" in shim
    assert "app.run()" in shim
    compile(shim, "agent.py", "exec")


def test_entrypoint_shim_matches_the_real_agent_module_surface() -> None:
    """The shim re-exports rather than reimplements; if an agent module
    stopped defining `app` at import time it would break at deploy time,
    not here."""
    assert hasattr(engagement_agent, "app")
    assert callable(engagement_agent.app.run)


def test_entrypoint_shim_rejects_an_unknown_agent() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        codezip.build_entrypoint_shim("marketing")


def test_stage_agent_builds_the_layout_agentcore_reads(tmp_path: Path) -> None:
    staging = codezip.stage_agent("engagement", tmp_path / "engagement", install_deps=False)

    assert (staging / "agent.py").is_file()
    assert (staging / "fde_agents" / "engagement" / "agent.py").is_file()
    assert (staging / "fde_mcp" / "server.py").is_file()
    # fde_training must never reach an agent payload: it is what would drag
    # torch into a runtime image (docs/11-python-conventions.md §2).
    assert not (staging / "fde_training").exists()
    assert "fde_training" not in codezip.VENDORED_WORKSPACE_PACKAGES


def test_stage_agent_clears_a_previous_run(tmp_path: Path) -> None:
    staging = tmp_path / "engagement"
    codezip.stage_agent("engagement", staging, install_deps=False)
    stale = staging / "stale_from_last_time.py"
    stale.write_text("# leftover\n")

    codezip.stage_agent("engagement", staging, install_deps=False)
    assert not stale.exists()
    assert (staging / "agent.py").is_file()


def test_s3_key_normalises_a_trailing_slash_in_the_prefix_root() -> None:
    assert codezip.s3_key("fde-agents/", "workflow", "fde_mcp/server.py") == (
        "fde-agents/workflow/fde_mcp/server.py"
    )


def test_codezip_main_requires_a_bucket_unless_staging_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert codezip.main(["--agents", "engagement"]) == 2
    assert "--code-bucket is required" in capsys.readouterr().err

    assert codezip.main(["--agents", "engagement", "--stage-only", str(tmp_path), "--no-deps"]) == 0
    assert (tmp_path / "engagement" / "agent.py").is_file()


def test_codezip_is_reachable_from_the_deploy_cli() -> None:
    """README's quick-start runs `fde-agents-deploy codezip`; a module that
    is not in the dispatch table makes that a lie."""
    assert deploy_cli._SUBCOMMANDS["codezip"] is codezip.main
