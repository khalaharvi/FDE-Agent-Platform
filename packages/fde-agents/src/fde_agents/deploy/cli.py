"""One console-script entrypoint (`fde-agents-deploy`) for every operator
deploy action, replacing four separate `python -m fde_agents.deploy.<x>`
invocations with one command and a subcommand name.

Design intent
-------------
`runtimes.py`, `gateway.py`, `memory.py`, and `invoke.py` each already
expose a `main(argv: list[str] | None) -> int` -- the standard shape this
platform's other CLIs use (see `fde_mcp.__main__`, `fde_mcp.embedder_
worker`). This module does not reimplement any of their argument parsing or
provisioning logic; it is a thin `argparse` subparser dispatcher that hands
the remaining argv straight to the chosen module's own `main()`, so
`fde-agents-deploy runtimes --role-arn ...` and
`python -m fde_agents.deploy.runtimes --role-arn ...` accept identical
arguments and behave identically -- there is exactly one place each
subcommand's argument grammar is defined, here or there, never both.

Kept as one flat dispatch table (`_SUBCOMMANDS`) rather than four
`click`/`typer` command groups because every one of the four modules needs
to remain independently runnable (`python -m ...`) for scripts and runbooks
that already call them that way; this file is additive, not a replacement
for those entrypoints.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from . import gateway, invoke, memory, runtimes

_SUBCOMMANDS: dict[str, Callable[[list[str] | None], int]] = {
    "runtimes": runtimes.main,
    "gateway": gateway.main,
    "memory": memory.main,
    "invoke": invoke.main,
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
    print("usage: fde-agents-deploy <subcommand> [args...]")
    print(f"subcommands: {', '.join(sorted(_SUBCOMMANDS))}")


if __name__ == "__main__":
    sys.exit(main())
