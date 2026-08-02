"""detectors.py -- the scheduled drift scan, and the escalation on top of it.

Deliberately agent-free. `sor.run_all_detectors` is pure SQL (db/007), and the
scan that calls it every six hours runs as `fde_ingest` in a Lambda or a
CronJob, not inside an agent runtime. Detection is the platform's claim that it
notices when the graph stops matching reality; making that claim depend on an
agent runtime being healthy, an LLM responding, and a tool loop terminating
would mean drift goes unnoticed exactly when the platform is unwell. The
Workflow Agent's `monitor_drift` triage path (docs/09 §7) still sits on top --
it decides what to DO about a signal, which is a judgement; whether a signal
exists is not.

Escalation
-----------
After the scan, critical `control_bypass` signals raised or refreshed BY THIS
SCAN (`last_seen_at >= scan start`) are published to SNS. Only that
intersection: `control_bypass` because a control that is being skipped is the
one drift kind with a compliance edge to it, critical because
`detect_control_bypass` reserves that for a bypass rate over 10%, and
"this scan" because `sor.record_drift` dedups by natural key -- an open signal
that nothing new happened to would otherwise page someone every six hours
until it was resolved, and docs/08 §6 is explicit that a noisy queue is worse
than no queue.

Reading those signals back needs `SELECT ON sor.drift_signal TO fde_ingest`,
which db/014 adds for exactly this: before it, the detector could write signals
the scanner could not see.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fde_mcp import db
from fde_mcp.logging import get_logger
from fde_sor import registry
from fde_sor.config import aws_region, get_settings

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["DriftScanResult", "drift_scan_once", "scan_all_engagements"]

log = get_logger(__name__)

_CRITICAL_BYPASS_SQL = """
SELECT signal_id, subject_ref, severity, state, detail, sample_size,
       effect_size, occurrences, detected_at, last_seen_at
  FROM sor.drift_signal
 WHERE engagement_id = %(eng)s::uuid
   AND drift_kind = 'control_bypass'
   AND severity = 'critical'
   AND state = 'open'
   AND last_seen_at >= %(since)s
 ORDER BY last_seen_at DESC
"""


@dataclass(frozen=True, slots=True)
class DriftScanResult:
    engagement_id: str
    summary: dict[str, Any]
    critical_bypasses: list[dict[str, Any]] = field(default_factory=list)
    alert_published: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "engagement_id": self.engagement_id,
            "summary": self.summary,
            "critical_bypasses": len(self.critical_bypasses),
            "alert_published": self.alert_published,
        }


def _publish_alert(topic_arn: str, engagement_id: str, signals: list[dict[str, Any]]) -> None:
    import boto3  # noqa: PLC0415

    client = boto3.client("sns", region_name=aws_region())
    subject = f"FDE drift: {len(signals)} critical control bypass(es)"
    body = {
        "engagement_id": engagement_id,
        "drift_kind": "control_bypass",
        "severity": "critical",
        "signals": [
            {
                "signal_id": s["signal_id"],
                "control": s["subject_ref"],
                "detail": s["detail"],
                "sample_size": s["sample_size"],
                "occurrences": s["occurrences"],
            }
            for s in signals
        ],
    }
    client.publish(
        TopicArn=topic_arn,
        # SNS caps Subject at 100 characters and rejects the message outright
        # if it is longer -- which would turn "we found a compliance problem"
        # into an unhandled exception in the scanner.
        Subject=subject[:100],
        Message=json.dumps(body, default=str, indent=2),
    )


async def drift_scan_once(engagement_id: str) -> DriftScanResult:
    """Run every detector for one engagement, then escalate what it found."""
    settings = get_settings()
    async with (
        db.tool_transaction(
            role=settings.role, statement_timeout=settings.drift_statement_timeout
        ) as conn,
        conn.cursor() as cur,
    ):
        # The scan-start marker comes from the DATABASE clock, not the
        # container's. `last_seen_at` is set by `now()` inside Postgres,
        # and comparing it against a Python `datetime.now()` from a
        # machine whose clock is a second behind would silently include
        # signals from the previous scan in this scan's alert.
        await cur.execute("SELECT now() AS scan_start")
        start_row = await cur.fetchone()
        assert start_row is not None
        scan_start: datetime = start_row["scan_start"]

        await cur.execute(
            "SELECT sor.run_all_detectors(%(eng)s::uuid) AS summary", {"eng": engagement_id}
        )
        summary_row = await cur.fetchone()
        summary = dict(summary_row["summary"]) if summary_row else {}

        await cur.execute(_CRITICAL_BYPASS_SQL, {"eng": engagement_id, "since": scan_start})
        critical = [dict(r) for r in await cur.fetchall()]

    published = False
    if critical and settings.alert_topic_arn:
        try:
            await asyncio.to_thread(
                _publish_alert, settings.alert_topic_arn, engagement_id, critical
            )
            published = True
        except Exception:
            # The signals are already in sor.drift_signal and visible in the
            # triage queue. A paging failure must not make the scan look like
            # it failed to detect anything.
            log.exception("drift_alert_publish_failed", engagement_id=engagement_id)
    elif critical:
        log.warning(
            "drift_critical_bypass_not_published",
            engagement_id=engagement_id,
            count=len(critical),
            reason="FDE_SOR_ALERT_TOPIC_ARN is not set",
        )

    log.info(
        "sor_drift_scan_complete",
        engagement_id=engagement_id,
        summary=summary,
        critical_bypasses=len(critical),
        alert_published=published,
    )
    return DriftScanResult(
        engagement_id=engagement_id,
        summary=summary,
        critical_bypasses=critical,
        alert_published=published,
    )


async def scan_all_engagements() -> list[DriftScanResult]:
    """Scan every engagement with at least one active adapter.

    One engagement's failure does not stop the others: a scan that aborts
    halfway leaves later engagements unmonitored until the next schedule, and
    the cause is usually specific to one customer's data.
    """
    engagement_ids = await registry.active_engagement_ids()
    log.info("sor_drift_scan_starting", engagements=len(engagement_ids))
    results: list[DriftScanResult] = []
    for engagement_id in engagement_ids:
        try:
            results.append(await drift_scan_once(engagement_id))
        except Exception:
            log.exception("drift_scan_failed", engagement_id=engagement_id)
    return results
