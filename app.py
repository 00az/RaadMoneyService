"""CDK entry point for RaadMoneyService."""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from raad_money.infrastructure import MoneyServiceStack

app = cdk.App()
environment = os.environ.get("RAAD_ENVIRONMENT", "dev")
edge_role_arn = app.node.try_get_context("edgeRoleArn") or os.environ.get("RAAD_EDGE_ROLE_ARN", "")
correlation_secret_arn = os.environ.get("RAAD_CORRELATION_SECRET_ARN", "")
if not edge_role_arn or not correlation_secret_arn:
    raise ValueError("RAAD_EDGE_ROLE_ARN and RAAD_CORRELATION_SECRET_ARN must come from the deployed Raad platform SSM parameters")
MoneyServiceStack(
    app,
    f"RaadMoneyService-{environment}",
    environment=environment,
    verifier_parameter=f"/raad/{environment}/verifier",
    correlation_secret_arn=correlation_secret_arn,
    edge_role_arn=edge_role_arn,
)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
