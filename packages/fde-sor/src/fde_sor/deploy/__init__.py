"""fde_sor.deploy -- boto3 provisioning for the serverless run target.

Written against verified API shapes; the live calls are not validated here
(the same labelling convention `fde_agents.deploy` uses). Everything that can
be checked without AWS -- argument assembly, the cron translation -- is a pure
function with a unit test.
"""

from __future__ import annotations

__all__: list[str] = []
