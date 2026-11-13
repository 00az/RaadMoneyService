"""CDK unit tests synthesize Lambda assets without Docker."""

import pytest
from aws_cdk import aws_lambda as lambda_


@pytest.fixture(autouse=True)
def use_inline_lambda_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the test asset source; production bundling remains unchanged."""
    monkeypatch.setattr(
        lambda_.Code,
        "from_asset",
        lambda *_args, **_kwargs: lambda_.Code.from_inline("def handle(event, context): return {}"),
    )
