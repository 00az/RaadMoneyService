"""Money service data resources and IAM-authenticated service Function URL."""

from __future__ import annotations

from typing import Any

from aws_cdk import BundlingOptions, CfnParameter, Duration, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from cdk_nag import NagSuppressions
from constructs import Construct


class MoneyServiceStack(Stack):
    """Owns MoneyApi and its three logically isolated tables."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        verifier_parameter: str,
        correlation_secret_arn: str,
        edge_role_arn: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        active = CfnParameter(
            self,
            "ActivateCanonicalWriter",
            type="String",
            default="false",
            allowed_values=["false", "true"],
            description="Enable only after writes are quiesced and canonical migration readiness passes",
        )
        removal = (
            __import__("aws_cdk").RemovalPolicy.DESTROY
            if environment == "dev"
            else __import__("aws_cdk").RemovalPolicy.RETAIN
        )
        self.ledger_table = dynamodb.Table(
            self,
            "LedgerTable",
            table_name=f"raad-{environment}-ledger",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=False,
            removal_policy=removal,
            deletion_protection=environment != "dev",
        )
        self.directory_table = dynamodb.Table(
            self,
            "WalletDirectoryTable",
            table_name=f"raad-{environment}-directory",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=False,
            removal_policy=removal,
            deletion_protection=environment != "dev",
        )
        self.transactions_table = dynamodb.Table(
            self,
            "TransactionsTable",
            table_name=f"raad-{environment}-transactions",
            partition_key=dynamodb.Attribute(name="transferId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=environment != "dev",
            removal_policy=removal,
            deletion_protection=environment != "dev",
        )
        self.log_group = logs.LogGroup(
            self,
            "MoneyApiLogs",
            log_group_name=f"/raad/{environment}/money-api",
            retention=logs.RetentionDays.TWO_WEEKS,
        )
        execution_role = iam.Role(
            self, "MoneyApiExecutionRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        )
        self.log_group.grant_write(execution_role)
        self.api = lambda_.Function(
            self,
            "MoneyApi",
            function_name=f"raad-{environment}-money-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="raad_money.api.handle",
            code=lambda_.Code.from_asset(
                "src",
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    platform="linux/amd64",
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                    ],
                ),
            ),
            timeout=Duration.seconds(7),
            reserved_concurrent_executions=20 if environment == "dev" else None,
            environment={
                "LEDGER_TABLE": self.ledger_table.table_name,
                "TRANSACTIONS_TABLE": self.transactions_table.table_name,
                "DIRECTORY_TABLE": self.directory_table.table_name,
                "TOKEN_VERIFIER_PARAMETER": verifier_parameter,
                "CORRELATION_SECRET_ARN": correlation_secret_arn,
                "ENVIRONMENT": environment,
            },
            log_group=self.log_group,
            role=execution_role,
        )
        # Tested supported AL2023 pin; do not jump major versions independently of CI.
        NagSuppressions.add_resource_suppressions(
            self.api,
            [
                {
                    "id": "AwsSolutions-L1",
                    "reason": "Approved tested Python3.12 AL2023 pin is AWS-supported until 2028-10-31 with managed patches. Dependency audit and package tests gate release; major migration requires synchronized local/CI pins.",
                }
            ],
        )
        # Ledger mutation uses transactions on the Ledger table. The directory role has separate access.
        self.api.add_environment("REQUIRE_FORWARDING_ENVELOPE", "true")
        self.api.add_environment("CANONICAL_WRITER_ACTIVE", active.value_as_string)
        self.ledger_table.grant_read_write_data(self.api)
        self.directory_table.grant_read_write_data(self.api)
        # Ordinary API code can conditionally insert/read canonical rows, never update/delete them.
        self.api.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:BatchGetItem"],
                resources=[self.transactions_table.table_arn],
            )
        )
        self.api.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=verifier_parameter.lstrip("/"),
                    )
                ],
            )
        )
        self.api.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"], resources=[correlation_secret_arn]
            )
        )
        self.function_url = self.api.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)
        lambda_.CfnPermission(
            self,
            "EdgeInvokeFunctionUrl",
            action="lambda:InvokeFunctionUrl",
            function_name=self.api.function_name,
            principal=edge_role_arn,
            function_url_auth_type="AWS_IAM",
        )
        lambda_.CfnPermission(
            self,
            "EdgeInvokeFunctionThroughUrl",
            action="lambda:InvokeFunction",
            function_name=self.api.function_name,
            principal=edge_role_arn,
            invoked_via_function_url=True,
        )
        ssm.StringParameter(
            self,
            "MoneyApiFunctionUrl",
            parameter_name=f"/raad/{environment}/money-api/function-url",
            string_value=self.function_url.url,
        )
