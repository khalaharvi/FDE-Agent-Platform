from __future__ import annotations

from tests.test_synth import synth_template


def _gate_lambda(functions: dict) -> dict:
    return next(
        f
        for f in functions.values()
        if f["Properties"].get("Handler") == "fde_gate.handler.lambda_handler"
    )


# ---------------------------------------------------------------------
# Agents: pull-through cache rule + three runtimes (brief's own tests)
# ---------------------------------------------------------------------


def test_three_agentcore_runtimes_and_cache_rule() -> None:
    t = synth_template()
    j = t.to_json()["Resources"]
    runtimes = [
        r
        for r in j.values()
        if r["Type"].startswith("AWS::BedrockAgentCore")
        and "Runtime" in r["Type"]
        and "Endpoint" not in r["Type"]
    ]
    assert len(runtimes) == 3
    t.resource_count_is("AWS::ECR::PullThroughCacheRule", 1)


def test_pull_through_cache_rule_shape() -> None:
    t = synth_template()
    rules = t.find_resources("AWS::ECR::PullThroughCacheRule")
    rule = next(iter(rules.values()))
    assert rule["Properties"]["EcrRepositoryPrefix"] == "ecr-public"
    assert rule["Properties"]["UpstreamRegistryUrl"] == "public.ecr.aws"


def test_runtimes_are_public_network_mode_with_runtime_role() -> None:
    t = synth_template()
    template = t.to_json()
    roles = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role"}
    runtime_role_id = next(
        k
        for k, v in roles.items()
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "runtime-permissions"
    )
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 3
    for r in runtimes.values():
        props = r["Properties"]
        assert props["NetworkConfiguration"]["NetworkMode"] == "PUBLIC"
        assert props["RoleArn"] == {"Fn::GetAtt": [runtime_role_id, "Arn"]}


def test_runtime_container_uri_goes_through_ecr_public_pull_through_cache() -> None:
    t = synth_template()
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    for r in runtimes.values():
        uri = r["Properties"]["AgentRuntimeArtifact"]["ContainerConfiguration"]["ContainerUri"]
        raw = str(uri)
        assert "ecr-public" in raw
        assert ".dkr.ecr." in raw
        assert "fde-" in raw


def test_runtime_agent_names_are_the_three_distinct_agents() -> None:
    t = synth_template()
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    names = sorted(
        r["Properties"]["EnvironmentVariables"]["FDE_AGENT_NAME"] for r in runtimes.values()
    )
    assert names == ["development", "engagement", "workflow"]


def test_runtime_env_has_gateway_url_and_provider_envs() -> None:
    t = synth_template()
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 3
    for r in runtimes.values():
        env = r["Properties"]["EnvironmentVariables"]
        assert "FDE_AGENT_NAME" in env
        assert "FDE_MODEL_ID" in env
        assert "FDE_GATEWAY_URL" in env
        assert env["FDE_MODEL_PROVIDER"] == {"Ref": "ModelProvider"}
        assert env["FDE_MODEL_BASE_URL"] == {"Ref": "CompatBaseUrl"}
        assert "FDE_MODEL_API_KEY" in env
        # FDE_GATEWAY_URL is a real Gateway attribute token, not a literal.
        assert isinstance(env["FDE_GATEWAY_URL"], dict)


def test_runtime_model_id_env_is_fn_if_on_is_bedrock_model() -> None:
    """Same rule gate.py's FDE_MODEL_ID follows -- params.model_id_env is
    the shared helper both now call."""
    t = synth_template()
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    for r in runtimes.values():
        env = r["Properties"]["EnvironmentVariables"]
        assert env["FDE_MODEL_ID"] == {"Fn::If": ["IsBedrockModel", "", {"Ref": "ModelId"}]}


def test_every_runtime_has_aws_region_and_db_secret_arn() -> None:
    """Fix-report finding: AWS_REGION (mcp_tools._mint_gateway_bearer_token
    raises RuntimeError without it -- the only MCP-connection path a
    deployed runtime takes) and FDE_DB_SECRET_ARN (tracing.py's raw_sql
    writes otherwise have no DSN source and silently no-op) must be
    present on EVERY runtime's env, not just one."""
    t = synth_template()
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 3
    for r in runtimes.values():
        env = r["Properties"]["EnvironmentVariables"]
        assert env["AWS_REGION"] == {"Ref": "AWS::Region"}
        db_secret_arn = env["FDE_DB_SECRET_ARN"]
        assert isinstance(db_secret_arn, dict)
        assert "fde/db/agent" in str(db_secret_arn)


