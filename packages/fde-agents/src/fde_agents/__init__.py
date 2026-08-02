"""fde_agents -- the three Bedrock AgentCore runtimes (Engagement, Workflow,
Development) that turn a live customer engagement into a faithful,
evidence-backed knowledge graph, then into runnable workflows, then into
deployable agent packages.

Package shape
-------------
Everything the three runtimes share -- Pydantic models, tracing, the MCP
client, the HITL gate wait, guardrails, streaming, and (most importantly)
the `BedrockAgentCoreApp` scaffolding itself -- lives in `fde_agents.common`.
Each of `fde_agents.{engagement,workflow,development}` should read as a
*configuration* of `common.runtime`, not a reimplementation of it: a system
prompt, a task-to-prompt mapping, a tool allowlist, and the handful of
task-specific hooks (a guardrail check on one tool's arguments, a scaffold
file extraction) that genuinely differ between them. If a change to how
sessions are traced, how gates are waited on, or how errors are reported
ever needs to touch more than one of those three modules, that is a signal
the change belongs in `common.runtime` instead.

`fde_agents.deploy` is operator tooling (AgentCore Runtime/Gateway/Memory
provisioning, a smoke-test invoker) and is not imported by any of the three
runtimes above -- it is meant to run on a developer's or a CI pipeline's
machine, never inside an agent's own container.
"""

from __future__ import annotations
