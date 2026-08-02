# Blueprint 3 — Development Agent

> *Agent that develops end-to-end workflow agents · Evals · Guardrails · Full tool-calling functionality*

**Runtime name:** `fde_development`
**Code:** `agents/development/`
**Role in the graph:** meta. Consumes published workflows; emits deployable agent packages. Writes no graph facts at all.

---

## 1. Contract

### Identity

Takes a published, faithful workflow and produces everything needed to run it as a
deployed agent: a system prompt, a tool allowlist, a memory configuration, a
guardrail set, an eval suite, and the AgentCore deployment manifest. It is the
factory, not a product.

Its output is a **package for human review**, not a deployment. It never calls
`create_agent_runtime`. The reason is narrow and worth stating: an agent that can
deploy agents can deploy an agent with a tool allowlist it chose, and the allowlist
is the entire security boundary. That decision stays with a person.

### Tasks

| Task | Input | Output |
|---|---|---|
| `generate_agent_spec` | `workflow_id` | complete agent definition (prompt, tools, memory, autonomy, guardrails) |
| `generate_evals` | `workflow_id` or spec | AgentCore Evaluations config + custom Lambda graders |
| `generate_guardrails` | spec | Bedrock Guardrail config + code guards |
| `scaffold_agent` | spec | a deployable package: `agent.py`, `prompt.py`, `Dockerfile`, IaC, tests |
| `review_agent` | an existing agent package | critique against the workflow it claims to implement |

---

## 2. Derivation rules: workflow → agent spec

Mechanical, again. Everything the agent generates traces to something in `wf.*` or
`kg.*`.

### Tool allowlist

Derived from the workflow's step bindings, not chosen:

```
for step in workflow.steps:
    if step.kind == 'tool':      allow step.tool_name
    if step.kind == 'sor_write': allow the adapter's write op for step.sor_adapter_key
    if step.kind == 'agent':     allow the read tools its bindings require
    if step.kind == 'human':     allow nothing (it is a pause, not a capability)
```

Plus the read tools implied by `depends_on` bindings. **Nothing else.** The prompt
is explicit: *"If a tool is not required by a step binding, it is not in the
allowlist. An agent with a tool it does not need is a liability with no upside. Do
not add 'useful' tools."*

Models are strongly inclined to be helpful here and will add a search tool "in case
it needs context." That instinct is the single biggest source of over-privileged
agents, so the rule is stated as a prohibition rather than a preference.

### Autonomy and human steps

`wf.workflow.autonomy_level` transfers directly. Every step with
`requires_human = true` becomes an `add_async_task` / approval-wait in the generated
code, with the correct `@app.ping` handling — see `docs/07-hitl-gates.md` for why
that matters and what happens if it is wrong.

### Guardrails

| Source | Generated guard |
|---|---|
| `gated_by` → `control` binding | hard stop before the step; the control's text goes in the operator prompt |
| `depends_on` → `system` | precondition check; fail closed if the system is unreachable |
| `wf.step.on_failure` | retry/halt/escalate policy, transferred |
| `wf.step.timeout_seconds` | per-step timeout |
| node `attributes.data_classification = 'regulated'` | PII/regulated-data Bedrock Guardrail attached, plus a redaction guard |
| `metric` nodes via `measured_by` | run-level instrumentation emitted |

---

## 3. Eval generation

The highest-leverage thing this agent does, because eval suites are what nobody
writes by hand and everybody wishes they had.

It emits an AgentCore Evaluations configuration using the real built-in evaluator
IDs, at their correct levels:

**SESSION** — `Builtin.GoalSuccessRate`, `Builtin.TrajectoryExactOrderMatch`,
`Builtin.TrajectoryInOrderMatch`, `Builtin.TrajectoryAnyOrderMatch`

**TRACE** — `Builtin.Helpfulness`, `Builtin.Correctness`, `Builtin.Faithfulness`,
`Builtin.Coherence`, `Builtin.InstructionFollowing`, `Builtin.ResponseRelevance`,
`Builtin.ContextRelevance`, `Builtin.Conciseness`, `Builtin.Refusal`,
`Builtin.Harmfulness`, `Builtin.Stereotyping`

