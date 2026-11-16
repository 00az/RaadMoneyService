"""CDK assertions for isolated service tables and internal Function URL access."""

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from raad_money.infrastructure import MoneyServiceStack


def template() -> Template:
    app = cdk.App()
    stack = MoneyServiceStack(
        app,
        "Service",
        environment="dev",
        verifier_parameter="/raad/dev/verifier",
        correlation_secret_arn="arn:aws:secretsmanager:us-east-1:111111111111:secret:raad/dev/correlation-hmac-abc",
        edge_role_arn="arn:aws:iam::111111111111:role/raad-dev-edge",
    )
    return Template.from_stack(stack)


def test_service_owns_three_separate_encrypted_tables() -> None:
    template().resource_count_is("AWS::DynamoDB::Table", 3)
    template().has_resource_properties(
        "AWS::DynamoDB::Table", {"SSESpecification": {"SSEEnabled": True}}
    )


def test_tables_use_on_demand_capacity() -> None:
    template().has_resource_properties("AWS::DynamoDB::Table", {"BillingMode": "PAY_PER_REQUEST"})


def test_money_api_has_seven_second_timeout_and_dev_cap() -> None:
    template().has_resource_properties(
        "AWS::Lambda::Function", {"Timeout": 7, "ReservedConcurrentExecutions": 20}
    )


def test_money_api_declares_x86_to_match_native_bundle() -> None:
    template().has_resource_properties("AWS::Lambda::Function", {"Architectures": ["x86_64"]})


def test_execution_role_has_no_managed_policy_or_unbounded_logs():
    built = template()
    assert all(
        not role["Properties"].get("ManagedPolicyArns")
        for role in built.find_resources("AWS::IAM::Role").values()
    )
    for policy in built.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if "logs:PutLogEvents" in statement.get("Action", []):
                assert statement["Resource"] != "*"


def test_service_bundle_matches_x86_ci_platform(monkeypatch):
    from aws_cdk import aws_lambda as lambda_

    platforms = []

    def asset(*args, **kwargs):
        platforms.append(kwargs["bundling"].platform)
        return lambda_.Code.from_inline("def handle(event, context): return {}")

    monkeypatch.setattr(lambda_.Code, "from_asset", asset)
    template()
    assert platforms == ["linux/amd64"]


def test_function_url_requires_iam_authentication() -> None:
    template().has_resource_properties("AWS::Lambda::Url", {"AuthType": "AWS_IAM"})


def test_function_url_is_published_to_environment_parameter() -> None:
    template().has_resource_properties(
        "AWS::SSM::Parameter", {"Name": "/raad/dev/money-api/function-url"}
    )


def test_edge_invoke_permission_is_limited_to_function_url() -> None:
    rendered = template().to_json()
    policies = [
        resource["Properties"]
        for resource in rendered["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Permission"
    ]
    assert any(
        policy.get("Action") == "lambda:InvokeFunctionUrl"
        and policy.get("FunctionUrlAuthType") == "AWS_IAM"
        for policy in policies
    )
    assert any(
        policy.get("Action") == "lambda:InvokeFunction"
        and policy.get("InvokedViaFunctionUrl") is True
        for policy in policies
    )


def test_dev_money_logs_have_fourteen_day_retention() -> None:
    template().has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


def test_writer_is_disabled_by_default_until_canonical_cutover_is_ready() -> None:
    rendered = template().to_json()
    assert rendered["Parameters"]["ActivateCanonicalWriter"]["Default"] == "false"
    template().has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {"CANONICAL_WRITER_ACTIVE": {"Ref": "ActivateCanonicalWriter"}}
                )
            }
        },
    )


def test_transactions_table_uses_only_transfer_id_key_and_no_ttl() -> None:
    tables = template().find_resources("AWS::DynamoDB::Table")
    table = next(
        r["Properties"]
        for r in tables.values()
        if r["Properties"].get("TableName") == "raad-dev-transactions"
    )
    assert table["KeySchema"] == [{"AttributeName": "transferId", "KeyType": "HASH"}]
    assert "TimeToLiveSpecification" not in table
