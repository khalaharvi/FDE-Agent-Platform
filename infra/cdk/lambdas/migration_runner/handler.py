"""Forward-only migration runner: the Lambda backing the launch stack's
`Migrations` custom resource (`fde_cdk/migrations.py`).

Two halves, deliberately import-isolated:

* `plan_migrations` is pure stdlib. `infra/cdk`'s own test venv has no
  `psycopg`/`boto3` installed (see `infra/cdk/pyproject.toml` -- this
  project only needs `aws-cdk-lib`/`constructs` plus dev tooling), yet
  `tests/test_migration_runner.py` imports this module directly to unit
  test `plan_migrations` without AWS. That only works because `psycopg`
  and `boto3` are imported LAZILY below, inside the functions that
  actually touch a database or an AWS API -- never at module scope.
* `handler` is the Lambda entry point. `build.py` bundles `psycopg[binary]`
  wheels into the deployment zip alongside this file; `boto3` ships in the
  Lambda python3.12 runtime image already, so it is never packaged.

The cfn-response protocol (`_send_cfn_response`) is hand-rolled with
stdlib `urllib.request` rather than `aws_cdk.custom_resources.Provider` --
see `fde_cdk/migrations.py`'s module docstring for why the Provider
framework (which creates its own CDK-asset-backed Lambdas) is off the
table for a bootstrap-free stack.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

_HANDLER_DIR = Path(__file__).resolve().parent
_MIGRATIONS_DIR = _HANDLER_DIR / "db"  # populated by build.py from repo-root db/

_DEFAULT_PHYSICAL_ID = "fde-migration-runner"
_ADMIN_DISPLAY_NAME = "Admin"

# Task 7.5: the EventBridge `rate(5 minutes)` rule `fde_cdk/ops.py`'s
# `OpsLayer` points at THIS SAME Lambda (`OpsMetricsRule`, `Condition:
# OpsEnabled`) to probe the embed-queue backlog and publish it as a custom
# CloudWatch metric for the `FdeEmbedQueueBacklog` alarm. Duplicated here
# (not imported from `ops.py`) because this file ships in the Lambda zip
# standalone -- `infra/cdk`'s `fde_cdk` package is never packaged alongside
# it (see `build.py`) -- the same reason gate.py's `_SCHEDULES` payloads
# (`"fde.gate.tick"`/`"fde.gate.expiry"`) are hand-copied literals against
# `fde_gate/deploy/schedule.py` rather than a shared import.
_OPS_METRICS_SOURCE = "fde.ops.metrics"
_OPS_METRICS_NAMESPACE = "FDE/Platform"
_OPS_METRICS_METRIC_NAME = "EmbedQueueDepth"

# Task 7.5 watchdog: how many milliseconds of runway the FAILED
# cfn-response PUT (a network call to a presigned S3 URL) needs before this
# Lambda's own timeout freezes the process mid-request. 10s is the brief's
# own figure -- generous for a small, same-region HTTPS PUT.
_WATCHDOG_THRESHOLD_MS = 10_000
_WATCHDOG_POLL_INTERVAL_S = 1.0

_LEDGER_DDL = (
    "CREATE SCHEMA IF NOT EXISTS ops;\n"
    "CREATE TABLE IF NOT EXISTS ops.applied_migration (\n"
    "  filename text PRIMARY KEY,\n"
    "  applied_at timestamptz NOT NULL DEFAULT now()\n"
    ");\n"
)

# (Secrets Manager name, LOGIN role created, NOLOGIN group role it joins --
# see db/010_roles_and_seed_policy.sql for what each group role can do).
# The group roles are NOLOGIN by design (010's own comment: "least
# privilege per component"); something has to actually be able to connect
# and inherit those grants, hence a distinct LOGIN role per component
# rather than making the group role itself loginable.
_LOGIN_USERS: tuple[tuple[str, str, str], ...] = (
    ("fde/db/agent", "fde_agent_login", "fde_agent"),
    ("fde/db/gate", "fde_gate_login", "fde_gate_service"),
    ("fde/db/ingest", "fde_ingest_login", "fde_ingest"),
)


def is_ops_metrics_event(event: dict[str, Any]) -> bool:
    """Pure dispatch predicate: True when `event` is the `OpsLayer`
    `OpsMetricsRule` invocation (`{"source": "fde.ops.metrics"}`), which
    `handler` routes to the embed-queue-depth probe instead of the
    cfn-response custom-resource path below. This check has to come FIRST
    in `handler` and be exhaustive about what it is NOT: an ops-metrics
    invocation carries none of `RequestType`/`ResponseURL`/`StackId` a
    CloudFormation custom-resource event always has, so falling through to
    `_send_cfn_response` for one would `KeyError` on `event["ResponseURL"]`
    (or worse, PUT garbage to a URL that doesn't exist)."""
    return event.get("source") == _OPS_METRICS_SOURCE


def watchdog_should_fire(
    remaining_time_ms: int, *, threshold_ms: int = _WATCHDOG_THRESHOLD_MS
) -> bool:
    """Pure decision function the watchdog background thread (`handler`,
    via `_run_watchdog`) polls against: True once fewer than `threshold_ms`
    milliseconds remain in this invocation
    (`context.get_remaining_time_in_millis()`).

    Runs on a background thread, not a synchronous pre-check between
    migration steps, because the failure mode this guards against is a
    SINGLE blocking call hanging for the Lambda's entire timeout (e.g.
    `psycopg.connect` wedged behind a misconfigured security group) --
    nothing on the main thread would ever reach a pre-check in that case.
    A background thread's `time.sleep`/poll loop keeps running through a
    blocking syscall on the main thread because CPython releases the GIL
    for blocking I/O, so the watchdog can still fire (and PUT the FAILED
    cfn-response itself) even while the main thread is stuck. Without this,
    CloudFormation would wait out its own ~1-hour custom-resource timeout
    with zero information about why."""
    return remaining_time_ms < threshold_ms


def _run_watchdog(
    get_remaining_time_ms: Callable[[], int],
    stop: threading.Event,
    fire: Callable[[], None],
) -> None:
    """Background-thread body: poll `get_remaining_time_ms()` (normally
    `context.get_remaining_time_in_millis`) once per
    `_WATCHDOG_POLL_INTERVAL_S`, calling `fire()` exactly once and
    returning as soon as `watchdog_should_fire` says so, or returning
    without ever calling `fire()` if `stop` is set first (the normal path:
    the main thread finished and does not need a watchdog rescue)."""
    while not stop.is_set():
        if watchdog_should_fire(get_remaining_time_ms()):
            fire()
            return
        stop.wait(_WATCHDOG_POLL_INTERVAL_S)


def _embed_queue_depth(conn: psycopg.Connection) -> int:
    """`kg.embed_queue`'s own pending-row definition (`db/003_vectors_hnsw.
    sql`'s partial index: `WHERE completed_at IS NULL`) -- the same
    predicate the FDE/Platform:EmbedQueueDepth metric this feeds
    (`FdeEmbedQueueBacklog` alarm, `ops.py`) is meant to track."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kg.embed_queue WHERE completed_at IS NULL")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _put_embed_queue_depth_metric(depth: int) -> None:
    import boto3  # local import -- see module docstring

    client = boto3.client("cloudwatch")
    client.put_metric_data(
        Namespace=_OPS_METRICS_NAMESPACE,
        MetricData=[
            {"MetricName": _OPS_METRICS_METRIC_NAME, "Value": float(depth), "Unit": "Count"}
        ],
    )


def _handle_ops_metrics_event() -> None:
    """The `is_ops_metrics_event(event)` branch of `handler`: connect via
    the SAME `DB_SECRET_ARN` env var the migration path uses (this Lambda
    already has VPC/security-group access to Postgres for that reason),
    probe `_embed_queue_depth`, publish it, done -- no cfn-response, no
    `ResourceProperties`, no ledger/migration work."""
    import psycopg  # local import -- see module docstring

    secret_json = _fetch_secret_json(os.environ["DB_SECRET_ARN"])
    dsn = _dsn_from_secret(secret_json)
    with psycopg.connect(dsn, autocommit=True) as conn:
        depth = _embed_queue_depth(conn)
    _put_embed_queue_depth_metric(depth)


def plan_migrations(applied: set[str], available: list[str]) -> list[str]:
    """Forward-only migration plan: `available` sorted lexicographically
    (which matches numeric order for db/0*.sql's zero-padded filenames),
    minus everything already in `applied`.

    Refuses a gap: if some already-`applied` filename sorts AFTER a
    filename that is NOT in `applied`, the ledger and the available
    migrations have diverged (e.g. someone hand-ran a later migration, or
    a file was deleted) and applying forward from here would silently
    skip the missing one. Raises `ValueError` naming the earliest missing
    filename rather than guessing.
    """
    ordered = sorted(available)
    pending: list[str] = []
    first_missing: str | None = None
    for name in ordered:
        if name in applied:
            if first_missing is not None:
                msg = (
                    f"migration ledger has a gap: {name!r} is already applied but "
                    f"{first_missing!r} (which sorts before it) is not -- forward-only "
                    "migrations refuse to apply out of order"
                )
                raise ValueError(msg)
            continue
        pending.append(name)
        if first_missing is None:
            first_missing = name
    return pending


def _available_migrations() -> list[str]:
    return sorted(path.name for path in _MIGRATIONS_DIR.glob("*.sql"))


def _generate_password(length: int = 40) -> str:
    # Alphanumeric only, deliberately: `_dsn_from_secret` below builds an
    # unquoted `key=value ...` libpq string, and a password containing a
    # space or a `'`/`\` would need escaping this module does not
    # implement. Excluding those characters at generation time is simpler
    # and just as secure at this length (40 chars from a 62-char alphabet).
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _dsn_from_secret(secret_json: dict[str, Any]) -> str:
    """Same JSON shape and DSN format as
    `fde_mcp.db._dsn_from_secrets_manager`
    (`packages/fde-mcp/src/fde_mcp/db.py`): `{"host","port","username",
    "password","dbname"}`. `fde_cdk/database.py`'s own docstring is the
    citation for why the cluster's generated-credentials secret already
    carries `host`/`port`/`dbname` (not just `username`/`password`) by the
    time this Lambda reads it -- the `SecretTargetAttachment` CloudFormation
    creates alongside it enriches the secret at DEPLOY time.
    """
    parts = {
        "host": secret_json["host"],
        "port": secret_json.get("port", 5432),
        "dbname": secret_json.get("dbname") or "fde",
        "user": secret_json["username"],
        "password": secret_json["password"],
        "sslmode": "require",
    }
    return " ".join(f"{key}={value}" for key, value in parts.items())


def _fetch_secret_json(secret_arn: str) -> dict[str, Any]:
    import boto3  # local import -- see module docstring

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    result: dict[str, Any] = json.loads(response["SecretString"])
    return result


def _ensure_ledger(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_LEDGER_DDL)


def _applied_migrations(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM ops.applied_migration")
        return {row[0] for row in cur.fetchall()}


def _apply_migration(conn: psycopg.Connection, filename: str) -> None:
    from psycopg import sql  # local import -- see module docstring

    body = (_MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    ledger_row = (
        sql.SQL("INSERT INTO ops.applied_migration (filename) VALUES ({});")
        .format(sql.Literal(filename))
        .as_string(conn)
    )
    # One `execute()` call, no bind params: psycopg sends this over the
    # SIMPLE query protocol (see `psycopg._cursor_base._execute_send`'s own
    # comment -- "let's use simple query protocol, as it can execute more
    # than one statement in a single query"), and Postgres wraps a
    # multi-statement simple-query string with no explicit BEGIN/COMMIT of
    # its own in ONE implicit transaction. None of db/0*.sql contain
    # explicit transaction control, so the migration file's DDL and its own
    # ledger row commit -- or roll back -- together, atomically. That is a
    # deliberate strengthening over `db/rebuild.sh`'s `psql -f` (which
    # auto-commits statement by statement) for this crash-prone bootstrap
    # path, not a divergence to reconcile.
    with conn.cursor() as cur:
        cur.execute(f"{body}\n{ledger_row}\n")


def _login_role_exists(conn: psycopg.Connection, login_role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login_role,))
        return cur.fetchone() is not None


def _create_login_role(
    conn: psycopg.Connection, login_role: str, group_role: str, password: str
) -> None:
    from psycopg import sql  # local import -- see module docstring

    # `CREATE ROLE` has no `IF NOT EXISTS` in Postgres, so this is wrapped
    # in a DO block that checks `pg_roles` itself. `_login_role_exists`
    # above is the idempotency signal that decides whether Secrets Manager
    # gets touched at all (see `_ensure_login_users`'s docstring); this DO
    # block's own guard is the belt-and-braces layer against a
    # concurrent/retried invocation racing this one at the database level.
    statement = sql.SQL(
        "DO $do$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {login}) THEN "
        "CREATE ROLE {login_ident} LOGIN PASSWORD {password} IN ROLE {group_ident}; "
        "END IF; END $do$;"
    ).format(
        login=sql.Literal(login_role),
        login_ident=sql.Identifier(login_role),
        password=sql.Literal(password),
        group_ident=sql.Identifier(group_role),
    )
    with conn.cursor() as cur:
        cur.execute(statement)


def _put_login_secret(
    name: str, *, host: str, port: int, dbname: str, login_role: str, password: str
) -> None:
    import boto3  # local import -- see module docstring

    client = boto3.client("secretsmanager")
    payload = json.dumps(
        {"host": host, "port": port, "dbname": dbname, "username": login_role, "password": password}
    )
    description = f"FDE Agent Platform login credential for {login_role} (fde_cdk migration Lambda)"
    try:
        client.create_secret(Name=name, SecretString=payload, Description=description)
    except client.exceptions.ResourceExistsException:
        # Reached only when a PRIOR invocation created this secret and then
        # crashed before finishing this user's role/ledger work -- on
        # retry `_login_role_exists` is still False, a fresh password is
        # generated, and the secret write must overwrite the stale one
        # rather than fail.
        client.put_secret_value(SecretId=name, SecretString=payload)


def _ensure_login_users(conn: psycopg.Connection, *, host: str, port: int, dbname: str) -> None:
    """Create the three least-privilege LOGIN users db/010's NOLOGIN group
    roles need someone to actually connect as, and mint their Secrets
    Manager credentials -- once each, ever.

    Idempotency signal is the DATABASE (`pg_roles`), not Secrets Manager:
    if the login role already exists, this run leaves its secret untouched
    (skips Secrets Manager entirely for that user) instead of writing a
    freshly generated password the role was never actually given, which
    would desync the secret from the role's real password. The only path
    that writes to Secrets Manager is "the role did not exist, so it was
    just created with this exact password" -- see `_put_login_secret`'s
    `ResourceExistsException` handling for the one case where that path
    still needs to overwrite an existing secret.
    """
    for secret_name, login_role, group_role in _LOGIN_USERS:
        if _login_role_exists(conn, login_role):
            continue
        password = _generate_password()
        _create_login_role(conn, login_role, group_role, password)
        _put_login_secret(
            secret_name,
            host=host,
            port=port,
            dbname=dbname,
            login_role=login_role,
            password=password,
        )


def _ensure_admin_principal(conn: psycopg.Connection, admin_email: str) -> None:
    """Seed `hitl.reviewer` (db/004_hitl_gates.sql) with the launch
    parameter's AdminEmail, so the review console has at least one
    reviewer the first time anyone logs in.

    Known v1 gap, not an oversight: `hitl.reviewer.principal` is normally
    the reviewer's IdP subject -- the Cognito JWT `sub` claim
    (`fde_gate/http.py`'s own docstring says as much) -- but that `sub` is
    only assigned when the seeded Cognito user (`Identity`'s
    `CfnUserPoolUser`) is created, and this Lambda has neither the IAM
    permission nor a reliable CloudFormation attribute to read it back
    (`AWS::Cognito::UserPoolUser` exposes no `Fn::GetAtt` for `sub`).
    Using the email itself as the principal here means this seeded
    reviewer row will not satisfy `hitl.gates_satisfied`'s reviewer-
    authority join against the admin's REAL Cognito-issued JWT until
    something reconciles the two (a later admin-console feature, or a
    one-time manual `UPDATE hitl.reviewer SET principal = ...`). Tracked as
    a follow-up, documented the same way `iam_roles.py` documents its own
    known least-privilege gap on `runtime_role`'s trust condition.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hitl.reviewer (principal, display_name, email, is_active) "
            "VALUES (%s, %s, %s, true) "
            "ON CONFLICT (principal) DO NOTHING",
            (admin_email, _ADMIN_DISPLAY_NAME, admin_email),
        )


def _send_cfn_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    *,
    physical_resource_id: str,
    reason: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    """Hand-rolled cfn-response PUT: stdlib `urllib.request` only, no
    `requests` dependency and no `aws_cdk.custom_resources.Provider` (see
    this module's own docstring for why). `event["ResponseURL"]` is a
    presigned S3 PUT URL; AWS's documented protocol for it requires the
    `content-type` header be sent as an empty string -- a real
    `application/json` (or an omitted header, which `urllib` would fill in
    itself) does not match what the URL was signed for and the PUT is
    rejected.
    """
    log_stream = getattr(context, "log_stream_name", "")
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch Logs: {log_stream}",
            "PhysicalResourceId": physical_resource_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": data or {},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"content-type": ""},
    )
    with urllib.request.urlopen(request) as response:  # presigned CFN URL, not user input
        response.read()


def handler(event: dict[str, Any], context: Any) -> None:
    """Lambda entry point, shared by two callers (`fde_cdk/migrations.py`'s
    custom resource AND `fde_cdk/ops.py`'s `OpsMetricsRule`):

    * Ops-metrics invocation (`is_ops_metrics_event(event)`): probe
      `kg.embed_queue`'s backlog depth, publish it, return. No
      cfn-response -- this event carries no `ResponseURL`.
    * Custom-resource invocation (everything else): Create/Update connects
      via the cluster's master secret (`DB_SECRET_ARN` env var), ensures
      the `ops.applied_migration` ledger, applies pending `db/0*.sql` in
      order (`plan_migrations`), mints the three login-user secrets, seeds
      the admin reviewer. Delete is a no-op SUCCESS -- the database (and
      its data) outlives the stack, per `fde_cdk/database.py`'s
      `RemovalPolicy.SNAPSHOT` and `Identity`'s `RemovalPolicy.RETAIN` on
      the user pool -- deleting the stack must never touch either.

    Task 7.5 watchdog: a background thread (`_run_watchdog`) races the main
    path here. If `watchdog_should_fire` trips first -- fewer than
    `_WATCHDOG_THRESHOLD_MS` remain in this invocation -- it sends FAILED
    itself and the main thread's own eventual `_send_once` call becomes a
    no-op (the `response_lock`/`response_sent` guard: CloudFormation's
    custom-resource protocol does not tolerate more than one response PUT
    per request). Without this, a Lambda that times out mid-`psycopg.
    connect` never gets to run its own `except`/`finally` -- the runtime
    just kills it -- and CloudFormation would sit out its own ~1-hour
    custom-resource timeout with no diagnostic beyond "no response".
    """
    if is_ops_metrics_event(event):
        _handle_ops_metrics_event()
        return

    physical_id = event.get("PhysicalResourceId") or _DEFAULT_PHYSICAL_ID
    request_type = event.get("RequestType")

    response_lock = threading.Lock()
    response_sent = False

    def _send_once(status: str, *, reason: str = "", data: dict[str, Any] | None = None) -> None:
        nonlocal response_sent
        with response_lock:
            if response_sent:
                return
            response_sent = True
        _send_cfn_response(
            event, context, status, physical_resource_id=physical_id, reason=reason, data=data
        )

    def _watchdog_fire() -> None:
        log_stream = getattr(context, "log_stream_name", "")
        _send_once(
            "FAILED",
            reason=(
                "migration runner watchdog: invocation approaching its own "
                f"timeout -- see CloudWatch Logs: {log_stream}"
            ),
        )

    stop_watchdog = threading.Event()
    watchdog_thread = threading.Thread(
        target=_run_watchdog,
        args=(context.get_remaining_time_in_millis, stop_watchdog, _watchdog_fire),
        daemon=True,
    )
    watchdog_thread.start()
    try:
        if request_type == "Delete":
            _send_once("SUCCESS")
            return

        import psycopg  # local import -- see module docstring

        properties = event.get("ResourceProperties", {})
        admin_email = properties["AdminEmail"]

        secret_json = _fetch_secret_json(os.environ["DB_SECRET_ARN"])
        dsn = _dsn_from_secret(secret_json)
        host = secret_json["host"]
        port = int(secret_json.get("port", 5432))
        dbname = secret_json.get("dbname") or "fde"

        with psycopg.connect(dsn, autocommit=True) as conn:
            _ensure_ledger(conn)
            applied = _applied_migrations(conn)
            available = _available_migrations()
            pending = plan_migrations(applied, available)
            for filename in pending:
                _apply_migration(conn, filename)
            _ensure_login_users(conn, host=host, port=port, dbname=dbname)
            _ensure_admin_principal(conn, admin_email)

        _send_once("SUCCESS", data={"AppliedMigrations": str(len(pending))})
    except Exception as exc:  # must always answer CFN, even for an unexpected bug
        # Deliberately NOT `str(exc)`: `_dsn_from_secret` builds the master
        # connection string with the password embedded in plain text, and
        # some libpq/psycopg connection-failure messages echo the offending
        # conninfo string back verbatim -- CloudFormation stack events are
        # visible to anyone with read access to the stack, which is a wider
        # audience than this Lambda's own CloudWatch Logs. The exception
        # TYPE plus a log-stream pointer is enough to triage from the CFN
        # console; the full message only ever reaches CloudWatch, via this
        # `raise`.
        log_stream = getattr(context, "log_stream_name", "")
        reason = f"{type(exc).__name__} -- see CloudWatch Logs: {log_stream}"
        _send_once("FAILED", reason=reason)
        raise
    finally:
        # Unblocks `_run_watchdog`'s `stop.wait(...)` immediately (an
        # `Event` unblocks a pending `wait` as soon as `set()` is called
        # from another thread) so the background thread exits promptly
        # once the main path has already answered CloudFormation, instead
        # of lingering up to `_WATCHDOG_POLL_INTERVAL_S` longer than
        # necessary.
        stop_watchdog.set()
