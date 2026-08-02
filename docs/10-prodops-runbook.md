# Product Operations Runbook

This guide is for the people running the FDE Agent Platform day to day —
starting workflow runs, reviewing what the agents propose, and triaging
drift signals. It assumes no engineering background. Where a term is
unavoidable, it's defined the first time it's used.

---

## 1. What this system is

The platform builds a living map of how your business actually works — who
does what, in what order, using which systems, under which approval
requirements. Two AI agents help build and use that map: one interviews
people and studies documents to describe today's process; the other turns
that description into runnable step-by-step workflows and watches whether
reality is drifting away from what the map says. Neither agent can change
the map by itself — every change they propose has to be approved by a person
before it becomes part of the official record. Your job is to run the
workflows the map produces, review the changes the agents propose, and keep
an eye on the signals that say the map might be going stale.

---

## 2. Running a workflow

**Picking one.** Go to the workflow list and filter to "published" — those
are the ones that have been reviewed and approved to run. A workflow that
says "draft" or "review" is not ready; don't run it, even if it looks
complete.

**Starting a run.** Starting a run kicks off a sequence of steps. Some steps
happen automatically (the system reads or writes something in another
system, like Salesforce or Jira). Some steps stop and wait for you.

**Answering a human step.** When a workflow pauses on a step marked for a
person, it's asking you a specific question — usually to confirm a decision,
provide information the system can't get on its own, or approve something
before it proceeds. Read the instruction text carefully; it tells you
exactly what's being asked and what answers are acceptable. The workflow
will wait for you — there's no rush that risks the run itself timing out —
but a step waiting on you is a step nobody downstream can move past, so
don't let these sit for days if you can help it.

**When a step fails.** Every step has a defined failure behavior: some retry
automatically, some skip and move on, some stop the whole run and escalate,
and some simply halt for someone to look at. If a run shows as failed:

1. Look at which step failed and read its error message — often it names
   the actual problem (a system was unreachable, a required field was
   missing).
