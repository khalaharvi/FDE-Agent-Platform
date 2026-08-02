"""deploy/ -- provisioning for the gate service, as boto3 scripts.

Same shape as `fde_agents.deploy`: every module exposes
`main(argv: list[str] | None) -> int`, `cli.py` is a flat dispatcher, and
printing is the interface rather than a debugging leftover.

No Terraform, no CDK. Not because either is wrong, but because this
platform's other provisioning already reads as Python that calls the API
once per resource, and an operator who has to context-switch between a
`boto3` script for the agent runtimes and an HCL module for the Lambda in
front of them will keep the two in sync by hand anyway.

Idempotency is deliberately absent, in the same way it is absent from
`runtimes.py`: `create` does not check whether the resource already exists,
so re-running it fails with a name collision from the API instead of quietly
double-creating something security-relevant. Redeploy with `--update`.
"""

from __future__ import annotations

__all__ = ["api", "cli", "lambda_fn", "package", "schedule"]
