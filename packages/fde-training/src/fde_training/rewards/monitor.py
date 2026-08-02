"""rewards/monitor.py -- catching reward hacking while it is still cheap
to fix.

A composite reward can look perfectly healthy on its own trend line while
the POLICY quietly learns to exploit a specific term. The documented
GRPO search-agent failure mode this module exists to catch: mean tool-call
count DECREASING over training, because the policy has discovered it can
sometimes guess the answer for free and the outcome/grounding terms don't
punish that specific shortcut hard enough, fast enough, for the trainer's
own loss curve to reveal it. By the time that shows up in eval quality, a
lot of GPU time has already gone into reinforcing the shortcut.

`RewardHackingMonitor` does not stop training by itself -- that decision
belongs to the training loop or a human watching a dashboard. It makes the
collapse impossible to miss in logs: `observe()` is meant to be called once
per training step with that step's batch of completions, and it fires a
loud, structured ERROR-level event (plus, in `strict=True` mode, raises)
the moment mean tool-call count has strictly decreased for
`collapse_window` consecutive recorded steps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from fde_mcp.logging import get_logger
from fde_training.rewards._episode import episode_logs
from fde_training.rewards.grounding import r_grounded

log = get_logger(__name__)


class RewardHackingCollapseWarning(RuntimeWarning):
    pass


@dataclass
class StepStats:
    step: int
    mean_tool_calls: float
    mean_hops: float
    ungrounded_claim_rate: float
    n_examples: int


class RewardHackingMonitor:
    """Tracks per-step aggregate behaviour and fires a loud warning (logged
    at ERROR level, plus raised as a catchable exception if `strict=True`)
    when mean tool-call count has DECREASED for `collapse_window`
    consecutive recorded steps -- the documented GRPO search-agent collapse
    signature. See module docstring.
    """

    def __init__(
        self, collapse_window: int = 5, strict: bool = False, history_size: int = 500
    ) -> None:
        self.collapse_window = collapse_window
        self.strict = strict
        self.history: deque[StepStats] = deque(maxlen=history_size)
        self._collapse_already_warned_at: int | None = None

    def observe(self, step: int, completions: list[Any], **kwargs: Any) -> StepStats:
        logs = episode_logs(completions, kwargs)
        n = len(logs) or 1
        mean_tool_calls = sum(episode.tool_call_count for episode in logs) / n
        mean_hops = sum(episode.max_hops_requested() for episode in logs) / n

        grounded_scores = r_grounded(completions, **kwargs)
        ungrounded_rate = sum(1 for g in grounded_scores if g < 0.5) / (len(grounded_scores) or 1)

        stats = StepStats(
            step=step,
            mean_tool_calls=mean_tool_calls,
            mean_hops=mean_hops,
            ungrounded_claim_rate=ungrounded_rate,
            n_examples=len(completions),
        )
        self.history.append(stats)
        self._check_collapse()
        return stats

    def _check_collapse(self) -> bool:
        if len(self.history) < self.collapse_window + 1:
            return False
        recent = list(self.history)[-(self.collapse_window + 1) :]
        deltas = [b.mean_tool_calls - a.mean_tool_calls for a, b in pairwise(recent)]
        is_collapsing = all(d < -1e-9 for d in deltas)
        if is_collapsing:
            last_step = recent[-1].step
            if self._collapse_already_warned_at == last_step:
                return True  # already warned for this step
            self._collapse_already_warned_at = last_step
            log.error(
                "REWARD HACKING ALERT: mean tool-call count has strictly decreased for "
                "consecutive recorded steps -- the documented GRPO search-agent collapse "
                "signature (policy discovering it can sometimes guess without retrieving). "
                "Inspect r_hop_efficiency's weight and r_grounded's trend before continuing "
                "training.",
                collapse_window=self.collapse_window,
                mean_tool_calls_before=recent[0].mean_tool_calls,
                mean_tool_calls_after=recent[-1].mean_tool_calls,
                step_from=recent[0].step,
                step_to=recent[-1].step,
            )
            if self.strict:
                msg = (
                    f"RewardHackingMonitor: tool-call collapse detected over "
                    f"steps {recent[0].step}-{recent[-1].step}"
                )
                raise RuntimeError(msg)
        return is_collapsing

    def summary(self) -> dict[str, Any]:
        if not self.history:
            return {"steps_recorded": 0}
        last = self.history[-1]
        return {
            "steps_recorded": len(self.history),
            "latest_mean_tool_calls": last.mean_tool_calls,
            "latest_mean_hops": last.mean_hops,
            "latest_ungrounded_claim_rate": last.ungrounded_claim_rate,
        }
