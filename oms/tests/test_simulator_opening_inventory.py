"""Round 2 / P1-5 — opening inventory, new purchases, supplier terms and cash are economically consistent.

Hand-computable portfolio: 10 owned orders/week of Rp100,000 at 50% cost → Rp500,000 of stock needed per week;
ads Rp200,000/week; no fees, fulfilment, returns, opex or carrying cost; customer cash arrives one week later.
"""

from dataclasses import replace
from decimal import Decimal as D

from app.modules.fpa import simulator
from app.modules.fpa.simulator import Channel, PortfolioInputs, SimInputs

ADS = D("200000")


def _inputs(weeks=3, **kw) -> SimInputs:
    ch = {"prepaid": Channel(D("1"), 2, D("0"))}
    kw = {"replenish": False, "carrying_cost_rate_weekly": D("0"), **kw}
    p = PortfolioInputs("X", ADS, D("20000"), D("100000"), D("0.5"), D("0"), D("0"), D("0"), D("0"), ch,
                        owned_share=D("1"), **kw)
    return SimInputs(opening_cash=D("5000000"), reserve=D("0"), weeks=weeks, portfolios=(p,))


def _run(**kw):
    out = simulator.run(_inputs(**kw))
    return out["portfolios"]["X"], [D(c) for c in out["consolidated"]["cash_path"]], out


def test_stock_already_owned_is_not_paid_again():
    x, path, _ = _run(opening_inventory=D("1000000"))
    assert path[0] == D("5000000") - ADS  # only the ad spend leaves in week 0
    assert x["inventory_closing"][:2] == ["500000", "0"]


def test_opening_stock_plus_a_new_week_zero_purchase_are_both_available_and_only_the_purchase_is_paid():
    x, path, _ = _run(opening_inventory=D("500000"), inventory_buy=D("300000"))
    assert x["inventory_closing"] == ["300000", "0", "0"]  # 800,000 available in week 0
    assert path[0] == D("5000000") - ADS - D("300000")
    assert x["stockout_weeks"] == [1, 2]


def test_supplier_credit_stock_arrives_before_it_is_paid():
    x, path, _ = _run(weeks=4, inventory_buy=D("500000"), supplier_payment_lag_weeks=2)
    assert x["inventory_closing"][0] == "0"  # sold in week 0
    assert path[0] == D("5000000") - ADS  # nothing paid yet
    assert x["supplier_payables"][:3] == ["500000", "500000", "0"]  # owed until paid in week 2
    assert path[2] == path[1] - ADS - D("500000")


def test_initial_purchase_with_lead_time_is_a_commitment_until_it_arrives():
    x, path, _ = _run(weeks=3, inventory_buy=D("500000"), initial_lead_time_weeks=1)
    assert x["open_purchase_commitments"][:2] == ["500000", "0"]
    assert x["stockout_weeks"] == [0, 2]  # nothing to sell in week 0; week 1 sells the arrival
    assert path[0] == D("5000000") - ADS - D("500000")  # paid at order (lag 0)


def test_replenishment_payment_and_arrival_follow_the_configured_timing():
    x, path, _ = _run(weeks=6, opening_inventory=D("500000"), replenish=True, lead_time_weeks=2,
                      safety_stock_weeks=D("1"), reorder_cover_weeks=D("4"), supplier_payment_lag_weeks=1)
    assert x["inventory_purchases"][0] == {"week": 0, "cost": "2000000"}
    assert x["open_purchase_commitments"][0] == "2000000" and x["open_purchase_commitments"][2] == "0"
    assert path[1] == path[0] + D("1000000") - ADS - D("2000000")  # paid one week after ordering


def test_cash_path_reconciles_to_opening_cash_plus_weekly_movements():
    _, path, out = _run(weeks=5, opening_inventory=D("500000"), inventory_buy=D("300000"), replenish=True,
                        supplier_payment_lag_weeks=1)
    c = out["consolidated"]
    assert D(c["opening_cash"]) + D(c["net_cash_flow"]) == path[-1]
    assert out == simulator.run(_inputs(weeks=5, opening_inventory=D("500000"), inventory_buy=D("300000"),
                                        replenish=True, supplier_payment_lag_weeks=1))  # deterministic


def test_seed_semantics_are_unchanged_for_portfolios_without_opening_stock():
    seed = simulator.rp100m_seed()
    a = simulator.run(seed)["portfolios"]["A"]
    explicit = replace(seed, portfolios=(replace(seed.portfolios[0], opening_inventory=D("0")), *seed.portfolios[1:]))
    assert simulator.run(explicit)["portfolios"]["A"]["inventory_closing"] == a["inventory_closing"]
