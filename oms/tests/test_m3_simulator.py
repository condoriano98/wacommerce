"""M3 deterministic simulator S1–S9 (ADR-017), risk alerts (#18), 13-week cash forecast, bulk-buy evaluator."""

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

import app.registry  # noqa: F401
from app.core.db import tenant_session
from app.core.outbox import dispatch_due
from app.models.fpa import SimulationRun
from app.models.governance import ExceptionCase, OutboxMessage
from app.models.org import RiskPolicy
from app.modules.fpa import cash, risk, simulator
from app.modules.fpa.simulator import Channel, PortfolioInputs, SimInputs
from tests.conftest import owner_session
from tests.test_accounting_services import _order, _ship_owned
from tests.world import D, build_world, principal


def tiny() -> SimInputs:
    """Hand-computable: 10 orders/week of Rp100,000, prepaid settles next week."""
    ch = {"prepaid": Channel(D("1"), 2, D("0.01"))}
    return SimInputs(opening_cash=D("1000000"), reserve=D("500000"), weeks=4, portfolios=(
        PortfolioInputs("X", D("200000"), D("20000"), D("100000"), D("0.5"), D("10000"), D("0"), D("0"),
                        D("50000"), ch),))


def test_hand_computed_base_case_and_determinism():
    out = simulator.run(tiny())
    # per week: revenue 1,000,000; fees 10,000; COGS 500,000; fulfilment 100,000; ads 200,000 → CM2 190,000
    p = out["portfolios"]["X"]
    assert p["cm2"] == "760000" and p["operating_profit"] == "560000"  # 4 weeks, opex 50,000/week
    # cash: weeks 0..3 pay 850,000 each; receipts of 990,000 arrive one week later (weeks 1..3 within horizon)
    assert out["consolidated"]["cash_path"] == ["150000", "290000", "430000", "570000"]
    assert out["consolidated"]["min_cash"] == "150000" and out["consolidated"]["reserve_breach_week"] == 0
    assert simulator.run(tiny()) == out  # deterministic


def test_each_stress_scenario_moves_the_expected_metric():
    base = simulator.run(simulator.rp100m_seed())
    res = simulator.stress(simulator.rp100m_seed())
    assert set(res) >= {"S1_cpa_+30", "S1_cpa_+60", "S2_cvr_-25", "S3_returns_x2", "S4_cod_delay_+7d",
                        "S4_cod_delay_+14d", "S5_supplier_+10", "S5_supplier_+20", "S6_sell_through_-40",
                        "S7_roas_-35", "S8_whatsapp_outage_7d", "S9_marketplace_delay_+14d_fee_+2pp",
                        "combined_downside"}
    cm = lambda r: Decimal(r["consolidated"]["cm2"])  # noqa: E731
    low = lambda r: Decimal(r["consolidated"]["min_cash"])  # noqa: E731
    b_cm, b_low = cm(base), low(base)
    for name in ("S1_cpa_+30", "S2_cvr_-25", "S3_returns_x2", "S5_supplier_+10", "S6_sell_through_-40",
                 "S7_roas_-35", "S8_whatsapp_outage_7d", "S9_marketplace_delay_+14d_fee_+2pp", "combined_downside"):
        assert cm(res[name]) < b_cm, name
    assert cm(res["S1_cpa_+60"]) < cm(res["S1_cpa_+30"]) and cm(res["S5_supplier_+20"]) < cm(res["S5_supplier_+10"])
    # Settlement delays change cash timing, not contribution.
    assert cm(res["S4_cod_delay_+14d"]) == b_cm and low(res["S4_cod_delay_+14d"]) < b_low
    assert low(res["S4_cod_delay_+14d"]) <= low(res["S4_cod_delay_+7d"])
    # S9 only touches the marketplace portfolio (C)
    s9 = res["S9_marketplace_delay_+14d_fee_+2pp"]["portfolios"]
    assert s9["A"] == base["portfolios"]["A"] and Decimal(s9["C"]["cm2"]) < Decimal(base["portfolios"]["C"]["cm2"])


