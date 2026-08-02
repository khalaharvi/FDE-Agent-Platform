"""Deployment tooling for the FDE Platform's three AgentCore runtimes.

Nothing in this package is meant to run inside an agent's own container --
these are operator-run scripts (control-plane provisioning and a CLI
invoker), kept in the same repository so the runtime definitions
(`fde_agents.{engagement,workflow,development}`) and the infrastructure
that creates them never drift apart silently. Reachable either as
individual modules (`python -m fde_agents.deploy.runtimes ...`) or through
the single `fde-agents-deploy` console script (`cli.py`), which dispatches
to the same `main(argv)` functions each module already exposes.
"""

from __future__ import annotations