def test_runtime_role_has_supplemental_grant_on_fde_db_secrets() -> None:
    """The gap this fix closes: runtime_role's OWN template only grants
    read on the Aurora cluster's master secret, not fde/db/agent -- same
    class of bug (and same fix shape) as gate.py's supplemental grant on
    gate_role (test_gate_role_has_supplemental_grant_on_fde_db_secrets)."""
    t = synth_template()
    template = t.to_json()
    resources = template["Resources"]
    runtime_role_id = next(
        k
        for k, v in resources.items()
        if v["Type"] == "AWS::IAM::Role"
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "runtime-permissions"
    )
    statements = []
    for v in resources.values():
        if v["Type"] == "AWS::IAM::Policy" and {"Ref": runtime_role_id} in v["Properties"].get(
            "Roles", []
        ):
            statements.extend(v["Properties"]["PolicyDocument"]["Statement"])

    read_login_secret = next(s for s in statements if s.get("Sid") == "ReadRuntimeLoginSecret")
    assert "fde/db/*" in str(read_login_secret["Resource"])


def test_runtime_role_has_ecr_public_pull_through_cache_grant() -> None:
    """C2 fix (final-fix-report.md): runtime_role's own template only
    grants pulls on `repository/fde-*-agent` -- the wrong repo path
    entirely (runtimes pull through the ECR-Public pull-through cache at
    `repository/ecr-public/{alias}/fde-{agent}`). This supplemental
    statement must exist with the pull actions AND the first-pull-only
    import/create actions (cached repositories are not pre-existing)."""
    t = synth_template()
    template = t.to_json()
    resources = template["Resources"]
    runtime_role_id = next(
        k
        for k, v in resources.items()
        if v["Type"] == "AWS::IAM::Role"
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "runtime-permissions"
    )
    statements = []
    for v in resources.values():
        if v["Type"] == "AWS::IAM::Policy" and {"Ref": runtime_role_id} in v["Properties"].get(
            "Roles", []
        ):
            statements.extend(v["Properties"]["PolicyDocument"]["Statement"])

    ecr_grant = next(s for s in statements if s.get("Sid") == "PullThroughCacheEcrPublicImages")
    actions = set(ecr_grant["Action"])
    assert {
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchImportUpstreamImage",
        "ecr:CreateRepository",
    } <= actions
    raw = str(ecr_grant["Resource"])
    assert "repository/ecr-public/" in raw


