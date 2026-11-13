"""Money service data resources and IAM-authenticated service Function URL."""

from __future__ import annotations

from aws_cdk import BundlingOptions, Duration, Stack, aws_dynamodb as dynamodb, aws_iam as iam, aws_lambda as lambda_, aws_logs as logs, aws_ssm as ssm
from constructs import Construct


class MoneyServiceStack(Stack):
    """Owns MoneyApi and its two logically isolated tables."""

    def __init__(self, scope: Construct, construct_id: str, *, environment: str, verifier_parameter: str, correlation_secret_arn: str, edge_role_arn: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = __import__("aws_cdk").RemovalPolicy.DESTROY if environment == "dev" else __import__("aws_cdk").RemovalPolicy.RETAIN
        self.ledger_table = dynamodb.Table(
            self, "LedgerTable", table_name=f"raad-{environment}-ledger", partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING), sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING), billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, encryption=dynamodb.TableEncryption.AWS_MANAGED, point_in_time_recovery=False, removal_policy=removal, deletion_protection=environment != "dev",
        )
        self.directory_table = dynamodb.Table(
            self, "WalletDirectoryTable", table_name=f"raad-{environment}-directory", partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING), sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING), billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, encryption=dynamodb.TableEncryption.AWS_MANAGED, point_in_time_recovery=False, removal_policy=removal, deletion_protection=environment != "dev",
        )
        self.log_group = logs.LogGroup(self, "MoneyApiLogs", log_group_name=f"/raad/{environment}/money-api", retention=logs.RetentionDays.TWO_WEEKS)
        self.api = lambda_.Function(
            self, "MoneyApi", function_name=f"raad-{environment}-money-api", runtime=lambda_.Runtime.PYTHON_3_12, handler="raad_money.api.handle", code=lambda_.Code.from_asset("src", bundling=BundlingOptions(image=lambda_.Runtime.PYTHON_3_12.bundling_image, command=["bash", "-c", "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"])), timeout=Duration.seconds(7), reserved_concurrent_executions=20 if environment == "dev" else None, environment={"LEDGER_TABLE": self.ledger_table.table_name, "DIRECTORY_TABLE": self.directory_table.table_name, "TOKEN_VERIFIER_PARAMETER": verifier_parameter, "CORRELATION_SECRET_ARN": correlation_secret_arn, "ENVIRONMENT": environment}, log_group=self.log_group,
        )
        # Ledger mutation uses transactions on the Ledger table. The directory role has separate access.
        self.ledger_table.grant_read_write_data(self.api)
        self.directory_table.grant_read_write_data(self.api)
        self.api.add_to_role_policy(iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[self.format_arn(service="ssm", resource="parameter", resource_name=verifier_parameter.lstrip("/"))]))
        self.api.add_to_role_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=[correlation_secret_arn]))
        self.function_url = self.api.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)
        edge_principal = iam.ArnPrincipal(edge_role_arn)
        lambda_.CfnPermission(self, "EdgeInvokeFunctionUrl", action="lambda:InvokeFunctionUrl", function_name=self.api.function_name, principal=edge_role_arn, function_url_auth_type="AWS_IAM")
        lambda_.CfnPermission(self, "EdgeInvokeFunctionThroughUrl", action="lambda:InvokeFunction", function_name=self.api.function_name, principal=edge_role_arn, invoked_via_function_url=True)
        ssm.StringParameter(self, "MoneyApiFunctionUrl", parameter_name=f"/raad/{environment}/money-api/function-url", string_value=self.function_url.url)
