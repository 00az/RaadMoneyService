"""Money service stack: three isolated tables, MoneyApi behind an IAM Function URL, the C2
consumer with separate delivery and processing dead-letter queues, a service-owned cursor
secret, and service alarms routed to the platform alarm topic. One stack per environment.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import BundlingOptions, CfnParameter, Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from cdk_nag import NagSuppressions
from constructs import Construct

from raad_money.consumer import DELIVERY_WINDOW, EVENT_DETAIL_TYPE, EVENT_SOURCE
from raad_money.history import RECIPIENT_INDEX, SENDER_INDEX

ENVIRONMENTS = ("dev", "stg", "prd")
_RUNTIME_SUPPRESSION = {
    "id": "AwsSolutions-L1",
    "reason": "Approved tested Python3.12 AL2023 pin is AWS-supported until 2028-10-31 with "
    "managed patches. Dependency audit and package tests gate release; major migration "
    "requires synchronized local/CI pins.",
}


class MoneyServiceStack(Stack):
    """Owns MoneyApi, the C2 consumer and their three logically isolated tables."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        verifier_parameter: str,
        correlation_secret_arn: str,
        edge_role_arn: str,
        event_bus_arn: str,
        alarm_topic_arn: str,
        history_indexes: int = 2,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if environment not in ENVIRONMENTS:
            raise ValueError("environment must be dev, stg or prd")
        if history_indexes not in (1, 2):
            raise ValueError("history_indexes stages one GSI per deployment: use 1 or 2")
        self.raad_environment = environment
        protected = environment != "dev"
        active = CfnParameter(
            self,
            "ActivateCanonicalWriter",
            type="String",
            default="false",
            allowed_values=["false", "true"],
            description="Enable only after writes are quiesced and canonical migration readiness passes",
        )
        self._tables(protected, history_indexes)
        retention = logs.RetentionDays.ONE_MONTH if protected else logs.RetentionDays.TWO_WEEKS
        self.cursor_secret = secretsmanager.Secret(
            self,
            "HistoryCursorSecret",
            secret_name=f"raad/{environment}/money-history-cursor",
            description="Signs history cursors only; rotate with a previous key kept >=24h",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"version":"1"}',
                generate_string_key="key",
                exclude_punctuation=True,
                password_length=64,
            ),
            removal_policy=cdk.RemovalPolicy.RETAIN if protected else cdk.RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            self.cursor_secret,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": "Cursor keys rotate by a reviewed change that moves the current key "
                    "to previousKey for at least 24 hours so live cursor chains stay valid; "
                    "automatic rotation would cut chains early.",
                }
            ],
        )
        self.alarm_topic = sns.Topic.from_topic_arn(self, "PlatformAlarmTopic", alarm_topic_arn)
        self._money_api(
            environment,
            verifier_parameter,
            correlation_secret_arn,
            edge_role_arn,
            active,
            retention,
        )
        self._consumer(environment, event_bus_arn, retention)
        self._alarms_and_dashboard(environment)

    def _tables(self, protected: bool, history_indexes: int) -> None:
        removal = cdk.RemovalPolicy.RETAIN if protected else cdk.RemovalPolicy.DESTROY
        recovery = dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=protected
        )
        common: dict[str, Any] = {
            "billing_mode": dynamodb.BillingMode.PAY_PER_REQUEST,
            "encryption": dynamodb.TableEncryption.AWS_MANAGED,
            "point_in_time_recovery_specification": recovery,
            "removal_policy": removal,
            "deletion_protection": protected,
        }
        pk_sk: dict[str, Any] = {
            "partition_key": dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            "sort_key": dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
        }
        name = self.raad_environment
        self.ledger_table = dynamodb.Table(
            self, "LedgerTable", table_name=f"raad-{name}-ledger", **pk_sk, **common
        )
        self.directory_table = dynamodb.Table(
            self, "WalletDirectoryTable", table_name=f"raad-{name}-directory", **pk_sk, **common
        )
        self.transactions_table = dynamodb.Table(
            self,
            "TransactionsTable",
            table_name=f"raad-{name}-transactions",
            partition_key=dynamodb.Attribute(name="transferId", type=dynamodb.AttributeType.STRING),
            **common,
        )
        # CloudFormation adds at most one GSI per table update. An existing table (dev after U1)
        # is upgraded with history_indexes=1 first, then 2; a new table gets both at once.
        indexes = [(SENDER_INDEX, "senderWalletId"), (RECIPIENT_INDEX, "recipientWalletId")]
        for index_name, partition in indexes[:history_indexes]:
            self.transactions_table.add_global_secondary_index(
                index_name=index_name,
                partition_key=dynamodb.Attribute(
                    name=partition, type=dynamodb.AttributeType.STRING
                ),
                sort_key=dynamodb.Attribute(
                    name="completedSortKey", type=dynamodb.AttributeType.STRING
                ),
                projection_type=dynamodb.ProjectionType.KEYS_ONLY,
            )
        if not protected:
            for table in (self.ledger_table, self.directory_table, self.transactions_table):
                NagSuppressions.add_resource_suppressions(
                    table,
                    [
                        {
                            "id": "AwsSolutions-DDB3",
                            "reason": "dev holds only simulated test data and is reset only by "
                            "explicit authorization; stg and prd enable PITR and retention.",
                        }
                    ],
                )

    def _code(self) -> lambda_.Code:
        return lambda_.Code.from_asset(
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
        )

    def _money_api(
        self,
        environment: str,
        verifier_parameter: str,
        correlation_secret_arn: str,
        edge_role_arn: str,
        active: CfnParameter,
        retention: logs.RetentionDays,
    ) -> None:
        self.log_group = logs.LogGroup(
            self,
            "MoneyApiLogs",
            log_group_name=f"/raad/{environment}/money-api",
            retention=retention,
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
            code=self._code(),
            timeout=Duration.seconds(7),
            memory_size=128,
            reserved_concurrent_executions=20,
            environment={
                "LEDGER_TABLE": self.ledger_table.table_name,
                "TRANSACTIONS_TABLE": self.transactions_table.table_name,
                "DIRECTORY_TABLE": self.directory_table.table_name,
                "TOKEN_VERIFIER_PARAMETER": verifier_parameter,
                "CORRELATION_SECRET_ARN": correlation_secret_arn,
                "CURSOR_SECRET_ARN": self.cursor_secret.secret_arn,
                "ENVIRONMENT": environment,
            },
            log_group=self.log_group,
            role=execution_role,
        )
        NagSuppressions.add_resource_suppressions(self.api, [_RUNTIME_SUPPRESSION])
        self.api.add_environment("REQUIRE_FORWARDING_ENVELOPE", "true")
        self.api.add_environment("CANONICAL_WRITER_ACTIVE", active.value_as_string)
        # Least privilege: transactions need Put/Update/ConditionCheck; no Scan or Delete.
        writer = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        self._allow(
            execution_role,
            [*writer, "dynamodb:ConditionCheckItem", "dynamodb:BatchGetItem"],
            [self.ledger_table.table_arn],
        )
        self._allow(
            execution_role,
            [*writer, "dynamodb:ConditionCheckItem", "dynamodb:BatchGetItem"],
            [self.directory_table.table_arn],
        )
        # Canonical rows are conditionally inserted and read; never updated or deleted.
        self._allow(
            execution_role,
            ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:BatchGetItem"],
            [self.transactions_table.table_arn],
        )
        self._allow(
            execution_role,
            ["dynamodb:Query"],
            [
                f"{self.transactions_table.table_arn}/index/{SENDER_INDEX}",
                f"{self.transactions_table.table_arn}/index/{RECIPIENT_INDEX}",
            ],
        )
        self._allow(
            execution_role,
            ["ssm:GetParameter"],
            [
                self.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=verifier_parameter.lstrip("/"),
                )
            ],
        )
        self._allow(execution_role, ["secretsmanager:GetSecretValue"], [correlation_secret_arn])
        self.cursor_secret.grant_read(execution_role)
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

    def _consumer(
        self, environment: str, event_bus_arn: str, retention: logs.RetentionDays
    ) -> None:
        dlq_settings: dict[str, Any] = {
            "encryption": sqs.QueueEncryption.SQS_MANAGED,
            "enforce_ssl": True,
            "retention_period": Duration.days(14),
        }
        self.processing_dlq = sqs.Queue(
            self,
            "UserConfirmedProcessingDlq",
            queue_name=f"raad-{environment}-user-confirmed-processing-dlq",
            **dlq_settings,
        )
        self.delivery_dlq = sqs.Queue(
            self,
            "UserConfirmedDeliveryDlq",
            queue_name=f"raad-{environment}-user-confirmed-delivery-dlq",
            **dlq_settings,
        )
        self.work_queue = sqs.Queue(
            self,
            "UserConfirmedWorkQueue",
            queue_name=f"raad-{environment}-user-confirmed",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(4),
            visibility_timeout=Duration.seconds(90),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=1000, queue=self.processing_dlq
            ),
        )
        for queue, reason in (
            (self.processing_dlq, "This queue is itself the processing dead-letter queue."),
            (self.delivery_dlq, "This queue is itself the EventBridge delivery dead-letter queue."),
        ):
            NagSuppressions.add_resource_suppressions(
                queue, [{"id": "AwsSolutions-SQS3", "reason": reason}]
            )
        bus = events.EventBus.from_event_bus_arn(self, "PlatformEventBus", event_bus_arn)
        self.user_confirmed_rule = events.Rule(
            self,
            "UserConfirmedRule",
            event_bus=bus,
            event_pattern=events.EventPattern(
                source=[EVENT_SOURCE],
                detail_type=[EVENT_DETAIL_TYPE],
                detail={"environment": [environment]},
            ),
        )
        self.user_confirmed_rule.add_target(
            targets.SqsQueue(
                self.work_queue,
                dead_letter_queue=self.delivery_dlq,
                max_event_age=Duration.seconds(int(DELIVERY_WINDOW.total_seconds())),
                retry_attempts=185,
            )
        )
        consumer_logs = logs.LogGroup(
            self,
            "UserConfirmedConsumerLogs",
            log_group_name=f"/raad/{environment}/user-confirmed-consumer",
            retention=retention,
        )
        role = iam.Role(
            self,
            "UserConfirmedConsumerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        consumer_logs.grant_write(role)
        writer = [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:ConditionCheckItem",
        ]
        self._allow(role, writer, [self.ledger_table.table_arn, self.directory_table.table_arn])
        self.processing_dlq.grant_send_messages(role)
        self.consumer = lambda_.Function(
            self,
            "UserConfirmedConsumer",
            function_name=f"raad-{environment}-user-confirmed-consumer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="raad_money.consumer.handle",
            code=self._code(),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "LEDGER_TABLE": self.ledger_table.table_name,
                "TRANSACTIONS_TABLE": self.transactions_table.table_name,
                "DIRECTORY_TABLE": self.directory_table.table_name,
                "PROCESSING_DLQ_URL": self.processing_dlq.queue_url,
                "ENVIRONMENT": environment,
            },
            log_group=consumer_logs,
            role=role,
        )
        NagSuppressions.add_resource_suppressions(self.consumer, [_RUNTIME_SUPPRESSION])
        self.consumer.add_event_source(
            event_sources.SqsEventSource(
                self.work_queue, batch_size=1, max_concurrency=2, report_batch_item_failures=True
            )
        )

    def _alarms_and_dashboard(self, environment: str) -> None:
        action = cloudwatch_actions.SnsAction(self.alarm_topic)
        five = Duration.minutes(5)

        def api_metric(name: str, period: Duration = five) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="Raad/MoneyApi",
                metric_name=name,
                dimensions_map={"Environment": environment},
                statistic="Sum",
                period=period,
            )

        error_rate = cloudwatch.MathExpression(
            # Fewer than 20 requests yields no datapoint, so low volume is never "healthy".
            expression="IF(requests >= 20, 100 * FILL(failures, 0) / requests)",
            using_metrics={
                "requests": api_metric("MoneyApiRequests"),
                "failures": api_metric("MoneyApiTechnicalFailures"),
            },
            period=five,
            label="Technical failure rate %",
        )
        alarms = [
            cloudwatch.Alarm(
                self,
                "TechnicalErrorRateAlarm",
                alarm_name=f"raad-{environment}-money-technical-error-rate",
                metric=error_rate,
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.MISSING,
            ),
            cloudwatch.Alarm(
                self,
                "TechnicalTransferFailuresAlarm",
                alarm_name=f"raad-{environment}-money-technical-transfer-failures",
                metric=api_metric("MoneyApiTechnicalTransferFailures"),
                threshold=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
        ]
        one = Duration.minutes(1)
        for label, function in (("Api", self.api), ("Consumer", self.consumer)):
            alarms.append(
                cloudwatch.Alarm(
                    self,
                    f"{label}ThrottleAlarm",
                    alarm_name=f"raad-{environment}-money-{label.lower()}-throttles",
                    metric=function.metric_throttles(period=one, statistic="Sum"),
                    threshold=1,
                    comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    evaluation_periods=2,
                    datapoints_to_alarm=2,
                    treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                )
            )
        for label, table in (
            ("Ledger", self.ledger_table),
            ("Directory", self.directory_table),
            ("Transactions", self.transactions_table),
        ):
            throttles = cloudwatch.MathExpression(
                expression="FILL(reads, 0) + FILL(writes, 0)",
                using_metrics={
                    "reads": table.metric("ReadThrottleEvents", period=one, statistic="Sum"),
                    "writes": table.metric("WriteThrottleEvents", period=one, statistic="Sum"),
                },
                period=one,
                label=f"{label} throttle events",
            )
            alarms.append(
                cloudwatch.Alarm(
                    self,
                    f"{label}ThrottleAlarm",
                    alarm_name=f"raad-{environment}-money-{label.lower()}-throttles",
                    metric=throttles,
                    threshold=1,
                    comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    evaluation_periods=2,
                    datapoints_to_alarm=2,
                    treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                )
            )
        for label, queue in (("Delivery", self.delivery_dlq), ("Processing", self.processing_dlq)):
            alarms.append(
                cloudwatch.Alarm(
                    self,
                    f"{label}DlqAlarm",
                    alarm_name=f"raad-{environment}-user-confirmed-{label.lower()}-dlq",
                    metric=queue.metric_approximate_number_of_messages_visible(
                        period=one, statistic="Maximum"
                    ),
                    threshold=1,
                    comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                    evaluation_periods=1,
                    treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                )
            )
        for alarm in alarms:
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)
        self.alarms = alarms
        dashboard = cloudwatch.Dashboard(
            self, "MoneyServiceDashboard", dashboard_name=f"raad-{environment}-money-service"
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Requests and technical failures (5 min)",
                left=[api_metric("MoneyApiRequests"), api_metric("MoneyApiTechnicalFailures")],
                right=[error_rate],
            ),
            cloudwatch.GraphWidget(
                title="Transfers (5 min)",
                left=[
                    api_metric("MoneyApiTransferSuccesses"),
                    api_metric("MoneyApiTechnicalTransferFailures"),
                ],
            ),
            cloudwatch.GraphWidget(
                title="Handler duration (diagnostic only; public latency is U4's)",
                left=[
                    cloudwatch.Metric(
                        namespace="Raad/MoneyApi",
                        metric_name="MoneyApiDurationMs",
                        dimensions_map={"Environment": environment},
                        statistic=stat,
                        period=five,
                    )
                    for stat in ("p50", "p95")
                ],
            ),
            cloudwatch.GraphWidget(
                title="UserConfirmed queues",
                left=[
                    self.work_queue.metric_approximate_age_of_oldest_message(),
                    self.delivery_dlq.metric_approximate_number_of_messages_visible(),
                    self.processing_dlq.metric_approximate_number_of_messages_visible(),
                ],
                right=[self.consumer.metric_errors()],
            ),
            cloudwatch.AlarmStatusWidget(title="Service alarms", alarms=alarms),
        )

    def _allow(self, role: iam.Role, actions: list[str], resources: list[str]) -> None:
        role.add_to_policy(iam.PolicyStatement(actions=actions, resources=resources))