def test_hand_computed_s1_and_s9():
    i = tiny()
    s1 = simulator.SCENARIOS["S1_cpa_+30"](i)
    # orders/week = 200,000 / 26,000; CM2/week = orders*(100,000-1,000-50,000-10,000) - 200,000
    orders = D("200000") / D("26000")
    expected = (orders * D("39000") - D("200000")) * 4
    assert simulator.run(s1)["portfolios"]["X"]["cm2"] == str(expected.quantize(D("1")))
    mp = replace(i, portfolios=(replace(i.portfolios[0], channels={"marketplace": Channel(D("1"), 14, D("0.08"))}),))
    s9 = simulator.run(simulator.SCENARIOS["S9_marketplace_delay_+14d_fee_+2pp"](mp))
    # per order: 100,000 − fee 10,000 (8% + 2pp) − COGS 50,000 − fulfilment 10,000 = 30,000
    assert s9["portfolios"]["X"]["cm2"] == str(((D("10") * D("30000")) - D("200000")) * 4)
    assert s9["consolidated"]["cash_path"][3] == str(D("1000000") - 4 * D("850000"))  # nothing settles in 4 weeks


def test_monte_carlo_is_flag_gated():
    with pytest.raises(RuntimeError):
        simulator.monte_carlo(simulator.rp100m_seed())


def test_base_case_reconciles_to_ledger_actuals_for_elapsed_weeks():
    actual = {"X": {0: {"net_sales": "123000", "cm2": "45000", "operating_profit": "40000", "cash_in": "0",
                        "cash_out": "0"}}}
    out = simulator.run(tiny(), actual)
    assert out["portfolios"]["X"]["weekly_operating_profit"][0] == "40000"
    assert out["portfolios"]["X"]["weekly_operating_profit"][1] == "140000"  # projected weeks unchanged


def test_18_stress_breaching_reserve_opens_case_and_notifies(tenant):
    owner = principal(tenant, "owner")
    def reserve_policy(s):  # seeded with the org defaults
        return s.scalars(select(RiskPolicy).where(RiskPolicy.organization_id == tenant.org_id,
                                                  RiskPolicy.key == "min_cash_reserve",
                                                  RiskPolicy.portfolio_id.is_(None))).one()

    with owner_session() as s:
        reserve_policy(s).threshold = D("90000000")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        res = risk.stress_and_alert(s, owner, simulator.rp100m_seed(), ["S4_cod_delay_+14d", "S1_cpa_+30"])
    assert all(r["breaches"] for r in res.values())
    with owner_session() as s:
        cases = s.scalars(select(ExceptionCase).where(ExceptionCase.organization_id == tenant.org_id,
                                                      ExceptionCase.type == "liquidity_breach")).all()
        assert len(cases) == 2 and all(c.exposure > 0 for c in cases)
        assert s.scalars(select(OutboxMessage).where(OutboxMessage.organization_id == tenant.org_id,
                                                     OutboxMessage.topic == "notify.risk_alert")).all()
        assert len(s.scalars(select(SimulationRun).where(SimulationRun.organization_id == tenant.org_id)).all()) == 2
    dispatch_due()
    # A comfortable reserve opens nothing.
    with owner_session() as s:
        reserve_policy(s).threshold = D("1")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        assert risk.stress_and_alert(s, owner, simulator.rp100m_seed(), ["S2_cvr_-25"])["S2_cvr_-25"]["breaches"] == []


def test_cash_forecast_from_ledger_receivables(tenant):
    w = build_world(tenant)
    oid = _order(tenant, w, [("A1", 1, "0")], method="cod")
    dispatch_due()
    _ship_owned(tenant, oid, cod_fee=D("4950"))
    owner = principal(tenant, "owner")
    with tenant_session(tenant.org_id, owner.user_id) as s:
        f = cash.forecast_13w(s, tenant.legal_entity_id)
    assert D(f["weeks"][2]["inflow"]) > 0  # COD receivable expected in ~2 weeks
    assert len(f["weeks"]) == 13


def test_bulk_buy_requires_all_rules():
    good = cash.BulkBuyInputs(weekly_demand=[20, 22, 19, 21], qty=60, dropship_unit_cost=D("100000"),
                              bulk_unit_cost=D("80000"), carrying_cost_per_unit_week=D("500"), max_holding_weeks=6,
                              supplier_reliability=D("0.98"), return_rate=D("0.03"), salvage_rate=D("0.5"),
                              post_purchase_min_cash=D("30000000"), reserve=D("15000000"), risk_limit=D("2000000"))
    assert cash.evaluate_bulk_buy(good)["recommend"] is True
    cheap_but_unstable = replace(good, weekly_demand=[2, 40, 5, 35])
    r = cash.evaluate_bulk_buy(cheap_but_unstable)
    assert r["recommend"] is False and any("unstable" in x for x in r["reasons"])
    illiquid = replace(good, post_purchase_min_cash=D("10000000"))
    assert any("reserve" in x for x in cash.evaluate_bulk_buy(illiquid)["reasons"])