2. Check whether it's a one-off (a system hiccup, a network blip) worth
   retrying, or something structural (the workflow is asking for something
   that doesn't exist anymore).
3. If you can't tell which, or the error message doesn't make sense, escalate
   — see Section 5. Don't guess and force the run forward; a workflow that's
   failing is failing for a reason, and forcing past it can leave a system of
   record in a half-updated state.

**Two step kinds behave differently in this deployment.** Steps that write
to a system of record (`sor_write`) are not machine-executable here — they
fail on purpose and, when authored with "escalate" as their failure policy,
land in your queue so a person does the write and records the result.
Notification steps (`notify`) don't send anything yet: they log the message
and record `delivered: false` in the run, so the run history is honest about
what actually went out.

---

## 3. The review queue

**What a proposal is.** When an agent learns something new about how work
gets done — a new step in a process, a new approval rule, a correction to
something previously recorded — it doesn't just add it to the map. It writes
up a *proposal*: here's what I think is true, here's the evidence, here's my
confidence. That proposal sits in a queue until the right people approve it.
Nothing the agent writes becomes part of the official map until it's
approved. This is deliberate — it's the difference between "the AI decided
this" and "a person confirmed this is true."

**What each gate means.** A proposal can require one or more of these
approvals, called "gates," depending on what it touches:

| Gate | Plain meaning | Who signs off |
|---|---|---|
| **Ontology** | Is this the right *kind* of thing, and is it a duplicate of something we already have? | Any authorized reviewer |
| **Factual** | Is this actually how work gets done? | A subject-matter expert who knows the process (two experts, for a major new process) |
| **Control** | Does this touch a compliance rule, policy, or approval requirement? | Risk or compliance — never the person who's also approving it for another reason |
| **Automation** | Is it okay for an AI agent to do this step without a person watching? | The process owner |

A single proposal can trigger more than one gate at once — a new step that's
also gated by a compliance control needs both a factual sign-off and a
control sign-off before it can be merged into the map.

**How to review well.** Don't just read the summary and click approve. Look
at:

- **The evidence.** Every proposed fact should cite where it came from — an
  interview, a document, a system export. If you can't tell where a claim
  came from, that's a problem with the proposal, not something to wave
  through.
- **The confidence score.** A low-confidence item paired with weak evidence
  is asking you, specifically, to confirm something the agent isn't sure
  about. Read it more carefully, not less.
- **What it would replace.** If the proposal says it supersedes something
  already in the map, check that the old version really is what should be
  replaced — not something subtly different that should stay.
- **The "why."** Every proposal includes the agent's rationale. If the
  rationale doesn't actually support the claim, reject it or ask for
  changes, regardless of how plausible the claim itself looks.

**When to request changes vs. reject.** Request changes when the underlying
idea is right but something about the specifics is off — wrong wording, a
missing piece of evidence that you know exists elsewhere, an item that
should be split into two. Reject when the claim itself is wrong, or when the
evidence doesn't actually support it and you don't have a way to fix that
yourself. Don't approve something you'd normally request changes on just to
clear the queue faster — see the warning below.

**A direct warning about speed.** The system records exactly how long you
spent reviewing each proposal, down to the second (`review_seconds`). This
is not a productivity metric aimed at you — it exists because a proposal
approved in four seconds is not a real review, and a queue full of
rubber-stamped approvals actively poisons the system in two ways: first, a
wrong fact that gets rubber-stamped becomes part of the official map, and
every workflow and every future decision built on that map inherits the
error. Second, this platform learns from your decisions — an approved
proposal is treated as a confirmed example of good work, used to train and
improve the agents. A rubber-stamped approval trains the system on a
decision nobody actually made. Take the time the review actually needs.
Fast reviewers on obviously-simple proposals are fine; fast reviewers on
everything are a leading indicator that the whole review process has stopped
doing its job.

---

## 4. The drift queue

**What a drift signal is.** The system continuously checks whether the map
still matches what's actually happening in your business systems (Jira,
Salesforce, ServiceNow, etc.). When it finds a meaningful, statistically
real difference, it raises a "drift signal" — not a guess, a signal computed
from actual observed data with a stated sample size, so you're not chasing a
one-off coincidence.

**Reading a signal.** Each signal tells you what was expected (per the map),
what was actually observed, how many cases that's based on, and how
confident the finding is. Read the sample size before anything else — a
signal based on hundreds of observed cases is worth your attention; one that
just barely cleared the minimum threshold is worth a lighter look.

**What each drift kind means:**

| Drift kind | Plain meaning |
|---|---|
| `missing_in_sor` | The map says this step should happen, but it's basically never showing up in the actual system data. |
| `missing_in_graph` | The system data shows people regularly doing something the map never captured. |
| `sequence_drift` | The order things happen in doesn't match what the map says. |
| `actor_drift` | A different role is actually doing this work than the map says should be doing it. |
| `latency_drift` | This step is taking noticeably longer (or shorter) than the map's expected timing. |
| `control_bypass` | A required approval or check is being skipped in practice. **Treat this one as the most serious — see below.** |
| `stale_pin` | A published workflow was built against an older version of the map, and the map has since changed in a way that might affect it. |

