"""CDK assertions for the money service stack in each environment, plus cdk-nag."""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from cdk_nag import AwsSolutionsChecks

from raad_money.infrastructure import MoneyServiceStack

TOPIC = "arn:aws:sns:us-east-1:111111111111:raad-alarms"
BUS = "arn:aws:events:us-east-1:111111111111:event-bus/raad-platform"


def stack(environment: str = "dev", *, nag: bool = False, **extra: Any) -> MoneyServiceStack:
    app = cdk.App()
    built = MoneyServiceStack(
        app,
        "Service",
        environment=environment,
        verifier_parameter=f"/raad/{environment}/verifier",
        correlation_secret_arn=f"arn:aws:secretsmanager:us-east-1:111111111111:secret:raad/{environment}/correlation-hmac-abc",
        edge_role_arn=f"arn:aws:iam::111111111111:role/raad-{environment}-edge",
        event_bus_arn=BUS,
        alarm_topic_arn=TOPIC,
        **extra,
    )
    if nag:
        cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    return built


def template(environment: str = "dev", **extra: Any) -> Template:
    return Template.from_stack(stack(environment, **extra))


def table(built: Template, name: str) -> dict[str, Any]:
    return next(
        r
        for r in built.find_resources("AWS::DynamoDB::Table").values()
        if r["Properties"]["TableName"] == name
    )


def statements(built: Template) -> list[dict[str, Any]]:
    return [
        statement
        for policy in built.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


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


@pytest.mark.parametrize("environment", ["stg", "prd"])
def test_protected_environments_retain_protect_and_recover_all_three_tables(
    environment: str,
) -> None:
    built = template(environment)
    for name in ("ledger", "directory", "transactions"):
        resource = table(built, f"raad-{environment}-{name}")
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["Properties"]["DeletionProtectionEnabled"] is True
        assert resource["Properties"]["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }
        assert "TimeToLiveSpecification" not in resource["Properties"]
    built.has_resource_properties("AWS::Lambda::Function", {"ReservedConcurrentExecutions": 20})
    built.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})


def test_dev_tables_are_resettable_and_named_per_environment() -> None:
    built = template("dev")
    for name in ("ledger", "directory", "transactions"):
        assert table(built, f"raad-dev-{name}")["DeletionPolicy"] == "Delete"


def test_unknown_environment_or_index_stage_is_refused() -> None:
    with pytest.raises(ValueError):
        stack("qa")
    with pytest.raises(ValueError):
        stack("dev", history_indexes=3)


def test_transactions_table_has_two_keys_only_role_indexes_and_no_ttl() -> None:
    resource = table(template(), "raad-dev-transactions")["Properties"]
    assert resource["KeySchema"] == [{"AttributeName": "transferId", "KeyType": "HASH"}]
    indexes = {index["IndexName"]: index for index in resource["GlobalSecondaryIndexes"]}
    assert set(indexes) == {"bySenderWallet", "byRecipientWallet"}
    assert indexes["bySenderWallet"]["KeySchema"] == [
        {"AttributeName": "senderWalletId", "KeyType": "HASH"},
        {"AttributeName": "completedSortKey", "KeyType": "RANGE"},
    ]
    assert all(index["Projection"] == {"ProjectionType": "KEYS_ONLY"} for index in indexes.values())
    assert "TimeToLiveSpecification" not in resource


def test_existing_table_upgrade_can_add_one_index_per_deployment() -> None:
    resource = table(template(history_indexes=1), "raad-dev-transactions")["Properties"]
    assert [index["IndexName"] for index in resource["GlobalSecondaryIndexes"]] == [
        "bySenderWallet"
    ]


def test_money_api_declares_x86_to_match_native_bundle() -> None:
    template().has_resource_properties("AWS::Lambda::Function", {"Architectures": ["x86_64"]})


