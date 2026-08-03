from __future__ import annotations

from tests.test_synth import synth_template


def _gate_lambda(functions: dict) -> dict:
    return next(
        f
        for f in functions.values()
        if f["Properties"].get("Handler") == "fde_gate.handler.lambda_handler"
    )


# ---------------------------------------------------------------------
# GateService: Lambda shape
# ---------------------------------------------------------------------


def test_gate_lambda_shape() -> None:
    t = synth_template()
    fns = t.find_resources("AWS::Lambda::Function")
    gate = [
        f
        for f in fns.values()
        if f["Properties"].get("Handler") == "fde_gate.handler.lambda_handler"
    ]
    assert len(gate) == 1
    assert gate[0]["Properties"]["Architectures"] == ["arm64"]
    assert gate[0]["Properties"]["Timeout"] == 900


def test_gate_lambda_is_named_and_sized_and_vpc_attached() -> None:
    t = synth_template()
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    props = fn["Properties"]
    assert props["FunctionName"] == "fde-gate-service"
    assert props["Runtime"] == "python3.12"
    assert props["MemorySize"] == 512
    assert "VpcConfig" in props


def test_gate_lambda_code_points_at_gate_zip_release_key() -> None:
    t = synth_template()
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    code = fn["Properties"]["Code"]
    assert code["S3Key"] == {
        "Fn::Join": ["", ["releases/", {"Ref": "ReleaseTag"}, "/fde-gate.zip"]]
    }
    assert code["S3Bucket"] == {
        "Fn::If": [
            "HasAssetsBucket",
            {"Ref": "AssetsBucket"},
            {"Fn::FindInMap": ["AssetsRegionMap", {"Ref": "AWS::Region"}, "bucket"]},
        ]
    }


def test_gate_lambda_uses_the_gate_role() -> None:
    t = synth_template()
    template = t.to_json()
    roles = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role"}
    gate_role_id = next(
        k
        for k, v in roles.items()
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "gate-permissions"
    )
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    assert fn["Properties"]["Role"] == {"Fn::GetAtt": [gate_role_id, "Arn"]}


def test_gate_lambda_env_has_expected_keys() -> None:
    t = synth_template()
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    env = fn["Properties"]["Environment"]["Variables"]
    assert env["FDE_GATE_FUNCTION_NAME"] == "fde-gate-service"
    assert env["FDE_SERVICE_NAME"] == "fde-gate"
    assert "FDE_DB_SECRET_ARN" in env
    assert "FDE_MCP_URL" in env
    assert env["FDE_MODEL_PROVIDER"] == {"Ref": "ModelProvider"}
    # Task 7 behavior change: stack.py now constructs Agents before
    # GateService and passes `agents.runtime_arns` through, so every
    # FDE_RUNTIME_ARN_* env carries a real `attr_agent_runtime_arn` token
    # (an Fn::GetAtt dict), never the "" placeholder this test pinned
    # through Task 6 (see gate.py's module docstring and
    # tests/test_agents_outputs.py for the fuller contract, including that
    # each env references its OWN agent's runtime, not just any token).
    for agent in ("ENGAGEMENT", "WORKFLOW", "DEVELOPMENT"):
        value = env[f"FDE_RUNTIME_ARN_{agent}"]
        assert value != ""
        assert isinstance(value, dict)


def test_gate_lambda_model_id_env_is_fn_if_on_is_bedrock_model() -> None:
    """FDE_MODEL_ID must be blank when IsBedrockModel is true -- params.py's
    own ModelId description: "ignored for bedrock"."""
    t = synth_template()
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    env = fn["Properties"]["Environment"]["Variables"]
    assert env["FDE_MODEL_ID"] == {"Fn::If": ["IsBedrockModel", "", {"Ref": "ModelId"}]}


def test_gate_role_has_supplemental_grant_on_fde_db_secrets() -> None:
    """The gap this task's gate.py closes: gate_role's OWN template only
    grants read on the Aurora cluster's master secret, not fde/db/gate.
    `Role.add_to_principal_policy` (gate.py) lands the new statement in a
    SEPARATE, CDK-auto-generated `AWS::IAM::Policy` resource attached to
    the same role -- not merged into the existing `gate-permissions`
    inline policy -- so this looks across every policy (inline `Policies`
    on the Role resource, and standalone `AWS::IAM::Policy` resources
    whose `Roles` references it) attached to the gate role."""
    t = synth_template()
    template = t.to_json()
    resources = template["Resources"]
    gate_role_id = next(
        k
        for k, v in resources.items()
        if v["Type"] == "AWS::IAM::Role"
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "gate-permissions"
    )
    gate_role = resources[gate_role_id]
    statements = [
        stmt
        for policy in gate_role["Properties"].get("Policies", [])
        for stmt in policy["PolicyDocument"]["Statement"]
    ]
    for v in resources.values():
        if v["Type"] == "AWS::IAM::Policy" and {"Ref": gate_role_id} in v["Properties"].get(
            "Roles", []
        ):
            statements.extend(v["Properties"]["PolicyDocument"]["Statement"])

    read_login_secret = next(s for s in statements if s.get("Sid") == "ReadGateLoginSecret")
    raw = str(read_login_secret["Resource"])
    assert "fde/db/*" in raw