def test_runtimes_depend_on_pull_through_cache_rule() -> None:
    t = synth_template()
    template = t.to_json()
    cache_rule_id = next(
        k for k, v in template["Resources"].items() if v["Type"] == "AWS::ECR::PullThroughCacheRule"
    )
    runtime_ids = [
        k for k, v in template["Resources"].items() if v["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    assert len(runtime_ids) == 3
    for rid in runtime_ids:
        depends = template["Resources"][rid].get("DependsOn", [])
        assert cache_rule_id in depends


# ---------------------------------------------------------------------
# Agents: Gateway + GatewayTarget
# ---------------------------------------------------------------------


def test_gateway_exists_with_mcp_semantic_search_and_custom_jwt() -> None:
    t = synth_template()
    t.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)
    gateways = t.find_resources("AWS::BedrockAgentCore::Gateway")
    gw = next(iter(gateways.values()))
    props = gw["Properties"]
    assert props["AuthorizerType"] == "CUSTOM_JWT"
    assert props["ProtocolType"] == "MCP"
    assert props["ProtocolConfiguration"]["Mcp"]["SearchType"] == "SEMANTIC"
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert "DiscoveryUrl" in jwt
    assert jwt["AllowedAudience"]


def test_gateway_jwt_audience_is_m2m_client_id() -> None:
    t = synth_template()
    template = t.to_json()
    clients = {
        k: v
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::Cognito::UserPoolClient"
    }
    m2m_client_id = next(
        k
        for k, v in clients.items()
        if "client_credentials" in v["Properties"].get("AllowedOAuthFlows", [])
    )
    gw = next(iter(t.find_resources("AWS::BedrockAgentCore::Gateway").values()))
    audience = gw["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedAudience"]
    assert audience == [{"Ref": m2m_client_id}]


def test_gateway_jwt_authorizer_also_has_allowed_clients() -> None:
    """I4(a) fix (final-fix-report.md): Cognito client-credentials tokens
    carry a `client_id` claim, not (reliably) `aud` -- `allowed_clients`
    must be set alongside `allowed_audience`, both naming the m2m client."""
    t = synth_template()
    template = t.to_json()
    clients = {
        k: v
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::Cognito::UserPoolClient"
    }
    m2m_client_id = next(
        k
        for k, v in clients.items()
        if "client_credentials" in v["Properties"].get("AllowedOAuthFlows", [])
    )
    gw = next(iter(t.find_resources("AWS::BedrockAgentCore::Gateway").values()))
    jwt = gw["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["AllowedClients"] == [{"Ref": m2m_client_id}]


def test_every_runtime_has_fde_gateway_scopes_env() -> None:
    """I4(b) fix (final-fix-report.md): the code default
    (`fde_agents/common/config.py`'s `GatewaySettings.scopes`) is
    "gateway:invoke" (colon) when unset -- Cognito's own scope separator
    is "/", so every runtime needs FDE_GATEWAY_SCOPES set explicitly."""
    t = synth_template()
    runtimes = t.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 3
    for r in runtimes.values():
        assert r["Properties"]["EnvironmentVariables"]["FDE_GATEWAY_SCOPES"] == "gateway/invoke"


def test_gateway_m2m_credential_provider_exists_with_cognito_oauth2_config() -> None:
    """I4(c) fix (final-fix-report.md): the `fde-gateway-m2m` AgentCore
    Identity OAuth2 credential provider `IdentityClient.get_token(
    provider_name="fde-gateway-m2m", ...)` (`mcp_tools.py`) needs to
    already exist -- provisioned here rather than left as a manual step,
    since the m2m client's secret IS retrievable without CDK assets (see
    `identity.py`'s own `m2m_client_secret`)."""
    t = synth_template()
    providers = t.find_resources("AWS::BedrockAgentCore::OAuth2CredentialProvider")
    assert len(providers) == 1
    provider = next(iter(providers.values()))
    props = provider["Properties"]
    assert props["Name"] == "fde-gateway-m2m"
    # Live-validated pairing (v0.3.0-rc1): custom config member requires the
    # CustomOauth2 vendor; "CognitoOauth2" + custom config is rejected by the
    # control plane with a ValidationException.
    assert props["CredentialProviderVendor"] == "CustomOauth2"
    custom = props["Oauth2ProviderConfigInput"]["CustomOauth2ProviderConfig"]
    assert ".well-known/openid-configuration" in str(custom["OauthDiscovery"]["DiscoveryUrl"])
    assert custom["ClientSecretSource"] == "EXTERNAL"
    assert custom["ClientSecretConfig"]["JsonKey"] == "client_secret"
    assert isinstance(custom["ClientId"], dict)  # a real Ref, not a literal


def test_gateway_target_points_at_mcp_alb_with_iam_role_credentials() -> None:
    t = synth_template()
    t.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 1)
    targets = t.find_resources("AWS::BedrockAgentCore::GatewayTarget")
    target = next(iter(targets.values()))
    endpoint = target["Properties"]["TargetConfiguration"]["Mcp"]["McpServer"]["Endpoint"]
    assert "/mcp" in str(endpoint)
    creds = target["Properties"]["CredentialProviderConfigurations"]
    assert creds[0]["CredentialProviderType"] == "GATEWAY_IAM_ROLE"


# ---------------------------------------------------------------------
# Agents: Memory
# ---------------------------------------------------------------------


def test_memory_has_three_strategies_and_90_day_expiry() -> None:
    t = synth_template()
    t.resource_count_is("AWS::BedrockAgentCore::Memory", 1)
    memories = t.find_resources("AWS::BedrockAgentCore::Memory")
    mem = next(iter(memories.values()))
    props = mem["Properties"]
    assert props["EventExpiryDuration"] == 90
    strategies = props["MemoryStrategies"]
    assert len(strategies) == 3
    kinds = {next(iter(s)) for s in strategies}
    assert kinds == {
        "SemanticMemoryStrategy",
        "SummaryMemoryStrategy",
        "UserPreferenceMemoryStrategy",
    }


def test_memory_uses_memory_role() -> None:
    t = synth_template()
    template = t.to_json()
    roles = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role"}
    memory_role_id = next(
        k
        for k, v in roles.items()
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "memory-execution-permissions"
    )
    mem = next(iter(t.find_resources("AWS::BedrockAgentCore::Memory").values()))
    assert mem["Properties"]["MemoryExecutionRoleArn"] == {"Fn::GetAtt": [memory_role_id, "Arn"]}


# ---------------------------------------------------------------------
# GateService: runtime ARN envs now carry real tokens (behavior change)
# ---------------------------------------------------------------------


def test_gate_lambda_runtime_arn_envs_are_real_tokens_not_blank_placeholders() -> None:
    t = synth_template()
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    env = fn["Properties"]["Environment"]["Variables"]
    for agent in ("ENGAGEMENT", "WORKFLOW", "DEVELOPMENT"):
        value = env[f"FDE_RUNTIME_ARN_{agent}"]
        assert value != ""
        assert isinstance(value, dict)


def test_gate_lambda_runtime_arns_reference_the_matching_runtime() -> None:
    """Each FDE_RUNTIME_ARN_{AGENT} env is that SAME agent's own runtime's
    attr_agent_runtime_arn -- not just any token, not swapped."""
    t = synth_template()
    template = t.to_json()
    runtimes = {
        k: v
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::BedrockAgentCore::Runtime"
    }
    fn = _gate_lambda(t.find_resources("AWS::Lambda::Function"))
    env = fn["Properties"]["Environment"]["Variables"]

    for agent_upper, agent_lower in (
        ("ENGAGEMENT", "engagement"),
        ("WORKFLOW", "workflow"),
        ("DEVELOPMENT", "development"),
    ):
        runtime_id = next(
            k
            for k, v in runtimes.items()
            if v["Properties"]["AgentRuntimeName"] == f"fde_{agent_lower}_agent"
        )
        assert env[f"FDE_RUNTIME_ARN_{agent_upper}"] == {
            "Fn::GetAtt": [runtime_id, "AgentRuntimeArn"]
        }


# ---------------------------------------------------------------------
# Outputs (brief's own test, plus shape checks)
# ---------------------------------------------------------------------


def test_outputs_complete() -> None:
    outs = synth_template().to_json()["Outputs"]
    for key in (
        "ReviewConsoleUrl",
        "CognitoLoginUrl",
        "ApiEndpoint",
        "McpEndpoint",
        "FirstStepsUrl",
    ):
        assert key in outs


def test_first_steps_url_points_at_docs_13_launch_stack() -> None:
    outs = synth_template().to_json()["Outputs"]
    assert outs["FirstStepsUrl"]["Value"] == (
        "https://github.com/khalaharvi/FDE-Agent-Platform/blob/main/docs/13-launch-stack.md"
    )


def test_review_console_url_and_api_endpoint_are_different_values() -> None:
    outs = synth_template().to_json()["Outputs"]
    assert outs["ReviewConsoleUrl"]["Value"] != outs["ApiEndpoint"]["Value"]


def test_mcp_endpoint_output_labeled_internal_only() -> None:
    outs = synth_template().to_json()["Outputs"]
    assert "internal" in outs["McpEndpoint"]["Description"].lower()


def test_cognito_login_url_carries_console_client_id_and_redirect() -> None:
    t = synth_template()
    outs = t.to_json()["Outputs"]
    template = t.to_json()
    clients = {
        k: v
        for k, v in template["Resources"].items()
        if v["Type"] == "AWS::Cognito::UserPoolClient"
    }
    console_client_id = next(
        k for k, v in clients.items() if "code" in v["Properties"].get("AllowedOAuthFlows", [])
    )
    raw = str(outs["CognitoLoginUrl"]["Value"])
    assert console_client_id in raw
    assert "response_type=code" in raw


def test_every_runtime_depends_on_the_runtime_role_default_policy() -> None:
    """Live-validated (v0.3.0-rc3): CreateAgentRuntime validates the ECR URI
    synchronously, so each runtime must wait for the CDK-generated
    DefaultPolicy (carrying the pull-through-cache ECR grants) to attach --
    referencing role_arn alone races the policy attachment."""
    t = synth_template()
    resources = t.to_json()["Resources"]
    default_policy_ids = [
        k
        for k, v in resources.items()
        if v["Type"] == "AWS::IAM::Policy" and k.startswith("IamRolesRuntimeRoleDefaultPolicy")
    ]
    assert len(default_policy_ids) == 1
    runtimes = {k: v for k, v in resources.items() if v["Type"] == "AWS::BedrockAgentCore::Runtime"}
    assert len(runtimes) == 3
    for runtime_id, runtime in runtimes.items():
        assert default_policy_ids[0] in runtime.get("DependsOn", []), runtime_id
