"""Constants shared by `conftest.py` and the test modules.

A module rather than an import from `conftest`, because importing conftest by
a relative path needs `tests/__init__.py`, and a second `tests` package in
this workspace would collide with `fde-training`'s under pytest's default
prepend import mode -- both would want the module name `tests.test_config`.
The distinctive filename is what keeps this importable without a package.

They are constants rather than literals in each test because several tests
assert on an AUTHORITY failure, and a typo in a principal string produces an
IDENTITY failure that looks superficially like the test passing.
"""

from __future__ import annotations

__all__ = [
    "AGENT_PRINCIPAL",
    "BOUND_NODE_KEYS",
    "COMPLIANCE",
    "OWNER",
    "SME",
    "STRANGER",
]

# Reviewers the seeded roster knows about, and the gate kinds they hold:
#   SME         -- ontology, factual
#   COMPLIANCE  -- ontology, control
#   OWNER       -- ontology, factual, automation
#   STRANGER    -- registered, but no authority on the test engagement
SME = "sme@example.com"
COMPLIANCE = "compliance@example.com"
OWNER = "owner@example.com"
STRANGER = "stranger@example.com"

# The principal the fixtures record as `hitl.proposal.authored_by`. Distinct
# from every reviewer above so the default proposal is not a self-review.
AGENT_PRINCIPAL = "agent:engagement"

# Graph nodes the workflow fixtures bind their steps to. They must be live as
# of the pinned commit or `wf.assert_faithful` refuses to publish.
BOUND_NODE_KEYS = ("sys.cpq", "act.discount_review", "act.create_quote")