# ---------------------------------------------------------------------
# GateService: HTTP API
# ---------------------------------------------------------------------


def test_healthz_route_has_no_authorizer() -> None:
    t = synth_template()
    routes = t.find_resources("AWS::ApiGatewayV2::Route")
    healthz = [r for r in routes.values() if r["Properties"]["RouteKey"] == "GET /healthz"]
    assert len(healthz) == 1
    assert healthz[0]["Properties"].get("AuthorizationType", "NONE") == "NONE"
    assert "AuthorizerId" not in healthz[0]["Properties"]


def test_proxy_route_has_jwt_authorizer() -> None:
    t = synth_template()
    routes = t.find_resources("AWS::ApiGatewayV2::Route")
    proxy = [r for r in routes.values() if r["Properties"]["RouteKey"] == "ANY /{proxy+}"]
    assert len(proxy) == 1
    assert proxy[0]["Properties"]["AuthorizationType"] == "JWT"
    assert "AuthorizerId" in proxy[0]["Properties"]


def test_jwt_authorizer_issuer_and_audience() -> None:
    t = synth_template()
    authorizers = t.find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    authorizer = next(iter(authorizers.values()))
    assert authorizer["Properties"]["AuthorizerType"] == "JWT"
    issuer = authorizer["Properties"]["JwtConfiguration"]["Issuer"]
    assert "cognito-idp" in str(issuer)
    assert ".well-known" not in str(issuer)


def test_http_api_exists_exactly_once() -> None:
    t = synth_template()
    t.resource_count_is("AWS::ApiGatewayV2::Api", 1)


# ---------------------------------------------------------------------
# GateService: EventBridge schedules
# ---------------------------------------------------------------------


def test_two_gate_schedules() -> None:
    t = synth_template()
    rules = t.find_resources("AWS::Events::Rule")
    exprs = sorted(r["Properties"]["ScheduleExpression"] for r in rules.values())
    assert exprs == ["rate(1 hour)", "rate(1 minute)"]


def test_schedule_payloads_match_fde_gate_deploy_schedule() -> None:
    t = synth_template()
    rules = t.find_resources("AWS::Events::Rule")
    payloads = set()
    for rule in rules.values():
        for target in rule["Properties"]["Targets"]:
            payloads.add(target["Input"])
    assert '{"source":"fde.gate.tick"}' in payloads
    assert '{"source":"fde.gate.expiry"}' in payloads


# ---------------------------------------------------------------------
# Services: ECS cluster / ALB / Fargate task shapes
# ---------------------------------------------------------------------


