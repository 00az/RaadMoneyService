"""CDK entry point for RaadMoneyService: one stack per environment (dev, stg or prd)."""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from raad_money.infrastructure import ENVIRONMENTS, MoneyServiceStack

app = cdk.App()
environment = os.environ.get("RAAD_ENVIRONMENT", "dev")
if environment not in ENVIRONMENTS:
    raise ValueError("RAAD_ENVIRONMENT must be dev, stg or prd")
edge_role_arn = app.node.try_get_context("edgeRoleArn") or os.environ.get("RAAD_EDGE_ROLE_ARN", "")
correlation_secret_arn = os.environ.get("RAAD_CORRELATION_SECRET_ARN", "")
event_bus_arn = os.environ.get("RAAD_EVENT_BUS_ARN", "")
alarm_topic_arn = os.environ.get("RAAD_ALARM_TOPIC_ARN", "")
if not all((edge_role_arn, correlation_secret_arn, event_bus_arn, alarm_topic_arn)):
    raise ValueError(
        "RAAD_EDGE_ROLE_ARN, RAAD_CORRELATION_SECRET_ARN, RAAD_EVENT_BUS_ARN and "
        "RAAD_ALARM_TOPIC_ARN must come from the deployed Raad platform SSM parameters"
    )
# One-time upgrade of an existing Transactions table: deploy with -c historyIndexes=1, then 2.
history_indexes = int(app.node.try_get_context("historyIndexes") or 2)
MoneyServiceStack(
    app,
    f"RaadMoneyService-{environment}",
    environment=environment,
    verifier_parameter=f"/raad/{environment}/verifier",
    correlation_secret_arn=correlation_secret_arn,
    edge_role_arn=edge_role_arn,
    event_bus_arn=event_bus_arn,
    alarm_topic_arn=alarm_topic_arn,
    history_indexes=history_indexes,
)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
