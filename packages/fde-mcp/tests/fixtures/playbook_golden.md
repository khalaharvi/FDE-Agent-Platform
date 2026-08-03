---
title: "Q2C discount approval"
slug: "q2c-discount-approval"
version: 1
status: "published"
process: "proc.quote_to_cash"
autonomy: "assisted"
runnable_by: []
pinned_commit_digest: "4a829dc84c807dcde1bfc803bd57704e4bdea9e1acc9bddf4943d8497878f9f5"
steps: 4
authored_by: "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-workflow"
published_by: "owner@example.com"
---

# Q2C discount approval

Status **published** · autonomy **assisted** · `q2c-discount-approval` v1

Runnable by: anyone in the product-operations group.

Pinned to graph commit `4a829dc84c807dcde1bfc803bd57704e4bdea9e1acc9bddf4943d8497878f9f5`.

Each step below lists the process elements it implements. That grounding is checked at publication, so no step here is one somebody invented.

## Process context

This workflow implements `proc.quote_to_cash`. The graph records 2 activities in that process, in the order control flows through them. This section reflects the graph as it stands today; the steps below are frozen to the pinned commit.

1. **Create Quote** (`act.create_quote`)
    - Gated by: `ctl.discount_threshold_20`
    - Followed by: `act.discount_review`
2. **Discount Review** (`act.discount_review`)
    - Performed by: `role.deal_desk`
    - Recorded in: `sys.cpq`

## Steps

### 1. Look up the quote's discount

- Kind: `tool`
- Step key: `check_discount`
- Requires a human: no
- On failure: `halt`
- Tool: `cpq_discount_check`

Read the quote's discount percentage from CPQ. If it is at or below 20%, the quote can be sent without deal-desk involvement.

**Grounded in:**

- `implements` node `act.create_quote` — Create Quote
- `enforces` node `ctl.discount_threshold_20` — 20% Discount Threshold

### 2. Deal desk reviews the discount

- Kind: `human`
- Step key: `deal_desk_review`
- Requires a human: yes
- On failure: `escalate`

Open the quote in CPQ and weigh the requested discount against the account's history and the current quarter's pricing guidance.

**Ask the operator:** Approve this discount, or send it back with a reason?

**Grounded in:**

- `implements` node `act.discount_review` — Discount Review
- `depends_on` node `sys.cpq` — CPQ

### 3. Record the decision in CPQ

- Kind: `sor_write`
- Step key: `record_outcome`
- Requires a human: no
- On failure: `halt`
- System of record: `cpq`
- Write operation: `update_quote_status`

Write the deal desk's decision back onto the quote record.

**Grounded in:**

- `records_to` node `sys.cpq` — CPQ

### 4. Tell the rep the outcome

- Kind: `notify`
- Step key: `notify_rep`
- Requires a human: no
- On failure: `halt`

Notify the quote's owner that the discount review has closed.

No grounding required: `notify` steps are exempt from the faithfulness check.