def test_alb_is_internal() -> None:
    t = synth_template()
    albs = t.find_resources("AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert all(a["Properties"]["Scheme"] == "internal" for a in albs.values())


def test_exactly_one_ecs_cluster_and_two_fargate_services() -> None:
    t = synth_template()
    t.resource_count_is("AWS::ECS::Cluster", 1)
    t.resource_count_is("AWS::ECS::Service", 2)


def test_task_definitions_are_arm64_fargate() -> None:
    t = synth_template()
    task_defs = t.find_resources("AWS::ECS::TaskDefinition")
    assert len(task_defs) == 2
    for td in task_defs.values():
        props = td["Properties"]
        assert props["RequiresCompatibilities"] == ["FARGATE"]
        assert props["RuntimePlatform"]["CpuArchitecture"] == "ARM64"


def test_mcp_container_image_and_port() -> None:
    t = synth_template()
    task_defs = t.find_resources("AWS::ECS::TaskDefinition")
    mcp_containers = [
        c
        for td in task_defs.values()
        for c in td["Properties"]["ContainerDefinitions"]
        if c["Name"] == "fde-mcp"
    ]
    assert len(mcp_containers) == 1
    container = mcp_containers[0]
    assert "/fde-mcp:" in str(container["Image"])
    assert container["PortMappings"][0]["ContainerPort"] == 8080
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["FDE_MCP_TRANSPORT"] == "http"
    assert "FDE_DB_SECRET_ARN" in env
    assert env["FDE_EMBED_PROVIDER"] == {"Ref": "EmbedProvider"}


def test_embedder_container_command_and_no_alb_attachment() -> None:
    t = synth_template()
    task_defs = t.find_resources("AWS::ECS::TaskDefinition")
    embedder_containers = [
        c
        for td in task_defs.values()
        for c in td["Properties"]["ContainerDefinitions"]
        if c["Name"] == "fde-embedder"
    ]
    assert len(embedder_containers) == 1
    container = embedder_containers[0]
    assert container["Command"] == ["fde-embedder"]
    assert "PortMappings" not in container
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["FDE_EMBEDDER_ROLE"] == "fde_ingest"
    assert "FDE_DB_SECRET_ARN" in env


def test_mcp_alb_health_check_path_and_matcher() -> None:
    t = synth_template()
    groups = t.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")
    assert len(groups) == 1
    tg = next(iter(groups.values()))
    assert tg["Properties"]["HealthCheckPath"] == "/mcp"
    assert tg["Properties"]["Matcher"]["HttpCode"] == "200-499"


def test_fargate_task_roles_have_fde_db_wildcard_grant() -> None:
    t = synth_template()
    template = t.to_json()
    task_role_policies = [
        v
        for v in template["Resources"].values()
        if v["Type"] == "AWS::IAM::Role"
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "read-db-login-secret"
    ]
    assert len(task_role_policies) == 2
    for role in task_role_policies:
        statements = [
            stmt
            for policy in role["Properties"]["Policies"]
            for stmt in policy["PolicyDocument"]["Statement"]
        ]
        assert any("fde/db/*" in str(s["Resource"]) for s in statements)


# ---------------------------------------------------------------------
# Provider API key secret: shared, conditional
# ---------------------------------------------------------------------


def test_provider_api_key_secret_is_conditioned_on_has_provider_key() -> None:
    t = synth_template()
    secrets = t.find_resources("AWS::SecretsManager::Secret")
    # 2 total: the RDS-managed master secret (Database) + this one.
    assert len(secrets) == 2
    conditional = [s for s in secrets.values() if s.get("Condition") == "HasProviderKey"]
    assert len(conditional) == 1
    assert conditional[0]["Properties"]["SecretString"] == {"Ref": "ProviderApiKey"}


def test_provider_api_key_flows_into_gate_and_mcp_env_via_dynamic_reference() -> None:
    t = synth_template()
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    model_key = fn["Properties"]["Environment"]["Variables"]["FDE_MODEL_API_KEY"]
    assert model_key["Fn::If"][0] == "HasProviderKey"
    assert model_key["Fn::If"][2] == ""
    joined = model_key["Fn::If"][1]["Fn::Join"][1]
    assert joined[0] == "{{resolve:secretsmanager:"

    task_defs = t.find_resources("AWS::ECS::TaskDefinition")
    mcp_containers = [
        c
        for td in task_defs.values()
        for c in td["Properties"]["ContainerDefinitions"]
        if c["Name"] == "fde-mcp"
    ]
    env = {e["Name"]: e["Value"] for e in mcp_containers[0]["Environment"]}
    embed_key = env["FDE_EMBED_API_KEY"]
    assert embed_key["Fn::If"][0] == "HasProviderKey"


# ---------------------------------------------------------------------
# Deploy ordering: Services and GateService must run after Migrations.
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Cognito console client: callback/logout URLs updated once the API exists.
# ---------------------------------------------------------------------


def test_console_client_callback_and_logout_urls_point_at_the_gate_api() -> None:
    t = synth_template()
    clients = t.find_resources("AWS::Cognito::UserPoolClient")
    console_clients = [
        c for c in clients.values() if "code" in c["Properties"].get("AllowedOAuthFlows", [])
    ]
    assert len(console_clients) == 1
    client = console_clients[0]
    assert client["Properties"]["CallbackURLs"] != ["https://example.com"]
    assert client["Properties"]["LogoutURLs"] != ["https://example.com"]
    callback = client["Properties"]["CallbackURLs"][0]
    assert client["Properties"]["LogoutURLs"][0] == callback
    # The callback URL is built from the HttpApi's own ApiEndpoint
    # attribute plus "/ui" (the console's root route) -- not a literal.
    assert isinstance(callback, dict)
    assert "/ui" in str(callback)


def test_services_and_gate_service_depend_on_migrations_custom_resource() -> None:
    t = synth_template()
    template = t.to_json()
    custom_resource_id = next(
        k
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::CloudFormation::CustomResource"
    )
    depends_on_sets = [
        set(v["DependsOn"]) for v in template["Resources"].values() if "DependsOn" in v
    ]
    assert any(custom_resource_id in s for s in depends_on_sets)