def test_execution_roles_have_no_managed_policy_wildcard_actions_scans_or_deletes() -> None:
    built = template()
    assert all(
        not role["Properties"].get("ManagedPolicyArns")
        for role in built.find_resources("AWS::IAM::Role").values()
    )
    for statement in statements(built):
        actions = (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        assert "dynamodb:*" not in actions
        assert not {"dynamodb:Scan", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"} & set(
            actions
        )
        if "logs:PutLogEvents" in actions:
            assert statement["Resource"] != "*"


def test_history_query_is_limited_to_the_two_role_indexes() -> None:
    query = [s for s in statements(template()) if s["Action"] == "dynamodb:Query"]
    assert len(query) == 1
    rendered = json.dumps(query[0]["Resource"])
    assert "/index/bySenderWallet" in rendered and "/index/byRecipientWallet" in rendered
    assert "/index/*" not in rendered


def test_service_bundle_matches_x86_ci_platform(monkeypatch):
    from aws_cdk import aws_lambda as lambda_

    platforms = []

    def asset(*args, **kwargs):
        platforms.append(kwargs["bundling"].platform)
        return lambda_.Code.from_inline("def handle(event, context): return {}")

    monkeypatch.setattr(lambda_.Code, "from_asset", asset)
    template()
    assert platforms == ["linux/amd64", "linux/amd64"]


def test_function_url_requires_iam_authentication() -> None:
    built = template()
    built.resource_count_is("AWS::Lambda::Url", 1)
    built.has_resource_properties("AWS::Lambda::Url", {"AuthType": "AWS_IAM"})


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
    assert not any(policy.get("Principal") == "*" for policy in policies)


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


def test_cursor_secret_is_generated_service_owned_and_readable_only_by_money_api() -> None:
    built = template()
    built.has_resource_properties(
        "AWS::SecretsManager::Secret",
        {
            "Name": "raad/dev/money-history-cursor",
            "GenerateSecretString": Match.object_like(
                {"GenerateStringKey": "key", "PasswordLength": 64}
            ),
        },
    )
    built.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "raad_money.api.handle",
            "Environment": {
                "Variables": Match.object_like({"CURSOR_SECRET_ARN": Match.any_value()})
            },
        },
    )
    readers = [
        s for s in statements(built) if "secretsmanager:GetSecretValue" in json.dumps(s["Action"])
    ]
    assert len(readers) == 2  # correlation secret and cursor secret, both on the API role


def test_user_confirmed_rule_is_bound_to_source_type_and_environment() -> None:
    template("stg").has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventBusName": "raad-platform",
            "EventPattern": {
                "source": ["raad.identity"],
                "detail-type": ["raad.identity.user-confirmed.v1"],
                "detail": {"environment": ["stg"]},
            },
            "Targets": [
                Match.object_like(
                    {
                        "DeadLetterConfig": Match.object_like({"Arn": Match.any_value()}),
                        "RetryPolicy": {
                            "MaximumEventAgeInSeconds": 86_400,
                            "MaximumRetryAttempts": 185,
                        },
                    }
                )
            ],
        },
    )


def test_work_queue_and_dead_letter_queues_follow_the_delivery_design() -> None:
    built = template()
    built.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "raad-dev-user-confirmed",
            "MessageRetentionPeriod": 345_600,
            "VisibilityTimeout": 90,
            "SqsManagedSseEnabled": True,
            "RedrivePolicy": Match.object_like({"maxReceiveCount": 1000}),
        },
    )
    for name in ("processing", "delivery"):
        built.has_resource_properties(
            "AWS::SQS::Queue",
            {
                "QueueName": f"raad-dev-user-confirmed-{name}-dlq",
                "MessageRetentionPeriod": 1_209_600,
                "SqsManagedSseEnabled": True,
            },
        )
    policies = json.dumps(built.find_resources("AWS::SQS::QueuePolicy"))
    assert "events.amazonaws.com" in policies and "aws:SourceArn" in policies


def test_consumer_reads_one_message_at_a_time_with_two_workers() -> None:
    template().has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "BatchSize": 1,
            "ScalingConfig": {"MaximumConcurrency": 2},
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
        },
    )
    template().has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "raad_money.consumer.handle", "Timeout": 30},
    )


def test_consumer_cannot_touch_transfer_rows() -> None:
    built = template()
    roles = built.find_resources("AWS::IAM::Role")
    consumer_role = next(name for name in roles if name.startswith("UserConfirmedConsumerRole"))
    policies = [
        p
        for p in built.find_resources("AWS::IAM::Policy").values()
        if any(r.get("Ref") == consumer_role for r in p["Properties"]["Roles"])
    ]
    rendered = json.dumps(policies)
    assert "TransactionsTable" not in rendered
    assert "dynamodb:Query" not in rendered


def test_service_alarms_route_to_the_platform_topic() -> None:
    built = template()
    alarms = built.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 9
    for alarm in alarms.values():
        assert alarm["Properties"]["AlarmActions"] == [TOPIC]
    error_rate = next(
        a["Properties"]
        for a in alarms.values()
        if a["Properties"]["AlarmName"] == "raad-dev-money-technical-error-rate"
    )
    assert error_rate["Threshold"] == 1 and error_rate["EvaluationPeriods"] == 2
    assert error_rate["TreatMissingData"] == "missing"
    expression = next(m["Expression"] for m in error_rate["Metrics"] if "Expression" in m)
    assert "requests >= 20" in expression
    dlq = [
        a["Properties"] for a in alarms.values() if a["Properties"]["AlarmName"].endswith("-dlq")
    ]
    assert len(dlq) == 2 and all(a["Threshold"] == 1 and a["Period"] == 60 for a in dlq)
    built.resource_count_is("AWS::CloudWatch::Dashboard", 1)


@pytest.mark.parametrize("environment", ["dev", "prd"])
def test_cdk_nag_reports_no_unsuppressed_findings(environment: str) -> None:
    annotations = Annotations.from_stack(stack(environment, nag=True))
    annotations.has_no_error("*", Match.string_like_regexp("AwsSolutions-.*"))
    annotations.has_no_warning("*", Match.string_like_regexp("AwsSolutions-.*"))