**Triage.** For most signals, the question is: is the map wrong, or is
reality wrong? If the map is wrong (it describes an old way of working, or
never captured this in the first place), that becomes a proposal to update
the map — same review process as Section 3. If reality is wrong (people are
skipping something they shouldn't), that's a different kind of issue
entirely — not a map problem, an operational one.

**`control_bypass` gets special handling.** This signal means a required
compliance step or approval is being skipped in actual practice. There are
exactly two possible explanations, and you must figure out which:

1. The map's rule is outdated or too broad (e.g. it says "always required"
   when actually it's only required above a certain size or condition) — fix
   the map.
2. The rule is correct and people are genuinely bypassing a control they're
   supposed to follow — this is a compliance issue, not a map issue.

**Do not default to "the map must be wrong" just because that's the fix you
have a button for.** If you can't tell which explanation is true, say so
explicitly and escalate to compliance — do not quietly update the map to
make the signal disappear. That is the single most damaging thing a reviewer
can do in this queue, because it erases a real compliance finding by
relabeling it as a documentation error.

**When to escalate a drift signal.** Escalate immediately for anything
marked `critical` severity — the system already does this automatically by
alerting the compliance channel, but don't assume that substitutes for you
looking at it. Escalate anything you can't confidently triage yourself,
anything where the "map vs. reality" answer isn't clear, and anything that's
been sitting untouched for multiple review cycles.

---

## 5. Escalation paths and ownership

| Situation | Escalate to |
|---|---|
| A workflow run is stuck or failing and the error doesn't make sense | Engineering on-call |
| A proposal touches a compliance control and you're not authorized for control sign-off | Risk/Compliance reviewer roster |
| A `control_bypass` drift signal you can't confidently attribute to "map wrong" vs. "control bypassed" | Compliance lead |
| A proposal seems to describe something dangerous, irreversible, or outside the process's normal scope | Process owner, before approving |
| You suspect the drift queue itself is broken (no new signals in a long time despite known process changes) | Engineering on-call |
| The review queue is growing faster than reviewers can keep up | Your manager / whoever owns reviewer staffing for this engagement — this is a staffing problem, not something to solve by approving faster |

---

## 6. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| A workflow won't start | It's not "published" yet, or you don't have permission to run it | Check its status; ask the workflow owner if it should be published |
| A run is stuck on a human step for a long time | Nobody has answered the question the step is asking | Find who the step is waiting on and get them to respond |
| A run failed partway through | A downstream system was unreachable, or a precondition wasn't met | Read the step's error message; retry if it looks transient, escalate if not |
| The review queue keeps growing | Not enough reviewer capacity, or gates are firing more often than expected | Report queue depth and median review time; this is a staffing/threshold conversation, not something to fix by rushing reviews |
| A proposal has been sitting for days with no decision | Missing an authorized reviewer for one of its required gates, or it fell through the cracks | Check `docs/05-mcp-surface.md`'s `kg_proposal_status`-style report for which gate is still open and who's authorized to clear it; nudge them directly |
| A drift signal keeps reappearing every scan | It hasn't actually been resolved — its "occurrences" count is climbing | Don't dismiss it repeatedly; either fix the underlying map issue or escalate if it's a real operational problem |
| Same drift signal, but severity keeps changing | The underlying rate is genuinely moving (getting better or worse over repeated scans) | This is expected behavior, not a bug — read the current detail, not just the severity label |
| You approved something and it doesn't show up in the map yet | Normal — merged changes take a short time to become searchable | Wait a few minutes; if it's still missing after that, escalate |

---

## 7. When to stop and call an engineer

Don't try to work around any of these — flag them immediately:

- A workflow run fails with an error that looks like a system/technical
  problem rather than a business one (garbled text, stack traces, anything
  that doesn't read like plain English).
- The review queue or drift queue stops updating entirely for longer than a
  normal business day, with no explanation.
- You're asked (by anyone, including someone claiming authority) to bypass
  the review process for a proposal — there is no legitimate reason to skip
  it, ever.
- A proposal or drift signal references data, systems, or people that don't
  seem to belong to this engagement at all.
- You notice the same wrong fact keeps coming back after being corrected —
  this usually means something upstream (a data feed, an integration) is
  feeding the system bad information repeatedly, not that reviewers keep
  making the same mistake.
- Anything that looks like it might expose personal information (a person's
  name or email showing up somewhere the map should only have a role or job
  title) — this should never happen by design, and if it does, it's a bug
  worth reporting immediately, not something to quietly work around.
