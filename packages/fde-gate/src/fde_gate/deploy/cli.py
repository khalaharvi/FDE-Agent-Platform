"""`fde-gate-deploy` -- one entrypoint for every gate-service deploy action.

A thin argparse-free dispatcher over four modules that each own their own
argument grammar, exactly as `fde_agents.deploy.cli` dispatches over five.
It reimplements none of their parsing: `fde-gate-deploy api --jwt-issuer X`
and `python -m fde_gate.deploy.api --jwt-issuer X` take identical arguments
and do identical things, because there is exactly one place that grammar is
defined and it is not here.

The usual order, once per environment:

    fde-gate-deploy package  --out dist/fde-gate.zip
    fde-gate-deploy lambda   --role-arn ... --zip dist/fde-gate.zip
    fde-gate-deploy api      --jwt-issuer ... --jwt-audience ...
    fde-gate-deploy schedule
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from . import api, lambda_fn, package, schedule

_SUBCOMMANDS: dict[str, Callable[[list[str] | None], int]] = {
    "package": package.main,
    "lambda": lambda_fn.main,
    "api": api.main,
    "schedule": schedule.main,
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        _print_usage()
        return 0 if args and args[0] in ("-h", "--help") else 2

    subcommand, *rest = args
    handler = _SUBCOMMANDS.get(subcommand)
    if handler is None:
        print(
            f"unknown subcommand {subcommand!r}; must be one of {sorted(_SUBCOMMANDS)}",
            file=sys.stderr,
        )
        return 2
    return handler(rest)


def _print_usage() -> None:
    print("usage: fde-gate-deploy <subcommand> [args...]")
    print(f"subcommands: {', '.join(sorted(_SUBCOMMANDS))}")
    print()
    print("  package   build the Lambda deployment zip (arm64, py312)")
    print("  lambda    create or update the gate service function")
    print("  api       create the HTTP API, its JWT authorizer, routes and stage")
    print("  schedule  create the EventBridge tick and expiry rules")


if __name__ == "__main__":
    sys.exit(main())
