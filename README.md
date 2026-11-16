# RaadMoneyService

RAAD's money core: accounts, wallets, wallet-to-wallet transfers in simulated Saudi Riyal (SAR), and transfer history. Python on AWS.

This is the only service that changes wallet balances. Planning and design records live in [00az/Raad](https://github.com/00az/Raad).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/), which builds the environment from the committed `uv.lock`.
- Python 3.12, pinned by `requires-python` in `pyproject.toml` for local work, CI and Lambda. `uv` uses a 3.12 interpreter even when your default Python is newer.
- Node.js (CI uses 22) for the AWS CDK CLI: `npm install --global aws-cdk`.
- Docker, for DynamoDB Local in the integration tests and for Lambda asset bundling in `cdk synth`.

## Setup

```sh
uv sync --locked --all-groups
```

## Test

Start DynamoDB Local, then run the full suite (unit and integration tests, with the 80% repository coverage floor):

```sh
docker compose up -d dynamodb-local
uv run pytest tests/unit tests/integration
```

The tests expect DynamoDB Local on `127.0.0.1:8000`; `DYNAMODB_ENDPOINT` may point at another loopback address. Do not add `--wait` to `docker compose up`: the compose healthcheck never reports this image as healthy, although the endpoint answers.

### Money-core coverage check

The money-movement core (`raad_money.ledger` and `raad_money.transfers`) must keep at least 95% line and 95% branch coverage, each module on its own:

```sh
uv run pytest tests/unit/test_ledger_rules.py tests/unit/test_u2_money_rules.py tests/unit/test_u2_money_core.py tests/unit/test_u2_transfer_service.py --cov-reset --cov=raad_money.ledger --cov=raad_money.transfers --cov-branch --cov-report=json:artifacts/coverage/money-core.json --cov-report=term-missing --cov-fail-under=95
uv run python scripts/check_ledger_coverage.py artifacts/coverage/money-core.json
```

`artifacts/coverage/money-core.json` is regenerated on every run and is not committed.

### Other checks

CI also runs Ruff (format and lint), strict mypy, `pip-audit`, `cdk synth` with the cdk-nag AwsSolutions rules, and gitleaks. `.github/workflows/ci.yml` holds the exact commands and the placeholder inputs `cdk synth` needs.

## Contracts

### Provided

| Contract | Canonical file | Contract ID | Repos that pin it |
|---|---|---|---|
| C1 — RAAD Money API v1 | `openapi/raad-money-v1.yaml` | `c1-money-api` | `RaadMobile`; `Raad`, for the `Error` shape of edge responses |

The last column is the register of consumers. A new consumer is added to it by pull request.

### Consumed

| Contract | Owning repo | Contract ID | Pin |
|---|---|---|---|
| C2 — UserConfirmed event v1 | `Raad` | `c2-identity-events` | Pinned in [`contracts/pins.yaml`](contracts/pins.yaml), copy under `contracts/pinned/`. `tests/unit/test_c2_contract.py` fails if the copy's SHA-256 differs from its pin or if the consumer or its EventBridge rule disagrees with it. |
| C5 — Public API edge and token-verifier contract v1 | `Raad` | `c5-money-edge` | Not pinned yet. |

Change a pin only through a pull request.

## Deployment

A merge to `main` deploys to `dev` through `.github/workflows/deploy-dev.yml`. The `stg` and `prd` pipelines are not built yet.
