"""
Restock recommendation engine (Task 2) - v2.

Pure Python / pandas, no extra libraries, so the same code runs locally
and inside a Foundry Python transform.

Three layers
------------
1. DEMAND RISK (statistical)
   Sales are sparse (about 1 unit per product per month), so demand is
   modelled as a Poisson process - the standard model for slow-moving items.
   The reorder point is the smallest stock level that covers demand during
   the delivery lead time with the chosen service level (default 95%).

2. BUSINESS POLICY
   A configurable minimum stock level (safety floor). Kept separate from the
   statistical layer so every recommendation states which rule triggered it.

3. FULFILMENT OPTIMISATION
   Before buying, check whether another warehouse holds surplus of the same
   product. Shortfalls are filled greedily from the warehouse with the
   largest surplus (priority queue / max-heap); anything left is purchased.
   Moving existing stock is cheaper and faster than buying new stock.
"""

from transforms.api import transform_pandas, Input, Output
import heapq
import math

import pandas as pd

# ---------------------------------------------------------------------------
# Business parameters - the only values a planner needs to change.
# ---------------------------------------------------------------------------
PARAMS = {
    "demand_window_days": 30,  # "last 30 days of sales data"
    "service_level": 0.95,     # probability of not running out before delivery
    "coverage_days": 30,       # days of demand a restock should cover
    "min_stock_level": 20,     # policy floor per product-warehouse
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def poisson_quantile(mean: float, service_level: float) -> int:
    """Smallest k with P(Poisson(mean) <= k) >= service_level."""
    if mean <= 0:
        return 0
    k, p = 0, math.exp(-mean)
    cdf = p
    while cdf < service_level:
        k += 1
        p *= mean / k
        cdf += p
    return k


def weighted_lead_time(carriers: pd.DataFrame) -> float:
    """Expected delivery time, weighted by each carrier's share of shipments."""
    w = carriers["total_shipments"]
    return float((carriers["expected_lead_time_days"] * w).sum() / w.sum())


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def build_recommendations(orders: pd.DataFrame,
                          inventory: pd.DataFrame,
                          carriers: pd.DataFrame,
                          params: dict = PARAMS) -> pd.DataFrame:
    p = params
    orders = orders.copy()
    orders["order_date"] = pd.to_datetime(orders["order_date"])

    # -- Demand: last 30 days, non-canceled, 1 order = 1 unit --------------
    reference_date = orders["order_date"].max().normalize()
    start = reference_date - pd.Timedelta(days=p["demand_window_days"] - 1)
    recent = orders[(orders["order_date"] >= start)
                    & (orders["order_status"] != "Canceled")]
    demand = recent.groupby("product_id").size().rename("units_sold_30d")

    df = inventory.merge(demand, on="product_id", how="left")
    df["units_sold_30d"] = df["units_sold_30d"].fillna(0).astype(int)
    n_wh = df.groupby("product_id")["warehouse_id"].transform("nunique")
    df["daily_demand"] = df["units_sold_30d"] / p["demand_window_days"] / n_wh

    # -- Lead time from carrier performance -------------------------------
    lead_time = weighted_lead_time(carriers)
    df["lead_time_days"] = round(lead_time, 2)

    # -- Layer 1: statistical reorder point and order-up-to level ----------
    sl = p["service_level"]
    df["stat_reorder_point"] = [
        poisson_quantile(d * lead_time, sl) for d in df["daily_demand"]]
    df["stat_order_up_to"] = [
        poisson_quantile(d * (lead_time + p["coverage_days"]), sl)
        for d in df["daily_demand"]]

    # -- Layer 2: policy floor --------------------------------------------
    floor = p["min_stock_level"]
    df["reorder_point"] = df["stat_reorder_point"].clip(lower=floor)
    df["target_stock"] = df[["stat_order_up_to", "reorder_point"]].max(axis=1)

    # -- Trigger and explanation ------------------------------------------
    def trigger(r):
        if r.stock_quantity == 0:
            return "STOCKOUT"
        if r.stock_quantity <= r.stat_reorder_point:
            return "DEMAND_RISK"
        if r.stock_quantity <= floor:
            return "POLICY_FLOOR"
        return None

    df["trigger_type"] = df.apply(trigger, axis=1)
    df["days_of_cover"] = [
        round(s / d, 0) if d > 0 else None
        for s, d in zip(df["stock_quantity"], df["daily_demand"])]

    # Surplus each location can give away without breaching its own target.
    df["surplus"] = (df["stock_quantity"] - df["target_stock"]).clip(lower=0)

    needs = df[df["trigger_type"].notna()].copy()
    needs["recommended_restock_quantity"] = (
        needs["target_stock"] - needs["stock_quantity"]).apply(math.ceil)

    # -- Layer 3: fulfilment optimisation (greedy, max-heap per product) ---
    rank = {"STOCKOUT": 0, "DEMAND_RISK": 1, "POLICY_FLOOR": 2}
    needs["_rank"] = needs["trigger_type"].map(rank)
    needs = needs.sort_values(["_rank", "stock_quantity"])

    donors = {}
    for r in df[df["trigger_type"].isna() & (df["surplus"] > 0)].itertuples():
        donors.setdefault(r.product_id, []).append((-int(r.surplus), r.warehouse_id))
    for heap in donors.values():
        heapq.heapify(heap)

    transfer_qty, transfer_from, purchase_qty = [], [], []
    for r in needs.itertuples():
        need = r.recommended_restock_quantity
        heap = donors.get(r.product_id, [])
        moved, sources = 0, []
        while need > 0 and heap:
            neg_surplus, wh = heapq.heappop(heap)
            take = min(need, -neg_surplus)
            need -= take
            moved += take
            sources.append(f"{wh} ({take})")
            if -neg_surplus - take > 0:
                heapq.heappush(heap, (neg_surplus + take, wh))
        transfer_qty.append(moved)
        transfer_from.append(", ".join(sources) or None)
        purchase_qty.append(need)

    needs["transfer_quantity"] = transfer_qty
    needs["transfer_from"] = transfer_from
    needs["purchase_quantity"] = purchase_qty
    needs["fulfilment"] = [
        "TRANSFER" if b == 0 else ("PURCHASE" if t == 0 else "TRANSFER + PURCHASE")
        for t, b in zip(transfer_qty, purchase_qty)]

    needs["priority"] = needs["trigger_type"].map(
        {"STOCKOUT": "CRITICAL", "DEMAND_RISK": "HIGH", "POLICY_FLOOR": "MEDIUM"})

    def reason(r):
        if r.units_sold_30d == 0:
            sales = "no sales in 30 days"
        else:
            sales = f"{r.units_sold_30d} sold in 30 days, {int(r.days_of_cover)} days of cover"
        why = {"STOCKOUT": "Out of stock",
               "DEMAND_RISK": f"Stock below {int(sl*100)}% service-level reorder point "
                              f"({r.stat_reorder_point})",
               "POLICY_FLOOR": f"Stock below policy minimum ({floor})"}[r.trigger_type]
        return f"{why}; {sales}."

    needs["reason"] = needs.apply(reason, axis=1)
    needs["run_date"] = reference_date
    needs["recommendation_id"] = needs["inventory_key"] + "-" + reference_date.strftime("%Y-%m-%d")
    needs["status"] = "Pending Review"
    needs["daily_demand"] = needs["daily_demand"].round(3)

    cols = ["recommendation_id", "inventory_key", "product_id", "warehouse_id",
            "stock_quantity", "units_sold_30d", "daily_demand", "days_of_cover",
            "lead_time_days", "stat_reorder_point", "reorder_point", "target_stock",
            "recommended_restock_quantity", "fulfilment", "transfer_quantity",
            "transfer_from", "purchase_quantity", "trigger_type", "priority",
            "reason", "run_date", "status"]
    return needs[cols].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Foundry transform: reads the Layer 1 datasets, writes the recommendations.
# ---------------------------------------------------------------------------
PROJECT = "/Aplos Data-5a9492/Logistics Supply Chain Challenge"


@transform_pandas(
    Output(f"{PROJECT}/Restock_Recommendations"),
    orders=Input(f"{PROJECT}/Orders_Final"),
    inventory=Input(f"{PROJECT}/Inventory_Final"),
    carriers=Input(f"{PROJECT}/Carriers_Final"),
)
def compute(orders, inventory, carriers):
    return build_recommendations(orders, inventory, carriers)