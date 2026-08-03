# Security Policy

## Reporting a vulnerability

Please do NOT open a public issue for security problems. Use GitHub's
private vulnerability reporting ("Report a vulnerability" under the
Security tab) so a fix can land before details are public. Expect an
acknowledgement within a few days.

Include: what you found, a minimal reproduction, and which layer it
breaks (see below) — that last part is the fastest way to convince us
it's real.

## What counts as a vulnerability here

This platform's core guarantee is: **agents propose, humans dispose;
only `hitl.merge_proposal` writes the knowledge graph.** That guarantee
is enforced at four independent layers (grants, SECURITY DEFINER
boundaries, in-transaction gate re-checks, fail-closed submission) and
asserted by twenty-nine privilege-denial checks in CI plus smoke tests 2–4
and 17–25.

Anything that lets an agent role write `kg.*` directly, merge without
satisfied gates, publish a workflow, label its own training data, or
read/write another role's surface is a critical vulnerability — even if
it needs a second bug to trigger. So is anything that leaks raw actor
identifiers (the `actor_hash` pipeline exists so no name or email is
ever stored) or that lets prompt-injected content in proposals/evidence
execute actions on the reviewer console (templates render with
autoescape on; bypasses are in scope).

Out of scope: vulnerabilities in AWS services themselves, denial of
service against your own deployment, and findings that require an
already-privileged database superuser.

## Supported versions

Pre-1.0: only the `main` branch receives fixes. The database schema is
rebuilt from scratch per deployment (no migration compatibility
guarantees yet).
