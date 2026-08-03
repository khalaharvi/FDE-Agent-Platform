from __future__ import annotations

from tests.test_synth import synth_template


def test_cognito_pool_and_three_clients() -> None:
    t = synth_template()
    t.resource_count_is("AWS::Cognito::UserPool", 1)
    t.resource_count_is("AWS::Cognito::UserPoolClient", 3)


def test_no_unsubstituted_placeholders_in_any_role() -> None:
    import json

    raw = json.dumps(synth_template().to_json())
    assert "${AWS_ACCOUNT_ID}" not in raw
    assert "${AWS_REGION}" not in raw
    assert "${FDE_DB_SECRET_ARN}" not in raw
    assert "${DB_RESOURCE_ID}" not in raw
    assert "${FDE_GATE_FUNCTION_NAME}" not in raw
    assert "${AGENT_RUNTIME_ARN}" not in raw


def test_self_signup_is_off() -> None:
    t = synth_template()
    pools = t.find_resources("AWS::Cognito::UserPool")
    pool = next(iter(pools.values()))
    assert pool["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True


def test_admin_user_seeded_with_admin_email_param() -> None:
    t = synth_template()
    users = t.find_resources("AWS::Cognito::UserPoolUser")
    assert len(users) == 1
    user = next(iter(users.values()))
    # Username is Ref'd from the AdminEmail CfnParameter, not a literal.
    assert user["Properties"]["Username"] == {"Ref": "AdminEmail"}


def test_one_client_has_client_credentials_grant() -> None:
    t = synth_template()
    clients = t.find_resources("AWS::Cognito::UserPoolClient")
    m2m = [
        c
        for c in clients.values()
        if "client_credentials" in c["Properties"].get("AllowedOAuthFlows", [])
    ]
    assert len(m2m) == 1
    assert m2m[0]["Properties"]["GenerateSecret"] is True
    # The scope string is "<resource-server-id>/invoke", built at deploy
    # time by Fn::Join since the resource server id is itself a Ref -- not
    # resolvable to a literal at synth time, so assert on the Join shape.
    scopes = m2m[0]["Properties"]["AllowedOAuthScopes"]
    assert len(scopes) == 1
    join_parts = scopes[0]["Fn::Join"][1]
    assert join_parts[-1] == "/invoke"


def test_resource_server_defines_gateway_invoke_scope() -> None:
    t = synth_template()
    servers = t.find_resources("AWS::Cognito::UserPoolResourceServer")
    assert len(servers) == 1
    server = next(iter(servers.values()))
    assert server["Properties"]["Identifier"] == "gateway"
    scope_names = [s["ScopeName"] for s in server["Properties"]["Scopes"]]
    assert scope_names == ["invoke"]


def test_hosted_ui_domain_exists() -> None:
    t = synth_template()
    t.resource_count_is("AWS::Cognito::UserPoolDomain", 1)


def test_five_iam_roles_exist_with_expected_trust_services() -> None:
    t = synth_template()
    roles = t.find_resources("AWS::IAM::Role")
    # 5 roles from IamRoles, exactly. (No other IAM::Role in the stack yet.)
    assert len(roles) == 5
    services = set()
    for role in roles.values():
        for stmt in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            principal = stmt["Principal"]
            if "Service" in principal:
                services.add(principal["Service"])
    assert services == {"bedrock-agentcore.amazonaws.com", "lambda.amazonaws.com"}


def test_runtime_role_permissions_reference_substituted_db_secret_arn() -> None:
    t = synth_template()
    roles = t.find_resources("AWS::IAM::Role")
    runtime_roles = [
        r
        for r in roles.values()
        for policy in r["Properties"].get("Policies", [])
        if policy["PolicyName"] == "runtime-permissions"
    ]
    assert len(runtime_roles) == 1
    policy = next(
        p
        for p in runtime_roles[0]["Properties"]["Policies"]
        if p["PolicyName"] == "runtime-permissions"
    )
    statements = policy["PolicyDocument"]["Statement"]
    read_secret = next(s for s in statements if s["Sid"] == "ReadTheDbSecret")
    # Not a literal "${FDE_DB_SECRET_ARN}" string -- a real Ref/GetAtt/Join.
    assert read_secret["Resource"] != "${FDE_DB_SECRET_ARN}"
    assert isinstance(read_secret["Resource"], dict)


def test_gate_role_log_group_uses_fixed_function_name() -> None:
    t = synth_template()
    roles = t.find_resources("AWS::IAM::Role")
    gate_roles = [
        r
        for r in roles.values()
        for policy in r["Properties"].get("Policies", [])
        if policy["PolicyName"] == "gate-permissions"
    ]
    assert len(gate_roles) == 1
    policy = next(
        p for p in gate_roles[0]["Properties"]["Policies"] if p["PolicyName"] == "gate-permissions"
    )
    statements = policy["PolicyDocument"]["Statement"]
    own_logs = next(s for s in statements if s["Sid"] == "OwnLogGroupOnly")
    raw = str(own_logs["Resource"])
    assert "fde-gate-service" in raw


def test_migration_role_has_vpc_managed_policy_and_scoped_secret_actions() -> None:
    t = synth_template()
    roles = t.find_resources("AWS::IAM::Role")
    migration_roles = [
        r
        for r in roles.values()
        for policy in r["Properties"].get("Policies", [])
        if policy["PolicyName"] == "migration-permissions"
    ]
    assert len(migration_roles) == 1
    role = migration_roles[0]
    managed = role["Properties"].get("ManagedPolicyArns", [])
    assert any("AWSLambdaVPCAccessExecutionRole" in str(m) for m in managed)
    policy = next(
        p for p in role["Properties"]["Policies"] if p["PolicyName"] == "migration-permissions"
    )
    actions = {
        a
        for s in policy["PolicyDocument"]["Statement"]
        for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    }
    assert "secretsmanager:GetSecretValue" in actions
    assert "secretsmanager:CreateSecret" in actions
    assert "secretsmanager:PutSecretValue" in actions


def test_gateway_and_memory_roles_have_inline_policies() -> None:
    t = synth_template()
    roles = t.find_resources("AWS::IAM::Role")
    policy_names = {
        policy["PolicyName"]
        for r in roles.values()
        for policy in r["Properties"].get("Policies", [])
    }
    assert "gateway-service-permissions" in policy_names
    assert "memory-execution-permissions" in policy_names


def test_discovery_url_is_well_formed_synth_time() -> None:
    """`Identity.discovery_url` is a Python-level string built from CDK
    tokens (region + pool id) -- assert it has the right literal shape, not
    resolved values (those are only known at deploy time)."""
    import aws_cdk as cdk

    from fde_cdk.stack import FdePlatformStack

    app = cdk.App()
    stack = FdePlatformStack(app, "FdePlatform")
    url = stack.identity.discovery_url
    assert url.startswith("https://cognito-idp.")
    assert url.endswith("/.well-known/openid-configuration")
