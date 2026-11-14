"""Migration refuses conflicts; coverage floors are independent."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from raad_money.ledger import CompletedTransfer


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[2] / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load("migrate_transactions")
coverage = load("check_ledger_coverage")
ID = "2bc440be-a85a-4cf8-9bf1-128fc7214c60"
STAMP = "2026-11-14T00:00:00.000Z"


def pair():
    payload = {
        "transfer_id": ID,
        "reference": "TXN-ONE",
        "sender_wallet_id": "s",
        "recipient_wallet_id": "r",
        "amount_halalas": 10,
        "completed_at": STAMP,
    }
    return [
        {"pk": "WALLET#" + wallet, "sk": f"TRANSFER#{STAMP}#{ID}", **payload}
        for wallet in ("s", "r")
    ]


def transactions():
    table = Mock()
    table.name = "transactions"
    table.get_item.return_value = {}
    return table


def test_matched_pair_creates_one_canonical_plan():
    plans = migration.plan(pair(), transactions())
    assert len(plans) == 1
    assert plans[0][0] == CompletedTransfer(ID, "TXN-ONE", "s", "r", 10, STAMP).as_item()


@pytest.mark.parametrize("change", ["unmatched", "amount", "key", "fractional"])
def test_invalid_pairs_stop_before_writes(change):
    rows = pair()
    if change == "unmatched":
        rows.pop()
    elif change == "amount":
        rows[1]["amount_halalas"] = 11
    elif change == "key":
        rows[1]["pk"] = "WALLET#other"
    else:
        rows[0]["amount_halalas"] = 1.5
    with pytest.raises(ValueError):
        migration.plan(rows, transactions())


def test_existing_canonical_conflict_is_not_overwritten():
    table = transactions()
    table.get_item.return_value = {
        "Item": {**CompletedTransfer(ID, "OTHER", "s", "r", 10, STAMP).as_item()}
    }
    with pytest.raises(migration.MigrationConflict):
        migration.plan(pair(), table)
    table.meta.client.transact_write_items.assert_not_called()


def test_reference_only_rerun_resolves_same_payload():
    table = transactions()
    table.get_item.return_value = {
        "Item": CompletedTransfer(ID, "TXN-ONE", "s", "r", 10, STAMP).as_item()
    }
    refs = [{"pk": row["pk"], "sk": row["sk"], "transferId": ID} for row in pair()]
    assert len(migration.plan(refs, table)) == 1


def test_dry_run_never_changes_data(monkeypatch):
    table = transactions()
    ledger = Mock()
    monkeypatch.setattr(migration, "scan", lambda _: pair())
    assert migration.migrate(ledger, table)["applied"] == 0
    ledger.meta.client.transact_write_items.assert_not_called()
    with pytest.raises(migration.MigrationConflict):
        migration.migrate(ledger, table, apply=True)


def test_conditional_put_guards_every_previous_field():
    put = migration.conditional_put("ledger", {"transferId": ID}, pair()[0])["Put"]
    assert len(put["ExpressionAttributeNames"]) == len(pair()[0])
    assert "ConditionExpression" in put


def test_coverage_checker_rejects_independent_branch_gap(tmp_path):
    import json

    path = tmp_path / "coverage.json"
    summary = {
        "num_statements": 100,
        "covered_lines": 100,
        "num_branches": 100,
        "covered_branches": 94,
    }
    path.write_text(json.dumps({"files": {"src/raad_money/ledger.py": {"summary": summary}}}))
    with pytest.raises(ValueError):
        coverage.check(str(path))
    summary["covered_branches"] = 95
    path.write_text(json.dumps({"files": {"src/raad_money/ledger.py": {"summary": summary}}}))
    assert coverage.check(str(path)) == (100, 95)


def test_pipeline_readiness_fails_closed_on_legacy_payloads(monkeypatch):
    import sys

    ledger, table = Mock(), transactions()
    monkeypatch.setattr(
        migration.boto3,
        "resource",
        lambda *args, **kwargs: Mock(Table=Mock(side_effect=[ledger, table])),
    )
    monkeypatch.setattr(migration, "scan", lambda _: pair())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate",
            "--environment",
            "dev",
            "--ledger-table",
            "ledger",
            "--transactions-table",
            "transactions",
            "--check-ready",
        ],
    )
    assert migration.main() == 1
    ledger.meta.client.transact_write_items.assert_not_called()


def test_fixture_setup_creates_six_distinct_new_users_without_logging_credentials(
    monkeypatch, capsys
):
    import json

    setup = load("setup_dev_users")
    secrets, cognito = Mock(), Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "users": [
                    {"email": "sender@example.test", "password": "do-not-log-password"},
                    {"email": "receiver@example.test", "password": "do-not-log-password"},
                ]
            }
        )
    }
    cognito.admin_initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": "do-not-log-token"}
    }
    monkeypatch.setattr(
        setup.boto3,
        "client",
        lambda name, **kwargs: secrets if name == "secretsmanager" else cognito,
    )
    monkeypatch.setattr(
        setup.requests,
        "put",
        lambda *args, **kwargs: Mock(
            status_code=201, json=lambda: {"balance": {"amountHalalas": 100_000}}
        ),
    )
    for key, value in {
        "TEST_USERS_SECRET_ARN": "test-secret",
        "USER_POOL_ID": "pool",
        "USER_POOL_CLIENT_ID": "client",
        "MONEY_API_BASE_URL": "https://api.example.test",
    }.items():
        monkeypatch.setenv(key, value)
    setup.main()
    created = [call.kwargs["Username"] for call in cognito.admin_create_user.call_args_list]
    assert len(set(created)) == 6
    stored = json.loads(secrets.put_secret_value.call_args.kwargs["SecretString"])
    assert set(stored["journeys"]) == {"api", "ios", "android"}
    output = capsys.readouterr().out
    assert "password" not in output and "token" not in output and "@" not in output


def test_fixture_setup_never_resets_existing_user_after_collision(monkeypatch):
    import json

    setup = load("setup_dev_users")
    secrets, cognito = Mock(), Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "users": [
                    {"email": "sender@example.test", "password": "secret"},
                    {"email": "receiver@example.test", "password": "secret"},
                ]
            }
        )
    }
    cognito.admin_create_user.side_effect = RuntimeError("existing test identity")
    monkeypatch.setattr(
        setup.boto3,
        "client",
        lambda name, **kwargs: secrets if name == "secretsmanager" else cognito,
    )
    for key, value in {
        "TEST_USERS_SECRET_ARN": "test-secret",
        "USER_POOL_ID": "pool",
        "USER_POOL_CLIENT_ID": "client",
        "MONEY_API_BASE_URL": "https://api.example.test",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        setup.main()
    cognito.admin_set_user_password.assert_not_called()
    secrets.put_secret_value.assert_not_called()
