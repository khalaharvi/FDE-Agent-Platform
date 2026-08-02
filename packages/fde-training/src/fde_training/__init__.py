"""fde_training -- SFT export, GRPO rewards, rollout environment, and rival
graders for the FDE Platform's offline training pipeline.

This package is deliberately the ONE place in the workspace allowed to grow
a heavy ML dependency (torch/trl/peft/transformers/datasets/accelerate,
gated behind the ``train`` optional-dependency group -- see this package's
``pyproject.toml``). Every module here must still IMPORT cleanly with none
of those installed; only the functions that actually train something
(``sft_config.build_sft_config``/``build_lora_config``) import them, and
only inside the function body. See ``sft_config``'s module docstring for
why that is the load-bearing property of this whole package, not an
incidental style choice.

What's re-exported here is deliberately small, matching ``fde_mcp``'s own
package root: ``get_settings``/``get_logger`` because every module in this
package (and, eventually, an orchestrator that wires several of them
together) needs a stable way to read configuration and emit structured
logs without reaching into ``fde_training.config``/``fde_mcp.logging``
internals. Everything else -- reward functions, the rollout environment,
the exporters -- is an implementation detail reached through its own
submodule.
"""

from __future__ import annotations

from importlib import metadata

from fde_mcp.logging import get_logger
from fde_training.config import Settings, get_settings

try:
    __version__ = metadata.version("fde-training")
except metadata.PackageNotFoundError:  # pragma: no cover - editable/unbuilt checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "Settings",
    "__version__",
    "get_logger",
    "get_settings",
]