**TOOL_CALL** — `Builtin.ToolSelectionAccuracy`, `Builtin.ToolParameterAccuracy`

### Trajectory expectations come from the workflow

This is the part that is hard to do by hand and trivial once the workflow is
faithful. The expected tool-call sequence *is* the step order:

```python
expected_trajectory = [s.tool_name for s in workflow.steps
                       if s.kind in ('tool', 'sor_write')]
```

Which matcher to use is derived from the workflow's shape, not guessed:

| Workflow shape | Matcher |
|---|---|
| strictly linear, no `decision` steps | `Builtin.TrajectoryExactOrderMatch` |
| has `decision` branches | `Builtin.TrajectoryInOrderMatch` |
| parallel / unordered segments | `Builtin.TrajectoryAnyOrderMatch` |

### Golden cases from run history

Where `wf.run` history exists, the agent mines successful runs into golden test
cases: input → expected trajectory → expected terminal state. Real cases beat
synthetic ones, and the runs the product operations group has already executed are
sitting there.

### Custom Lambda evaluators it generates

| Evaluator | Level | Checks |
|---|---|---|
| `ControlNotBypassed` | SESSION | every `gated_by` control step executed before its gated activity |
| `HumanStepsRespected` | SESSION | every `requires_human` step actually paused |
| `BindingCitation` | TRACE | claims about the process cite the bound graph element |
| `SorWriteIdempotence` | TOOL_CALL | no duplicate `sor_write` for the same case ref |

`ControlNotBypassed` is the important one: it is the same check
`sor.detect_control_bypass` runs against production, applied at eval time. The
agent generates a test for the exact failure the drift monitor is watching for,
which means a control bypass gets caught in CI rather than in a compliance review.

---

## 4. Tool allowlist (of this agent)

| Tool | Purpose |
|---|---|
| `wf_list`, `wf_get` | read published workflows |
| `kg_get_node`, `kg_traverse` | resolve bindings to their graph context |
| `kg_dependency_closure` | preconditions and required systems |
| `kg_process_flow` | cross-check the workflow against the process |
| Code Interpreter | validate generated code actually parses and its tests run |

No write tools. No `kg_propose` — if it finds the workflow is wrong, it says so and
hands off to the Workflow Agent. That separation keeps the "who may change the
model of the business" answer to exactly one agent.

**Code Interpreter is genuinely useful here** and is the one place in this platform
it earns its cost. A generated `agent.py` that does not import is worthless, and the
agent can find that out itself in a sandboxed session
(`StartCodeInterpreterSession` → `InvokeCodeInterpreter` → `StopCodeInterpreterSession`)
before a human ever opens the package.

---

## 5. Guardrails

> **Naming note.** The table below is the *design intent* for the guard set. The
> shipped `agents/common/guardrails.py` implements the load-bearing subset under
> its own names -- `no_person_name_in_role_label`, `no_unbound_step` /
> `no_unbound_steps`, and `citation_required`, aggregated by `check_proposal_item`,
> `check_workflow`, and `check_turn`. The remaining rows are specified here and
> not yet implemented; treat them as the backlog for that module, not as shipped
> behaviour.


| Guard | Trigger | Action |
|---|---|---|
| `no_deploy` | any attempt to call `create_agent_runtime` or push to ECR | block |
| `allowlist_derivation` | a tool in the spec not traceable to a step binding | block, name the tool |
| `human_step_preserved` | a `requires_human` step generated without an approval wait | block |
| `no_credential_literals` | secret-shaped string in generated code | block |
| `generated_code_compiles` | generated Python fails `py_compile` in Code Interpreter | block, return the error |
| `eval_suite_nonempty` | a spec with no evals | block |

`allowlist_derivation` is the load-bearing one. Every other guard here catches a
mistake; this one catches the model being helpful in the wrong direction.

