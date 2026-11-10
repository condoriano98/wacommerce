"""Sanity check of docs/fixtures/journals.yaml (spec §9, §27.3). This only proves the fixtures are internally
consistent; acceptance test #19 (tests/test_accounting_services.py) proves the OMS services *produce* them."""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

FIXTURES = yaml.safe_load((Path(__file__).parents[2] / "docs" / "fixtures" / "journals.yaml").read_text())["fixtures"]
CLEARING = {"1250", "2200", "1010", "1050", "1100", "1110", "1020", "1105", "1120"}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_journal_balances(name):
    for journal in FIXTURES[name]:
        debit = sum(Decimal(str(line[1])) for line in journal["lines"])
        credit = sum(Decimal(str(line[2])) for line in journal["lines"])
        assert debit == credit, f"{name}/{journal['event']}"


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_scenarios_clear_transit_and_settlement_accounts(name):
    net: dict[str, Decimal] = {}
    for journal in FIXTURES[name]:
        for account, debit, credit in journal["lines"]:
            net[str(account)] = net.get(str(account), Decimal(0)) + Decimal(str(debit)) - Decimal(str(credit))
    # 1105 is presented net of its allowance 1109 (contra-asset).
    net["1105"] = net.get("1105", Decimal(0)) + net.pop("1109", Decimal(0))
    open_clearing = {a: v for a, v in net.items() if a in CLEARING and v != 0}
    # Scenarios that intentionally stop before settlement keep a documented open balance.
    expected_open = {"F5_dropship_prepaid": {"1010"}, "F6_partial_fulfilment": {"1010"}}
    assert set(open_clearing) <= expected_open.get(name, set()), (name, open_clearing)


def test_receipts_and_attestations_never_touch_the_bank():
    for name, journals in FIXTURES.items():
        for j in journals:
            if j["event"] in ("receipt_reported", "finance_attested_with_bank_ref"):
                assert j["lines"] == [], name