---

## 6. Evals (of this agent)

**SESSION** — `Builtin.GoalSuccessRate`, `Builtin.TrajectoryAnyOrderMatch`

**TRACE** — `Builtin.Correctness`, `Builtin.InstructionFollowing`,
`Builtin.Faithfulness` (does the spec match the workflow it claims to implement?)

**TOOL_CALL** — `Builtin.ToolSelectionAccuracy`

**Custom**

| Evaluator | Level | Checks |
|---|---|---|
| `AllowlistMinimality` | SESSION | generated allowlist ⊆ tools derivable from bindings |
| `GeneratedCodeCompiles` | SESSION | package passes `py_compile` and its own tests |
| `EvalSuiteCoverage` | SESSION | ≥1 eval per workflow step; every control step has a `ControlNotBypassed` case |
| `HumanStepFidelity` | SESSION | count of `requires_human` steps == count of approval waits in generated code |

---

## 7. Output package

```
generated/{workflow_slug}/
├── agent.py              # BedrockAgentCoreApp; entrypoint, ping, async tasks
├── prompt.py             # system prompt derived from the workflow
├── tools.py              # allowlist bindings, typed
├── guardrails.py         # generated guards
├── evals/
│   ├── evaluators.json   # AgentCore Evaluations config
│   ├── lambda_graders/   # custom code evaluators
│   └── golden_cases.jsonl
├── Dockerfile            # ARM64
├── deploy.py             # create_agent_runtime call — NOT executed
├── tests/test_agent.py
├── PROVENANCE.md         # workflow_id, pinned_commit_id, digest, generated_at
└── README.md
```

`PROVENANCE.md` is not ceremony. It records the workflow id, its pinned commit, and
the content digest, so six months later you can answer "what did we believe about
this process when we generated this agent?" — and so a `stale_pin` drift signal on
that workflow tells you which deployed agents are downstream of it. Without it, the
chain from graph fact to running agent breaks at exactly the point where you need it.

---

## 8. Deployment

Same runtime shape as the others, with one addition: this agent needs Code
Interpreter permissions in its execution role.

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock-agentcore:CreateCodeInterpreter",
    "bedrock-agentcore:StartCodeInterpreterSession",
    "bedrock-agentcore:InvokeCodeInterpreter",
    "bedrock-agentcore:StopCodeInterpreterSession",
    "bedrock-agentcore:GetCodeInterpreterSession"
  ],
  "Resource": "arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*"
}
```

Code Interpreter sessions default to a 15-minute timeout (8-hour max) and are billed
per vCPU-hour and GB-hour with a 128MB memory floor. Stop sessions explicitly —
the generated code in `agent.py` uses a context manager for exactly this reason.

`scaffold_agent` on a large workflow will exceed 15 minutes synchronously. Wrap it
in `add_async_task` and stream.

---

## 9. Failure modes worth designing against

**Over-privileged allowlists.** The main risk, and the reason `AllowlistMinimality`
is a hard eval rather than a soft metric. Audit generated allowlists against step
bindings on every package; the diff should always be empty.

**Plausible non-compiling code.** Mitigated by running `py_compile` and the
generated tests in Code Interpreter before the package is emitted. A package that
did not compile in the sandbox does not get written.

**Eval theatre.** Generating fifteen evaluators that all pass trivially is worse
than generating three that bite, because it produces a green dashboard with no
information in it. `EvalSuiteCoverage` requires per-step coverage; the deeper
defence is to check that the generated suite *fails* on a deliberately broken
version of the agent. Include that as a CI step — generate the package, mutate one
step to skip its control, and assert `ControlNotBypassed` fails. An eval suite that
never fails has never been tested.

**Drift between the generated agent and its workflow.** The workflow moves on; the
deployed agent does not. `PROVENANCE.md` plus the `stale_pin` drift signal make this
detectable: when a workflow's pin goes stale, every package generated from it is a
candidate for regeneration. Wire that into the prod-ops dashboard rather than
relying on someone remembering.
